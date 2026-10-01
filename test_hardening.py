"""Regression coverage for the six October 2026 audit findings. No live agents."""
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

import bridge
from test_transports import BridgeFixture


class HardeningTests(BridgeFixture):
    def test_unsafe_state_directory_is_rejected_before_mutation(self):
        before = (self.root / 'ledger.json').read_bytes()
        self.root.chmod(0o777)
        try:
            with self.assertRaisesRegex(ValueError, 'State directory'):
                self.create()
        finally:
            self.root.chmod(0o700)
        self.assertEqual((self.root / 'ledger.json').read_bytes(), before)

    def test_unsafe_configuration_is_rejected(self):
        path = self.root / 'endpoints.json'
        path.chmod(0o666)
        with self.assertRaisesRegex(ValueError, 'without group/other write'):
            bridge.load_config(self.root)

    def test_unsafe_ancestor_is_rejected(self):
        directory = self.root / 'shared'
        directory.mkdir(); directory.chmod(0o777)
        state = directory / 'state'; state.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, 'unsafe writable ancestor'):
            bridge.check_state_directory(state)

    def test_configuration_symlink_is_rejected(self):
        path = self.root / 'endpoints.json'
        target = self.root / 'target.json'; path.rename(target); path.symlink_to(target)
        with self.assertRaises(ValueError):
            bridge.load_config(self.root)

    def test_lock_symlinks_and_hardlinks_are_rejected(self):
        mid = self.create()
        target = self.root / 'target'; target.write_text('unchanged')
        for name in ('.ledger.lock', f'.delivery-{mid}.lock'):
            lock = self.root / name
            for kind in ('symlink', 'hardlink'):
                with self.subTest(name=name, kind=kind):
                    lock.unlink(missing_ok=True)
                    if kind == 'symlink': lock.symlink_to(target)
                    else: os.link(target, lock)
                    action = (lambda: bridge.status(self.root)) if name == '.ledger.lock' else (
                        lambda: bridge.deliver(self.root, self.config, mid, transport=Mock()))
                    with self.assertRaises((OSError, ValueError)):
                        action()
                    self.assertEqual(target.read_text(), 'unchanged')
                    lock.unlink()
            lock.touch(mode=0o600)

    def test_unsafe_queue_executable_is_rejected_before_launch(self):
        executable = self.root / 'fake-codex'
        executable.write_text('#!/bin/sh\nexit 0\n'); executable.chmod(0o777)
        self.config['codex_cli'] = str(executable)
        mid = self.create(recipient='codex')
        with patch.object(bridge.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                bridge.deliver(self.root, self.config, mid)
            run.assert_not_called()
        self.config['codex_cli'] = 'codex'
        with self.assertRaisesRegex(ValueError, 'absolute'):
            bridge.queue_executable(self.config)

    def test_commit_syncs_file_then_rename_then_directory(self):
        events = []
        real_sync, real_replace = os.fsync, os.replace
        def sync(fd):
            events.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            return real_sync(fd)
        def replace(*args):
            events.append('replace'); return real_replace(*args)
        with patch.object(bridge.os, 'fsync', side_effect=sync), patch.object(bridge.os, 'replace', side_effect=replace):
            self.create()
        self.assertEqual(events, ['file', 'replace', 'directory'])

    def test_directory_sync_failure_is_not_reported_as_success(self):
        real_sync = os.fsync
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError('fake directory sync failure')
            return real_sync(fd)
        with patch.object(bridge.os, 'fsync', side_effect=sync):
            with self.assertRaises(OSError): self.create()

    def test_lost_history_cannot_be_silently_recreated(self):
        mid = self.create(); bridge.claim(self.root, self.config, mid, 'claude')
        (self.root / 'ledger.json').rename(self.root / 'saved-history')
        for action in (lambda: bridge.status(self.root), self.create,
                       lambda: bridge.initialize(self.root, self.config, 'codex')):
            with self.assertRaises(ValueError): action()
            self.assertFalse((self.root / 'ledger.json').exists())

    def test_status_does_not_rewrite_history(self):
        self.create()
        with patch.object(bridge, 'atomic_json', side_effect=AssertionError('unexpected write')):
            self.assertEqual(len(bridge.status(self.root)), 1)

    def test_fresh_status_does_not_prevent_explicit_init(self):
        root = self.root / 'fresh'; root.mkdir()
        with self.assertRaisesRegex(ValueError, 'Missing ledger'):
            bridge.status(root, self.config)
        self.assertFalse((root / '.ledger.lock').exists())
        self.assertEqual(bridge.initialize(root, self.config, 'codex')['status'], 'initialized')
        self.assertEqual(bridge.initialize(root, self.config, 'codex')['status'], 'already_initialized')

    def test_init_requires_actor_identity_and_preserves_existing_history(self):
        mid = self.create()
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'wrong'}):
            with self.assertRaisesRegex(ValueError, 'pinned identity'):
                bridge.initialize(self.root, self.config, 'codex')
        bridge.initialize(self.root, self.config, 'codex')
        self.assertIn(mid, self.messages())

    def test_reply_is_linked_before_delivery_and_after_uncertain_receipt(self):
        parent = self.create(); bridge.claim(self.root, self.config, parent, 'claude')
        reply = self.create(recipient='codex', reply_to=parent)
        self.assertEqual(self.messages()[parent]['replies'], [reply])
        def transport(message, config, root):
            bridge.claim(root, config, message['id'], 'codex')
            raise TimeoutError('accepted before fake timeout')
        with self.assertRaises(RuntimeError):
            bridge.deliver(self.root, self.config, reply, transport=transport)
        self.assertEqual(bridge.deliver(self.root, self.config, reply, retry=True)['status'], 'already_claimed')
        self.assertEqual(self.messages()[parent]['replies'], [reply])

    def test_invalid_reply_ids_never_change_history(self):
        before = (self.root / 'ledger.json').read_bytes()
        for reply in ('', 'bad', '../escape'):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                self.create(reply_to=reply)
            self.assertEqual((self.root / 'ledger.json').read_bytes(), before)
        body = self.root / 'body'; body.write_text('test')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = bridge.main(['--state-dir', str(self.root), 'send', '--to', 'claude',
                                '--task', 'audit', '--body-file', str(body), '--reply-to', '', '--prepare-only'])
        self.assertEqual(code, 1)
        self.assertEqual(bridge.status(self.root), [])

    def test_orphan_queue_keeps_recovery_locked_until_it_exits(self):
        executable = self.root / 'fake-codex'
        executable.write_text(
            f'#!{sys.executable}\n'
            'import time\nfrom pathlib import Path\n'
            'Path("queue-ready").touch()\n'
            'deadline=time.monotonic()+15\n'
            'while not Path("queue-stop").exists() and time.monotonic()<deadline: time.sleep(.01)\n'
        ); executable.chmod(0o700)
        self.config['codex_cli'] = str(executable)
        (self.root / 'endpoints.json').write_text(json.dumps(self.config))
        mid = self.create(recipient='codex')
        sender = subprocess.Popen([sys.executable, '-B', str(bridge.HELPER_PATH), '--state-dir',
                                   str(self.root), 'deliver', mid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / 'queue-ready').exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue((self.root / 'queue-ready').exists())
            sender.kill(); sender.wait(timeout=3)
            attempt = self.messages()[mid]['attempts'][-1]['id']
            with self.assertRaisesRegex(ValueError, 'sender is still active'):
                bridge.recover(self.root, self.config, mid, 'claude', attempt, 'helper exited')
            with self.assertRaisesRegex(ValueError, 'sender is still active'):
                bridge.deliver(self.root, self.config, mid, retry=True, transport=Mock())
            (self.root / 'queue-stop').touch()
            deadline = time.monotonic() + 5
            while True:
                try:
                    result = bridge.recover(self.root, self.config, mid, 'claude', attempt, 'helper and queue exited')
                    break
                except ValueError:
                    if time.monotonic() >= deadline: raise
                    time.sleep(.01)
            self.assertEqual(result['status'], 'delivery_unknown')
        finally:
            (self.root / 'queue-stop').touch()
            if sender.poll() is None: sender.kill()
            sender.wait(timeout=3)
