# yt-dlp media pack

This pack separates media monitoring from transfer policy:

- `yt_dlp.media_availability_sensor` inspects any HTTP or HTTPS source supported by yt-dlp.
- `yt_dlp.media_available` reports an item's current availability state.
- `yt_dlp.download` downloads on-demand media or records a live stream as a private Attune file artifact.

The sensor does not start downloads. Rules decide which states should trigger work, and workflows own scheduling, approvals, retries, naming policy, and any downstream publishing.

## Requirements

The pack installs the default `yt-dlp` dependency group, including `yt-dlp-ejs`. It uses Node for JavaScript challenge solving and does not download or update a standalone executable at runtime.

Some formats require `ffmpeg` on the action worker. Install it on workers that merge separate audio and video streams or use yt-dlp postprocessors.

## Monitor a source

Create an enabled rule against `yt_dlp.media_available`. Its trigger parameters configure the sensor instance:

```yaml
source_url: "https://www.youtube.com/@example/streams"
live_statuses: [is_live, is_upcoming]
poll_interval_seconds: 300
socket_timeout_seconds: 30
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
  "playlist": false
}
```

The action downloads into an isolated temporary directory, registers each completed media file as a private, execution-linked Attune artifact, and removes the temporary files. Its result contains artifact and version IDs rather than worker filesystem paths.

`filename_template` controls the displayed artifact filename and cannot contain directory components. The default is `%(title)s [%(id)s].%(ext)s`.

`yt_dlp_options` accepts non-file-producing, JSON-serializable `YoutubeDL` options for extractor and format behavior. The action rejects output paths, credential sources, callbacks, postprocessors, JavaScript runtime overrides, sidecar files, external executables, loggers, and skip-download controls. This prevents unregistered files and stops action input from turning yt-dlp into a general command runner or local credential reader.

Example options:

```json
{
  "url": "https://example.invalid/watch/123",
  "format": "bestvideo+bestaudio/best",
  "filename_template": "%(title)s [%(id)s].%(ext)s",
  "yt_dlp_options": {
    "merge_output_format": "mkv",
    "retries": 5,
    "fragment_retries": 5
  }
}
```

## Intentional limits

- Source and media URLs must use HTTP or HTTPS. Local files and URL forms containing user information are rejected.
- The sensor does not accept credentials yet. Add a key-backed credential boundary before monitoring private sources.
- The action does not retain a yt-dlp download archive across executions. Use an Attune-managed state store if workflows need cross-execution deduplication.
- The action returns registered artifact IDs and concise media metadata. It does not print or return worker paths or direct extractor stream URLs, which often contain short-lived credentials.
