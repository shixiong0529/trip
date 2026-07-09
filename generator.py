"""
文档生成层
Markdown → HTML (Jinja2) → PDF (WeasyPrint) / DOCX (python-docx)

PDF 生成依赖 WeasyPrint，需要系统级依赖（pango、cairo、gobject）。
macOS 上可通过 `brew install pango cairo glib` 安装。
如果环境不支持，PDF 功能将不可用，但 HTML 和 DOCX 不受影响。
"""

import re
import io
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn

logger = logging.getLogger(__name__)

# WeasyPrint 延迟导入，环境不支持时优雅降级
_weasyprint_available = False
try:
    from weasyprint import HTML as WeasyHTML
    _weasyprint_available = True
except (ImportError, OSError) as e:
    logger.warning(f"WeasyPrint 不可用（PDF 功能将受限）: {e}")


# ---------- Markdown 解析器 ----------
def _parse_markdown_to_blocks(md: str) -> list[dict]:
    """将 Markdown 文本解析为结构化块"""
    blocks = []
    lines = md.split("\n")
    i = 0

    # 跳过开头空行
    while i < len(lines) and not lines[i].strip():
        i += 1

    while i < len(lines):
        line = lines[i]

        # 一级标题 #
        m = re.match(r"^#\s+(.+)", line)
        if m:
            blocks.append({"type": "h1", "content": m.group(1).strip()})
            i += 1
            continue

        # 二级标题 ##
        m = re.match(r"^##\s+(.+)", line)
        if m:
            blocks.append({"type": "h2", "content": m.group(1).strip()})
            i += 1
            continue

        # 三级标题 ###
        m = re.match(r"^###\s+(.+)", line)
        if m:
            blocks.append({"type": "h3", "content": m.group(1).strip()})
            i += 1
            continue

        # 引用 >
        if line.startswith("> "):
            ref_lines = []
            while i < len(lines) and lines[i].startswith("> "):
                ref_lines.append(lines[i][2:])
                i += 1
            blocks.append({"type": "blockquote", "content": "\n".join(ref_lines)})
            continue

        # 代码块 ```
        if line.startswith("```"):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过结束 ```
            blocks.append({"type": "code", "content": "\n".join(code_lines)})
            continue

        # 表格（检测 | 开头）
        if line.strip().startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            blocks.append({"type": "table", "rows": _parse_table(table_lines)})
            continue

        # 任务列表 - [ ] xxx 或 - [x] xxx（必须先于无序列表判断，否则会被当成普通列表）
        if re.match(r"^[\s]*[-*]\s+\[[ x]\]", line):
            task_items = []
            while i < len(lines) and re.match(r"^[\s]*[-*]\s+\[[ x]\]", lines[i]):
                m = re.match(r"^[\s]*[-*]\s+\[([ x])\]\s*(.*)", lines[i])
                if m:
                    task_items.append({"checked": m.group(1) == "x", "text": m.group(2)})
                i += 1
            blocks.append({"type": "tasks", "items": task_items})
            continue

        # 无序列表 - xxx 或 * xxx
        if re.match(r"^[\s]*[-*]\s+", line):
            list_items = []
            while i < len(lines) and re.match(r"^[\s]*[-*]\s+", lines[i]) \
                    and not re.match(r"^[\s]*[-*]\s+\[[ x]\]", lines[i]):
                item_text = re.sub(r"^[\s]*[-*]\s+", "", lines[i])
                list_items.append(item_text)
                i += 1
            blocks.append({"type": "ul", "items": list_items})
            continue

        # 有序列表 1. xxx
        if re.match(r"^[\s]*\d+\.\s+", line):
            list_items = []
            while i < len(lines) and re.match(r"^[\s]*\d+\.\s+", lines[i]):
                item_text = re.sub(r"^[\s]*\d+\.\s+", "", lines[i])
                list_items.append(item_text)
                i += 1
            blocks.append({"type": "ol", "items": list_items})
            continue

        # 水平线 ---
        if line.strip() in ("---", "***", "___"):
            blocks.append({"type": "hr"})
            i += 1
            continue

        # 空行
        if not line.strip():
            i += 1
            continue

        # 普通段落
        para_lines = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", ">", "```", "|", "- ", "* ", "1. ")):
            para_lines.append(lines[i])
            i += 1
        blocks.append({"type": "p", "content": "\n".join(para_lines)})

    return blocks


