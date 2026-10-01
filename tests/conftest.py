import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import collector

NOW = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Tests must never access the network")
    monkeypatch.setattr(collector.requests.Session, "request", no_network)
    monkeypatch.delenv("BILIBILI_COOKIE", raising=False)
    monkeypatch.delenv("HTTP_PROXY_URL", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    for key in ("RETENTION_HOURS", "LATEST_HOURS", "RECENT_HOURS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(collector.time, "sleep", lambda _: None)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "utc_now", lambda: NOW)
    for name, relative in {
        "ARCHIVE_PATH": "data/archive.json", "LATEST_PATH": "public/latest.json",
        "RECENT_PATH": "public/recent.json", "HEALTH_PATH": "public/health.json",
    }.items():
        monkeypatch.setattr(collector, name, tmp_path / relative)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    return tmp_path


def item(item_id="100", age_hours=1, *, kind="dynamic", url=None, up_uid="42"):
    ts = int(NOW.timestamp()) - int(age_hours * 3600)
    return {
        "id": item_id, "kind": kind, "up": "测试来源", "up_uid": up_uid,
        "published_ts": ts, "published_at": collector.iso_utc(ts),
        "title": "更新标题", "text": "有实质内容的简介",
        "url": url or f"https://t.bilibili.com/{item_id}",
    }


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def seed(items=None):
    items = items if items is not None else [item()]
    old = "2026-10-01T12:00:00Z"
    collector.write_bundle({
        collector.ARCHIVE_PATH: {"items": items, "updated_at": old},
        collector.LATEST_PATH: {"items": items, "count": len(items), "generated_at": old},
        collector.RECENT_PATH: {"items": items, "count": len(items), "generated_at": old},
        collector.HEALTH_PATH: {"status": "ok", "last_success_at": old, "checked_at": old},
    })
    return old


def mock_feed(monkeypatch, items):
    monkeypatch.setenv("BILIBILI_COOKIE", "offline-test-value")
    monkeypatch.setattr(collector, "verify_login", lambda _: None)
    monkeypatch.setattr(collector, "fetch_feed", lambda *args, **kwargs: items)
