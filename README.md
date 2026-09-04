# yt-dlp media pack

This pack separates media monitoring from transfer policy:

- `yt_dlp.media_availability_sensor` inspects any HTTP or HTTPS source supported by yt-dlp.
- `yt_dlp.media_available` reports an item's current availability state.
- `yt_dlp.download` downloads on-demand media or records a live stream into the execution artifact directory.

The sensor does not start downloads. Rules decide which states should trigger work, and workflows own scheduling, approvals, retries, naming policy, and any downstream publishing.

## Requirements

The pack installs `yt-dlp` as a Python requirement. It does not download or update a standalone executable at runtime.

Some formats require `ffmpeg` on the action worker. Install it on workers that merge separate audio and video streams or use yt-dlp postprocessors.

## Monitor a source

Create an enabled rule against `yt_dlp.media_available`. Its trigger parameters configure the sensor instance:

```yaml
source_url: "https://www.youtube.com/@example/streams"
live_statuses: [is_live, is_upcoming]
poll_interval_seconds: 300
timeout_seconds: 30
max_entries: 50
emit_initial: true
```

Common yt-dlp `live_status` values are `is_live`, `is_upcoming`, `was_live`, `post_live`, and `not_live`. The sensor checkpoints each extractor and media ID pair. It emits an item again when its status changes, such as `is_upcoming` to `is_live`.

For immediate recording, condition the rule or workflow on `event.payload.live_status == "is_live"` and pass `event.payload.media_url` to `yt_dlp.download`.

## Download or record

Minimal action parameters:

```json
{
  "url": "https://example.invalid/watch/123",
  "output_directory": "archives",
  "playlist": false
}
```

`output_directory`, `output_template`, and `download_archive` are relative to `ATTUNE_ARTIFACTS_DIR`. This keeps writes inside the worker's artifact storage. Use worker placement and mounted artifact storage when archives must land on a particular filesystem.

`yt_dlp_options` accepts JSON-serializable `YoutubeDL` options for extractor and format behavior. The action rejects output paths, credential sources, callbacks, postprocessors, external executables, loggers, and skip-download controls. This prevents action input from turning yt-dlp into a general command runner or local credential reader.

Example options:

```json
{
  "url": "https://example.invalid/watch/123",
  "format": "bestvideo+bestaudio/best",
  "download_archive": "downloaded.txt",
  "yt_dlp_options": {
    "merge_output_format": "mkv",
    "writesubtitles": true,
    "subtitleslangs": ["en"]
  }
}
```

## Intentional limits

- Source and media URLs must use HTTP or HTTPS. Local files and URL forms containing user information are rejected.
- The sensor does not accept credentials yet. Add a key-backed credential boundary before monitoring private sources.
- The action returns file paths and concise media metadata. It does not print or return direct extractor stream URLs, which often contain short-lived credentials.