def _parse_table(lines: list[str]) -> list[list[str]]:
    """解析 Markdown 表格"""
    rows = []
    for line in lines:
        # 跳过分隔行 |---|
        if re.match(r"^\|[\s\-:|]+\|$", line.strip()):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)
    return rows


# ---------- HTML 生成 ----------
def _blocks_to_html_fragment(blocks: list[dict]) -> str:
    """将块转为 HTML Report Generator 风格的片段"""
    html = ""
    i = 0
    in_section = False  # 追踪是否在 section div 内
    in_container = False  # 追踪 hero 后的 container div 是否已打开

    while i < len(blocks):
        block = blocks[i]
        t = block["type"]
        content = block.get("content", "")

        # H1 标题 → Hero 区
        if t == "h1":
            html += _render_hero(content)
            in_container = True
            # 接下来的 1-3 个段落通常是概览统计 + 路线总览 + 重要提示
            overview_paras = []
            j = i + 1
            while j < len(blocks) and blocks[j]["type"] == "p" and len(overview_paras) < 3:
                overview_paras.append(blocks[j]["content"])
                j += 1
            if overview_paras:
                html += _render_overview(overview_paras)
                i = j - 1

        # H2 分节标题 → Section
        elif t == "h2":
            if in_section:
                html += "</div>\n"  # 关闭上一个 section
            slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", content).strip("-")
            # \u6807\u9898\u81ea\u5e26 emoji \u65f6\u4e0d\u518d\u8ffd\u52a0\u56fe\u6807\uff0c\u907f\u514d\u53cc\u56fe\u6807
            if re.match(r"^[\w\u4e00-\u9fff]", content):
                icon_html = f'<span class="icon">{_section_icon(content)}</span>'
            else:
                icon_html = ""
            html += f'<div class="section" id="{slug}">\n'
            html += f'<h2 class="section-title">{icon_html}{_inline_md(content)}</h2>\n'
            in_section = True

        # H3 "Day X" → 日程卡片
        elif t == "h3" and ("Day" in content or content.startswith("Day ") or "day" in content.lower()):
            day_html, new_i = _render_day_card(blocks, i)
            html += day_html
            i = new_i
            continue

        # 其他 H3（预约清单、证件等小节标题）
        elif t == "h3":
            html += f'<h3 class="sub-title">{_inline_md(content)}</h3>\n'

        # 表格
        elif t == "table":
            html += _render_table(block["rows"])

        # 列表
        elif t == "ul":
            items = "".join(f"<li>{_inline_md(it)}</li>" for it in block["items"])
            html += f"<div class=\"card\"><ul>{items}</ul></div>\n"

        elif t == "ol":
            items = "".join(f"<li>{_inline_md(it)}</li>" for it in block["items"])
            html += f"<div class=\"card\"><ol>{items}</ol></div>\n"

        elif t == "tasks":
            items = ""
            for it in block["items"]:
                chk = 'checked disabled' if it["checked"] else ""
                items += f'<li class="task-item"><input type="checkbox" {chk}/><span>{_inline_md(it["text"])}</span></li>'
            html += f'<div class="card"><ul class="task-list">{items}</ul></div>\n'

        # 代码块（通常是知识图谱树）
        elif t == "code":
            html += f'<div class="tree">{_escape_html(content)}</div>\n'

        # 引用块
        elif t == "blockquote":
            html += f'<blockquote>{_inline_md(content)}</blockquote>\n'

        # 普通段落
        elif t == "p":
            # 重要提示 / 预算说明 等段落用 card 包裹
            html += f'<div class="card"><p>{_inline_md(content)}</p></div>\n'

        i += 1

    if in_section:
        html += "</div>\n"  # 关闭最后一个 section
    if in_container:
        html += "</div>\n"  # 关闭 hero 后打开的 container
    return html


def _render_hero(title: str) -> str:
    """渲染 Hero 区"""
    return f'''<div class="hero">
  <h1>{_inline_md(title)}</h1>
  <p class="meta">AI 旅行管家 · 路小鲜（Leo）定制</p>
</div>
<div class="container">\n'''


