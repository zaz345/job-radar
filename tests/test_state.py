"""State + config tests.

The state file exists because GitHub Actions gives every tick a fresh container: without
it the same job is pushed on every run and the heartbeat fires 48 times a day.

Run: python -m unittest tests.test_state
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import run
from core.config import load_env


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_is_empty(self):
        self.assertEqual(run._State(self.path).seen(), set())

    def test_remember_survives_a_cold_runner(self):
        run._State(self.path).remember(["digest-a", "digest-b"])
        # a brand-new process reading the same file — this is what the next CI tick does
        self.assertEqual(run._State(self.path).seen(), {"digest-a", "digest-b"})

    def test_remember_deduplicates(self):
        s = run._State(self.path)
        s.remember(["a"])
        s.remember(["a", "b"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["pushed"], ["a", "b"])

    def test_list_is_capped(self):
        s = run._State(self.path)
        s.data["pushed"] = [f"x{i}" for i in range(1000)]
        s.flush()
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))["pushed"]), 800)

    def test_cool_true_when_never_marked(self):
        self.assertTrue(run._State(self.path).cool("heartbeat", 24))

    def test_cool_false_right_after_mark(self):
        run._State(self.path).mark("heartbeat")
        self.assertFalse(run._State(self.path).cool("heartbeat", 24))

    def test_no_path_means_no_persistence_but_no_crash(self):
        s = run._State(None)
        self.assertEqual(s.seen(), set())
        s.remember(["a"])
        self.assertEqual(s.seen(), set())

    def test_corrupt_file_does_not_crash(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(run._State(self.path).seen(), set())

    def test_json_array_instead_of_object_does_not_crash(self):
        self.path.write_text("[1,2,3]", encoding="utf-8")
        self.assertEqual(run._State(self.path).seen(), set())

    def test_keepalive_rewrites_a_stale_timestamp(self):
        # GitHub disables scheduled workflows after 60 days without a commit
        self.path.write_text(
            json.dumps({"pushed": [], "updated_at": "2020-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        run._State(self.path).keepalive(20)
        self.assertNotEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["updated_at"],
            "2020-01-01T00:00:00+00:00",
        )

    def test_keepalive_leaves_fresh_file_alone(self):
        s = run._State(self.path)
        s.flush()
        stamp = json.loads(self.path.read_text(encoding="utf-8"))["updated_at"]
        run._State(self.path).keepalive(20)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["updated_at"], stamp)


class EnvOverrideTest(unittest.TestCase):
    """CI has no .env: secrets arrive as environment variables and must win."""

    def test_env_var_beats_file(self):
        os.environ["NOTIFY_MIN_SCORE"] = "77"
        try:
            self.assertEqual(load_env()["NOTIFY_MIN_SCORE"], "77")
        finally:
            del os.environ["NOTIFY_MIN_SCORE"]

    def test_unrelated_env_var_does_not_leak_into_config(self):
        os.environ["SOME_RANDOM_THING"] = "x"
        try:
            self.assertNotIn("SOME_RANDOM_THING", load_env())
        finally:
            del os.environ["SOME_RANDOM_THING"]

    def test_empty_env_var_does_not_clear_a_file_value(self):
        # an unset-but-present variable in CI must never blank out working config
        before = load_env().get("UPWORK_IMAP_USER")
        self.assertTrue(before, "test needs a real UPWORK_IMAP_USER in .env")
        os.environ["UPWORK_IMAP_USER"] = ""
        try:
            self.assertEqual(load_env().get("UPWORK_IMAP_USER"), before)
        finally:
            del os.environ["UPWORK_IMAP_USER"]


if __name__ == "__main__":
    unittest.main()
