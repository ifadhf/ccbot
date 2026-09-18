# Agrinas CCBot customizations

Version `0.1.0+agrinas.5` extends `ifadhf/ccbot` at
`f2019b96081a0a7aa75a4478a1ef97c7a26ca84f` (`local-patches`). It supports private
groups for individual users and a shared group, with an independent Claude
session for each named Telegram topic.

The recovery update is on `feat/agrinas-session-recovery`, based on commit
`6d834ca788f39be5c81564136adc9e33ce6034bd` from
`feat/agrinas-topics-files-workspace`.

## Install and configure

Install the recovery branch using the same account that runs tmux and Claude
Code. Back up the existing state and transcripts, then stop the bot before
installing. The older `feat/agrinas-topics-files-workspace` branch contains
`agrinas.4`, without recovery.

```bash
git clone --branch feat/agrinas-session-recovery https://github.com/ifadhf/ccbot.git
cd ccbot
uv tool install .
ccbot hook --install
```

Claude Code must already be installed and authenticated. Put the bot token and
allowed numeric Telegram user IDs in `~/.ccbot/.env` (or the directory selected
by `CCBOT_DIR`). Keep the directory private and the file readable only by its
owner. `.env.example` contains placeholders and the recommended feature flags:

```dotenv
CCBOT_CHAT_SCOPED_TOPICS=true
CCBOT_TOPIC_OWNER_LOCK=true
CCBOT_SHOW_USER_MESSAGES=false
CCBOT_SHOW_TOOL_CALLS=false
CCBOT_SHOW_THINKING=false
CCBOT_FIXED_WORKDIR=/srv/project
```

Create the workspace first, with permissions for the account running CCBot.
`CCBOT_FIXED_WORKDIR` must resolve to an existing absolute directory; leave it
empty to retain the directory and session pickers. Restart CCBot after editing
its configuration. The chat-scoped routing and owner-lock flags default to
`false` when omitted, for compatibility with existing installations.

The hook installer preserves unrelated Claude settings and installs
`SessionStart`, `Stop`, and `Notification` hooks. Completion notifications mention
the recorded topic owner. The three display flags above suppress prompt echoes,
tool-call details, and thinking output.

## Topics and ownership

- Routing uses `(chat_id, topic_id)`, so identical topic numbers in different
  groups do not collide. Authorization still checks the sender against
  `ALLOWED_USERS`.
- With owner locking enabled, a topic belongs to its allowed creator. If CCBot
  missed the creation event, the first allowed sender claims it. The first
  message receives an owner mention. Other users cannot control that topic
  through text, attachments, or buttons.
- Owners are recorded in `state.json` under
  `topic_owners["<chat_id>:<topic_id>"]`, including `user_id`, `username`,
  `first_name`, and `announced`. Authorization uses the stable numeric ID;
  usernames are refreshed when the owner sends another message. Ownership
  survives restarts and removal of a topic's window binding.
- With owner locking disabled, allowed users share the topic's single session.
- General-topic updates are silently ignored, including commands, attachments,
  and callbacks. Use named topics for bot interaction.

With a fixed workspace configured, creating a topic immediately starts its own
tmux window and fresh Claude session in that directory. The first message or
supported attachment can also initialize a session if the creation event was
missed. Folder, existing-window, and resume pickers are skipped; stale picker
buttons cannot change the selection. Existing live bindings are preserved.

The workspace setting controls the starting directory and picker flow. It does
not restrict the operating-system account's filesystem access, and separate
topics can edit files in the same workspace.

## Recovery after reboot

At startup, CCBot uses the existing `state.json` to restore lost topic sessions
before stale-window cleanup. It runs `claude --resume <saved-session-id>` in each
conversation's original directory and reconnects its original Telegram topic
only after a matching `SessionStart` hook. A bot-only restart keeps surviving
windows. No user message or interrupted command is resent by recovery.

The same file stores pending recoveries, original session IDs, directories,
owners, display names, read offsets, and a tmux lifetime identifier. There is no
second recovery state file. A unique launch token prevents duplicate processes
after a bot crash, and the lifetime identifier prevents reused tmux IDs from
mixing topics after a server reset. The hook carries both identifiers in the
existing `session_map.json`; monitor offsets remain in `monitor_state.json`.

