"""Message lifecycle and transport checks with isolated state and fake endpoints."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import bridge


class BridgeFixture(unittest.TestCase):
    def setUp(self):
        # Keep the socket path below macOS's Unix socket path-length limit.
        temporary = tempfile.TemporaryDirectory(prefix="bridge-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        executable = self.root / "fake-codex"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
        self.config = {
            "codex_thread_id": "test-codex", "claude_session_id": "test-claude",
            "codex_cli": str(executable), "repository": str(self.root),
        }
        environment = patch.dict(os.environ, {
            "CODEX_THREAD_ID": "test-codex", "CLAUDE_CODE_SESSION_ID": "test-claude",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        (self.root / "endpoints.json").write_text(json.dumps(self.config), encoding="utf-8")
        bridge.initialize(self.root, self.config, "codex")

    def create(self, recipient="claude", body="test message", reply_to=None):
        return bridge.create(self.root, self.config, recipient, "test-task", body, reply_to)

    def messages(self):
        return json.loads((self.root / "ledger.json").read_text())["messages"]


class MessageTests(BridgeFixture):
    def test_body_limit_counts_utf8_bytes(self):
        message_id = self.create(body="é" * (bridge.MAX_BODY // 2))
        for body in ("", " \n\t", "é" * (bridge.MAX_BODY // 2 + 1)):
            with self.subTest(body_size=len(body)), self.assertRaisesRegex(ValueError, "Body must"):
                self.create(body=body)
        self.assertEqual(set(self.messages()), {message_id})

    def test_invalid_task_does_not_create_history(self):
        for task in ("", "x" * 121, "bad\ntask", "bad\x00task"):
            with self.subTest(task=task), self.assertRaisesRegex(ValueError, "Task ID"):
                bridge.create(self.root, self.config, "claude", task, "body")
        self.assertEqual(self.messages(), {})

    def test_reply_requires_claim(self):
        parent_id = self.create()
        with self.assertRaisesRegex(ValueError, "Claim the incoming message"):
            self.create(recipient="codex", reply_to=parent_id)
        self.assertEqual(set(self.messages()), {parent_id})

    def test_tampered_body_cannot_be_claimed(self):
        message_id = self.create()
        with bridge.ledger(self.root) as state:
            state["messages"][message_id]["body"] = "tampered body"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            bridge.claim(self.root, self.config, message_id, "claude")
        self.assertIsNone(self.messages()[message_id]["claimed_at"])

    def test_other_agent_cannot_claim(self):
        message_id = self.create()
        with self.assertRaisesRegex(ValueError, "addressed to the other agent"):
            bridge.claim(self.root, self.config, message_id, "codex")
        self.assertIsNone(self.messages()[message_id]["claimed_at"])

    def test_uncertain_delivery_retries_the_original_message(self):
        message_id = self.create()

        def failing_transport(*args):
            raise TimeoutError("fake transport timed out")

        with self.assertRaisesRegex(RuntimeError, "Transport timed out"):
            bridge.deliver(self.root, self.config, message_id, transport=failing_transport)
        attempt = self.messages()[message_id]["attempts"][0]
        self.assertEqual(attempt["status"], "delivery_unknown")
        self.assertIn("finished_at", attempt)
        submitted = []

        def successful_transport(message, config, root):
            submitted.append(message["id"])
            return {"transport": "fake", "status": "written_unacknowledged"}

        self.assertFalse(bridge.deliver(self.root, self.config, message_id, transport=successful_transport)["sent"])
        self.assertTrue(bridge.deliver(self.root, self.config, message_id, retry=True, transport=successful_transport)["sent"])
        self.assertEqual(submitted, [message_id])
        messages = self.messages()
        self.assertEqual(set(messages), {message_id})
        self.assertEqual(len(messages[message_id]["attempts"]), 2)

    def test_unresolved_inflight_attempt_blocks_retry(self):
        message_id = self.create()
        with bridge.ledger(self.root) as state:
            state["messages"][message_id]["attempts"].append({"status": "in_flight"})
        transport = unittest.mock.Mock()
        with self.assertRaisesRegex(ValueError, "in flight"):
            bridge.deliver(self.root, self.config, message_id, retry=True, transport=transport)
        transport.assert_not_called()
        self.assertEqual(len(self.messages()[message_id]["attempts"]), 1)

    def test_failed_atomic_save_preserves_prior_history(self):
        self.create()
        before = (self.root / "ledger.json").read_bytes()
        with patch.object(bridge.os, "replace", side_effect=OSError("fake disk failure")):
            with self.assertRaises(OSError):
                self.create(body="unsaved message")
        self.assertEqual((self.root / "ledger.json").read_bytes(), before)
        self.assertEqual(list(self.root.glob(".bridge-*")), [])

    def test_concurrent_processes_claim_only_once(self):
        message_id = self.create()
        command = [sys.executable, "-B", str(bridge.HELPER_PATH), "--state-dir", str(self.root),
                   "claim", message_id, "--as", "claude"]
        processes = []
        try:
            # Hold the real lock while both child processes begin their claims.
            with bridge.ledger(self.root):
                for _ in range(2):
                    processes.append(subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            outcomes = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                outcomes.append(json.loads(stdout))
            self.assertEqual(sorted(outcome["duplicate"] for outcome in outcomes), [False, True])
            self.assertEqual(sum(outcome["body"] == "test message" for outcome in outcomes), 1)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()


class SocketFixture(BridgeFixture):
    def setUp(self):
        super().setUp()
        self.endpoint = self.root / "inbox.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.server.close)
        self.server.bind(str(self.endpoint))
        self.endpoint.chmod(0o600)
        self.server.listen(1)
        self.server.settimeout(2)
        info = self.endpoint.stat()
        self.registry = self.root / "registry.json"
        self.record = {
            "sessionId": "test-claude", "pid": os.getpid(), "procStart": "fake-process-start",
            "messagingSocketPath": str(self.endpoint), "status": "idle", "version": "test",
        }
        self.registry.write_text(json.dumps(self.record), encoding="utf-8")
        self.config.update({
            "claude_registry_file": str(self.registry), "claude_pid": self.record["pid"],
            "claude_process_start": self.record["procStart"], "claude_socket": str(self.endpoint),
            "claude_socket_identity": [info.st_dev, info.st_ino],
        })


class TransportTests(SocketFixture):
    def test_fake_socket_receives_frame_with_shared_claim_directory(self):
        message_id = self.create(body="Unicode reply: café")
        outcome = bridge.deliver(self.root, self.config, message_id, transport=bridge.submit)
        self.assertEqual(outcome["status"], "written_unacknowledged")
        connection, _ = self.server.accept()
        with connection:
            connection.settimeout(2)
            chunks = []
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        frame = json.loads(b"".join(chunks).decode("utf-8"))
        self.assertEqual(frame["uuid"], message_id)
        self.assertEqual(frame["msg_id"], message_id)
        self.assertEqual(frame["session_id"], "test-claude")
        self.assertIn(f"State directory: {self.root}", frame["message"]["content"])
        self.assertNotIn("Unicode reply: café", frame["message"]["content"])
        receipt = bridge.claim(self.root, self.config, message_id, "claude")
        self.assertFalse(receipt["duplicate"])
        self.assertEqual(receipt["body"], "Unicode reply: café")

    def test_changed_registry_pins_reject_delivery(self):
        for field in ("sessionId", "pid", "procStart", "messagingSocketPath"):
            with self.subTest(field=field):
                record = dict(self.record, **{field: "changed"})
                self.registry.write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "registry changed"):
                    bridge.check_claude(self.config)

    def test_nonprivate_socket_is_rejected(self):
        self.endpoint.chmod(0o666)
        with self.assertRaisesRegex(ValueError, "no longer private"):
            bridge.check_claude(self.config)

    def test_changed_socket_identity_is_rejected(self):
        self.config["claude_socket_identity"][1] += 1
        with self.assertRaisesRegex(ValueError, "socket was replaced"):
            bridge.check_claude(self.config)

    def test_symlink_socket_is_rejected(self):
        alias = self.root / "alias.sock"
        alias.symlink_to(self.endpoint)
        self.config["claude_socket"] = str(alias)
        self.record["messagingSocketPath"] = str(alias)
        self.registry.write_text(json.dumps(self.record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must be a socket"):
            bridge.check_claude(self.config)

    def test_fake_queue_uses_pinned_thread_and_repository(self):
        message_id = self.create(recipient="codex")
        result = subprocess.CompletedProcess([], 0, stdout="queued", stderr="")
        with patch.object(bridge.subprocess, "run", return_value=result) as queue:
            outcome = bridge.deliver(self.root, self.config, message_id, transport=bridge.submit)
        self.assertEqual(outcome["status"], "queued_unacknowledged")
        arguments = queue.call_args.args[0]
        self.assertEqual(arguments[:5], [self.config["codex_cli"], "queue", "--thread", "test-codex", "--message"])
        self.assertIn(f"State directory: {self.root}", arguments[5])
        self.assertEqual(queue.call_args.kwargs["cwd"], str(self.root))
        self.assertFalse(queue.call_args.kwargs.get("shell", False))

    def test_queue_failure_records_uncertain_attempt(self):
        message_id = self.create(recipient="codex")
        error = subprocess.CalledProcessError(7, ["fake-codex"], stderr="fake queue failure")
        with patch.object(bridge.subprocess, "run", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "Transport exited with status 7"):
                bridge.deliver(self.root, self.config, message_id, transport=bridge.submit)
        self.assertEqual(self.messages()[message_id]["attempts"][0]["status"], "delivery_unknown")

    def test_queue_timeout_records_uncertain_attempt(self):
        message_id = self.create(recipient="codex")
        with patch.object(bridge.subprocess, "run", side_effect=subprocess.TimeoutExpired("fake-codex", 30)):
            with self.assertRaisesRegex(RuntimeError, "Transport timed out"):
                bridge.deliver(self.root, self.config, message_id, transport=bridge.submit)
        self.assertEqual(self.messages()[message_id]["attempts"][0]["status"], "delivery_unknown")


if __name__ == "__main__":
    unittest.main()
