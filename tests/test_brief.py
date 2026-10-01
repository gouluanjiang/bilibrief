from datetime import timedelta

import pytest

import brief
import collector
from conftest import NOW, item, mock_feed, read


@pytest.fixture
def feed(workspace, monkeypatch):
    entries = [item("new", 1, up_uid="1"), item("old", 25, up_uid="2")]
    mock_feed(monkeypatch, entries)
    assert collector.main() == 0
    return [read(p) for p in (collector.HEALTH_PATH, collector.LATEST_PATH, collector.RECENT_PATH)]


def test_first_run_only_reports_primary_window(feed):
    out = brief.build_brief(*feed, now=NOW)
    assert out["status"] == "updates"
    assert out["count"] == 1
    assert out["deduplication"] == "first_run_only"
    row = out["groups"][0]["items"][0]
    assert all(row[k] for k in ("up", "title", "published_at", "summary", "url", "identity_keys"))
    assert row["catch_up"] is False


def test_persistent_keys_prevent_redelivery_and_allow_catchup(feed):
    first = brief.build_brief(*feed, now=NOW, reported_keys=set())
    assert first["count"] == 2
    assert len(first["groups"]) == 2
    assert any(i["catch_up"] for g in first["groups"] for i in g["items"])
    ledger = {key for g in first["groups"] for row in g["items"] for key in row["identity_keys"]}
    assert brief.build_brief(*feed, now=NOW, reported_keys=ledger)["status"] == "no_updates"


def test_cutoff_is_exclusive_at_start_and_inclusive_at_end(feed):
    out = brief.build_brief(*feed, now=NOW, since=NOW-timedelta(hours=2), until=NOW-timedelta(hours=1))
    assert out["count"] == 1
    out = brief.build_brief(*feed, now=NOW, since=NOW-timedelta(hours=1))
    assert out["count"] == 0
    assert out["status"] == "no_updates"


@pytest.mark.parametrize("mutation,reason", [
    (lambda p: p[0].update(status="error", error_code="login_required"), "collection_failed"),
    (lambda p: p[0].update(checked_at="2026-10-02T13:00:00Z"), "invalid_data"),
    (lambda p: p[0].update(coverage={"complete": False}), "incomplete_coverage"),
    (lambda p: p[1].update(count=100), "invalid_data"),
    (lambda p: p[2].update(items=[]), "invalid_data"),
    (lambda p: p[1].update(generated_at="2026-10-01T12:00:00Z"), "invalid_data"),
    (lambda p: p[1]["items"][0].update(published_at="invalid"), "invalid_data"),
    (lambda p: p[2]["items"][0].update(title="mismatched snapshot"), "invalid_data"),
])
def test_source_errors_never_become_no_updates(feed, mutation, reason):
    mutation(feed)
    out = brief.build_brief(*feed, now=NOW)
    assert out["status"] == "source_error"
    assert out["reason"] == reason
    assert out["groups"] == []


def test_old_ok_health_is_rejected(feed):
    out = brief.build_brief(*feed, now=NOW+timedelta(hours=11))
    assert out["status"] == "source_error"
    assert out["reason"] == "stale_data"
    assert out["last_success_at"] == "2026-10-01T13:00:00Z"


def test_bv_and_url_aliases_deduplicate_promotions(workspace, monkeypatch):
    url = "https://www.bilibili.com/video/BV1xx411c7mD"
    video = item("video", kind="video", url=url+"?tracking=1")
    promotion = item("promo", url=url+"/?tracking=2")
    promotion["text"] = "补充了正式发售日期"
    mock_feed(monkeypatch, [promotion, video])
    assert collector.main() == 0
    feeds = [read(p) for p in (collector.HEALTH_PATH, collector.LATEST_PATH, collector.RECENT_PATH)]
    out = brief.build_brief(*feeds, now=NOW, reported_keys=set())
    assert out["count"] == 1
    row = out["groups"][0]["items"][0]
    assert row["kind"] == "video"
    assert "dynamic:promo" in row["identity_keys"]
    assert row["related_updates"][0]["text"] == promotion["text"]
    assert brief.build_brief(*feeds, now=NOW, reported_keys=set(row["identity_keys"]))["count"] == 0


def test_invalid_input_types_are_source_errors():
    assert brief.build_brief(None, None, None, now=NOW)["status"] == "source_error"


def test_new_information_on_an_already_reported_target_is_reviewable(workspace, monkeypatch):
    url = "https://www.bilibili.com/video/BV1xx411c7mD"
    mock_feed(monkeypatch, [item("announcement", url=url)])
    assert collector.main() == 0
    feeds = [read(p) for p in (collector.HEALTH_PATH, collector.LATEST_PATH, collector.RECENT_PATH)]
    out = brief.build_brief(*feeds, now=NOW, reported_keys={"video:BV1xx411c7mD"})
    assert out["groups"][0]["items"][0]["possible_supplement"] is True


def test_uncovered_time_range_is_not_no_updates(feed):
    out = brief.build_brief(*feed, now=NOW, since=NOW-timedelta(days=10), reported_keys=set())
    assert out["status"] == "source_error"
    assert out["reason"] == "window_outside_retention"


def test_valid_empty_feed_is_no_updates(workspace, monkeypatch):
    mock_feed(monkeypatch, [])
    assert collector.main() == 0
    feeds = [read(p) for p in (collector.HEALTH_PATH, collector.LATEST_PATH, collector.RECENT_PATH)]
    assert brief.build_brief(*feeds, now=NOW)["status"] == "no_updates"


def test_cli_missing_input_is_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["brief.py", "--public-dir", str(tmp_path)])
    assert brief.main() == 1
    assert '"source_error"' in capsys.readouterr().out
