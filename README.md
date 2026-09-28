# Claude–Codex bridge

[![CI](https://github.com/rtfisher/claude-codex-bridge/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/rtfisher/claude-codex-bridge/actions/workflows/ci.yml)

A Python standard-library helper for explicit messages between an existing
Claude session and an existing Codex session on the same machine. It records
delivery attempts, verifies pinned identities, and detects duplicate claims.
Each configured session pair keeps its own ledger beside the helper.

Requires Python 3.10 or newer on macOS or Linux. Live delivery also requires
the configured Claude messaging socket and a Codex CLI supporting `queue`.
The helper does not start agents, discover endpoints, or forward final answers
automatically.

## Configure a session pair

Create `sessions/<name>/endpoints.json` with verified endpoint information for
the intended pair. See [the configuration template](examples/endpoints.example.json)
for the required fields; its placeholder values must be replaced with the
actual session, registry, process, socket, and CLI values. Do not create a
second ledger for a pair that already has message history.

```text
claude-codex-bridge/
  bridge.py
  sessions/
    my-session/
      endpoints.json
      ledger.json       # Created on the first ledger operation
      .ledger.lock
```

From the repository directory:

```bash
python3 bridge.py --help
python3 bridge.py --session my-session check
python3 bridge.py --session my-session status
```

The state directory is selected by `--session NAME` or `--state-dir PATH`, then
by `CLAUDE_CODEX_BRIDGE_STATE_DIR`, then by matching the calling agent's identity
against local session configurations. Explicit options go before the command.
Missing or ambiguous automatic matches fail without selecting another ledger.

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
same ledger and lock. A duplicate claim must not repeat the work. Reply with
the same task and `--reply-to MESSAGE_ID` after claiming the incoming message.

For an uncertain delivery, inspect the recorded attempt and retry the same ID
with `deliver MESSAGE_ID --retry`. A transport accepting a message does not
establish that the recipient read or completed it.

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
checks, message integrity, retries, duplicate claims, and reply history.

[GitHub Actions](.github/workflows/ci.yml) runs on pushes, pull requests, and
manual dispatches. The matrix covers Python 3.10–3.14 on Linux and Python 3.14
on macOS. The badge above reports the `main` branch's latest workflow result.

Pushing a version tag such as `v0.1.0` runs the same tests and, after every
matrix job passes, publishes a GitHub Release with a source archive and its
SHA-256 checksum. Release archives contain only tracked source files.
Creating a tag is a separate maintainer action; ordinary pushes only run CI.
