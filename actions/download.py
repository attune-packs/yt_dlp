#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable
from urllib.parse import urlparse

import yt_dlp


MANAGED_OPTIONS = {
    "batchfile",
    "cookiefile",
    "cookiesfrombrowser",
    "download_archive",
    "exec_before_dl_cmd",
    "exec_cmd",
    "external_downloader",
    "external_downloader_args",
    "ffmpeg_location",
    "load_info_filename",
    "logger",
    "netrc_location",
    "noplaylist",
    "outtmpl",
    "overwrites",
    "password",
    "paths",
    "postprocessors",
    "postprocessor_hooks",
    "progress_hooks",
    "quiet",
    "simulate",
    "skip_download",
    "usenetrc",
    "username",
    "videopassword",
}


class ActionError(Exception):
    pass


class _Logger:
    def debug(self, message: str) -> None:
        return None

    def info(self, message: str) -> None:
        return None

    def warning(self, message: str) -> None:
        print(f"yt-dlp warning: {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        print(f"yt-dlp error: {message}", file=sys.stderr)


def _required_url(params: dict[str, Any]) -> str:
    value = params.get("url")
    if not isinstance(value, str) or not value:
        raise ActionError("url must be a non-empty string")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        raise ActionError("url must be an HTTP or HTTPS URL without user information")
    return value


def _relative_path(params: dict[str, Any], name: str, default: str | None = None) -> Path | None:
    value = params.get(name, default)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise ActionError(f"{name} must be a path string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ActionError(f"{name} must stay below the action artifact directory")
    return path


def _boolean(params: dict[str, Any], name: str, default: bool) -> bool:
    value = params.get(name, default)
    if not isinstance(value, bool):
        raise ActionError(f"{name} must be a boolean")
    return value


def _media_entries(info: dict[str, Any]) -> Iterable[dict[str, Any]]:
    entries = info.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                yield from _media_entries(entry)
        return
    yield info


def _media_summary(entry: dict[str, Any]) -> dict[str, Any]:
    fields = ("id", "title", "extractor", "webpage_url", "live_status", "duration")
    return {name: entry[name] for name in fields if entry.get(name) is not None}


def download(params: dict[str, Any], artifacts_root: Path, ydl_type: type = yt_dlp.YoutubeDL) -> dict[str, Any]:
    url = _required_url(params)
    output_relative = _relative_path(params, "output_directory", "downloads")
    assert output_relative is not None
    output_directory = (artifacts_root / output_relative).resolve()
    root = artifacts_root.resolve()
    if output_directory != root and root not in output_directory.parents:
        raise ActionError("output_directory escapes ATTUNE_ARTIFACTS_DIR")
    output_directory.mkdir(parents=True, exist_ok=True)

    template = params.get("output_template", "%(extractor)s/%(uploader)s/%(title)s [%(id)s].%(ext)s")
    if not isinstance(template, str) or not template or Path(template).is_absolute() or ".." in Path(template).parts:
        raise ActionError("output_template must stay below output_directory")
    extra = params.get("yt_dlp_options", {})
    if not isinstance(extra, dict):
        raise ActionError("yt_dlp_options must be an object")
    conflicts = MANAGED_OPTIONS.intersection(extra)
    if conflicts:
        raise ActionError(f"yt_dlp_options contains managed field: {sorted(conflicts)[0]}")

    finished_files: list[str] = []

    def progress(event: dict[str, Any]) -> None:
        filename = event.get("filename")
        if event.get("status") == "finished" and isinstance(filename, str):
            finished_files.append(str(Path(filename).resolve()))

    options = dict(extra)
    options.update({
        "logger": _Logger(),
        "noplaylist": not _boolean(params, "playlist", False),
        "outtmpl": str(output_directory / template),
        "overwrites": _boolean(params, "overwrite", False),
        "progress_hooks": [progress],
        "quiet": True,
    })
    format_selector = params.get("format")
    if format_selector is not None:
        if not isinstance(format_selector, str) or not format_selector:
            raise ActionError("format must be a non-empty string")
        options["format"] = format_selector
    archive_relative = _relative_path(params, "download_archive")
    if archive_relative is not None:
        archive_path = (output_directory / archive_relative).resolve()
        if archive_path != output_directory and output_directory not in archive_path.parents:
            raise ActionError("download_archive escapes output_directory")
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        options["download_archive"] = str(archive_path)

    with ydl_type(options) as downloader:
        info = downloader.extract_info(url, download=True)
    if not isinstance(info, dict):
        raise ActionError("yt-dlp returned no media metadata")
    media = [_media_summary(entry) for entry in _media_entries(info)]
    files = sorted(set(finished_files))
    return {
        "media": media,
        "files": files,
        "media_count": len(media),
        "file_count": len(files),
        "output_directory": str(output_directory),
    }


def main() -> int:
    try:
        raw = sys.stdin.read()
        params = json.loads(raw) if raw.strip() else {}
        if not isinstance(params, dict):
            raise ActionError("parameters must be a JSON object")
        artifacts = os.environ.get("ATTUNE_ARTIFACTS_DIR")
        if not artifacts:
            raise ActionError("ATTUNE_ARTIFACTS_DIR is required")
        result = download(params, Path(artifacts))
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (ActionError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: media download failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
