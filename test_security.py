"""Regression tests for session binding, confidentiality, integrity, and recovery."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
from unittest.mock import Mock, patch

import bridge
from test_transports import BridgeFixture, SocketFixture


class SecurityTests(BridgeFixture):
    def run_cli(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = bridge.main(["--state-dir", str(self.root), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def legacy_history(self):
        state = json.loads((self.root / "ledger.json").read_text())
        state.pop("schema_version")
        state.pop("session_pair")
        for message in state["messages"].values():
            for key in ("session_pair", "envelope_sha256", "completed_at"):
                message.pop(key, None)
        (self.root / "ledger.json").write_text(json.dumps(state), encoding="utf-8")
        return copy.deepcopy(state)

    def migrate(self, **overrides):
        arguments = {"actor": "codex", "expected_codex": "test-codex", "expected_claude": "test-claude"}
        arguments.update(overrides)
        return bridge.migrate(self.root, self.config, **arguments)

    def inflight(self, message_id):
        attempt_id = str(uuid.uuid4())
        with bridge.ledger(self.root) as state:
            state["messages"][message_id]["attempts"].append({
                "id": attempt_id, "started_at": bridge.now(), "status": "in_flight",
            })
        return attempt_id

    def test_endpoint_changes_cannot_rebind_history(self):
        message_id = self.create()
        original = (self.root / "ledger.json").read_bytes()
        transport = Mock()
        changed = dict(self.config, codex_thread_id="different-codex", claude_session_id="different-claude")
        operations = (
            lambda: bridge.create(self.root, changed, "claude", "task", "new message"),
            lambda: bridge.deliver(self.root, changed, message_id, transport=transport),
            lambda: bridge.claim(self.root, changed, message_id, "claude"),
            lambda: bridge.status(self.root, changed),
        )
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "different-codex", "CLAUDE_CODE_SESSION_ID": "different-claude"}):
            for operation in operations:
                with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "different session pair"):
                    operation()
        transport.assert_not_called()
        self.assertEqual((self.root / "ledger.json").read_bytes(), original)

    def test_message_with_another_pair_is_rejected_even_with_a_valid_digest(self):
        message_id = self.create()
        with bridge.ledger(self.root) as state:
            message = state["messages"][message_id]
            message["session_pair"] = dict(message["session_pair"], claude_session_id="other-claude")
            message["envelope_sha256"] = bridge.envelope_digest(message)
        transport = Mock()
        with self.assertRaisesRegex(ValueError, "different session pair"):
            bridge.deliver(self.root, self.config, message_id, transport=transport)
        with self.assertRaisesRegex(ValueError, "different session pair"):
            bridge.claim(self.root, self.config, message_id, "claude")
        transport.assert_not_called()

    def test_legacy_history_is_never_bound_implicitly(self):
        message_id = self.create()
        self.legacy_history()
        original = (self.root / "ledger.json").read_bytes()
        for operation in (
            lambda: self.create(), lambda: bridge.status(self.root),
            lambda: bridge.claim(self.root, self.config, message_id, "claude"),
            lambda: bridge.deliver(self.root, self.config, message_id, transport=Mock()),
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "explicitly migrate"):
                operation()
        self.assertEqual((self.root / "ledger.json").read_bytes(), original)

    def test_migration_requires_matching_original_ids(self):
        self.create()
        self.legacy_history()
        original = (self.root / "ledger.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "original session IDs"):
            self.migrate(expected_claude="wrong-original-session")
        self.assertEqual((self.root / "ledger.json").read_bytes(), original)

    def test_migration_preserves_claims_attempts_and_reply_history(self):
        parent_id = self.create()
        bridge.claim(self.root, self.config, parent_id, "claude")
        reply_id = self.create(recipient="codex", reply_to=parent_id)
        bridge.deliver(self.root, self.config, reply_id,
                       transport=lambda *args: {"transport": "fake", "status": "written_unacknowledged"})
        old = self.legacy_history()
        code, stdout, stderr = self.run_cli([
            "migrate", "--as", "codex", "--codex-thread-id", "test-codex", "--claude-session-id", "test-claude",
        ])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["messages"], 2)
        upgraded = self.messages()
        for message_id, message in old["messages"].items():
            for key, value in message.items():
                self.assertEqual(upgraded[message_id][key], value)
            bridge.validate_message(upgraded[message_id], self.config, message_id)
        self.assertTrue(bridge.claim(self.root, self.config, parent_id, "claude")["duplicate"])
        self.assertEqual(bridge.read_message(self.root, self.config, parent_id, "claude")["body"], "test message")
        self.assertEqual(self.migrate()["status"], "already_bound")

    def test_migration_failure_is_atomic(self):
        self.create()
        second_id = self.create(body="second body")
        self.legacy_history()
        with bridge.ledger(self.root) as state:
            state["messages"][second_id]["body"] = "corrupted body"
        original = (self.root / "ledger.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.migrate()
        self.assertEqual((self.root / "ledger.json").read_bytes(), original)

    def test_partial_bindings_are_not_overwritten_by_migration(self):
        message_id = self.create()
        self.legacy_history()
        with bridge.ledger(self.root) as state:
            state["messages"][message_id]["session_pair"] = {"claude_session_id": "another-session"}
        with self.assertRaisesRegex(ValueError, "Partially bound"):
            self.migrate()

    def test_corrupt_body_never_reaches_a_transport(self):
        message_id = self.create()
        with bridge.ledger(self.root) as state:
            state["messages"][message_id]["body"] = "changed without rehashing"
        transport = Mock()
        with self.assertRaisesRegex(ValueError, "body hash mismatch"):
            bridge.deliver(self.root, self.config, message_id, transport=transport)
        transport.assert_not_called()
        self.assertEqual(self.messages()[message_id]["attempts"], [])
        with patch.object(bridge.subprocess, "run") as queue:
            with self.assertRaisesRegex(ValueError, "body hash mismatch"):
                bridge.submit(self.messages()[message_id], self.config, self.root)
            queue.assert_not_called()

    def test_immutable_envelope_changes_are_rejected(self):
        message_id = self.create()
        baseline = self.messages()[message_id]
        for changes in ({"task": "different task"}, {"created_at": "different time"},
                        {"reply_to": str(uuid.uuid4())}, {"sender": "claude", "recipient": "codex"}):
            with self.subTest(changes=changes):
                with bridge.ledger(self.root) as state:
                    state["messages"][message_id] = dict(baseline, **changes)
                transport = Mock()
                with self.assertRaisesRegex(ValueError, "envelope hash mismatch"):
                    bridge.deliver(self.root, self.config, message_id, transport=transport)
                transport.assert_not_called()

    def test_real_fake_cli_receives_only_a_notification_and_output_is_discarded(self):
        body = "PRIVATE_BODY_café_42"
        task = "PRIVATE_TASK_42"
        executable = self.root / "fake-codex"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\nfrom pathlib import Path\n"
            "Path('observed-arguments.json').write_text(json.dumps(sys.argv[1:]))\n"
            f"print({body!r})\nprint({body!r}, file=sys.stderr)\n", encoding="utf-8")
        executable.chmod(0o700)
        self.config["codex_cli"] = str(executable)
        message_id = bridge.create(self.root, self.config, "codex", task, body)
        result = bridge.deliver(self.root, self.config, message_id, transport=bridge.submit)
        arguments = json.loads((self.root / "observed-arguments.json").read_text())
        self.assertEqual(arguments[:4], ["queue", "--thread", "test-codex", "--message"])
        self.assertNotIn(body, str(arguments))
        self.assertNotIn(task, str(arguments))
        self.assertIn(message_id, arguments[4])
        self.assertNotIn(body, json.dumps(result))
        self.assertNotIn(body, json.dumps(self.messages()[message_id]["attempts"]))
        receipt = bridge.claim(self.root, self.config, message_id, "codex")
        self.assertEqual((receipt["body"], receipt["task"]), (body, task))

    def test_transport_exception_details_never_reach_cli_or_status(self):
        body = "PRIVATE_BODY_IN_EXCEPTION"
        body_file = self.root / "body.md"
        body_file.write_text(body)
        failures = (
            subprocess.TimeoutExpired(["fake-codex", "--message", body], 30, output=body, stderr=body),
            subprocess.CalledProcessError(7, ["fake-codex", body], output=body, stderr=body),
            RuntimeError(body),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch.object(bridge.subprocess, "run", side_effect=failure):
                code, stdout, stderr = self.run_cli([
                    "send", "--to", "codex", "--task", "task", "--body-file", str(body_file),
                ])
                self.assertEqual(code, 1)
                self.assertNotIn(body, stdout + stderr)
                self.assertIn("delivery status is unknown", json.loads(stderr)["error"])
        for message in self.messages().values():
            self.assertNotIn(body, json.dumps(message["attempts"]))
        self.assertNotIn(body, json.dumps(bridge.status(self.root)))

    def test_status_hides_sensitive_diagnostics_already_in_history(self):
        message_id = self.create()
        marker = "SENSITIVE_OLD_QUEUE_OUTPUT"
        with bridge.ledger(self.root) as state:
            state["messages"][message_id]["attempts"].append({
                "id": str(uuid.uuid4()), "status": "delivery_unknown", "error": marker, "queue_output": marker,
            })
        self.assertNotIn(marker, json.dumps(bridge.status(self.root)))

    def test_interruption_is_recorded_without_leaking_exception_arguments(self):
        message_id = self.create()
        with self.assertRaises(KeyboardInterrupt):
            bridge.deliver(self.root, self.config, message_id,
                           transport=Mock(side_effect=KeyboardInterrupt("PRIVATE_EXCEPTION")))
        attempt = self.messages()[message_id]["attempts"][0]
        self.assertEqual(attempt["status"], "delivery_unknown")
        self.assertEqual(attempt["error_code"], "interrupted")
        self.assertNotIn("PRIVATE_EXCEPTION", json.dumps(attempt))
        self.assertTrue(bridge.deliver(self.root, self.config, message_id, retry=True,
                                      transport=lambda *args: {"status": "written_unacknowledged"})["sent"])

    def test_system_exit_does_not_render_sensitive_transport_details(self):
        message_id = self.create()
        with self.assertRaises(SystemExit) as caught:
            bridge.deliver(self.root, self.config, message_id,
                           transport=Mock(side_effect=SystemExit("PRIVATE_EXCEPTION")))
        self.assertEqual(caught.exception.code, 1)
        self.assertNotIn("PRIVATE_EXCEPTION", json.dumps(self.messages()[message_id]["attempts"]))

    def test_hard_crash_can_be_recovered_without_losing_duplicate_detection(self):
        message_id = self.create()
        script = """import os, sys
