#!/bin/bash
set -e

echo "============================================"
echo "  AI 旅行攻略生成器"
echo "============================================"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 查找合适的 Python
PYTHON=""
for py in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$py" &>/dev/null; then
        ver=$("$py" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$py"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "错误: 未找到 Python 3.10+。请先安装 Python。"
    exit 1
fi

echo "Python: $($PYTHON --version)"

# 检查并安装系统依赖（PDF 生成需要）
HAS_BREW=false
if command -v brew &>/dev/null; then
    HAS_BREW=true
fi

# 检查 WeasyPrint 系统依赖
MISSING_DEPS=""
for lib in pango cairo glib; do
    if [ "$HAS_BREW" = true ]; then
        if ! brew list "$lib" &>/dev/null; then
            MISSING_DEPS="$MISSING_DEPS $lib"
        fi
    fi
done

if [ -n "$MISSING_DEPS" ] && [ "$HAS_BREW" = true ]; then
    echo ""
    echo "检测到 PDF 生成所需的系统依赖未安装: $MISSING_DEPS"
    echo "安装这些依赖以获得 PDF 下载功能:"
    echo "  brew install pango cairo glib"
    echo ""
    echo "HTML 和 DOCX 下载不受影响。"
    echo ""
fi

# 创建/激活虚拟环境
VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "创建虚拟环境..."
    $PYTHON -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate" 2>/dev/null || . "$VENV_DIR/bin/activate"

# 安装依赖
echo "检查依赖..."
pip install -q --upgrade pip 2>/dev/null
pip install -q -r requirements.txt

# 检查 .env 配置
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo ""
    echo "已创建 .env 文件，请编辑填入 DeepSeek API Key:"
    echo "  vim $SCRIPT_DIR/.env"
    echo ""
    read -rp "按回车键继续（可在浏览器中配置 API Key）..."
fi

# 启动
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"

echo ""
echo "============================================"
echo "  启动服务..."
echo ""
echo "  本地访问: http://localhost:$PORT"
echo "  API 文档: http://localhost:$PORT/docs"
echo ""
echo "  按 Ctrl+C 停止服务"
echo "============================================"
echo ""

python app.py
