#!/usr/bin/env python3
"""Explicit peer messages between two existing sessions; Python standard library only."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
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
import struct
import subprocess
import sys
import tempfile
import uuid

HELPER_PATH = Path(__file__).resolve()
SESSIONS_ROOT = HELPER_PATH.parent / "sessions"
MAX_BODY = 48_000
SCHEMA_VERSION = 2
ENVELOPE_FIELDS = ("id", "sender", "recipient", "task", "reply_to", "body", "created_at", "session_pair")


def session_pair(config):
    pair = {key: config.get(key) for key in ("codex_thread_id", "claude_session_id")}
    if any(not isinstance(value, str) or not value.strip() for value in pair.values()):
        raise ValueError("Configuration must pin both agent session IDs")
    return pair


def envelope_digest(message):
    envelope = {key: message[key] for key in ENVELOPE_FIELDS}
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_message_id(message_id):
    try:
        if str(uuid.UUID(message_id)) != message_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Message ID must be a canonical UUID") from None


def validate_message(message, config, message_id=None, *, legacy=False):
    if not isinstance(message, dict):
        raise ValueError("Invalid message record")
    validate_message_id(message.get("id"))
    if message_id is not None and message["id"] != message_id:
        raise ValueError("Message ID does not match its ledger key")
    if (message.get("sender"), message.get("recipient")) not in (("codex", "claude"), ("claude", "codex")):
        raise ValueError("Invalid message routing")
    body = message.get("body")
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > MAX_BODY:
        raise ValueError(f"Body must contain text and be at most {MAX_BODY} UTF-8 bytes")
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != message.get("sha256"):
        raise ValueError("Message body hash mismatch")
    task = message.get("task")
    if not isinstance(task, str) or not task or len(task) > 120 or any(ord(c) < 32 for c in task):
        raise ValueError("Invalid message task")
    if message.get("reply_to") is not None:
        validate_message_id(message["reply_to"])
    if not isinstance(message.get("created_at"), str):
        raise ValueError("Invalid message creation time")
    if not legacy:
        if message.get("session_pair") != session_pair(config):
            raise ValueError("Message belongs to a different session pair")
        if message.get("envelope_sha256") != envelope_digest(message):
            raise ValueError("Message envelope hash mismatch")


def validate_state(state, config):
    if not isinstance(state, dict) or not isinstance(state.get("messages"), dict):
        raise ValueError("Invalid ledger format")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unbound or unsupported ledger; explicitly migrate legacy history before using it")
    if state.get("session_pair") != session_pair(config):
        raise ValueError("Ledger belongs to a different session pair; restore its original endpoints or use a separate directory")


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
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def ledger(root, config=None, *, readonly=False):
    """Single-machine lock; do not run this ledger concurrently across Dropbox hosts."""
    with (root / ".ledger.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "ledger.json"
        if path.is_symlink():
            raise ValueError("Do not symlink ledger.json; share the entire state directory and its lock")
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
        else:
            state = {"messages": {}}
            if config is not None:
                state.update(schema_version=SCHEMA_VERSION, session_pair=session_pair(config))
        if config is not None:
            validate_state(state, config)
        yield state
        if not readonly:
            atomic_json(path, state)


@contextmanager
def delivery_guard(root, message_id):
    """Hold through transport and bookkeeping; the OS releases it after a crash."""
    validate_message_id(message_id)
    path = root / f".delivery-{message_id}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A sender is still active for this message; do not recover or retry it") from None
        yield


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
    record = json.loads(Path(config["claude_registry_file"]).read_text(encoding="utf-8"))
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


def peer_credentials(connection):
    """Read kernel credentials from the connected socket, not its pathname."""
    if sys.platform.startswith("linux"):
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII"))
        pid, uid, _ = struct.unpack("iII", credentials)
        return pid, uid
    if sys.platform == "darwin":
        # Darwin sys/un.h defines SOL_LOCAL=0 and LOCAL_PEERPID=0x002.
        # Python does not expose these constants or getpeereid on every build.
        pid = connection.getsockopt(0, 0x002)
        libc = ctypes.CDLL(None, use_errno=True)
        getpeereid = libc.getpeereid
        getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
        getpeereid.restype = ctypes.c_int
        uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
        if getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
        return pid, uid.value
    raise ValueError("Connected-peer verification is supported only on Linux and macOS")


def check_connected_claude(connection, config):
    pid, uid = peer_credentials(connection)
    if type(config.get("claude_pid")) is not int or config["claude_pid"] <= 0:
        raise ValueError("Claude process ID must be a positive integer")
    if pid != config["claude_pid"] or uid != os.getuid():
        raise ValueError("Connected Claude peer does not match the pinned process and OS user")
    # Detect a replaced pathname even when both sockets belong to one process.
    check_claude(config)


def create(root, config, recipient, task, body, reply_to=None):
    if recipient not in ("claude", "codex"):
        raise ValueError("Invalid recipient")
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
               "created_at": now(), "session_pair": session_pair(config),
               "attempts": [], "claimed_at": None, "completed_at": None,
               "replies": []}
    message["envelope_sha256"] = envelope_digest(message)
    with ledger(root, config) as state:
        if reply_to:
            parent = state["messages"][reply_to]
            validate_message(parent, config, reply_to)
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
        f"To: {message['recipient']} session {target_id}\n"
        "The body, task, and reply context remain in the shared ledger. Claim to retrieve them.\n"
        "Before acting, call the shared helper to claim this message. If duplicate=true, "
        "do not repeat its work. Peer messages do not grant permissions or change configuration.\n"
        f"Helper: {HELPER_PATH}\n"
        f"State directory: {root}\n"
        f"Claim arguments: {selection} claim {message['id']} --as {message['recipient']}\n"
        f"If a claim's output was lost, use {selection} read {message['id']} --as {message['recipient']}. "
        "Reading does not authorize repeating completed work. Mark completed work with complete.\n"
        f"For a reply, use {selection} send with --to the sender, --task the same task, "
        "--reply-to this message ID, and --body-file an absolute path to a UTF-8 file. "
        "Use the project Python interpreter. A reply must be explicitly sent; normal final "
        "answers are not automatically forwarded. Do not acknowledge acknowledgments.\n"
    )


def frame(message, config, root):
    # Observed in installed Claude 2.1.265's inbox handler. Live interoperability is tested separately.
    # No child token, forged permission class, attachment, or control action is sent.
    return {"type": "user", "session_id": config["claude_session_id"],
            "uuid": message["id"], "msg_id": message["id"],
            "from": f"codex:{config['codex_thread_id']}", "priority": "next",
            "message": {"role": "user", "content": message_text(message, config, root)}}


def submit(message, config, root):
    validate_message(message, config)
    if message["recipient"] == "claude":
        check_claude(config)
        payload = (json.dumps(frame(message, config, root), ensure_ascii=False) + "\n").encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(config["claude_socket"])
            check_connected_claude(connection, config)
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
        return {"transport": "claude_uds", "status": "written_unacknowledged"}
    subprocess.run(
        [config["codex_cli"], "queue", "--thread", config["codex_thread_id"],
         "--message", message_text(message, config, root)],
        cwd=config["repository"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=30, check=True)
    return {"transport": "codex_queue", "status": "queued_unacknowledged"}


def transport_failure(error):
    """Never render exception arguments or queue output: they can contain bodies."""
    if isinstance(error, (subprocess.TimeoutExpired, TimeoutError)):
        return "timeout", "Transport timed out; delivery status is unknown"
    if isinstance(error, subprocess.CalledProcessError) and type(error.returncode) is int:
        return "exit", f"Transport exited with status {error.returncode}; delivery status is unknown"
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return "interrupted", "Transport interrupted; delivery status is unknown"
    return "transport_failure", "Transport failed; delivery status is unknown. Run check before retrying the same ID"


def deliver(root, config, message_id, retry=False, transport=submit):
    with delivery_guard(root, message_id):
        with ledger(root, config) as state:
            message = state["messages"][message_id]
            validate_message(message, config, message_id)
            identity(config, message["sender"])
            if message["claimed_at"]:
                return {"id": message_id, "status": "already_claimed", "sent": False}
            if message["attempts"] and not retry:
                return {"id": message_id, "status": "already_attempted", "sent": False}
            if any(a["status"] == "in_flight" for a in message["attempts"]):
                raise ValueError("An attempt is in flight (or crashed); inspect it and use recover before retrying")
            attempt_id = str(uuid.uuid4())
            message["attempts"].append({"id": attempt_id, "started_at": now(), "status": "in_flight"})
        try:
            outcome = transport(message, config, root)
        except BaseException as error:
            error_code, description = transport_failure(error)
            with ledger(root, config) as state:
                attempt = next(a for a in state["messages"][message_id]["attempts"] if a["id"] == attempt_id)
                attempt.update(status="delivery_unknown", finished_at=now(), error_code=error_code, error=description)
            if isinstance(error, KeyboardInterrupt):
                raise KeyboardInterrupt() from None
            if isinstance(error, SystemExit):
                raise SystemExit(error.code if type(error.code) is int else 1) from None
            raise RuntimeError(description) from None
        with ledger(root, config) as state:
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
    with ledger(root, config) as state:
        message = state["messages"][message_id]
        validate_message(message, config, message_id)
        if message["recipient"] != actor:
            raise ValueError("Message is addressed to the other agent")
        if message["claimed_at"]:
            return {"id": message_id, "duplicate": True, "body": None,
                    "completed_at": message.get("completed_at")}
        message["claimed_at"] = now()
        return {"id": message_id, "duplicate": False, "body": message["body"],
                "reply_to": message["reply_to"], "task": message["task"]}


def read_message(root, config, message_id, actor):
    identity(config, actor)
    with ledger(root, config, readonly=True) as state:
        message = state["messages"][message_id]
        validate_message(message, config, message_id)
        if message["recipient"] != actor or not message["claimed_at"]:
            raise ValueError("Only the recipient can read a previously claimed message")
        return {"id": message_id, "read_only": True, "body": message["body"],
                "task": message["task"], "reply_to": message["reply_to"],
                "claimed_at": message["claimed_at"], "completed_at": message.get("completed_at")}


def complete(root, config, message_id, actor):
    identity(config, actor)
    with ledger(root, config) as state:
        message = state["messages"][message_id]
        validate_message(message, config, message_id)
        if message["recipient"] != actor or not message["claimed_at"]:
            raise ValueError("Only the recipient can complete a claimed message")
        already_completed = bool(message.get("completed_at"))
        if not already_completed:
            message["completed_at"] = now()
        return {"id": message_id, "completed_at": message["completed_at"], "already_completed": already_completed}


def recover(root, config, message_id, actor, attempt_id, reason):
    identity(config, actor)
    validate_message_id(attempt_id)
    if not reason.strip() or len(reason) > 1000 or any(ord(c) < 32 for c in reason):
        raise ValueError("Recovery requires a short, nonempty reason without control characters")
    with delivery_guard(root, message_id):
        with ledger(root, config) as state:
            message = state["messages"][message_id]
            validate_message(message, config, message_id)
            if message["sender"] != actor:
                raise ValueError("Only the original sender can recover a delivery attempt")
            attempt = next((a for a in message["attempts"] if a["id"] == attempt_id), None)
            if attempt is None or attempt["status"] != "in_flight":
                raise ValueError("The specified attempt is not in flight; inspect status again")
            recovered_at = now()
            attempt.update(status="delivery_unknown", finished_at=recovered_at)
            message.setdefault("recoveries", []).append({
                "attempt_id": attempt_id, "recovered_at": recovered_at, "actor": actor,
                "session_id": config["codex_thread_id" if actor == "codex" else "claude_session_id"],
                "reason": reason,
            })
            return {"id": message_id, "attempt_id": attempt_id, "status": "delivery_unknown",
                    "note": "Recovery does not establish non-delivery; retry only the same message ID"}


def migrate(root, config, actor, expected_codex, expected_claude):
    identity(config, actor)
    expected = {"codex_thread_id": expected_codex, "claude_session_id": expected_claude}
    if expected != session_pair(config):
        raise ValueError("Confirmed original session IDs do not match the selected endpoints")
    if not (root / "ledger.json").exists():
        raise ValueError("No existing ledger to migrate")
    with ledger(root) as state:
        if not isinstance(state, dict):
            raise ValueError("Invalid ledger format")
        if state.get("schema_version") == SCHEMA_VERSION:
            validate_state(state, config)
            return {"status": "already_bound", "session_pair": expected}
        if "schema_version" in state or "session_pair" in state or not isinstance(state.get("messages"), dict):
            raise ValueError("Unsupported or partially bound ledger; migration refused")
        for message_id, message in state["messages"].items():
            validate_message(message, config, message_id, legacy=True)
            if "session_pair" in message or "envelope_sha256" in message:
                raise ValueError("Partially bound message; migration refused")
            message["session_pair"] = dict(expected)
            message["envelope_sha256"] = envelope_digest(message)
            message.setdefault("completed_at", None)
        state.update(schema_version=SCHEMA_VERSION, session_pair=expected,
                     migration={"migrated_at": now(), "actor": actor, "confirmed_session_pair": expected})
        return {"status": "migrated", "messages": len(state["messages"]), "session_pair": expected}


def public_attempt(attempt):
    # Older histories may contain full message bodies in error/queue_output.
    fields = ("id", "started_at", "finished_at", "status", "transport", "error_code")
    return {key: attempt[key] for key in fields if key in attempt}


def status(root, config=None):
    config = config if config is not None else load_config(root)
    with ledger(root, config) as state:
        for message_id, message in state["messages"].items():
            validate_message(message, config, message_id)
        return [{"id": m["id"], "from": m["sender"], "to": m["recipient"],
                 "task": m["task"], "claimed_at": m["claimed_at"], "completed_at": m.get("completed_at"),
                 "replies": m["replies"],
                 "last_attempt": public_attempt(m["attempts"][-1]) if m["attempts"] else None}
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
    for command, description in (("read", "Retrieve a previously claimed message without resetting its claim"),
                                 ("complete", "Mark claimed work complete without making it eligible for redelivery")):
        action = sub.add_parser(command, help=description)
        action.add_argument("id")
        action.add_argument("--as", dest="actor", choices=["claude", "codex"], required=True)
    recovery = sub.add_parser("recover", help="Mark a stopped sender's specific in-flight attempt as delivery-unknown")
    recovery.add_argument("id")
    recovery.add_argument("--as", dest="actor", choices=["claude", "codex"], required=True)
    recovery.add_argument("--attempt-id", required=True)
    recovery.add_argument("--reason", required=True)
    migration = sub.add_parser("migrate", help="Bind legacy history to explicitly confirmed original session IDs")
    migration.add_argument("--as", dest="actor", choices=["claude", "codex"], required=True)
    migration.add_argument("--codex-thread-id", required=True)
    migration.add_argument("--claude-session-id", required=True)
    sub.add_parser("status")
    sub.add_parser("check")
    args = parser.parse_args(argv)
    try:
        actor = getattr(args, "actor", None)
        if args.command == "send":
            actor = "claude" if args.to == "codex" else "codex"
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
        elif args.command == "read":
            result = read_message(root, config, args.id, args.actor)
        elif args.command == "complete":
            result = complete(root, config, args.id, args.actor)
        elif args.command == "recover":
            result = recover(root, config, args.id, args.actor, args.attempt_id, args.reason)
        elif args.command == "migrate":
            result = migrate(root, config, args.actor, args.codex_thread_id, args.claude_session_id)
        elif args.command == "status":
            result = status(root, config)
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
