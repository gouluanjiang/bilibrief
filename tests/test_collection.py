import json
from unittest.mock import Mock

import pytest
import requests

import collector
from conftest import NOW, item, mock_feed, read, seed


def snapshots():
    return {p: p.read_bytes() for p in (collector.ARCHIVE_PATH, collector.LATEST_PATH, collector.RECENT_PATH)}


@pytest.mark.parametrize("code", ["missing_cookie", "login_required", "network_error", "invalid_json",
                                  "api_error", "incomplete_feed", "unexpected_error", "invalid_config"])
def test_failure_preserves_snapshot_and_success(workspace, monkeypatch, capsys, code):
    previous = seed()
    before = snapshots()
    secret = "SESSDATA=private-test-value; proxy=http://user:password@example.invalid"
    if code != "missing_cookie":
        monkeypatch.setenv("BILIBILI_COOKIE", secret)
    if code == "invalid_config":
        monkeypatch.setenv("LATEST_HOURS", "invalid")
    else:
        error = RuntimeError(secret) if code == "unexpected_error" else collector.CollectionError(code)
        monkeypatch.setattr(collector, "verify_login", Mock(side_effect=error))
    assert collector.main() != 0
    health = read(collector.HEALTH_PATH)
    assert health["status"] == "error"
    assert health["error_code"] == code
    assert health["last_success_at"] == previous
    assert health["checked_at"] == "2026-10-01T13:00:00Z"
    assert health["coverage"]["complete"] is False
    assert snapshots() == before
    assert "publishable=true" in (workspace / "outputs").read_text()
    assert secret not in collector.HEALTH_PATH.read_text() + capsys.readouterr().err


def test_first_run_failure(workspace):
    assert collector.main() == 2
    assert read(collector.HEALTH_PATH)["last_success_at"] is None
    assert not collector.LATEST_PATH.exists()


def test_idempotent_collection_and_window_boundaries(workspace, monkeypatch):
    entries = [item("a", 0), item("b", 30), item("c", 30 + 1 / 3600), item("d", 72),
               item("e", 72 + 1 / 3600), item("f", 168), item("g", 168 + 1 / 3600),
               item("live", 1, kind="live")]
    mock_feed(monkeypatch, entries + [entries[0].copy()])
    assert collector.main() == 0
    assert {i["id"] for i in read(collector.LATEST_PATH)["items"]} == {"a", "b"}
    assert {i["id"] for i in read(collector.RECENT_PATH)["items"]} == {"a", "b", "c", "d"}
    assert "f" in {i["id"] for i in read(collector.ARCHIVE_PATH)["items"]}
    first = snapshots()
    assert collector.main() == 0
    assert snapshots() == first
    assert read(collector.HEALTH_PATH)["new_items"] == 0
    assert read(collector.LATEST_PATH)["items"][0]["first_seen_at"] == "2026-10-01T13:00:00Z"


def test_successful_empty_feed_keeps_prior_items(workspace, monkeypatch):
    seed()
    mock_feed(monkeypatch, [])
    assert collector.main() == 0
    assert read(collector.HEALTH_PATH)["new_items"] == 0
    assert read(collector.LATEST_PATH)["count"] == 1


def test_successful_empty_first_collection(workspace, monkeypatch):
    mock_feed(monkeypatch, [])
    assert collector.main() == 0
    assert read(collector.LATEST_PATH)["items"] == []
    assert read(collector.HEALTH_PATH)["coverage"]["complete"] is True


def test_corrupt_archive_is_not_discarded(workspace, monkeypatch):
    seed()
    collector.ARCHIVE_PATH.write_text("broken-json")
    before = snapshots()
    mock_feed(monkeypatch, [])
    assert collector.main() == 1
    assert snapshots() == before
    assert read(collector.HEALTH_PATH)["error_code"] == "archive_invalid"


def test_replace_failure_rolls_back_all_data(workspace, monkeypatch):
    previous = seed()
    before = snapshots()
    mock_feed(monkeypatch, [item("new")])
    replace = collector.os.replace
    fail_once = True
    def failing_replace(src, dst):
        nonlocal fail_once
        if dst == collector.RECENT_PATH and fail_once:
            fail_once = False
            raise OSError("injected replace failure")
        replace(src, dst)
    monkeypatch.setattr(collector.os, "replace", failing_replace)
    assert collector.main() == 1
    assert snapshots() == before
    assert read(collector.HEALTH_PATH)["last_success_at"] == previous
    assert read(collector.HEALTH_PATH)["error_code"] == "write_error"
    assert "publishable=true" in (workspace / "outputs").read_text()


