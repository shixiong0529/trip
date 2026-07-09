from services.amap_client import format_amap_summary


def test_format_amap_summary_includes_poi_weather_and_route():
    data = {
        "destination": "成都",
        "geocode": {
            "formatted_address": "四川省成都市",
            "adcode": "510100",
            "location": "104.066301,30.572961",
        },
        "hotels": [
            {
                "name": "锦江宾馆锦苑楼",
                "type": "住宿服务;宾馆酒店;四星级宾馆",
                "address": "人民南路二段80号",
                "tel": "028-12345678",
                "biz_ext": {"rating": "4.8"},
                "photos": [{"url": "https://example.com/hotel.jpg"}],
            }
        ],
        "restaurants": [
            {
                "name": "蜀大侠火锅",
                "type": "餐饮服务;中餐厅;火锅店",
                "address": "商业街1号",
                "distance": "36",
                "biz_ext": {"rating": "4.7", "cost": "92.00", "open_time": "11:00-24:00"},
            }
        ],
        "scenic": [
            {
                "name": "宽窄巷子景区",
                "type": "购物服务;特色商业街;特色商业街|风景名胜;旅游景点",
                "address": "金河路口",
                "biz_ext": {"rating": "4.8"},
            }
        ],
        "weather": {
            "city": "成都市",
            "reporttime": "2026-07-09 21:02:38",
            "casts": [
                {
                    "date": "2026-07-09",
                    "dayweather": "多云",
                    "nightweather": "阴",
                    "daytemp": "37",
                    "nighttemp": "27",
                }
            ],
        },
        "route": {"origin": "上海", "distance": "10953", "duration": "2192"},
    }

    summary = format_amap_summary(data)

    assert "### 高德地图参考数据 · 成都" in summary
    assert "四川省成都市" in summary
    assert "锦江宾馆锦苑楼" in summary
    assert "蜀大侠火锅" in summary
    assert "人均约 ¥92" in summary
    assert "宽窄巷子景区" in summary
    assert "2026-07-09 多云/阴 27-37°C" in summary
    assert "上海 → 成都约 11.0km / 37分钟" in summary
