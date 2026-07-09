"""prompts.py 覆盖测试：System Prompt 占位符回归 + User Message 数据注入分支。"""

from prompts import SYSTEM_PROMPT, build_user_message


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
