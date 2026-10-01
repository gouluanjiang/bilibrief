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
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

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

ERROR_MESSAGES = {
    "missing_cookie": "BILIBILI_COOKIE is not configured.",
    "login_required": "Bilibili login verification failed; check the monitoring account login and Secret privately.",
    "network_error": "Bilibili could not be reached after retries.",
    "http_error": "Bilibili returned an HTTP error after retries.",
    "invalid_json": "Bilibili returned invalid JSON after retries.",
    "api_error": "Bilibili rejected the feed request.",
    "incomplete_feed": "Feed data or pagination is incomplete; the previous snapshot was retained.",
    "archive_invalid": "The existing archive is invalid; it was retained for recovery.",
    "invalid_config": "Collection windows must be positive integers: latest <= recent <= retention.",
    "write_error": "Output could not be saved safely.",
    "rollback_failed": "Snapshot recovery failed; publishing is blocked.",
    "unexpected_error": "Collection failed unexpectedly; no raw exception details are published.",
}


class CollectionError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(ERROR_MESSAGES[code])


def identity_keys(item: dict[str, Any]) -> list[str]:
    """Stable aliases for delivery deduplication; tracking parameters are ignored."""
    keys = []
    if item.get("id"):
        keys.append(f"dynamic:{item['id']}")
    url = normalize_url(item.get("url"))
    parts = urlsplit(url)
    if parts.hostname in {"www.bilibili.com", "bilibili.com", "t.bilibili.com"}:
        bvid = re.search(r"/video/(BV[0-9A-Za-z]+)(?:/|$)", parts.path)
        if bvid:
            keys.append(f"video:{bvid[1]}")
        keys.append("url:" + urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), "", "")))
    return keys


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
    parsed["identity_keys"] = identity_keys(parsed)

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
    except (OSError, ValueError):
        raise CollectionError("archive_invalid") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise CollectionError("archive_invalid")
    items = payload["items"]
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not valid_item(item) or str(item["id"]) in result:
            raise CollectionError("archive_invalid")
        result[str(item["id"])] = item
    return result


def valid_item(item: Any) -> bool:
    return (
        isinstance(item, dict) and isinstance(item.get("id"), str) and bool(item["id"])
        and type(item.get("published_ts")) is int and item["published_ts"] > 0
    )


def stage_bytes(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".bilibrief-", delete=False) as tmp:
        staged = Path(tmp.name)
        try:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        except Exception:
            tmp.close()
            staged.unlink(missing_ok=True)
            raise
    return staged


