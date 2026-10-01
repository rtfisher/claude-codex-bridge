# Claude–Codex bridge

[![CI](https://github.com/rtfisher/claude-codex-bridge/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/rtfisher/claude-codex-bridge/actions/workflows/ci.yml)

A Python standard-library helper for explicit messages between an existing
Claude session and an existing Codex session on the same machine. It records
delivery attempts, verifies pinned identities, and detects duplicate claims.
Each configured session pair keeps its own ledger beside the helper. Stored
messages are bound to that pair and checked for integrity before delivery.

Requires Python 3.10 or newer on macOS or Linux. Live delivery also requires
the configured Claude messaging socket and a Codex CLI supporting `queue`.
The helper does not start agents, discover endpoints, or forward final answers
automatically.

## Configure a session pair

Create `sessions/<name>/endpoints.json` with verified endpoint information for
the intended pair. See [the configuration template](examples/endpoints.example.json)
for the required fields; its placeholder values must be replaced with the
actual session, registry, process, socket, and CLI values. Do not create a
second ledger for a pair that already has message history. Keep the state
directory private (`0700`) and its files owner-controlled (`0600` recommended).
The helper rejects group/other-writable state, configuration, and executables.
Changing session IDs in an existing configuration does not reassign its ledger.

```text
claude-codex-bridge/
  bridge.py
  sessions/
    my-session/
      endpoints.json
      ledger.json       # Created by explicit init
      .ledger.lock
```

From the repository directory:

```bash
python3 bridge.py --help
python3 bridge.py --session my-session check
# Once for a new pair; run as the calling agent (Claude uses --as claude):
python3 bridge.py --session my-session init --as codex
python3 bridge.py --session my-session status
```

The state directory is selected by `--session NAME` or `--state-dir PATH`, then
by `CLAUDE_CODEX_BRIDGE_STATE_DIR`, then by matching the calling agent's identity
against local session configurations. Explicit options go before the command.
Missing or ambiguous automatic matches fail without selecting another ledger.
Ordinary commands never create missing history, and `status` does not rewrite
it. If an existing ledger disappears, restore it; initialization refuses to
reset a directory with an existing lock. Existing ledgers need no reinitialization.

The session environment must match the pinned endpoint before sending or
claiming: `CODEX_THREAD_ID` for Codex; `CLAUDE_CODE_SESSION_ID` or the fallback
`CODEX_COMPANION_SESSION_ID` for Claude. Do not change these variables to
impersonate a different session.

## Send and receive

Write a nonempty UTF-8 message body of at most 48,000 bytes to a local file.
For example, from the intended Codex session:

```bash
python3 bridge.py --session my-session send --to claude \
  --task review --body-file /absolute/path/to/message.md
```

The recipient uses the exact claim arguments embedded in the incoming message.
Those arguments include the sender's state directory so both agents share the
same ledger and lock. Notifications contain routing and claim instructions;
the body and task text stay in the ledger and are returned by a verified claim.
They are never passed through queue command-line arguments. Socket delivery
checks the connected peer's PID and UID before writing a notification, and
transport diagnostics are sanitized.

A duplicate claim must not repeat the work. Reply with
the same task and `--reply-to MESSAGE_ID` after claiming the incoming message.
Reply links are recorded at preparation time, independently of delivery success.

For an uncertain delivery, inspect the recorded attempt and retry the same ID
with `deliver MESSAGE_ID --retry`. A transport accepting a message does not
establish that the recipient read or completed it.

## Recovery and existing ledgers

If a claim's output was lost, the original recipient can retrieve the body
without resetting the claim, then record completion after finishing the work:

```bash
python3 bridge.py --session my-session read MESSAGE_ID --as claude
python3 bridge.py --session my-session complete MESSAGE_ID --as claude
```

`read` reports claim and completion times. An absent completion record does
not prove no work occurred; check before resuming. `complete` is idempotent.

After a hard sender crash, inspect `status` and ensure the sender and any
orphaned transport have stopped. Recover the exact attempt before retrying:

```bash
python3 bridge.py --session my-session recover MESSAGE_ID --as codex \
  --attempt-id ATTEMPT_ID --reason "Original sender and transport have exited"
python3 bridge.py --session my-session deliver MESSAGE_ID --retry
```

Recovery refuses an active sender or a queue process holding its inherited
delivery lock, records an audit entry, and marks the attempt's delivery as unknown. It preserves claims and duplicate detection.
Queue wrappers must preserve the inherited lock descriptor until exit; wrappers
that close it or detach workers without it are unsupported. Ledger commits sync
both file contents and the parent directory before reporting success.

Unversioned ledgers require explicit migration. First stop and upgrade or
retire every older helper accessing that directory, then confirm the original
pairing and migrate using those IDs:

```bash
python3 bridge.py --state-dir /absolute/path/to/existing/session migrate \
  --as codex --codex-thread-id ORIGINAL_CODEX_THREAD_ID \
  --claude-session-id ORIGINAL_CLAUDE_SESSION_ID
```

Migration adds schema-version-2 bindings and envelope hashes without changing
message IDs, delivery history, claims, or replies. The supplied original IDs
must match the endpoints, and the calling agent's identity is checked.

[AGENTS.md](AGENTS.md) contains the complete operating and maintenance instructions.
Session directories, message bodies, and machine-specific `COPY_ORIGIN.json`
files are local data and are excluded from Git.

## Tests and releases

Run the same suite as CI without installing dependencies:

```bash
python3 -B -m unittest discover -v
```

Tests use temporary directories, a temporary Unix socket, and fake queue
transports. They cover session selection and isolation, identity and endpoint
checks, message integrity before transmission, private transport notifications,
socket replacement, legacy migration, crash recovery, duplicate claims, and
reply history. Tests do not send messages to live agent sessions.

[GitHub Actions](.github/workflows/ci.yml) runs on pushes, pull requests, and
manual dispatches. The matrix covers Python 3.10–3.14 on Linux and Python 3.14
on macOS. The badge above reports the `main` branch's latest workflow result.

Pushing a version tag such as `v0.1.0` runs the same tests and, after every
matrix job passes, publishes a GitHub Release with a source archive and its
SHA-256 checksum. Release archives contain only tracked source files.
Creating a tag is a separate maintainer action; ordinary pushes only run CI.
