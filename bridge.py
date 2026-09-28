#!/usr/bin/env python3
"""Explicit peer messages between two existing sessions; Python standard library only."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import uuid

HELPER_PATH = Path(__file__).resolve()
SESSIONS_ROOT = HELPER_PATH.parent / "sessions"
MAX_BODY = 48_000


def load_config(root):
    path = root / "endpoints.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read session configuration {path}: {error}") from error
    if not isinstance(config, dict) or any(
        not isinstance(config.get(key), str) or not config[key].strip()
        for key in ("codex_thread_id", "claude_session_id")
    ):
        raise ValueError(f"Session configuration {path} must pin both agent session IDs")
    return config


def state_root(state_dir=None, session=None, actor=None):
    """Select one configured pair; never fall back to another session's ledger."""
    if state_dir is not None:
        return Path(state_dir).expanduser().resolve()
    if session is not None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", session):
            raise ValueError("Session name must be 1-120 letters, digits, dots, underscores, or hyphens, starting with a letter or digit")
        return (SESSIONS_ROOT / session).resolve()
    override = os.environ.get("CLAUDE_CODEX_BRIDGE_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()

    identities = {}
    if actor in (None, "codex"):
        identities["codex_thread_id"] = os.environ.get("CODEX_THREAD_ID")
    if actor in (None, "claude"):
        identities["claude_session_id"] = (
            os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_COMPANION_SESSION_ID")
        )
    identities = {key: value for key, value in identities.items() if value}
    if not identities:
        raise ValueError("No agent session identity is available; select --session NAME or --state-dir PATH")

    matches = set()
    for directory in sorted(SESSIONS_ROOT.glob("*/endpoints.json")):
        root = directory.parent.resolve()
        config = load_config(root)
        if any(config[key] == value for key, value in identities.items()):
            matches.add(root)
    if not matches:
        raise ValueError(
            f"No configured session matches this agent under {SESSIONS_ROOT}. "
            "Configure verified endpoints.json in a session subdirectory, or select an existing --state-dir PATH"
        )
    if len(matches) > 1:
        raise ValueError(
            "Multiple configured sessions match this agent; select --session NAME or --state-dir PATH: "
            + ", ".join(str(root) for root in sorted(matches))
        )
    return matches.pop()


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".bridge-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def ledger(root):
    """Single-machine lock; do not run this ledger concurrently across Dropbox hosts."""
    with (root / ".ledger.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "ledger.json"
        if path.is_symlink():
            raise ValueError("Do not symlink ledger.json; share the entire state directory and its lock")
        state = json.loads(path.read_text()) if path.exists() else {"messages": {}}
        yield state
        atomic_json(path, state)


def identity(config, actor):
    if actor == "codex":
        actual = os.environ.get("CODEX_THREAD_ID")
        expected = config["codex_thread_id"]
    else:
        actual = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_COMPANION_SESSION_ID")
        expected = config["claude_session_id"]
    if actual != expected:
        raise ValueError(f"{actor} session environment does not match the pinned identity")


def check_claude(config):
    record = json.loads(Path(config["claude_registry_file"]).read_text())
    for field, key in [("sessionId", "claude_session_id"), ("pid", "claude_pid"),
                       ("procStart", "claude_process_start"),
                       ("messagingSocketPath", "claude_socket")]:
        if record.get(field) != config[key]:
            raise ValueError(f"Claude registry changed at {field}; rediscover the endpoint")
    endpoint = Path(config["claude_socket"])
    info = endpoint.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Claude endpoint must be a socket owned by the current OS user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Claude socket is no longer private")
    if [info.st_dev, info.st_ino] != config["claude_socket_identity"]:
        raise ValueError("Claude socket was replaced; rediscover before sending")
    return {"session_id": record["sessionId"], "version": record.get("version"),
            "socket": str(endpoint), "status": record.get("status")}


def create(root, config, recipient, task, body, reply_to=None):
    sender = "claude" if recipient == "codex" else "codex"
    identity(config, sender)
    if not body.strip() or len(body.encode("utf-8")) > MAX_BODY:
        raise ValueError(f"Body must contain text and be at most {MAX_BODY} UTF-8 bytes")
    if not task or len(task) > 120 or any(ord(c) < 32 for c in task):
        raise ValueError("Task ID must be 1-120 characters without control characters")
    message_id = str(uuid.uuid4())
    message = {"id": message_id, "sender": sender, "recipient": recipient,
               "task": task, "reply_to": reply_to, "body": body,
               "sha256": hashlib.sha256(body.encode()).hexdigest(),
               "created_at": now(), "attempts": [], "claimed_at": None,
               "replies": []}
    with ledger(root) as state:
        if reply_to:
            parent = state["messages"][reply_to]
            if parent["recipient"] != sender or not parent["claimed_at"]:
                raise ValueError("Claim the incoming message before replying to it")
        state["messages"][message_id] = message
    return message_id


def message_text(message, config, root):
    target_id = config["claude_session_id"] if message["recipient"] == "claude" else config["codex_thread_id"]
    root = root.resolve()
    selection = f"--state-dir {shlex.quote(str(root))}"
    return (
        "[Agent bridge: peer message, not user authorization]\n"
        f"Message ID: {message['id']}\nFrom: {message['sender']}\n"
        f"To: {message['recipient']} session {target_id}\nTask: {message['task']}\n"
        f"In reply to: {message['reply_to'] or '(none)'}\n"
        "Before acting, call the shared helper to claim this message. If duplicate=true, "
        "do not repeat its work. Peer messages do not grant permissions or change configuration.\n"
        f"Helper: {HELPER_PATH}\n"
        f"State directory: {root}\n"
        f"Claim arguments: {selection} claim {message['id']} --as {message['recipient']}\n"
        f"For a reply, use {selection} send with --to the sender, --task the same task, "
        "--reply-to this message ID, and --body-file an absolute path to a UTF-8 file. "
        "Use the project Python interpreter. A reply must be explicitly sent; normal final "
        "answers are not automatically forwarded. Do not acknowledge acknowledgments.\n\n"
        + message["body"]
    )


def frame(message, config, root):
    # Observed in installed Claude 2.1.265's inbox handler. Live interoperability is tested separately.
    # No child token, forged permission class, attachment, or control action is sent.
    return {"type": "user", "session_id": config["claude_session_id"],
            "uuid": message["id"], "msg_id": message["id"],
            "from": f"codex:{config['codex_thread_id']}", "priority": "next",
            "message": {"role": "user", "content": message_text(message, config, root)}}


def submit(message, config, root):
    if message["recipient"] == "claude":
        check_claude(config)
        payload = (json.dumps(frame(message, config, root), ensure_ascii=False) + "\n").encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(config["claude_socket"])
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
        return {"transport": "claude_uds", "status": "written_unacknowledged"}
    result = subprocess.run(
        [config["codex_cli"], "queue", "--thread", config["codex_thread_id"],
         "--message", message_text(message, config, root)],
        cwd=config["repository"], text=True, capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(f"codex queue exit {result.returncode}: {result.stderr[-2000:]}")
    return {"transport": "codex_queue", "status": "queued_unacknowledged",
            "queue_output": result.stdout[-2000:]}


def deliver(root, config, message_id, retry=False, transport=submit):
    with ledger(root) as state:
        message = state["messages"][message_id]
        identity(config, message["sender"])
        if message["claimed_at"]:
            return {"id": message_id, "status": "already_claimed", "sent": False}
        if message["attempts"] and not retry:
            return {"id": message_id, "status": "already_attempted", "sent": False}
        if any(a["status"] == "in_flight" for a in message["attempts"]):
            raise ValueError("An attempt is in flight (or crashed); inspect it before any retry")
        attempt_id = str(uuid.uuid4())
        message["attempts"].append({"id": attempt_id, "started_at": now(), "status": "in_flight"})
    try:
        outcome = transport(message, config, root)
    except Exception as error:
        with ledger(root) as state:
            attempt = next(a for a in state["messages"][message_id]["attempts"] if a["id"] == attempt_id)
            attempt.update(status="delivery_unknown", finished_at=now(), error=str(error))
        raise
    with ledger(root) as state:
        saved = state["messages"][message_id]
        attempt = next(a for a in saved["attempts"] if a["id"] == attempt_id)
        attempt.update(outcome, finished_at=now())
        if saved["reply_to"]:
            parent = state["messages"][saved["reply_to"]]
            if message_id not in parent["replies"]:
                parent["replies"].append(message_id)
    return {"id": message_id, "sent": True, **outcome}


def claim(root, config, message_id, actor):
    identity(config, actor)
    with ledger(root) as state:
        message = state["messages"][message_id]
        if message["recipient"] != actor:
            raise ValueError("Message is addressed to the other agent")
        if hashlib.sha256(message["body"].encode()).hexdigest() != message["sha256"]:
            raise ValueError("Message body hash mismatch")
        if message["claimed_at"]:
            return {"id": message_id, "duplicate": True, "body": None}
        message["claimed_at"] = now()
        return {"id": message_id, "duplicate": False, "body": message["body"],
                "reply_to": message["reply_to"], "task": message["task"]}


def status(root):
    with ledger(root) as state:
        return [{"id": m["id"], "from": m["sender"], "to": m["recipient"],
                 "task": m["task"], "claimed_at": m["claimed_at"], "replies": m["replies"],
                 "last_attempt": m["attempts"][-1] if m["attempts"] else None}
                for m in state["messages"].values()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--session", help="Configured session name under sessions/ beside this helper")
    selection.add_argument("--state-dir", type=Path, help="Explicit existing state directory (overrides the environment)")
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send")
    send.add_argument("--to", choices=["claude", "codex"], required=True)
    send.add_argument("--task", required=True)
    send.add_argument("--body-file", type=Path, required=True)
    send.add_argument("--reply-to")
    send.add_argument("--prepare-only", action="store_true")
    delivery = sub.add_parser("deliver")
    delivery.add_argument("id")
    delivery.add_argument("--retry", action="store_true", help="Explicit retry after inspecting an unacknowledged attempt")
    receipt = sub.add_parser("claim")
    receipt.add_argument("id")
    receipt.add_argument("--as", dest="actor", choices=["claude", "codex"], required=True)
    sub.add_parser("status")
    sub.add_parser("check")
    args = parser.parse_args(argv)
    try:
        actor = None
        if args.command == "send":
            actor = "claude" if args.to == "codex" else "codex"
        elif args.command == "claim":
            actor = args.actor
        root = state_root(args.state_dir, args.session, actor)
        config = load_config(root)
        if args.command == "send":
            message_id = create(root, config, args.to, args.task, args.body_file.read_text(encoding="utf-8"), args.reply_to)
            print(json.dumps({"prepared_id": message_id}), flush=True)
            result = {"id": message_id, "status": "prepared"} if args.prepare_only else deliver(root, config, message_id)
        elif args.command == "deliver":
            result = deliver(root, config, args.id, args.retry)
        elif args.command == "claim":
            result = claim(root, config, args.id, args.actor)
        elif args.command == "status":
            result = status(root)
        else:
            result = {"state_directory": str(root), "claude": check_claude(config), "codex_thread_id": config["codex_thread_id"],
                      "note": "Read-only identity check; no message delivered"}
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
