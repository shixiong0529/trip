"""prompts.py 覆盖测试：System Prompt 占位符回归 + User Message 数据注入分支。"""

from prompts import (
    STANDARD_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_user_message,
    compact_travel_data,
    get_system_prompt,
    report_max_tokens,
)


def test_system_prompt_has_no_leftover_placeholders():
    # 回归测试：曾经出现过 "{CTRIP_..." 之类未被替换的占位符残留
    assert "{CTRIP_" not in SYSTEM_PROMPT


def test_build_user_message_without_data_has_no_real_data_section():
    msg = build_user_message("北京3日游", {})
    assert "真实" not in msg
    assert "北京3日游" in msg


def test_build_user_message_without_data_arg_defaults_to_empty():
    msg = build_user_message("北京3日游")
    assert "真实" not in msg


def test_build_user_message_with_train_data_includes_real_train_section():
    msg = build_user_message("武汉出发西藏15天", {"train": "G1234 二等座 有票"})
    assert "【真实火车票数据】" in msg
    assert "G1234 二等座 有票" in msg


def test_build_user_message_with_transport_includes_real_transport_section():
    msg = build_user_message("北京3日游", {"transport": "机票 ¥800"})
    assert "【真实交通数据】" in msg
    assert "机票 ¥800" in msg


def test_build_user_message_with_amap_data_includes_location_section():
    msg = build_user_message("成都3日游", {"amap": "春熙路附近火锅 人均 ¥92"})
    assert "【高德地图位置与周边数据】" in msg
    assert "春熙路附近火锅 人均 ¥92" in msg


def test_generation_modes_keep_professional_and_add_compact_standard_prompt():
    assert get_system_prompt("professional") == SYSTEM_PROMPT
    assert get_system_prompt("standard") == STANDARD_SYSTEM_PROMPT
    assert "知识图谱" in SYSTEM_PROMPT
    assert "物品清单和知识图谱必须生成但保持精简" in STANDARD_SYSTEM_PROMPT
    assert "不要生成知识图谱" not in STANDARD_SYSTEM_PROMPT
    assert report_max_tokens("standard", 16384) == 10000
    assert report_max_tokens("professional", 16384) == 16384


def test_standard_message_uses_compact_rules():
    msg = build_user_message("北京3日游", {}, mode="standard")

    assert "按照标准版输出规范" in msg
    assert "控制篇幅" in msg
    assert "行前物品清单必须覆盖" in msg
    assert "行程知识图谱必须覆盖每个 Day" in msg
    assert "10 个板块必须全部输出" not in msg


def test_compact_travel_data_limits_prompt_copy_without_mutating_raw_data():
    raw_transport = "交通信息\n\n" + "价格 ¥800，需预约\n" * 1000
    raw = {
        "transport": raw_transport,
        "hotels": "酒店\n" * 1000,
        "route_plan": "路线骨架" * 2000,
    }

    standard = compact_travel_data(raw, "standard")
    professional = compact_travel_data(raw, "professional")

    assert len(standard["transport"]) <= 1800
    assert len(professional["transport"]) <= 4000
    assert len(standard["hotels"]) <= 1100
    assert standard["route_plan"] == raw["route_plan"]
    assert raw["transport"] == raw_transport
    assert "已保留前述关键部分" in standard["transport"]