def test_rollback_failure_blocks_publication(workspace, monkeypatch):
    seed()
    mock_feed(monkeypatch, [item("new")])
    replace = collector.os.replace
    writes = 0
    def failing_replace(src, dst):
        nonlocal writes
        writes += 1
        if writes in (2, 3):  # second snapshot replace and restoration of the first
            raise OSError("injected rollback failure")
        replace(src, dst)
    monkeypatch.setattr(collector.os, "replace", failing_replace)
    assert collector.main() == 1
    assert "publishable=false" in (workspace / "outputs").read_text()
    assert read(collector.HEALTH_PATH)["error_code"] == "rollback_failed"


def test_staging_failure_never_changes_data(workspace, monkeypatch):
    seed()
    before = snapshots()
    mock_feed(monkeypatch, [item("new")])
    stage = collector.stage_bytes
    def fail_stage(path, content):
        if path == collector.RECENT_PATH:
            raise OSError("disk full")
        return stage(path, content)
    monkeypatch.setattr(collector, "stage_bytes", fail_stage)
    assert collector.main() == 1
    assert snapshots() == before


def test_health_write_failure_blocks_publication(workspace, monkeypatch):
    seed()
    before = snapshots()
    monkeypatch.setattr(collector, "stage_bytes", Mock(side_effect=OSError("disk full")))
    assert collector.main() != 0
    assert snapshots() == before
    assert "publishable=false" in (workspace / "outputs").read_text()


@pytest.mark.parametrize("error,expected", [(requests.ConnectionError("private-proxy"), "network_error"),
                                            (requests.HTTPError("private-url"), "http_error"),
                                            (ValueError("private-body"), "invalid_json")])
def test_request_errors_are_sanitized_and_retried(error, expected):
    session = Mock()
    session.get.side_effect = error
    with pytest.raises(collector.CollectionError) as exc:
        collector.request_json(session, collector.API_NAV)
    assert exc.value.code == expected
    assert "private" not in str(exc.value)
    assert session.get.call_count == 4


def test_login_api_message_is_not_exposed(monkeypatch):
    monkeypatch.setattr(collector, "request_json", lambda *a, **k: {"code": -101, "message": "SESSDATA=private"})
    with pytest.raises(collector.CollectionError, match="login verification") as exc:
        collector.verify_login(Mock())
    assert "SESSDATA" not in str(exc.value)


def raw(item_id="1", ts=None):
    return {"id_str": item_id, "modules": {"module_author": {"pub_ts": ts or int(NOW.timestamp()), "name": "UP"}}}


@pytest.mark.parametrize("data", [{}, {"items": []}, {"items": [], "has_more": 1, "offset": "next"},
                                  {"items": [None], "has_more": 0}, {"items": [{}], "has_more": 0},
                                  {"items": [raw()], "has_more": 1}, {"items": [], "has_more": "0"}])
def test_incomplete_feed_is_an_error(monkeypatch, data):
    monkeypatch.setattr(collector, "request_json", lambda *a, **k: {"code": 0, "data": data})
    with pytest.raises(collector.CollectionError, match="incomplete"):
        collector.fetch_feed(Mock(), 0)


def test_pagination_cap_and_repeated_offset_fail(monkeypatch):
    monkeypatch.setattr(collector, "request_json", lambda *a, **k: {
        "code": 0, "data": {"items": [raw()], "has_more": 1, "offset": "same"}})
    for limit in (1, 30):
        with pytest.raises(collector.CollectionError, match="incomplete"):
            collector.fetch_feed(Mock(), 0, max_pages=limit)


def test_old_pinned_entry_does_not_truncate_feed(monkeypatch):
    responses = [{"code": 0, "data": {"items": [raw("old", 1), raw("new")], "has_more": 1, "offset": "next"}},
                 {"code": 0, "data": {"items": [raw("newer")], "has_more": 0}}]
    monkeypatch.setattr(collector, "request_json", Mock(side_effect=responses))
    assert len(collector.fetch_feed(Mock(), int(NOW.timestamp()) - 3600)) == 3


def test_valid_empty_feed(monkeypatch):
    monkeypatch.setattr(collector, "request_json", lambda *a, **k: {"code": 0, "data": {"items": [], "has_more": 0}})
    assert collector.fetch_feed(Mock(), 0) == []


def test_future_publication_rejected(workspace, monkeypatch):
    seed()
    before = snapshots()
    mock_feed(monkeypatch, [item("future", -1)])
    assert collector.main() == 1
    assert snapshots() == before