from pathlib import Path
import bridge
root = Path(sys.argv[1])
def crash(*args):
    os._exit(31)
bridge.deliver(root, bridge.load_config(root), sys.argv[2], transport=crash)
"""
        process = subprocess.run([sys.executable, "-B", "-c", script, str(self.root), message_id],
                                 cwd=bridge.HELPER_PATH.parent, capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 31, process.stderr)
        attempt = self.messages()[message_id]["attempts"][0]
        self.assertEqual(attempt["status"], "in_flight")
        code, stdout, stderr = self.run_cli([
            "recover", message_id, "--as", "codex", "--attempt-id", attempt["id"],
            "--reason", "The isolated sender process has exited",
        ])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "delivery_unknown")
        sent = Mock(return_value={"status": "written_unacknowledged"})
        self.assertTrue(bridge.deliver(self.root, self.config, message_id, retry=True, transport=sent)["sent"])
        self.assertFalse(bridge.claim(self.root, self.config, message_id, "claude")["duplicate"])
        self.assertTrue(bridge.claim(self.root, self.config, message_id, "claude")["duplicate"])
        self.assertFalse(bridge.deliver(self.root, self.config, message_id, retry=True, transport=sent)["sent"])
        sent.assert_called_once()
        self.assertEqual(self.messages()[message_id]["recoveries"][0]["attempt_id"], attempt["id"])

    def test_recovery_refuses_a_live_sender_in_another_process(self):
        message_id = self.create()
        attempt_id = self.inflight(message_id)
        script = """import sys, time