def _render_overview(paras: list[str]) -> str:
    """渲染概览区：统计卡片 + 路线总览 + 重要提示"""
    html = ""

    # 从第一段提取关键统计数字；提取到 ≥2 项才视为"统计段"，
    # 否则按普通段落输出，避免 H1 后的正文段落被静默丢弃
    stats = _extract_stats(paras[0]) if paras else []
    rest = paras[1:]
    if len(stats) >= 2:
        html += '<div class="stats-grid">\n'
        for label, value in stats:
            html += f'  <div class="stat-card"><div class="stat-value">{value}</div><div class="stat-label">{label}</div></div>\n'
        html += '</div>\n'
    else:
        rest = paras

    # 路线总览 + 重要提示用 card 包裹
    for p in rest:
        html += f'<div class="card"><p>{_inline_md(p)}</p></div>\n'

    return html


def _extract_stats(text: str) -> list[tuple[str, str]]:
    """从概览段落中提取关键统计"""
    stats = []
    # 天数
    m = re.search(r"(\d+)\s*天", text)
    if m:
        stats.append(("旅行天数", m.group(1)))
    # 里程
    m = re.search(r"([\d,]+)\s*(km|公里)", text, re.I)
    if m:
        stats.append(("总里程", m.group(1) + "km"))
    # 人数
    m = re.search(r"(\d+)\s*人", text)
    if m:
        stats.append(("出行人数", m.group(1) + "人"))
    # 海拔
    m = re.search(r"(\d+,?\d+)\s*m", text)
    if m:
        stats.append(("最高海拔", m.group(1).replace(",", "") + "m"))
    # 预算（兼容"人均预算 ¥1,850"、"人均约¥2000"等写法）
    m = re.search(r"人均(?:预算)?[约\s]*[¥￥]?\s*([\d,]+)", text)
    if m:
        stats.append(("人均预算", "¥" + m.group(1)))
    # 核心景点数
    m = re.search(r"核心景点[约\s]*(\d+)", text)
    if m:
        stats.append(("核心景点", m.group(1) + "个"))
    return stats


def _section_icon(title: str) -> str:
    """根据标题返回 emoji 图标"""
    mapping = {
        "天气": "🌤️",
        "交通": "🚄",
        "住宿": "🏨",
        "分日": "📅",
        "预算": "💰",
        "预约": "🚨",
        "证件": "📄",
        "避坑": "⚠️",
        "物品": "🎒",
        "知识": "🌳",
        "出境": "🌍",
    }
    for key, icon in mapping.items():
        if key in title:
            return icon
    return "📌"


def _render_day_card(blocks: list[dict], start_idx: int) -> tuple[str, int]:
    """渲染一个 Day 卡片，返回 HTML 和新的索引"""
    title = _inline_md(blocks[start_idx].get("content", ""))

    # 分离标题中的路线和里程信息
    # 例如：Day 1 · 高速启程 永州 → 成都 · 1,150km · 约 13h
    route_info = ""
    main_title = title
    if "·" in title:
        parts = title.split("·")
        main_title = parts[0].strip()
        route_info = " · ".join(p.strip() for p in parts[1:])

    html = '<div class="day-card">\n'
    html += f'  <div class="day-header">\n'
    html += f'    <h3>{main_title}</h3>\n'
    if route_info:
        html += f'    <div class="day-route">{route_info}</div>\n'
    html += f'  </div>\n'

    # 查找紧随其后的表格（时段表）
    i = start_idx + 1
    if i < len(blocks) and blocks[i]["type"] == "table":
        html += '  <div class="day-body">\n'
        html += _render_table(blocks[i]["rows"])
        html += '  </div>\n'
        i += 1

    # 收集本日亮点 / 本日预算作为 footer
    # 注意：两行相邻时会被解析为同一个段落块，需按行拆分成独立徽章
    footer_items = []
    while i < len(blocks) and blocks[i]["type"] == "p":
        p = blocks[i]["content"]
        if not (p.startswith("🎯") or p.startswith("💰")):
            break
        for line in p.split("\n"):
            line = line.strip()
            if line.startswith("🎯") or line.startswith("💰"):
                badge_class = "badge-green" if line.startswith("🎯") else "badge-orange"
                # 移除 emoji
                text = line[1:].strip()
                footer_items.append(f'<span class="badge {badge_class}">{_inline_md(text)}</span>')
            elif line:
                footer_items.append(f'<span class="badge badge-blue">{_inline_md(line)}</span>')
        i += 1

    if footer_items:
        html += '  <div class="day-footer">\n    ' + "\n    ".join(footer_items) + "\n  </div>\n"

    html += "</div>\n"
    return html, i  # i 指向下一个未处理的块，跳过所有已消费内容


