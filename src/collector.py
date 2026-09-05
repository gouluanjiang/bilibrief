#!/usr/bin/env python3
"""BiliBrief collector.

Reads the authenticated Bilibili following dynamic feed, normalizes entries,
persists a short rolling archive, and emits static JSON for GitHub Pages.

Important: the Bilibili cookie is read only from the BILIBILI_COOKIE
environment variable and is never written to output files or logs.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

API_NAV = "https://api.bilibili.com/x/web-interface/nav"
API_FEED = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
TZ_CN = timezone.utc  # timestamps are stored as UTC ISO-8601

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
PUBLIC_DIR = ROOT / "public"
ARCHIVE_PATH = DATA_DIR / "archive.json"
LATEST_PATH = PUBLIC_DIR / "latest.json"
RECENT_PATH = PUBLIC_DIR / "recent.json"
HEALTH_PATH = PUBLIC_DIR / "health.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(ts: int | float | None = None) -> str:
    if ts is None:
        dt = utc_now()
    else:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_url(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("/"):
        return urljoin("https://www.bilibili.com", value)
    return value


def get_nested(obj: Any, *keys: str, default: Any = None) -> Any:
    cur = obj
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def first_nonempty(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def recursive_find_string(obj: Any, keys: tuple[str, ...]) -> str:
    """Best-effort fallback for fields whose exact container varies by type."""
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in obj.values():
            found = recursive_find_string(value, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = recursive_find_string(value, keys)
            if found:
                return found
    return ""


def parse_major(major: Any) -> tuple[str, str, str, str]:
    """Return (major_type, title, description, target_url)."""
    if not isinstance(major, dict):
        return "", "", "", ""

    major_type = str(major.get("type") or "")

    # Video archive
    archive = major.get("archive")
    if isinstance(archive, dict):
        title = first_nonempty(archive.get("title"))
        desc = first_nonempty(archive.get("desc"))
        url = normalize_url(first_nonempty(archive.get("jump_url")))
        if not url and archive.get("bvid"):
            url = f"https://www.bilibili.com/video/{archive['bvid']}"
        return major_type or "MAJOR_TYPE_ARCHIVE", title, desc, url

    # Modern opus / text+images
    opus = major.get("opus")
    if isinstance(opus, dict):
        summary = opus.get("summary") if isinstance(opus.get("summary"), dict) else {}
        title = first_nonempty(opus.get("title"), summary.get("title"))
        desc = first_nonempty(summary.get("text"))
        url = normalize_url(first_nonempty(opus.get("jump_url")))
        return major_type or "MAJOR_TYPE_OPUS", title, desc, url

    # Article / common / music / PGC etc. vary; use defensive fallbacks.
    title = recursive_find_string(major, ("title", "name"))
    desc = recursive_find_string(major, ("desc", "summary", "text"))
    url = normalize_url(recursive_find_string(major, ("jump_url", "url")))
    return major_type, title, desc, url


def parse_one(item: dict[str, Any], *, nested: bool = False) -> dict[str, Any]:
    dynamic_id = str(item.get("id_str") or item.get("id") or "")
    modules = item.get("modules") if isinstance(item.get("modules"), dict) else {}
    author = modules.get("module_author") if isinstance(modules.get("module_author"), dict) else {}
    dynamic = modules.get("module_dynamic") if isinstance(modules.get("module_dynamic"), dict) else {}
    desc_obj = dynamic.get("desc") if isinstance(dynamic.get("desc"), dict) else {}

    author_mid = str(author.get("mid") or "")
    author_name = first_nonempty(author.get("name"))
    pub_ts = author.get("pub_ts")
    try:
        pub_ts_int = int(pub_ts)
    except (TypeError, ValueError):
        pub_ts_int = 0

    desc_text = first_nonempty(desc_obj.get("text"))
    major_type, major_title, major_desc, major_url = parse_major(dynamic.get("major"))
    dynamic_type = str(item.get("type") or "")

    title = major_title
    text = first_nonempty(desc_text, major_desc)

    # If title is blank, do not duplicate a huge text blob; take first 80 chars.
    if not title and text:
        compact = " ".join(text.split())
        title = compact[:80] + ("…" if len(compact) > 80 else "")

    target_url = major_url
    if not target_url and dynamic_id:
        target_url = f"https://t.bilibili.com/{dynamic_id}"

    kind = "dynamic"
    if dynamic_type == "DYNAMIC_TYPE_AV" or major_type == "MAJOR_TYPE_ARCHIVE":
        kind = "video"
    elif "LIVE" in dynamic_type or "LIVE" in major_type:
        kind = "live"
    elif dynamic_type == "DYNAMIC_TYPE_FORWARD":
        kind = "repost"
    elif "ARTICLE" in dynamic_type or "ARTICLE" in major_type:
        kind = "article"

    parsed: dict[str, Any] = {
        "id": dynamic_id,
        "kind": kind,
        "dynamic_type": dynamic_type,
        "major_type": major_type,
        "up": author_name,
        "up_uid": author_mid,
        "published_at": iso_utc(pub_ts_int) if pub_ts_int else "",
        "published_ts": pub_ts_int,
        "title": title,
        "text": text,
        "url": target_url,
        "author_action": first_nonempty(author.get("pub_action")),
    }

    # For reposts, preserve the original item's useful metadata.
    orig = item.get("orig")
    if isinstance(orig, dict) and not nested:
        parsed["original"] = parse_one(orig, nested=True)

    return parsed


def load_archive() -> dict[str, dict[str, Any]]:
    if not ARCHIVE_PATH.exists():
        return {}
    try:
        payload = json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = payload.get("items", []) if isinstance(payload, dict) else []
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            result[str(item["id"])] = item
    return result


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_session(cookie: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0.0.0 Safari/537.36"
            ),
            "Referer": "https://t.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie,
        }
    )
    proxy = os.environ.get("HTTP_PROXY_URL", "").strip()
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def request_json(session: requests.Session, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            r = session.get(url, params=params, timeout=25)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                raise RuntimeError("Bilibili returned non-object JSON")
            return data
        except Exception as exc:  # noqa: BLE001 - we want retry across network/json/http failures
            last_error = exc
            if attempt < 3:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Request failed after retries: {last_error}")


def verify_login(session: requests.Session) -> None:
    data = request_json(session, API_NAV)
    if data.get("code") != 0 or not get_nested(data, "data", "isLogin", default=False):
        raise RuntimeError(
            "Bilibili login verification failed. The cookie may be expired or blocked. "
            f"API message: {data.get('message', '')!s}"
        )


def fetch_feed(session: requests.Session, stop_before_ts: int, max_pages: int = 30) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    offset = ""

    for page in range(max_pages):
        params = {
            "type": "all",
            "offset": offset,
            "timezone_offset": "-480",
            "platform": "web",
            "features": "itemOpusStyle,listOnlyfans,opusBigCover,decorationCard,onlyfansAssetsV2,forwardListHidden",
        }
        payload = request_json(session, API_FEED, params=params)
        code = payload.get("code")
        if code != 0:
            raise RuntimeError(f"Bilibili feed API error: code={code}, message={payload.get('message')}")

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        page_items = data.get("items") if isinstance(data.get("items"), list) else []
        if not page_items:
            break

        oldest_ts = None
        for raw in page_items:
            if not isinstance(raw, dict):
                continue
            parsed = parse_one(raw)
            if not parsed.get("id"):
                continue
            results.append(parsed)
            ts = parsed.get("published_ts") or 0
            if ts:
                oldest_ts = ts if oldest_ts is None else min(oldest_ts, ts)

        has_more = bool(data.get("has_more"))
        offset = str(data.get("offset") or "")

        # We already have everything newer than our retention window.
        if oldest_ts is not None and oldest_ts < stop_before_ts:
            break
        if not has_more or not offset:
            break

        time.sleep(1.2)  # polite spacing to reduce rate-limit risk

    return results


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

    cookie = os.environ.get("BILIBILI_COOKIE", "").strip()
    retention_hours = int(os.environ.get("RETENTION_HOURS", "168"))
    latest_hours = int(os.environ.get("LATEST_HOURS", "30"))
    recent_hours = int(os.environ.get("RECENT_HOURS", "72"))
    now = utc_now()

    health = {
        "status": "error",
        "checked_at": iso_utc(),
        "last_success_at": None,
        "fetched_items": 0,
        "latest_items": 0,
        "message": "",
    }

    if not cookie:
        health["message"] = "Missing BILIBILI_COOKIE environment variable."
        write_json(HEALTH_PATH, health)
        print(health["message"], file=sys.stderr)
        return 2

    try:
        session = make_session(cookie)
        verify_login(session)

        stop_before_ts = int(now.timestamp()) - retention_hours * 3600
        fetched = fetch_feed(session, stop_before_ts=stop_before_ts)

        archive = load_archive()
        seen_at = iso_utc()
        for item in fetched:
            item_id = str(item["id"])
            previous = archive.get(item_id, {})
            item["first_seen_at"] = previous.get("first_seen_at") or seen_at
            item["last_seen_at"] = seen_at
            archive[item_id] = item

        # Keep rolling archive based on publish time; malformed entries expire too.
        keep_after = int(now.timestamp()) - retention_hours * 3600
        kept = [
            item
            for item in archive.values()
            if int(item.get("published_ts") or 0) >= keep_after
        ]
        kept.sort(key=lambda x: int(x.get("published_ts") or 0), reverse=True)

        latest_after = int(now.timestamp()) - latest_hours * 3600
        recent_after = int(now.timestamp()) - recent_hours * 3600

        # Pure live status cards are intentionally omitted from the public brief feed.
        # A creator's textual "important livestream announcement" remains a normal dynamic.
        publishable = [item for item in kept if item.get("kind") != "live"]
        latest = [item for item in publishable if int(item.get("published_ts") or 0) >= latest_after]
        recent = [item for item in publishable if int(item.get("published_ts") or 0) >= recent_after]

        archive_payload = {
            "schema_version": 1,
            "updated_at": iso_utc(),
            "retention_hours": retention_hours,
            "items": kept,
        }
        latest_payload = {
            "schema_version": 1,
            "generated_at": iso_utc(),
            "window_hours": latest_hours,
            "count": len(latest),
            "items": latest,
        }
        recent_payload = {
            "schema_version": 1,
            "generated_at": iso_utc(),
            "window_hours": recent_hours,
            "count": len(recent),
            "items": recent,
        }

        write_json(ARCHIVE_PATH, archive_payload)
        write_json(LATEST_PATH, latest_payload)
        write_json(RECENT_PATH, recent_payload)

        health.update(
            {
                "status": "ok",
                "checked_at": iso_utc(),
                "last_success_at": iso_utc(),
                "fetched_items": len(fetched),
                "latest_items": len(latest),
                "message": "ok",
            }
        )
        write_json(HEALTH_PATH, health)
        print(f"OK: fetched={len(fetched)}, latest={len(latest)}, archive={len(kept)}")
        return 0

    except Exception as exc:  # noqa: BLE001
        health["message"] = str(exc)[:500]
        # Preserve last successful timestamp if previous health exists.
        if HEALTH_PATH.exists():
            try:
                old = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
                if isinstance(old, dict):
                    health["last_success_at"] = old.get("last_success_at")
            except Exception:
                pass
        write_json(HEALTH_PATH, health)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
