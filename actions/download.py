#!/usr/bin/env python3
from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Iterable
from urllib.parse import urlparse

import attune
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
    "force_write_download_archive",
    "load_info_filename",
    "logger",
    "js_runtimes",
    "netrc_location",
    "noplaylist",
    "outtmpl",
    "overwrites",
    "password",
    "paths",
    "postprocessors",
    "postprocessor_hooks",
    "post_hooks",
    "print_to_file",
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


def download(params: dict[str, Any], staging_directory: Path, ydl_type: type = yt_dlp.YoutubeDL) -> dict[str, Any]:
    url = _required_url(params)
    staging_directory = staging_directory.resolve()
    staging_directory.mkdir(parents=True, exist_ok=True)

    template = params.get("filename_template", "%(title)s [%(id)s].%(ext)s")
    if (
        not isinstance(template, str)
        or not template
        or "\x00" in template
        or "/" in template
        or "\\" in template
        or template in {".", ".."}
    ):
        raise ActionError("filename_template must not contain directory components")
    extra = params.get("yt_dlp_options", {})
    if not isinstance(extra, dict):
        raise ActionError("yt_dlp_options must be an object")
    conflicts = MANAGED_OPTIONS.intersection(extra)
    conflicts.update(name for name in extra if name.startswith("write"))
    if conflicts:
        raise ActionError(f"yt_dlp_options contains managed field: {sorted(conflicts)[0]}")

    finished_files: list[str] = []

    def finished(filename: str) -> None:
        finished_files.append(str(Path(filename).resolve()))

    options = dict(extra)
    options.update({
        "js_runtimes": {
            "node": {
                "path": None,
            },
        },
        "post_hooks": [finished],
        "logger": _Logger(),
        "noplaylist": not _boolean(params, "playlist", False),
        "outtmpl": str(staging_directory / template),
        "quiet": True,
    })
    format_selector = params.get("format")
    if format_selector is not None:
        if not isinstance(format_selector, str) or not format_selector:
            raise ActionError("format must be a non-empty string")
        options["format"] = format_selector
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
    }


def register_files(files: list[str], execution_id: str, allocator=attune.artifacts.allocate_file_version) -> list[dict[str, Any]]:
    artifacts = []
    for index, filename in enumerate(files, start=1):
        source = Path(filename)
        if not source.is_file():
            raise ActionError(f"downloaded media file is missing: {source.name}")
        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        allocation = allocator(
            f"yt_dlp.download.media.{execution_id}.{index}",
            artifact_type="file_binary",
            visibility="private",
            content_type=content_type,
            name=source.name,
            description="Media downloaded by yt_dlp.download",
        )
        shutil.move(source, allocation.absolute_path)
        artifacts.append({
            "artifact_id": allocation.artifact_id,
            "artifact_ref": allocation.artifact_ref,
            "version_id": allocation.version_id,
            "name": source.name,
            "content_type": content_type,
        })
    return artifacts


def execute(
    params: dict[str, Any],
    artifacts_root: Path,
    execution_id: str,
    ydl_type: type = yt_dlp.YoutubeDL,
    allocator=attune.artifacts.allocate_file_version,
) -> dict[str, Any]:
    staging_root = artifacts_root.resolve() / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"yt-dlp-{execution_id}-", dir=staging_root) as directory:
        result = download(params, Path(directory), ydl_type)
        registered = register_files(result.pop("files"), execution_id, allocator)
    result["artifact_count"] = len(registered)
    result["artifacts"] = registered
    return result


def main() -> int:
    try:
        raw = sys.stdin.read()
        params = json.loads(raw) if raw.strip() else {}
        if not isinstance(params, dict):
            raise ActionError("parameters must be a JSON object")
        if not attune.context.has_api_token:
            raise ActionError("artifact registration requires an execution-scoped API token")
        artifacts = os.environ.get("ATTUNE_ARTIFACTS_DIR")
        if not artifacts:
            raise ActionError("ATTUNE_ARTIFACTS_DIR is required")
        result = execute(params, Path(artifacts), str(attune.context.execution_id))
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