def _render_table(rows: list[list[str]]) -> str:
    """渲染表格"""
    if not rows:
        return ""
    th = "<tr>" + "".join(f"<th>{_inline_md(c)}</th>" for c in rows[0]) + "</tr>"
    trs = ""
    for row in rows[1:]:
        trs += "<tr>" + "".join(f"<td>{_inline_md(c)}</td>" for c in row) + "</tr>"
    return f'<div class="table-wrapper"><table><thead>{th}</thead><tbody>{trs}</tbody></table></div>\n'



def _escape_html(text: str) -> str:
    """HTML 特殊字符转义，防止 LLM 输出破坏页面结构或注入脚本"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _inline_md(text: str) -> str:
    """行内 Markdown 转 HTML（先转义，再应用加粗、斜体、代码）"""
    text = _escape_html(text)
    # 加粗 **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # 斜体 *text*
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text)
    # 行内代码 `code`
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


def _strip_inline_md(text: str) -> str:
    """去掉行内 Markdown 标记（DOCX 纯文本单元格用）"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    return text


def _add_md_runs(p, text: str):
    """将带 **加粗** 标记的文本按 run 添加到段落，加粗生效而非输出星号"""
    parts = re.split(r"(\*\*.+?\*\*)", text)
    for part in parts:
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            run = p.add_run(_strip_inline_md(part[2:-2]))
            run.bold = True
        elif part:
            p.add_run(_strip_inline_md(part))