def write_bundle(payloads: dict[Path, Any]) -> None:
    """Stage everything first; on replace failure restore every changed file.

    Several files cannot be atomically swapped together. CI only publishes after
    this function completes (or after a successful rollback and error health).
    """
    staged: dict[Path, Path] = {}
    previous: dict[Path, bytes | None] = {}
    replaced: list[Path] = []
    try:
        for path, payload in payloads.items():
            content = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            json.loads(content)  # validate before touching a previous snapshot
            previous[path] = path.read_bytes() if path.exists() else None
            staged[path] = stage_bytes(path, content)
        for path, temp_path in staged.items():
            os.replace(temp_path, path)
            replaced.append(path)
    except Exception:
        recovery_failed = False
        for path in reversed(replaced):
            try:
                if previous[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    restore = stage_bytes(path, previous[path])
                    try:
                        os.replace(restore, path)
                    finally:
                        restore.unlink(missing_ok=True)
            except OSError:
                recovery_failed = True
        raise CollectionError("rollback_failed" if recovery_failed else "write_error") from None
    finally:
        for path in staged.values():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # Never replace a rollback failure with a cleanup failure.


def write_json(path: Path, payload: Any) -> None:
    write_bundle({path: payload})


def previous_success(now: datetime) -> str | None:
    try:
        old = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
        value = old.get("last_success_at")
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo and timestamp <= now:
            return value
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def publishing_output(ready: bool) -> bool:
    if os.environ.get("GITHUB_OUTPUT"):
        try:
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
                stream.write(f"publishable={'true' if ready else 'false'}\n")
        except OSError:
            print("ERROR [write_error]: Workflow output could not be saved; publishing is blocked.", file=sys.stderr)
            return False
    return True


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
    error_code = "network_error"
    for attempt in range(4):
        try:
            r = session.get(url, params=params, timeout=25)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError("non-object JSON")
            return data
        except ValueError:
            error_code = "invalid_json"
        except requests.HTTPError:
            error_code = "http_error"
        except requests.RequestException:
            error_code = "network_error"
        if attempt < 3:
            time.sleep(2 ** attempt)
    raise CollectionError(error_code)


def verify_login(session: requests.Session) -> None:
    data = request_json(session, API_NAV)
    if data.get("code") != 0 or get_nested(data, "data", "isLogin") is not True:
        raise CollectionError("login_required")


def fetch_feed(session: requests.Session, stop_before_ts: int, max_pages: int = 30) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    offset = ""
    offsets = {offset}

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
            raise CollectionError("login_required" if code == -101 else "api_error")

        data = payload.get("data")
        if (not isinstance(data, dict) or not isinstance(data.get("items"), list)
                or type(data.get("has_more")) not in (bool, int) or data["has_more"] not in (0, 1)):
            raise CollectionError("incomplete_feed")
        page_items = data["items"]

        page_times = []
        for raw in page_items:
            if not isinstance(raw, dict):
                raise CollectionError("incomplete_feed")
            try:
                parsed = parse_one(raw)
            except (TypeError, ValueError, OverflowError, OSError):
                raise CollectionError("incomplete_feed") from None
            if not valid_item(parsed):
                raise CollectionError("incomplete_feed")
            results.append(parsed)
            page_times.append(parsed["published_ts"])

        has_more = bool(data.get("has_more"))
        if not has_more:
            return results
        next_offset = data.get("offset")
        if not page_items or not isinstance(next_offset, str) or not next_offset or next_offset in offsets:
            raise CollectionError("incomplete_feed")
        # A single old/pinned entry must not truncate newer pages.
        if page_times and max(page_times) < stop_before_ts:
            return results
        offset = next_offset
        offsets.add(offset)

        if page + 1 < max_pages:
            time.sleep(1.2)  # polite spacing to reduce rate-limit risk

    raise CollectionError("incomplete_feed")


def main() -> int:
    now = utc_now()
    seen_at = iso_utc(now.timestamp())
    last_success = previous_success(now)

    health = {
        "schema_version": 2,
        "status": "error",
        "checked_at": seen_at,
        "last_success_at": last_success,
        "snapshot_generated_at": last_success,
        "fetched_items": 0,
        "latest_items": 0,
        "new_items": 0,
        "coverage": {"complete": False, "scope": "observed_following_feed_window"},
        "error_code": None,
        "message": "",
    }

    try:
        cookie = os.environ.get("BILIBILI_COOKIE", "").strip()
        if not cookie:
            raise CollectionError("missing_cookie")
        try:
            retention_hours = int(os.environ.get("RETENTION_HOURS", "168"))
            latest_hours = int(os.environ.get("LATEST_HOURS", "30"))
            recent_hours = int(os.environ.get("RECENT_HOURS", "72"))
            if not 0 < latest_hours <= recent_hours <= retention_hours:
                raise ValueError
        except ValueError:
            raise CollectionError("invalid_config") from None

        archive = load_archive()
        stop_before_ts = int(now.timestamp()) - retention_hours * 3600
        with make_session(cookie) as session:
            verify_login(session)
            fetched = fetch_feed(session, stop_before_ts=stop_before_ts)

        previous_ids = set(archive)
        for item in fetched:
            if not valid_item(item) or item["published_ts"] > int(now.timestamp()):
                raise CollectionError("incomplete_feed")
            item_id = str(item["id"])
            previous = archive.get(item_id, {})
            item["first_seen_at"] = previous.get("first_seen_at") or seen_at
            item["last_seen_at"] = seen_at
            item["identity_keys"] = identity_keys(item)
            archive[item_id] = item

        # Keep rolling archive based on publish time; malformed entries expire too.
        keep_after = int(now.timestamp()) - retention_hours * 3600
        kept = [
            item
            for item in archive.values()
            if int(item.get("published_ts") or 0) >= keep_after
        ]
        kept.sort(key=lambda x: (x["published_ts"], x["id"]), reverse=True)

        latest_after = int(now.timestamp()) - latest_hours * 3600
        recent_after = int(now.timestamp()) - recent_hours * 3600

        # Pure live status cards are intentionally omitted from the public brief feed.
        # A creator's textual "important livestream announcement" remains a normal dynamic.
        publishable = [item for item in kept if item.get("kind") != "live"]
        latest = [item for item in publishable if int(item.get("published_ts") or 0) >= latest_after]
        recent = [item for item in publishable if int(item.get("published_ts") or 0) >= recent_after]

        archive_payload = {
            "schema_version": 1,
            "updated_at": seen_at,
            "retention_hours": retention_hours,
            "items": kept,
        }
        latest_payload = {
            "schema_version": 1,
            "generated_at": seen_at,
            "coverage": {"complete": True, "scope": "observed_following_feed_window"},
            "window_hours": latest_hours,
            "count": len(latest),
            "items": latest,
        }
        recent_payload = {
            "schema_version": 1,
            "generated_at": seen_at,
            "coverage": {"complete": True, "scope": "observed_following_feed_window"},
            "window_hours": recent_hours,
            "count": len(recent),
            "items": recent,
        }

        health.update(
            {
                "status": "ok",
                "last_success_at": seen_at,
                "snapshot_generated_at": seen_at,
                "fetched_items": len(fetched),
                "latest_items": len(latest),
                "new_items": len({item["id"] for item in publishable} - previous_ids),
                "coverage": {"complete": True, "scope": "observed_following_feed_window"},
                "message": "ok",
            }
        )
        write_bundle({ARCHIVE_PATH: archive_payload, LATEST_PATH: latest_payload,
                      RECENT_PATH: recent_payload, HEALTH_PATH: health})
        if not publishing_output(True):
            return 1
        print(f"OK: fetched={len(fetched)}, latest={len(latest)}, archive={len(kept)}")
        return 0

    except Exception as exc:  # Never publish raw exceptions, API messages, headers or proxy URLs.
        code = exc.code if isinstance(exc, CollectionError) else "unexpected_error"
        health.update(status="error", last_success_at=last_success, snapshot_generated_at=last_success,
                      error_code=code, message=ERROR_MESSAGES[code], fetched_items=0, latest_items=0,
                      new_items=0, coverage={"complete": False, "scope": "observed_following_feed_window"})
        ready = False
        try:
            write_json(HEALTH_PATH, health)
            ready = code != "rollback_failed"
        except Exception:
            code = "write_error"
        print(f"ERROR [{code}]: {ERROR_MESSAGES[code]}", file=sys.stderr)
        publishing_output(ready)
        return 2 if code == "missing_cookie" else 1


if __name__ == "__main__":
    raise SystemExit(main())
