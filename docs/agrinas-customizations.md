# Agrinas CCBot customizations

Version `0.1.0+agrinas.4` extends `ifadhf/ccbot` at
`f2019b96081a0a7aa75a4478a1ef97c7a26ca84f` (`local-patches`). It supports private
groups for individual users and a shared group, with an independent Claude
session for each named Telegram topic.

## Install and configure

Install this branch using the same account that runs tmux and Claude Code:

```bash
git clone --branch feat/agrinas-topics-files-workspace https://github.com/ifadhf/ccbot.git
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
and fixed-workspace creation and retry behavior. Telegram transport is mocked;
the suite does not send real messages or attachments.

Ruff's original `E4`, `E7`, `E9`, and `F` rule selection is explicit in
`pyproject.toml`, following the
[Ruff 0.16 migration guidance](https://astral.sh/blog/ruff-v0.16.0) after its
default rule set expanded. This keeps the project's previous lint scope stable
across tool upgrades.