# ---------- 文档生成器 ----------
class TravelGuideGenerator:
    """旅游攻略文档生成器"""

    def __init__(self, templates_dir: str):
        self.templates_dir = templates_dir
        self.env = Environment(
            loader=FileSystemLoader(templates_dir),
            autoescape=True,
        )

    def to_html(self, markdown_content: str, guide_id: str) -> str:
        """Markdown → 完整 HTML 页面"""
        blocks = _parse_markdown_to_blocks(markdown_content)
        body_html = _blocks_to_html_fragment(blocks)

        # 尝试从 h1 提取标题
        title = "旅行攻略"
        for block in blocks:
            if block["type"] == "h1":
                title = block["content"]
                break

        # 渲染 Jinja2 模板
        try:
            template = self.env.get_template("guide.html")
            return template.render(
                guide_id=guide_id,
                title=title,
                body_html=body_html,
            )
        except Exception:
            # 模板不存在时，使用内置模板
            return self._builtin_html(title, body_html, guide_id)

    def to_pdf(self, html_content: str, guide_id: str) -> bytes:
        """HTML → PDF，需要 WeasyPrint 系统依赖"""
        if not _weasyprint_available:
            raise RuntimeError(
                "PDF 生成需要 WeasyPrint。"
                "macOS: brew install pango cairo glib\n"
                "Linux: apt install libpango-1.0-0 libcairo2 libgobject-2.0-0"
            )
        doc = WeasyHTML(string=html_content)
        return doc.write_pdf(
            presentational_hints=True,
        )

    def to_docx(self, markdown_content: str, guide_id: str) -> bytes:
        """Markdown → DOCX"""
        blocks = _parse_markdown_to_blocks(markdown_content)
        doc = Document()

        # 页面设置
        section = doc.sections[0]
        section.page_width = Cm(21)
        section.page_height = Cm(29.7)
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2.5)

        for block in blocks:
            self._add_docx_block(doc, block)

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        return buf.read()

    def _add_docx_block(self, doc: Document, block: dict):
        """将块添加到 Word 文档"""
        t = block["type"]
        text = block.get("content", "").strip()

        if t == "h1":
            p = doc.add_heading(_strip_inline_md(text), level=1)
        elif t == "h2":
            p = doc.add_heading(_strip_inline_md(text), level=2)
        elif t == "h3":
            p = doc.add_heading(_strip_inline_md(text), level=3)
        elif t == "p":
            p = doc.add_paragraph()
            _add_md_runs(p, text)
        elif t == "blockquote":
            p = doc.add_paragraph(_strip_inline_md(text))
            p.paragraph_format.left_indent = Cm(1)
            run = p.runs[0] if p.runs else p.add_run(text)
            run.font.italic = True
            run.font.color.rgb = RGBColor(100, 116, 139)
        elif t in ("ul", "ol"):
            for item in block.get("items", []):
                p = doc.add_paragraph(style="List Bullet" if t == "ul" else "List Number")
                _add_md_runs(p, item)
        elif t == "tasks":
            for item in block.get("items", []):
                prefix = "☑ " if item["checked"] else "☐ "
                p = doc.add_paragraph(prefix)
                _add_md_runs(p, item["text"])
        elif t == "code":
            p = doc.add_paragraph()
            run = p.add_run(text)
            run.font.name = "Courier New"
            run.font.size = Pt(9)
        elif t == "table":
            rows = block.get("rows", [])
            if not rows:
                return
            # LLM 输出的表格可能行宽不一致，按最宽行建表，缺的补空
            n_cols = max(len(r) for r in rows)
            table = doc.add_table(rows=len(rows), cols=n_cols)
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            for ri, row in enumerate(rows):
                for ci in range(n_cols):
                    cell_text = row[ci] if ci < len(row) else ""
                    cell = table.cell(ri, ci)
                    cell.text = _strip_inline_md(cell_text)
                    if ri == 0:
                        for p in cell.paragraphs:
                            for run in p.runs:
                                run.bold = True
        elif t == "hr":
            doc.add_paragraph("─" * 40)

    def _builtin_html(self, title: str, body_html: str, guide_id: str) -> str:
        """内置 HTML 模板（当 Jinja2 模板文件不可用时）"""
        return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_escape_html(title)}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif; background:#f8f9fa; color:#1a1a2e; line-height:1.8; }}
  .guide-container {{ max-width:860px; margin:0 auto; padding:48px 32px; background:#fff; box-shadow:0 1px 3px rgba(0,0,0,0.06); }}
  h1 {{ font-size:1.8em; color:#1a1a2e; margin-bottom:8px; padding-bottom:16px; border-bottom:3px solid #2563eb; }}
  h2 {{ font-size:1.25em; color:#2563eb; margin:36px 0 16px; padding-left:12px; border-left:4px solid #2563eb; }}
  h3 {{ font-size:1.05em; color:#1a1a2e; margin:24px 0 12px; }}
  p {{ color:#475569; margin:8px 0; }}
  blockquote {{ background:#f0f9ff; border-left:4px solid #2563eb; margin:16px 0; padding:12px 20px; border-radius:0 8px 8px 0; color:#475569; }}
  table {{ width:100%; border-collapse:collapse; margin:12px 0; font-size:0.88em; }}
  th {{ background:#eff6ff; color:#2563eb; font-weight:600; padding:10px 14px; text-align:left; border-bottom:2px solid #bfdbfe; }}
  td {{ color:#475569; padding:10px 14px; border-bottom:1px solid #f1f5f9; }}
  tr:hover td {{ background:#f8fafc; }}
  ul, ol {{ margin:8px 0 8px 24px; color:#475569; }}
  li {{ margin:4px 0; }}
  .task-list {{ list-style:none; padding-left:0; }}
  .task-item {{ display:flex; align-items:flex-start; gap:8px; padding:4px 0; }}
  .task-item input[type=checkbox] {{ margin-top:4px; }}
  pre {{ background:#1e293b; color:#e2e8f0; padding:16px 20px; border-radius:8px; overflow-x:auto; font-size:0.85em; line-height:1.6; margin:12px 0; }}
  code {{ font-family:'SF Mono','Fira Code',monospace; background:#f1f5f9; padding:2px 6px; border-radius:4px; font-size:0.9em; }}
  pre code {{ background:none; padding:0; }}
  hr {{ border:none; border-top:1px solid #e2e8f0; margin:24px 0; }}
  .table-wrapper {{ overflow-x:auto; }}
  @media print {{
    body {{ background:#fff; }}
    .guide-container {{ box-shadow:none; padding:0; }}
    h2 {{ break-before:page; }}
  }}
</style>
</head>
<body>
<div class="guide-container">
{body_html}
<p style="text-align:center;color:#94a3b8;font-size:0.78em;margin-top:40px;border-top:1px solid #e2e8f0;padding-top:20px;">
  AI 旅行攻略生成器 · 攻略编号 {guide_id} · 价格信息仅供参考
</p>
</div>
</body>
</html>"""
