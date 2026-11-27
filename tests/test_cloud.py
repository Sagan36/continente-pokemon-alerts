import base64
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_run as c
import watcher as w


def product():
    return {"id": "123", "name": "Pokemon - Cartas TCG Booster", "price": 5.99,
            "url": "https://www.continente.pt/produto/pokemon-123.html", "in_stock": False}


def config():
    return {"interval_seconds": 300, "request_delay_seconds": 0,
            "ntfy_server": "https://ntfy.sh", "ntfy_topic": "test-topic-0123456789"}


class MemoryRemote:
    def __init__(self):
        self.state = {"initialized": True, "products": {}, "pending": []}
        self.commits = []

    def restore(self, baseline):
        return copy.deepcopy(self.state)

    def save(self, state):
        self.state = copy.deepcopy(state)
        self.commits.append(copy.deepcopy(state))


class CloudTests(unittest.TestCase):
    def test_manual_cloud_test_sends_after_success(self):
        env = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "test-token",
               "GITHUB_SHA": "a" * 40, "NTFY_TOPIC": "test-topic-0123456789", "SEND_TEST_ALERT": "true"}
        with patch.dict(os.environ, env, clear=True), patch.object(c, "run", return_value=0), \
             patch.object(w, "notify", return_value="accepted") as send:
            self.assertEqual(c.main(), 0)
            send.assert_called_once()

    def test_scheduled_checks_do_not_send_test_alerts(self):
        env = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "test-token",
               "GITHUB_SHA": "a" * 40, "NTFY_TOPIC": "test-topic-0123456789"}
        with patch.dict(os.environ, env, clear=True), patch.object(c, "run", return_value=0), \
             patch.object(w, "notify") as send:
            self.assertEqual(c.main(), 0)
            send.assert_not_called()

    def test_cloud_outbox_is_committed_before_sending(self):
        remote = MemoryRemote()
        def delivered(*args, **kwargs):
            self.assertEqual(len(remote.state["pending"]), 1)
            return "accepted"
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(w, "scan", return_value=({"123": product()}, 1)), \
             patch.object(w, "notify", side_effect=delivered) as send, \
             patch.object(w.time, "sleep"):
            root = Path(directory)
            self.assertEqual(c.run(remote, config(), root / "runtime", root / "baseline.json"), 0)
            send.assert_called_once()
        self.assertEqual(remote.state["pending"], [])

    def test_fresh_runner_restores_state_and_does_not_repeat(self):
        remote = MemoryRemote()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(w, "scan", return_value=({"123": product()}, 1)), \
             patch.object(w, "notify", return_value="accepted") as send, \
             patch.object(w.time, "sleep"):
            root = Path(directory)
            c.run(remote, config(), root / "first-vm", root / "baseline.json")
            c.run(remote, config(), root / "second-vm", root / "baseline.json")
            send.assert_called_once()

    def test_storage_failure_prevents_product_notification(self):
        remote = MemoryRemote()
        remote.save = Mock(side_effect=w.MonitorError("storage unavailable"))
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(w, "scan", return_value=({"123": product()}, 1)), \
             patch.object(w, "notify") as send:
            root = Path(directory)
            self.assertEqual(c.run(remote, config(), root / "runtime", root / "baseline.json"), 1)
            send.assert_not_called()

    def test_error_count_survives_separate_runs(self):
        remote = MemoryRemote()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(w, "scan", side_effect=w.MonitorError("shop unavailable")), \
             patch.object(w, "notify", return_value="health-notice") as send:
            root = Path(directory)
            for index in range(3):
                self.assertEqual(c.run(remote, config(), root / str(index), root / "baseline.json"), 1)
            send.assert_called_once()
        self.assertEqual(remote.state["cloud_consecutive_errors"], 3)

    def test_github_content_is_encoded_and_saved_to_state_branch(self):
        remote = c.GitHubState("owner/repo", "test-token", "a" * 40)
        remote.sha = "old-file-sha"
        remote.api = Mock(return_value={"content": {"sha": "new-file-sha"}})
        state = {"products": {}, "pending": []}
        remote.save(state)
        args = remote.api.call_args.args
        self.assertEqual(args[:2], ("/contents/.monitor-state.json", "PUT"))
        self.assertEqual(args[2]["branch"], "monitor-state")
        self.assertEqual(args[2]["sha"], "old-file-sha")
        self.assertEqual(json.loads(base64.b64decode(args[2]["content"])), state)
        self.assertNotIn("test-token", json.dumps(args[2]))
        self.assertEqual(remote.sha, "new-file-sha")
        remote.save(state)
        self.assertEqual(remote.api.call_count, 1)

    def test_existing_github_state_is_restored(self):
        state = {"initialized": True, "products": {}, "pending": []}
        remote = c.GitHubState("owner/repo", "token", "a" * 40)
        remote.api = Mock(return_value={"sha": "state-sha", "content": base64.b64encode(json.dumps(state).encode()).decode()})
        self.assertEqual(remote.restore(Path("unused")), state)
        self.assertEqual(remote.sha, "state-sha")

    def test_first_run_creates_branch_from_source_and_uses_baseline(self):
        state = {"initialized": True, "products": {"123": product()}, "pending": []}
        remote = c.GitHubState("owner/repo", "token", "a" * 40)
        remote.api = Mock(side_effect=[None, None, {"ref": "refs/heads/monitor-state"}])
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "baseline.json"
            baseline.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(remote.restore(baseline), state)
        self.assertEqual(remote.api.call_args.args, ("/git/refs", "POST", {"ref": "refs/heads/monitor-state", "sha": "a" * 40}))

    def test_corrupt_remote_state_is_never_silently_reset(self):
        remote = c.GitHubState("owner/repo", "token", "a" * 40)
        remote.api = Mock(return_value={"sha": "state-sha", "content": base64.b64encode(b'{broken').decode()})
        with self.assertRaises(w.MonitorError):
            remote.restore(Path("unused"))


if __name__ == "__main__":
    unittest.main()
