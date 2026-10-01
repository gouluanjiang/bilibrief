#!/usr/bin/env python3
"""Read-only, offline daily-brief candidates. Delivery and its ledger are external."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from collector import identity_keys, valid_item


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_brief(health: dict, latest: dict, recent: dict, *, now: datetime,
                since: datetime | None = None, until: datetime | None = None,
                reported_keys: set[str] | None = None, max_age_hours: int = 10) -> dict:
    """Select (since, until], with unseen 72h catch-up only when a ledger exists.

    The result is candidates, not a claim that videos have been watched or sent.
    Only the delivery owner may persist keys after successfully sending a report.
    """
    until = until or now
    since = since or until - timedelta(hours=24)
    result = {
        "schema_version": 1, "status": "source_error", "reason": "invalid_data",
        "window_start": iso(since), "window_end": iso(until),
        "data_cutoff": None, "last_success_at": None,
        "deduplication": "persistent" if reported_keys is not None else "first_run_only",
        "groups": [], "count": 0,
    }
    try:
        if not all(t.tzinfo for t in (now, since, until)) or not since < until <= now:
            raise ValueError("invalid window")
        checked = timestamp(health.get("checked_at"))
        successful = timestamp(health.get("last_success_at"))
        if not successful <= checked <= now:
            raise ValueError("invalid health times")
        result["last_success_at"] = iso(successful)
        if health.get("status") != "ok":
            result["reason"] = "collection_failed"
            return result
        if now - successful > timedelta(hours=max_age_hours):
            result["reason"] = "stale_data"
            return result
        if health.get("coverage", {}).get("complete") is not True:
            result["reason"] = "incomplete_coverage"
            return result
        if timestamp(health.get("snapshot_generated_at")) != successful:
            raise ValueError("mismatched health and snapshot")
        for snapshot, minimum_hours in ((latest, 24), (recent, 72)):
            if (timestamp(snapshot.get("generated_at")) != successful
                    or snapshot.get("coverage", {}).get("complete") is not True
                    or type(snapshot.get("window_hours")) is not int
                    or snapshot["window_hours"] < minimum_hours):
                raise ValueError("inconsistent snapshot")
            items = snapshot.get("items")
            if not isinstance(items, list) or type(snapshot.get("count")) is not int or snapshot["count"] != len(items):
                raise ValueError("incomplete items")
            ids = set()
            for item in items:
                if (not valid_item(item) or item["id"] in ids
                        or item.get("kind") not in {"video", "dynamic", "repost", "article", "live"}
                        or not isinstance(item.get("up"), str) or not item["up"]
                        or not isinstance(item.get("title"), str)
                        or not isinstance(item.get("text"), str)
                        or not isinstance(item.get("url"), str) or not item["url"]):
                    raise ValueError("invalid item")
                published = timestamp(item.get("published_at"))
                if not any(key.startswith("url:") for key in identity_keys(item)):
                    raise ValueError("unsupported original link")
                if (published.timestamp() != item["published_ts"] or published > successful
                        or published < successful - timedelta(hours=snapshot["window_hours"])):
                    raise ValueError("invalid publication time")
                ids.add(item["id"])
        recent_by_id = {i["id"]: i for i in recent["items"]}
        if any(recent_by_id.get(i["id"]) != i for i in latest["items"]):
            raise ValueError("mixed snapshot versions")
    except (ValueError, TypeError, AttributeError, OverflowError):
        return result

    result["data_cutoff"] = iso(successful)
    result["coverage_note"] = "仅代表已观察到的关注流；不保证删除内容或上游未返回内容的覆盖。"
    if since < successful - timedelta(hours=recent["window_hours"]):
        result["coverage_note"] += " 请求起点超出保留窗口，更早内容无法补漏。"
        result["reason"] = "window_outside_retention"
        return result
    reported = reported_keys or set()
    selected: dict[str, dict] = {}
    aliases: dict[str, dict] = {}
    # Prefer formal video entries when the same target also appears as promotion.
    ordered = sorted(recent["items"], key=lambda i: (i["kind"] != "video", -i["published_ts"], i["id"]))
    for item in ordered:
        published = timestamp(item["published_at"])
        if published > until or item.get("kind") == "live":
            continue
        catch_up = published <= since
        if catch_up and (reported_keys is None or published < until - timedelta(hours=72)):
            continue
        keys = identity_keys(item)
        if f"dynamic:{item['id']}" in reported:
            continue
        possible_supplement = bool(reported.intersection(keys))
        if possible_supplement and (item["kind"] == "video" or not item["text"]):
            continue
        existing = next((aliases[k] for k in keys if k in aliases), None)
        if existing is not None:
            # Keep extra wording available for the report's semantic filtering.
            if item["text"] and item["text"] != existing["summary"]:
                existing.setdefault("related_updates", []).append({
                    "up": item["up"], "published_at": item["published_at"],
                    "text": item["text"], "url": item["url"],
                })
            existing["identity_keys"] = sorted(set(existing["identity_keys"] + keys))
            for key in keys:
                aliases[key] = existing
            continue
        row = {
            "id": item["id"], "kind": item["kind"], "up": item["up"],
            "up_uid": item.get("up_uid", ""), "title": item["title"],
            "published_at": item["published_at"], "summary": item["text"][:500],
            "summary_basis": "title_and_description", "url": item["url"],
            "catch_up": catch_up, "identity_keys": keys,
        }
        if possible_supplement:
            row["possible_supplement"] = True  # A new dynamic may add information to an already reported video.
        if "original" in item:
            row["original"] = item["original"]
        selected[item["id"]] = row
        for key in keys:
            aliases[key] = row
    groups: dict[str, dict] = {}
    for row in sorted(selected.values(), key=lambda i: (i["published_at"], i["id"]), reverse=True):
        source_id = str(row["up_uid"] or row["up"])
        group = groups.setdefault(source_id, {"source": "Bilibili", "up": row["up"],
                                               "up_uid": row["up_uid"], "items": []})
        group["items"].append(row)
    result.update(status="updates" if selected else "no_updates", reason=None,
                  groups=list(groups.values()), count=len(selected))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-dir", type=Path, default=Path(__file__).resolve().parents[1] / "public")
    parser.add_argument("--since", type=timestamp)
    parser.add_argument("--until", type=timestamp)
    parser.add_argument("--reported", type=Path, help="JSON array of keys from reports actually delivered; never modified")
    args = parser.parse_args()
    try:
        payloads = [json.loads((args.public_dir / f"{name}.json").read_text(encoding="utf-8"))
                    for name in ("health", "latest", "recent")]
        ledger = json.loads(args.reported.read_text(encoding="utf-8")) if args.reported else None
        if ledger is not None and (not isinstance(ledger, list) or not all(isinstance(k, str) for k in ledger)):
            raise ValueError("invalid delivery ledger")
        result = build_brief(*payloads, now=datetime.now(timezone.utc), since=args.since,
                             until=args.until, reported_keys=set(ledger) if ledger is not None else None)
    except (OSError, ValueError, TypeError):
        result = {"status": "source_error", "reason": "unreadable_input", "groups": [], "count": 0}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "source_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