from pathlib import Path
import bridge
root = Path(sys.argv[1])
with bridge.delivery_guard(root, sys.argv[2]):
    (root / 'sender-ready').touch()
    deadline = time.monotonic() + 15
    while not (root / 'sender-stop').exists() and time.monotonic() < deadline:
        time.sleep(0.01)
"""
        process = subprocess.Popen([sys.executable, "-B", "-c", script, str(self.root), message_id],
                                   cwd=bridge.HELPER_PATH.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / "sender-ready").exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / "sender-ready").exists())
            before = (self.root / "ledger.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "sender is still active"):
                bridge.recover(self.root, self.config, message_id, "codex", attempt_id, "Attempted early recovery")
            transport = Mock()
            with self.assertRaisesRegex(ValueError, "sender is still active"):
                bridge.deliver(self.root, self.config, message_id, retry=True, transport=transport)
            transport.assert_not_called()
            self.assertEqual((self.root / "ledger.json").read_bytes(), before)
        finally:
            (self.root / "sender-stop").touch()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

    def test_recovery_requires_the_sender_and_the_exact_inflight_attempt(self):
        message_id = self.create()
        attempt_id = self.inflight(message_id)
        with self.assertRaisesRegex(ValueError, "original sender"):
            bridge.recover(self.root, self.config, message_id, "claude", attempt_id, "Wrong actor")
        with self.assertRaisesRegex(ValueError, "not in flight"):
            bridge.recover(self.root, self.config, message_id, "codex", str(uuid.uuid4()), "Stale attempt ID")
        bridge.claim(self.root, self.config, message_id, "claude")
        bridge.recover(self.root, self.config, message_id, "codex", attempt_id, "Sender stopped after receipt")
        self.assertFalse(bridge.deliver(self.root, self.config, message_id, retry=True, transport=Mock())["sent"])
        with self.assertRaisesRegex(ValueError, "not in flight"):
            bridge.recover(self.root, self.config, message_id, "codex", attempt_id, "Already recovered")

    def test_lost_claim_output_can_be_read_and_completed_without_resetting_the_claim(self):
        message_id = self.create(body="recover this receipt")

        class BrokenOutput(io.StringIO):
            def write(self, text):
                raise BrokenPipeError("isolated output failure")

        with redirect_stdout(BrokenOutput()), redirect_stderr(io.StringIO()):
            self.assertEqual(bridge.main(["--state-dir", str(self.root), "claim", message_id, "--as", "claude"]), 1)
        claimed_at = self.messages()[message_id]["claimed_at"]
        self.assertTrue(bridge.claim(self.root, self.config, message_id, "claude")["duplicate"])
        before = (self.root / "ledger.json").read_bytes()
        code, stdout, stderr = self.run_cli(["read", message_id, "--as", "claude"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["body"], "recover this receipt")
        self.assertIsNone(json.loads(stdout)["completed_at"])
        self.assertEqual((self.root / "ledger.json").read_bytes(), before)
        code, stdout, stderr = self.run_cli(["complete", message_id, "--as", "claude"])
        self.assertEqual((code, stderr), (0, ""))
        completion = json.loads(stdout)
        self.assertTrue(completion["completed_at"])
        repeated = bridge.complete(self.root, self.config, message_id, "claude")
        self.assertTrue(repeated["already_completed"])
        self.assertEqual(repeated["completed_at"], completion["completed_at"])
        self.assertEqual(self.messages()[message_id]["claimed_at"], claimed_at)
        self.assertTrue(bridge.claim(self.root, self.config, message_id, "claude")["duplicate"])

    def test_read_and_complete_require_the_claimed_recipient(self):
        message_id = self.create()
        for action in (bridge.read_message, bridge.complete):
            with self.subTest(action=action), self.assertRaises(ValueError):
                action(self.root, self.config, message_id, "claude")
        bridge.claim(self.root, self.config, message_id, "claude")
        for action in (bridge.read_message, bridge.complete):
            with self.subTest(action=action), self.assertRaises(ValueError):
                action(self.root, self.config, message_id, "codex")


class PeerSecurityTests(SocketFixture):
    def test_replaced_socket_receives_no_payload(self):
        message_id = self.create()
        original_frame = bridge.frame
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as replacement:
            replacement.settimeout(2)

            def replace_socket(message, config, root):
                self.endpoint.unlink()
                replacement.bind(str(self.endpoint))
                self.endpoint.chmod(0o600)
                replacement.listen(1)
                return original_frame(message, config, root)

            with patch.object(bridge, "frame", side_effect=replace_socket):
                with self.assertRaisesRegex(RuntimeError, "Transport failed"):
                    bridge.deliver(self.root, self.config, message_id, transport=bridge.submit)
            connection, _ = replacement.accept()
            with connection:
                connection.settimeout(2)
                self.assertEqual(connection.recv(4096), b"")

    def test_connected_pid_and_uid_are_both_checked_before_writing(self):
        for credentials in ((os.getpid() + 1, os.getuid()), (os.getpid(), os.getuid() + 1)):
            with self.subTest(credentials=credentials):
                message_id = self.create()
                with patch.object(bridge, "peer_credentials", return_value=credentials):
                    with self.assertRaisesRegex(RuntimeError, "Transport failed"):
                        bridge.deliver(self.root, self.config, message_id, transport=bridge.submit)
                connection, _ = self.server.accept()
                with connection:
                    connection.settimeout(2)
                    self.assertEqual(connection.recv(4096), b"")

    def test_unavailable_peer_credentials_fail_closed(self):
        message_id = self.create()
        with patch.object(bridge, "peer_credentials", side_effect=OSError("unsupported")):
            with self.assertRaisesRegex(RuntimeError, "Transport failed"):
                bridge.deliver(self.root, self.config, message_id, transport=bridge.submit)
        connection, _ = self.server.accept()
        with connection:
            connection.settimeout(2)
            self.assertEqual(connection.recv(4096), b"")
