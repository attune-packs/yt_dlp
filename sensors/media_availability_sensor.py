#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterable
from urllib.parse import urlparse

import attune
import yt_dlp


TRIGGER_REF = "yt_dlp.media_available"


def _required_url(values: dict[str, Any]) -> str:
    value = values.get("source_url")
    if not isinstance(value, str) or not value:
        raise ValueError("source_url must be a non-empty string")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        raise ValueError("source_url must be an HTTP or HTTPS URL without user information")
    return value


def _bounded_integer(values: dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
    value = values.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _statuses(values: dict[str, Any]) -> set[str]:
    statuses = values.get("live_statuses", ["is_live", "is_upcoming"])
    if not isinstance(statuses, list) or not statuses or any(not isinstance(item, str) or not item for item in statuses):
        raise ValueError("live_statuses must be a non-empty array of strings")
    return set(statuses)


def _entries(info: dict[str, Any]) -> Iterable[dict[str, Any]]:
    entries = info.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                yield from _entries(entry)
        return
    yield info


def discover(source_url: str, timeout_seconds: int, max_entries: int) -> list[dict[str, Any]]:
    options = {
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "lazy_playlist": False,
        "playlistend": max_entries,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": timeout_seconds,
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(source_url, download=False)
    if not isinstance(info, dict):
        return []
    return list(_entries(info))[:max_entries]


def _iso_timestamp(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _string(entry: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = entry.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def normalize(entry: dict[str, Any], source_url: str, discovered_at: str) -> tuple[str, str, dict[str, Any]]:
    media_id = _string(entry, "id") or hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()[:24]
    extractor = _string(entry, "extractor_key", "extractor") or "unknown"
    media_url = _string(entry, "webpage_url", "original_url")
    raw_url = _string(entry, "url")
    if media_url is None and raw_url and urlparse(raw_url).scheme in {"http", "https"}:
        media_url = raw_url
    media_url = media_url or source_url
    live_status = _string(entry, "live_status") or ("is_live" if entry.get("is_live") is True else "unknown")
    payload: dict[str, Any] = {
        "media_id": media_id,
        "source_url": source_url,
        "media_url": media_url,
        "title": _string(entry, "title", "fulltitle") or media_id,
        "extractor": extractor,
        "live_status": live_status,
        "is_live": live_status == "is_live" or entry.get("is_live") is True,
        "discovered_at": discovered_at,
    }
    optional = {
        "scheduled_at": _iso_timestamp(entry.get("release_timestamp") or entry.get("timestamp")),
        "duration_seconds": entry.get("duration") if isinstance(entry.get("duration"), (int, float)) and not isinstance(entry.get("duration"), bool) else None,
        "channel": _string(entry, "channel", "uploader"),
        "playlist": _string(entry, "playlist_title", "playlist"),
    }
    payload.update({name: value for name, value in optional.items() if value is not None})
    identity = f"{extractor}:{media_id}"
    return identity, live_status, payload


def _checkpoint_path(rule_id: int, source_url: str) -> Path:
    root = Path(os.environ["ATTUNE_ARTIFACTS_DIR"]) / "yt_dlp_media_sensor"
    digest = hashlib.sha256(f"{rule_id}\0{source_url}".encode()).hexdigest()[:24]
    return root / f"{digest}.json"


def _read_checkpoint(path: Path) -> dict[str, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("media sensor checkpoint is unreadable") from exc
    if not isinstance(document, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in document.items()):
        raise RuntimeError("media sensor checkpoint is invalid")
    return document


def _write_checkpoint(path: Path, states: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(states, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


class MediaAvailabilitySensor(attune.PollingSensor):
    interval = 5.0

    def setup(self) -> None:
        self._next_due: dict[int, float] = {}
        self._locks: dict[int, threading.Lock] = {}

    def poll(self, rule: Any) -> None:
        values = dict(rule.trigger_params or {})
        rule_id = int(getattr(rule, "id", getattr(rule, "rule_id", 0)))
        interval = _bounded_integer(values, "poll_interval_seconds", 300, 5, 86400)
        now = time.monotonic()
        if now < self._next_due.get(rule_id, 0):
            return
        self._next_due[rule_id] = now + interval
        lock = self._locks.setdefault(rule_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return
        try:
            source_url = _required_url(values)
            timeout = _bounded_integer(values, "timeout_seconds", 30, 1, 600)
            max_entries = _bounded_integer(values, "max_entries", 50, 1, 500)
            matching_statuses = _statuses(values)
            emit_initial = values.get("emit_initial", True)
            if not isinstance(emit_initial, bool):
                raise ValueError("emit_initial must be a boolean")
            checkpoint = _checkpoint_path(rule_id, source_url)
            states = _read_checkpoint(checkpoint)
            had_checkpoint = checkpoint.exists()
            discovered_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
            for entry in discover(source_url, timeout, max_entries):
                identity, live_status, payload = normalize(entry, source_url, discovered_at)
                previous = states.get(identity)
                should_emit = live_status in matching_statuses and previous != live_status and (had_checkpoint or emit_initial)
                if should_emit:
                    event_id = self.emit(payload, rule=rule)
                    if event_id is None:
                        raise RuntimeError("Attune event emission failed")
                if previous != live_status:
                    states[identity] = live_status
                    _write_checkpoint(checkpoint, states)
        except Exception as exc:
            self._next_due[rule_id] = time.monotonic() + min(300, interval * 2)
            self.logger.warning("rule %s media poll failed: %s", rule_id, type(exc).__name__)
        finally:
            lock.release()


if __name__ == "__main__":
    attune.run_sensor(MediaAvailabilitySensor)