The original transcript, working directory, Claude login, and installed hook
must be available. A failed topic stays pending, with its saved state intact,
while other topics recover. Ordinary messages and attachments do not create a
replacement conversation or consume its unread output. The topic owner can
use `/recover` after fixing the cause. `/unbind` deliberately cancels recovery
and allows a new conversation; `/kill` also stops its recovery window. These
operations preserve the owner record and conversation files.

Previously closed or explicitly unbound topics are not resurrected from orphan
window records or transcript files. Recovery requires a saved topic binding or
pending recovery record; it never guesses the original topic from its name.

If tmux disappears while the bot keeps running, the dispatch and polling paths
preserve lost bindings before using new window IDs. Use `/recover` in each
affected topic, or restart the bot later to retry all pending topics.

An existing `agrinas.4` state is accepted without a separate migration file.
Legacy windows are retained only when their tmux lifetime, ID, name, directory,
and Claude process match. Invalid JSON stops startup instead of overwriting
saved sessions. Back up `state.json`, `session_map.json`, `monitor_state.json`,
and the original Claude transcripts before activation. Older CCBot versions
ignore pending recovery records, so a downgrade must restore the matching
pre-upgrade state backup while the bot is stopped.

## Telegram files and Claude instructions

Incoming Telegram documents are saved in a private topic inbox. Their local
paths and captions are forwarded to Claude. Files may be up to 20,000,000 bytes.
Existing photo and voice handlers remain available.

Outgoing files are sent explicitly from inside the topic's CCBot tmux pane:

```bash
ccbot files
ccbot send-file "/absolute/topic/outbox/report.csv" --caption "Requested report"
```

`ccbot files` returns the topic's inbox and outbox paths as JSON. The directories
live under `$CCBOT_DIR/files/<chat_id>/<topic_id>/`. Save the requested file
directly in that outbox; there is no automatic upload watcher. CSV, PDF, images,
spreadsheets, archives, and other regular files use the same helper, subject to
the 50,000,000-byte upload limit. Empty files, symlinks, hard links, and paths
outside the topic's outbox are rejected. The helper requires one unambiguous
named-topic binding and a recorded owner who is still allowed.

Successful delivery returns JSON containing `"sent": true` and the Telegram
message ID. An ambiguous network failure is not retried automatically; check the
topic before retrying to avoid duplicate uploads.

Copy [telegram-files-CLAUDE.md](telegram-files-CLAUDE.md) to
`~/.ccbot/CLAUDE.md`, then add this import to the existing
`~/.claude/CLAUDE.md` without replacing its other instructions:

```text
@~/.ccbot/CLAUDE.md
```

Use the actual configuration path when overriding `CCBOT_DIR`. Start a new
Claude session after changing its instructions. The example tells Claude to
create and send the requested deliverable, use the helper's resolved destination,
and report success only after confirmed delivery. The repository's root
`CLAUDE.md` contains developer instructions for CCBot itself; it is a separate
file from these runtime instructions.

## Existing installations

Changing `CCBOT_CHAT_SCOPED_TOPICS` changes the meaning of saved routing keys.
CCBot rejects a routing scope that differs from `state.json`; it does not guess
a migration. Stop the bot and back up its configuration and state first. Either
migrate bindings using their verified Telegram chat IDs, or archive the old
state and initialize fresh bindings. Preserve the original state to roll back.
Do not simply relabel the saved `routing_scope` field.

Old topics without owner records are claimed by the first allowed sender when
owner locking is enabled. If ownership must be preserved, populate verified
owner records while the bot is stopped before allowing new messages.

## Validation

Run the same checks as CI:

```bash
uv sync --all-extras
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/ccbot/
uv run pytest --tb=short -q
```

Regression tests cover cross-group routing, General-topic dispatch, ownership
and callbacks, file boundaries and Telegram transport errors, hook installation,
and fixed-workspace creation and retry behavior. Recovery tests cover cold
startup, bot crashes during launch, reused IDs, failed topics, and retained
owners and offsets. An integration test uses a unique tmux socket and a fake
Claude CLI that invokes the real hook, then destroys only that test server to
simulate a reboot. Telegram transport is mocked; the suite does not send real
messages or attachments. Live Claude resume and an actual VM reboot have not
been exercised for this development update.

Ruff's original `E4`, `E7`, `E9`, and `F` rule selection is explicit in
`pyproject.toml`, following the
[Ruff 0.16 migration guidance](https://astral.sh/blog/ruff-v0.16.0) after its
default rule set expanded. This keeps the project's previous lint scope stable
across tool upgrades.
