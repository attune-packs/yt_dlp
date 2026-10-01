from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

import yaml


PACK_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path, modules: dict[str, ModuleType]):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class FakeYoutubeDL:
    instances = []
    info = {}

    def __init__(self, options):
        self.options = options
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def extract_info(self, url, download=False):
        self.url = url
        self.download = download
        if download:
            filename = Path(self.options["outtmpl"]).parent / "video.mkv"
            filename.write_bytes(b"video")
            self.options["post_hooks"][0](str(filename))
        return self.__class__.info


def fake_yt_dlp() -> ModuleType:
    module = ModuleType("yt_dlp")
    module.YoutubeDL = FakeYoutubeDL
    return module


class MetadataTests(unittest.TestCase):
    def test_component_contracts(self):
        manifest = yaml.safe_load((PACK_ROOT / "pack.yaml").read_text(encoding="utf-8"))
        trigger = yaml.safe_load((PACK_ROOT / "triggers" / "media_available.yaml").read_text(encoding="utf-8"))
        sensor = yaml.safe_load((PACK_ROOT / "sensors" / "media_availability_sensor.yaml").read_text(encoding="utf-8"))
        action = yaml.safe_load((PACK_ROOT / "actions" / "download.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["ref"], "yt_dlp")
        self.assertEqual(manifest["runtime_deps"], ["python"])
        self.assertEqual(sensor["trigger_types"], [trigger["ref"]])
        self.assertNotIn("poll_interval", sensor)
        self.assertEqual(set(trigger["parameters"]), {
            "source_url", "live_statuses", "poll_interval_seconds",
            "socket_timeout_seconds", "max_entries", "emit_initial",
        })
        self.assertEqual(trigger["parameters"]["live_statuses"]["items"]["enum"], [
            "is_live", "is_upcoming", "was_live", "post_live", "not_live", "unknown",
        ])
        self.assertEqual(action["ref"], "yt_dlp.download")
        self.assertEqual(action["parameter_delivery"], "stdin")
        self.assertEqual(action["parameter_format"], "json")
        self.assertEqual(action["output_format"], "json")
        self.assertEqual(action["default_execution_permission_set_refs"], ["standard"])
        self.assertEqual(set(action["parameters"]), {
            "url", "format", "playlist", "filename_template", "yt_dlp_options",
        })
        self.assertEqual(set(action["output"]), {
            "media", "media_count", "artifact_count", "artifacts",
        })
        self.assertEqual(set(trigger["output"]), {
            "media_id", "source_url", "media_url", "title", "extractor", "live_status",
            "is_live", "scheduled_at", "duration_seconds", "channel", "playlist", "discovered_at",
        })

    def test_python_library_is_a_pack_requirement(self):
        requirements = (PACK_ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("yt-dlp[default]>=2025.1.15,<2027.0.0", requirements)
        source = (PACK_ROOT / "sensors" / "media_availability_sensor.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("releases/latest/download", source)


class SensorTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.instances = []
        FakeYoutubeDL.info = {
            "entries": [{
                "id": "abc123",
                "title": "Scheduled stream",
                "extractor_key": "Youtube",
                "webpage_url": "https://www.youtube.com/watch?v=abc123",
                "live_status": "is_upcoming",
                "release_timestamp": 1788134400,
                "channel": "Example",
            }]
        }
        attune = ModuleType("attune")

        class PollingSensor:
            def __init__(self):
                self.events = []
                self.logger = mock.Mock()
                self.setup()

            def emit(self, payload, rule=None):
                self.events.append((payload, rule))
                return len(self.events)

        attune.PollingSensor = PollingSensor
        attune.run_sensor = lambda sensor: None
        self.sensor = load_module(
            "media_availability_sensor_test",
            PACK_ROOT / "sensors" / "media_availability_sensor.py",
            {"attune": attune, "yt_dlp": fake_yt_dlp()},
        )

    def test_discovery_uses_library_and_normalizes_payload(self):
        entries = self.sensor.discover("https://www.youtube.com/@example/streams", 15, 20)
        self.assertEqual(len(entries), 1)
        options = FakeYoutubeDL.instances[0].options
        self.assertEqual(options["js_runtimes"], {"node": {"path": None}})
        self.assertEqual(options["playlistend"], 20)
        self.assertEqual(options["socket_timeout"], 15)
        identity, status, payload = self.sensor.normalize(entries[0], "https://www.youtube.com/@example/streams", "2026-08-31T00:00:00Z")
        self.assertEqual(identity, "Youtube:abc123")
        self.assertEqual(status, "is_upcoming")
        self.assertEqual(payload["media_url"], "https://www.youtube.com/watch?v=abc123")
        self.assertFalse(payload["is_live"])
        self.assertEqual(payload["scheduled_at"], "2026-08-31T00:00:00Z")

    def test_sensor_emits_initial_once_and_emits_status_change(self):
        rule = SimpleNamespace(id=42, trigger_params={
            "source_url": "https://www.youtube.com/@example/streams",
            "live_statuses": ["is_upcoming", "is_live"],
            "poll_interval_seconds": 5,
            "socket_timeout_seconds": 17,
        })
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"ATTUNE_ARTIFACTS_DIR": directory}), mock.patch.object(self.sensor.time, "monotonic", side_effect=[0, 10, 20]):
            instance = self.sensor.MediaAvailabilitySensor()
            instance.poll(rule)
            instance.poll(rule)
            FakeYoutubeDL.info["entries"][0]["live_status"] = "is_live"
            instance.poll(rule)
            checkpoints = list((Path(directory) / "yt_dlp_media_sensor").glob("*.json"))
        self.assertEqual([event[0]["live_status"] for event in instance.events], ["is_upcoming", "is_live"])
        self.assertEqual(FakeYoutubeDL.instances[0].options["socket_timeout"], 17)
        self.assertEqual(len(checkpoints), 1)

    def test_emit_initial_false_baselines_without_event(self):
        rule = SimpleNamespace(id=7, trigger_params={
            "source_url": "https://example.invalid/live",
            "live_statuses": ["is_upcoming"],
            "poll_interval_seconds": 5,
            "emit_initial": False,
        })
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"ATTUNE_ARTIFACTS_DIR": directory}):
            instance = self.sensor.MediaAvailabilitySensor()
            instance.poll(rule)
        self.assertEqual(instance.events, [])

    def test_rejects_unknown_live_status(self):
        with self.assertRaisesRegex(ValueError, "unsupported value"):
            self.sensor._statuses({"live_statuses": ["is-live"]})


class ActionTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.instances = []
        FakeYoutubeDL.info = {
            "id": "abc123",
            "title": "Live stream",
            "extractor": "youtube",
            "webpage_url": "https://www.youtube.com/watch?v=abc123",
            "live_status": "is_live",
        }
        attune = ModuleType("attune")
        attune.context = SimpleNamespace(has_api_token=False, execution_id="")
        attune.artifacts = SimpleNamespace(allocate_file_version=mock.Mock())
        self.attune = attune
        self.action = load_module(
            "download_action_test",
            PACK_ROOT / "actions" / "download.py",
            {"attune": attune, "yt_dlp": fake_yt_dlp()},
        )

    def test_download_builds_managed_options_and_returns_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.action.download({
                "url": "https://www.youtube.com/watch?v=abc123",
                "filename_template": "recording.%(ext)s",
                "format": "best",
                "yt_dlp_options": {"merge_output_format": "mkv"},
            }, root, FakeYoutubeDL)
        options = FakeYoutubeDL.instances[0].options
        self.assertTrue(FakeYoutubeDL.instances[0].download)
        self.assertEqual(options["format"], "best")
        self.assertEqual(options["js_runtimes"], {"node": {"path": None}})
        self.assertEqual(options["merge_output_format"], "mkv")
        self.assertTrue(options["noplaylist"])
        self.assertEqual(options["outtmpl"], str(root / "recording.%(ext)s"))
        self.assertNotIn("overwrites", options)
        self.assertEqual(result["media_count"], 1)
        self.assertEqual(result["files"], [str(root / "video.mkv")])
        self.assertEqual(result["media"][0]["live_status"], "is_live")

    def test_register_files_moves_media_into_allocated_artifact_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "downloads" / "video.mp4"
            source.parent.mkdir()
            source.write_bytes(b"video")
            destination = root / "yt_dlp" / "download" / "media" / "v1.mp4"
            allocations = []

            def allocate(artifact_ref, **options):
                allocation = SimpleNamespace(
                    artifact_id=7,
                    artifact_ref=artifact_ref,
                    version_id=11,
                    file_path="yt_dlp/download/media/v1.mp4",
                    absolute_path=destination,
                )
                allocations.append((allocation, options))
                destination.parent.mkdir(parents=True)
                return allocation

            registered = self.action.register_files([str(source)], "42", allocate)
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"video")
            self.assertEqual(allocations[0][0].artifact_ref, "yt_dlp.download.media.42.1")
            self.assertEqual(allocations[0][1]["artifact_type"], "file_binary")
            self.assertNotIn("retention_policy", allocations[0][1])
            self.assertNotIn("retention_limit", allocations[0][1])
            self.assertEqual(registered[0]["artifact_id"], 7)
            self.assertNotIn("file_path", registered[0])

    def test_execute_isolates_and_cleans_staging_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destinations = iter([root / "registered-1.mkv", root / "registered-2.mkv"])
            staging_paths = []

            def allocate(artifact_ref, **_options):
                destination = next(destinations)
                return SimpleNamespace(
                    artifact_id=7,
                    artifact_ref=artifact_ref,
                    version_id=11,
                    absolute_path=destination,
                )

            for execution_id in ("42", "43"):
                result = self.action.execute(
                    {"url": "https://example.invalid/video"},
                    root,
                    execution_id,
                    FakeYoutubeDL,
                    allocate,
                )
                staging_paths.append(Path(FakeYoutubeDL.instances[-1].options["outtmpl"]).parent)
                self.assertEqual(result["artifact_count"], 1)
                self.assertNotIn("files", result)

            self.assertNotEqual(staging_paths[0], staging_paths[1])
            self.assertTrue(all(not path.exists() for path in staging_paths))
            self.assertEqual(list((root / ".staging").iterdir()), [])

    def test_playlist_registers_every_completed_media_file(self):
        class PlaylistYoutubeDL(FakeYoutubeDL):
            def extract_info(self, url, download=False):
                self.url = url
                self.download = download
                for filename in ("first.mp4", "second.mp4"):
                    path = Path(self.options["outtmpl"]).parent / filename
                    path.write_bytes(b"video")
                    self.options["post_hooks"][0](str(path))
                return {"entries": [
                    {"id": "first", "title": "First"},
                    {"id": "second", "title": "Second"},
                ]}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def allocate(artifact_ref, **options):
                destination = root / f"registered-{options['name']}"
                return SimpleNamespace(
                    artifact_id=len(list(root.glob("registered-*"))) + 1,
                    artifact_ref=artifact_ref,
                    version_id=11,
                    absolute_path=destination,
                )

            result = self.action.execute(
                {"url": "https://example.invalid/playlist", "playlist": True},
                root,
                "42",
                PlaylistYoutubeDL,
                allocate,
            )

        self.assertEqual(result["media_count"], 2)
        self.assertEqual(result["artifact_count"], 2)
        self.assertEqual(
            [artifact["name"] for artifact in result["artifacts"]],
            ["first.mp4", "second.mp4"],
        )

    def test_main_requires_artifact_access_before_downloading(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"ATTUNE_ARTIFACTS_DIR": directory}), \
                mock.patch.object(sys, "stdin", io.StringIO('{"url":"https://example.invalid/video"}')), \
                mock.patch.object(sys, "stderr", io.StringIO()), \
                mock.patch.object(self.action, "execute") as execute:
            self.assertEqual(self.action.main(), 1)
        execute.assert_not_called()

    def test_download_rejects_filename_paths_and_managed_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(self.action.ActionError, "directory components"):
                self.action.download({"url": "https://example.invalid/video", "filename_template": "../outside.%(ext)s"}, root, FakeYoutubeDL)
            for option in ("quiet", "postprocessors", "js_runtimes", "writesubtitles", "writethumbnail", "print_to_file"):
                with self.subTest(option=option), self.assertRaisesRegex(self.action.ActionError, "managed field"):
                    self.action.download({"url": "https://example.invalid/video", "yt_dlp_options": {option: True}}, root, FakeYoutubeDL)

    def test_action_rejects_non_http_urls(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(self.action.ActionError, "HTTP or HTTPS"):
            self.action.download({"url": "file:///etc/passwd"}, Path(directory), FakeYoutubeDL)


if __name__ == "__main__":
    unittest.main()
