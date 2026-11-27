"""Run one cloud scan, saving its notification outbox durably on a state branch."""
import base64
import json
import logging
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import watcher as w


class GitHubState:
    def __init__(self, repository, token, source_sha):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise w.MonitorError("Invalid repository name")
        if not token or not re.fullmatch(r"[a-f0-9]{40,64}", source_sha):
            raise w.MonitorError("GitHub workflow credentials are missing")
        self.base = "https://api.github.com/repos/" + repository
        self.token = token
        self.source_sha = source_sha
        self.sha = None
        self.last_content = None

    def api(self, path, method="GET", data=None, missing_ok=False):
        headers = {"User-Agent": "ContinentePokemonWatcher/1.0",
                   "Accept": "application/vnd.github+json",
                   "Authorization": "Bearer " + self.token,
                   "X-GitHub-Api-Version": "2026-03-10"}
        payload = None
        if data is not None:
            payload = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            raise w.MonitorError(f"GitHub state storage returned HTTP {exc.code}; keeping alerts unsent") from exc
        except (urllib.error.URLError, ValueError) as exc:
            raise w.MonitorError("GitHub state storage could not be reached; keeping alerts unsent") from exc

    def restore(self, baseline):
        result = self.api("/contents/.monitor-state.json?ref=monitor-state", missing_ok=True)
        if result is None:
            branch = self.api("/git/ref/heads/monitor-state", missing_ok=True)
            if branch is None:
                self.api("/git/refs", "POST", {"ref": "refs/heads/monitor-state", "sha": self.source_sha})
            state = json.loads(baseline.read_text(encoding="utf-8"))
        else:
            try:
                self.sha = result["sha"]
                content = base64.b64decode(result["content"]).decode()
                state = json.loads(content)
                self.last_content = content
            except (ValueError, KeyError, TypeError) as exc:
                raise w.MonitorError("Stored cloud state is invalid; refusing to reset product history") from exc
        if not isinstance(state, dict) or not isinstance(state.get("products"), dict) or not isinstance(state.get("pending"), list):
            raise w.MonitorError("Cloud product state has an unexpected format")
        return state

    def save(self, state):
        content = json.dumps(state, indent=2, ensure_ascii=False)
        if content == self.last_content:
            return
        data = {"message": "Save Pokemon monitor state", "branch": "monitor-state",
                "content": base64.b64encode(content.encode()).decode()}
        if self.sha is not None:
            data["sha"] = self.sha
        result = self.api("/contents/.monitor-state.json", "PUT", data)
        try:
            self.sha = result["content"]["sha"]
        except (KeyError, TypeError) as exc:
            raise w.MonitorError("GitHub did not acknowledge the saved state") from exc
        self.last_content = content


def run(remote, config, runtime, baseline):
    runtime.mkdir(parents=True, exist_ok=True)
    original_root = w.ROOT
    original_save = w.save_json
    w.ROOT = runtime
    state_path = runtime / "state.json"
    original_save(state_path, remote.restore(baseline))
    original_save(runtime / "config.json", config)
    config = w.load_config()

    def durable_save(path, value):
        # Persist remotely before a notification can be sent. If this fails,
        # check_once stops, so a fresh cloud VM cannot forget queued events.
        if path == state_path:
            remote.save(value)
        original_save(path, value)

    w.save_json = durable_save
    try:
        try:
            state = w.check_once(config)
            if state.get("cloud_consecutive_errors", 0):
                state["cloud_consecutive_errors"] = 0
                durable_save(state_path, state)
            w.LOG.info("Cloud check finished; state saved on monitor-state branch")
            return 0
        except Exception as exc:
            w.LOG.error("Cloud check failed: %s", w.error_summary(exc))
            state = w.load_json(state_path)
            state["cloud_consecutive_errors"] = state.get("cloud_consecutive_errors", 0) + 1
            state["cloud_last_error_at"] = w.now()
            if state["cloud_consecutive_errors"] >= 3 and time.time() - state.get("cloud_last_error_notice", 0) > 21600:
                try:
                    w.notify(config, "Cloud Pokemon watcher needs attention", "Repeated cloud checks are failing. Open the repository's Actions tab to inspect the failed run. The schedule will keep retrying.")
                    state["cloud_last_error_notice"] = time.time()
                except Exception:
                    pass
            try:
                durable_save(state_path, state)
            except Exception:
                w.LOG.error("Could not save cloud failure status; the workflow will report failure")
            return 1
    finally:
        w.save_json = original_save
        w.ROOT = original_root


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        root = Path(__file__).resolve().parent
        config = {"interval_seconds": 300, "request_delay_seconds": 1.5, "max_pages": 30,
                  "ntfy_server": "https://ntfy.sh", "ntfy_topic": os.environ.get("NTFY_TOPIC", ""),
                  "watch_urls": [w.SEED]}
        remote = GitHubState(os.environ.get("GITHUB_REPOSITORY", ""),
                             os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_SHA", ""))
        return run(remote, config, root / ".runtime", root / "baseline.json")
    except Exception as exc:
        w.LOG.error("Cloud setup failed: %s", w.error_summary(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
