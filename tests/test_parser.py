import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "collector.py"
spec = importlib.util.spec_from_file_location("collector", MODULE_PATH)
collector = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(collector)


def test_video_parse():
    raw = {
        "id_str": "123456",
        "type": "DYNAMIC_TYPE_AV",
        "modules": {
            "module_author": {"mid": 42, "name": "测试UP", "pub_ts": 1700000000, "pub_action": "投稿了视频"},
            "module_dynamic": {
                "desc": {"text": "视频动态正文"},
                "major": {
                    "type": "MAJOR_TYPE_ARCHIVE",
                    "archive": {
                        "title": "测试视频",
                        "desc": "简介",
                        "bvid": "BV1xx411c7mD",
                        "jump_url": "//www.bilibili.com/video/BV1xx411c7mD",
                    },
                },
            },
        },
    }
    out = collector.parse_one(raw)
    assert out["kind"] == "video"
    assert out["up"] == "测试UP"
    assert out["title"] == "测试视频"
    assert out["url"].startswith("https://")


def test_text_dynamic_parse():
    raw = {
        "id_str": "987654",
        "type": "DYNAMIC_TYPE_WORD",
        "modules": {
            "module_author": {"mid": 7, "name": "官方账号", "pub_ts": 1700000100},
            "module_dynamic": {"desc": {"text": "公测将于下周开始。"}, "major": None},
        },
    }
    out = collector.parse_one(raw)
    assert out["kind"] == "dynamic"
    assert "公测" in out["text"]
    assert out["url"] == "https://t.bilibili.com/987654"
