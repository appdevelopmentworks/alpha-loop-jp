from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_loop.common import read_json
from alpha_loop.models import switch_model


class ModelSwitchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_switch_stops_other_before_starting_target(self):
        states = {"openjev": "running", "qwen": "exited"}
        actions = []
        def docker(action, name, *rest, **kwargs):
            actions.append((action, name))
            if action == "stop":
                states["openjev"] = "exited"
            if action == "start":
                states["qwen"] = "running"
            return name
        with (patch("alpha_loop.models._state", side_effect=lambda kind: states[kind]),
              patch("alpha_loop.models._docker", side_effect=docker),
              patch("alpha_loop.models._ready", return_value=True)):
            record = switch_model(self.root, "qwen")
        self.assertEqual(record["status"], "SUCCEEDED")
        self.assertEqual(actions, [("stop", "alpha-loop-openjev"), ("start", "alpha-loop-qwen")])
        self.assertEqual(record["after"], {"openjev": "exited", "qwen": "running"})

    def test_both_running_is_logged_without_stopping_either(self):
        with (patch("alpha_loop.models._state", return_value="running"),
              patch("alpha_loop.models._docker") as docker):
            with self.assertRaisesRegex(RuntimeError, "both project GPU containers"):
                switch_model(self.root, "qwen")
        docker.assert_not_called()
        records = list((self.root / "data" / "operations" / "model_switches").glob("*.json"))
        self.assertEqual(read_json(records[0])["status"], "FAILED")

    def test_new_target_is_stopped_after_readiness_timeout(self):
        states = {"openjev": "exited", "qwen": "exited"}
        actions = []
        def docker(action, name, *rest, **kwargs):
            actions.append((action, name))
            states["qwen"] = "running" if action == "start" else "exited"
            return name
        with (patch("alpha_loop.models._state", side_effect=lambda kind: states[kind]),
              patch("alpha_loop.models._docker", side_effect=docker),
              patch("alpha_loop.models._ready", return_value=False),
              patch("alpha_loop.models.time.sleep")):
            with self.assertRaisesRegex(TimeoutError, "did not become ready"):
                switch_model(self.root, "qwen", .01)
        self.assertEqual(actions, [("start", "alpha-loop-qwen"), ("stop", "alpha-loop-qwen")])
