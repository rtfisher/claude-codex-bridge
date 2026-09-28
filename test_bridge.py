"""Session isolation checks. All state and transports are temporary or fake."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

import bridge


class SessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bridge-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.sessions = self.base / "sessions"
        for context in (
            patch.object(bridge, "SESSIONS_ROOT", self.sessions),
            patch.dict(os.environ, {}, clear=True),
        ):
            context.start()
            self.addCleanup(context.stop)

    def configure(self, name="first", codex="codex-first", claude="claude-first", root=None):
        root = root or self.sessions / name
        root.mkdir(parents=True)
        config = {"codex_thread_id": codex, "claude_session_id": claude}
        (root / "endpoints.json").write_text(json.dumps(config), encoding="utf-8")
        return root, config

    def run_cli(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = bridge.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_both_agents_select_the_same_local_pair(self):
        root, _ = self.configure()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-first"}):
            self.assertEqual(bridge.state_root(actor="codex"), root)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "claude-first"}):
            self.assertEqual(bridge.state_root(actor="claude"), root)
        with patch.dict(os.environ, {"CODEX_COMPANION_SESSION_ID": "claude-first"}):
            self.assertEqual(bridge.state_root(actor="claude"), root)
        self.assertFalse((root / "ledger.json").exists())

    def test_pair_selection_uses_the_command_actor(self):
        first, _ = self.configure()
        second, _ = self.configure("second", "codex-second", "claude-second")
        with patch.dict(os.environ, {
            "CODEX_THREAD_ID": "codex-first", "CLAUDE_CODE_SESSION_ID": "claude-second",
        }):
            self.assertEqual(bridge.state_root(actor="codex"), first)
            self.assertEqual(bridge.state_root(actor="claude"), second)
            with self.assertRaisesRegex(ValueError, "Multiple configured sessions"):
                bridge.state_root()

    def test_separate_sessions_keep_separate_messages_and_locks(self):
        first, first_config = self.configure()
        second, second_config = self.configure("second", "codex-second", "claude-second")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-first"}):
            first_id = bridge.create(bridge.state_root(), first_config, "claude", "task", "first")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-second"}):
            second_id = bridge.create(bridge.state_root(), second_config, "claude", "task", "second")
        for root, message_id in ((first, first_id), (second, second_id)):
            state = json.loads((root / "ledger.json").read_text())
            self.assertEqual(set(state["messages"]), {message_id})
            self.assertTrue((root / ".ledger.lock").exists())
        self.assertNotEqual((first / ".ledger.lock").stat().st_ino, (second / ".ledger.lock").stat().st_ino)
        self.assertFalse((self.sessions / "ledger.json").exists())

    def test_no_identity_does_not_select_the_only_session(self):
        root, _ = self.configure()
        with self.assertRaisesRegex(ValueError, "No agent session identity"):
            bridge.state_root()
        self.assertFalse((root / "ledger.json").exists())

    def test_unmatched_identity_does_not_fall_back(self):
        root, _ = self.configure()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "unrelated"}):
            code, stdout, stderr = self.run_cli(["status"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("No configured session matches", json.loads(stderr)["error"])
        self.assertFalse((root / "ledger.json").exists())

    def test_unconfigured_install_does_not_create_state(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "unconfigured"}):
            with self.assertRaisesRegex(ValueError, "No configured session matches"):
                bridge.state_root()
        self.assertFalse(self.sessions.exists())

    def test_ambiguous_pairs_require_explicit_selection(self):
        first, _ = self.configure()
        self.configure("second", "codex-first", "claude-second")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-first"}):
            with self.assertRaisesRegex(ValueError, "Multiple configured sessions"):
                bridge.state_root()
            self.assertEqual(bridge.state_root(session="first"), first)

    def test_explicit_selectors_override_environment(self):
        root, _ = self.configure()
        external, _ = self.configure(root=self.base / "external")
        with patch.dict(os.environ, {"CLAUDE_CODEX_BRIDGE_STATE_DIR": str(external)}):
            self.assertEqual(bridge.state_root(), external)
            self.assertEqual(bridge.state_root(session="first"), root)
            self.assertEqual(bridge.state_root(state_dir=root), root)

    def test_override_is_read_per_call(self):
        root, _ = self.configure()
        other, _ = self.configure("other", "codex-other", "claude-other")
        with patch.dict(os.environ, {"CLAUDE_CODEX_BRIDGE_STATE_DIR": str(root)}):
            self.assertEqual(bridge.state_root(), root)
        with patch.dict(os.environ, {"CLAUDE_CODEX_BRIDGE_STATE_DIR": str(other)}):
            self.assertEqual(bridge.state_root(), other)

    def test_session_names_cannot_traverse_directories(self):
        for name in ("../escape", ".", "..", "/tmp/escape", "nested/name", "", "a" * 121):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Session name"):
                bridge.state_root(session=name)

    def test_directory_aliases_share_the_same_ledger(self):
        root, config = self.configure()
        (self.sessions / "alias").symlink_to(root, target_is_directory=True)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-first"}):
            self.assertEqual(bridge.state_root(), root)
            message_id = bridge.create(bridge.state_root(session="alias"), config, "claude", "task", "shared")
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "claude-first"}):
            self.assertFalse(bridge.claim(root, config, message_id, "claude")["duplicate"])
            self.assertTrue(bridge.claim(bridge.state_root(session="alias"), config, message_id, "claude")["duplicate"])

    def test_individual_ledger_symlink_cannot_split_history(self):
        root, _ = self.configure()
        original = self.base / "ledger.json"
        original.write_text('{"messages": {}}')
        (root / "ledger.json").symlink_to(original)
        with self.assertRaisesRegex(ValueError, "Do not symlink ledger.json"):
            bridge.status(root)
        self.assertTrue((root / "ledger.json").is_symlink())
        self.assertEqual(original.read_text(), '{"messages": {}}')

    def test_missing_configuration_is_a_json_error(self):
        code, stdout, stderr = self.run_cli(["--session", "missing", "status"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Cannot read session configuration", json.loads(stderr)["error"])
        self.assertFalse(self.sessions.exists())

    def test_invalid_configuration_fails_before_creating_ledger(self):
        root, _ = self.configure()
        for content in ("{bad json", "[]", "{}", '{"codex_thread_id": "x", "claude_session_id": ""}'):
            with self.subTest(content=content):
                (root / "endpoints.json").write_text(content)
                code, _, stderr = self.run_cli(["--session", "first", "status"])
                self.assertEqual(code, 1)
                self.assertIn("error", json.loads(stderr))
                self.assertFalse((root / "ledger.json").exists())

    def test_explicit_selection_does_not_bypass_identity(self):
        root, _ = self.configure()
        body = self.base / "body.md"
        body.write_text("test")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "wrong-session"}):
            code, stdout, stderr = self.run_cli([
                "--state-dir", str(root), "send", "--to", "claude", "--task", "test",
                "--body-file", str(body), "--prepare-only",
            ])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("pinned identity", json.loads(stderr)["error"])
        self.assertFalse((root / "ledger.json").exists())

    def test_prepare_only_uses_actor_identity_and_local_state(self):
        root, _ = self.configure()
        other, _ = self.configure("other", "codex-other", "claude-other")
        body = self.base / "body.md"
        body.write_text("Résumé", encoding="utf-8")
        with patch.dict(os.environ, {
            "CODEX_THREAD_ID": "codex-first", "CLAUDE_CODE_SESSION_ID": "claude-other",
        }), patch.object(bridge, "deliver", side_effect=AssertionError("No real transport")):
            code, stdout, stderr = self.run_cli([
                "send", "--to", "claude", "--task", "test", "--body-file", str(body), "--prepare-only",
            ])
        self.assertEqual((code, stderr), (0, ""))
        message_id = json.loads(stdout.splitlines()[0])["prepared_id"]
        saved = json.loads((root / "ledger.json").read_text())["messages"][message_id]
        self.assertEqual(saved["body"], "Résumé")
        self.assertEqual(saved["attempts"], [])
        self.assertFalse((other / "ledger.json").exists())

    def test_advertised_claim_uses_same_state_from_another_working_directory(self):
        root, config = self.configure(root=self.base / "session's state")
        other, _ = self.configure("other", "codex-other", "claude-other")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-first"}):
            message_id = bridge.create(root, config, "claude", "task", "Résumé")
        message = json.loads((root / "ledger.json").read_text())["messages"][message_id]
        text = bridge.message_text(message, config, root)
        arguments = next(line.removeprefix("Claim arguments: ") for line in text.splitlines() if line.startswith("Claim arguments: "))
        arguments = shlex.split(arguments)
        self.assertEqual(arguments[:2], ["--state-dir", str(root)])
        self.assertIn(f"For a reply, use --state-dir {shlex.quote(str(root))} send", text)
        previous_cwd = Path.cwd()
        try:
            os.chdir(other)
            with patch.dict(os.environ, {
                "CLAUDE_CODE_SESSION_ID": "claude-first", "CLAUDE_CODEX_BRIDGE_STATE_DIR": str(other),
            }):
                code, stdout, stderr = self.run_cli(arguments)
                self.assertEqual((code, stderr), (0, ""))
                self.assertEqual(json.loads(stdout)["body"], "Résumé")
                self.assertFalse(json.loads(stdout)["duplicate"])
                code, stdout, stderr = self.run_cli(arguments)
                self.assertEqual((code, stderr), (0, ""))
                self.assertTrue(json.loads(stdout)["duplicate"])
        finally:
            os.chdir(previous_cwd)
        self.assertFalse((other / "ledger.json").exists())

    def test_fake_transport_round_trip_and_retry_preserve_history(self):
        root, config = self.configure()
        sent = []

        def fake_transport(message, endpoint_config, selected_root):
            self.assertEqual(selected_root, root)
            self.assertEqual(endpoint_config, config)
            sent.append(message["id"])
            self.assertIn(f"State directory: {root}", bridge.message_text(message, config, selected_root))
            return {"transport": "fake", "status": "written_unacknowledged"}

        with patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-first"}):
            message_id = bridge.create(bridge.state_root(), config, "claude", "task", "request")
            self.assertTrue(bridge.deliver(root, config, message_id, transport=fake_transport)["sent"])
            self.assertFalse(bridge.deliver(root, config, message_id, transport=fake_transport)["sent"])
            self.assertTrue(bridge.deliver(root, config, message_id, retry=True, transport=fake_transport)["sent"])
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "claude-first"}):
            self.assertFalse(bridge.claim(bridge.state_root(), config, message_id, "claude")["duplicate"])
            reply_id = bridge.create(bridge.state_root(), config, "codex", "task", "reply", message_id)
            bridge.deliver(root, config, reply_id, transport=fake_transport)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-first"}):
            self.assertFalse(bridge.claim(bridge.state_root(), config, reply_id, "codex")["duplicate"])
            self.assertFalse(bridge.deliver(root, config, message_id, retry=True, transport=fake_transport)["sent"])
        state = json.loads((root / "ledger.json").read_text())["messages"]
        self.assertEqual(state[message_id]["replies"], [reply_id])
        self.assertEqual(sent, [message_id, message_id, reply_id])
        self.assertEqual(set(state), {message_id, reply_id})

    def test_cli_can_still_open_an_explicit_legacy_directory(self):
        root, config = self.configure(root=self.base / "legacy")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-first"}):
            message_id = bridge.create(root, config, "claude", "task", "old history")
        code, stdout, stderr = self.run_cli(["--state-dir", str(root), "status"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual([entry["id"] for entry in json.loads(stdout)], [message_id])
        self.assertFalse(self.sessions.exists())


if __name__ == "__main__":
    unittest.main()
