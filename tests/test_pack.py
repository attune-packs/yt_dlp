from __future__ import annotations

import importlib.util
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
            self.options["progress_hooks"][0]({"status": "finished", "filename": "/tmp/artifacts/downloads/video.webm"})
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
        self.assertEqual(action["ref"], "yt_dlp.download")
        self.assertEqual(action["parameter_delivery"], "stdin")
        self.assertEqual(action["parameter_format"], "json")
        self.assertEqual(action["output_format"], "json")
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
        })
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"ATTUNE_ARTIFACTS_DIR": directory}), mock.patch.object(self.sensor.time, "monotonic", side_effect=[0, 10, 20]):
            instance = self.sensor.MediaAvailabilitySensor()
            instance.poll(rule)
            instance.poll(rule)
            FakeYoutubeDL.info["entries"][0]["live_status"] = "is_live"
            instance.poll(rule)
            checkpoints = list((Path(directory) / "yt_dlp_media_sensor").glob("*.json"))
        self.assertEqual([event[0]["live_status"] for event in instance.events], ["is_upcoming", "is_live"])
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
        self.action = load_module(
            "download_action_test",
            PACK_ROOT / "actions" / "download.py",
            {"yt_dlp": fake_yt_dlp()},
        )

    def test_download_builds_managed_options_and_returns_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.action.download({
                "url": "https://www.youtube.com/watch?v=abc123",
                "output_directory": "archives",
                "format": "best",
                "yt_dlp_options": {"merge_output_format": "mkv"},
            }, root, FakeYoutubeDL)
        options = FakeYoutubeDL.instances[0].options
        self.assertTrue(FakeYoutubeDL.instances[0].download)
        self.assertEqual(options["format"], "best")
        self.assertEqual(options["js_runtimes"], {"node": {"path": None}})
        self.assertEqual(options["merge_output_format"], "mkv")
        self.assertTrue(options["noplaylist"])
        self.assertEqual(result["media_count"], 1)
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(result["media"][0]["live_status"], "is_live")

    def test_download_rejects_path_escape_and_managed_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(self.action.ActionError, "below"):
                self.action.download({"url": "https://example.invalid/video", "output_directory": "../outside"}, root, FakeYoutubeDL)
            with self.assertRaisesRegex(self.action.ActionError, "managed field"):
                self.action.download({"url": "https://example.invalid/video", "yt_dlp_options": {"quiet": False}}, root, FakeYoutubeDL)
            with self.assertRaisesRegex(self.action.ActionError, "managed field"):
                self.action.download({"url": "https://example.invalid/video", "yt_dlp_options": {"postprocessors": [{"key": "Exec", "exec_cmd": "id"}]}}, root, FakeYoutubeDL)

    def test_action_rejects_non_http_urls(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(self.action.ActionError, "HTTP or HTTPS"):
            self.action.download({"url": "file:///etc/passwd"}, Path(directory), FakeYoutubeDL)


if __name__ == "__main__":
    unittest.main()
