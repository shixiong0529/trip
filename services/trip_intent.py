"""轻量旅行意图判断。

这里只做调用外部路线服务前的保守分流，不尝试替代 LLM 理解完整需求：
- 高德驾车骨架只用于用户明确声明的中国大陆自驾行程；
- 明显的出境及港澳台行程跳过高德驾车矩阵和 12306 查询；
- 判断不确定时不强行套用自驾路线，避免生成虚假里程。
"""

from dataclasses import dataclass
import re


_SELF_DRIVE_RE = re.compile(r"自驾|开车|驾车|租车自驾|房车")

# 覆盖首页示例及常见出境目的地。它是安全护栏而非目的地数据库；
# “出境/签证/护照”等显式语义可覆盖未列出的长尾地点。
_OUTBOUND_HINTS = (
    "出境", "国外", "海外", "国际旅行", "签证", "护照",
    "日本", "韩国", "朝鲜", "蒙古", "泰国", "新加坡", "马来西亚", "印度尼西亚",
    "印尼", "菲律宾", "越南", "柬埔寨", "老挝", "缅甸", "印度", "斯里兰卡",
    "马尔代夫", "阿联酋", "迪拜", "土耳其", "以色列", "沙特", "卡塔尔",
    "英国", "法国", "德国", "意大利", "西班牙", "葡萄牙", "瑞士", "奥地利",
    "荷兰", "比利时", "希腊", "冰岛", "挪威", "瑞典", "芬兰", "丹麦", "俄罗斯",
    "美国", "加拿大", "墨西哥", "巴西", "阿根廷", "智利", "秘鲁",
    "澳大利亚", "澳洲", "新西兰", "埃及", "摩洛哥", "南非", "肯尼亚",
    "东京", "大阪", "京都", "北海道", "冲绳", "首尔", "釜山", "曼谷", "清迈",
    "普吉", "巴黎", "伦敦", "罗马", "米兰", "柏林", "慕尼黑", "纽约", "洛杉矶",
    "旧金山", "温哥华", "多伦多", "悉尼", "墨尔本", "奥克兰",
    "香港", "澳门", "台湾", "台北", "高雄",
)


@dataclass(frozen=True)
class TripIntent:
    is_outbound: bool
    is_self_drive: bool

    @property
    def use_drive_planner(self) -> bool:
        return self.is_self_drive and not self.is_outbound


def classify_trip_intent(query: str) -> TripIntent:
    text = re.sub(r"\s+", "", query or "")
    return TripIntent(
        is_outbound=any(hint in text for hint in _OUTBOUND_HINTS),
        is_self_drive=bool(_SELF_DRIVE_RE.search(text)),
    )


def should_use_drive_planner(query: str) -> bool:
    return classify_trip_intent(query).use_drive_planner
