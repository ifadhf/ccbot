"""Exercise file routing, attachment bytes, and filesystem rejection boundaries."""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ccbot.bot as bot_module
import ccbot.file_transfer as transfer
import ccbot.hook as hook_module
from ccbot.config import config


@pytest.fixture
def file_topic(monkeypatch, tmp_path):
    monkeypatch.setattr(transfer, "ccbot_dir", lambda: tmp_path)
    monkeypatch.setattr(
        hook_module, "_get_tmux_window", lambda: ("ccbot", "@1", "project")
    )
    (tmp_path / ".env").write_text("ALLOWED_USERS=111\nTMUX_SESSION_NAME=ccbot\n")
    state = {
        "thread_bindings": {"-10011": {"42": "@1"}},
        "group_chat_ids": {"-10011:42": -10011},
        "topic_owners": {"-10011:42": {"user_id": 111, "username": "owner"}},
    }
    (tmp_path / "state.json").write_text(json.dumps(state))
    return tmp_path, state


@pytest.mark.asyncio
async def test_upload_sends_csv_bytes_to_bound_topic(file_topic, monkeypatch):
    outbox = transfer.topic_directories(-10011, 42)["outbox"]
    path = outbox / "spec.csv"
    path.write_bytes(b"cpu,ram\n2,4GB\n")
    api = SimpleNamespace(
        send_document=AsyncMock(return_value=SimpleNamespace(message_id=77))
    )
    context = AsyncMock()
    context.__aenter__.return_value = api
    monkeypatch.setattr(transfer, "Bot", lambda **kwargs: context)
    monkeypatch.setattr(hook_module, "_read_bot_token", lambda _: "test-token")
    assert await transfer.send_file(path, "Specs") == 77
    sent = api.send_document.call_args.kwargs
    assert sent["chat_id"] == -10011 and sent["message_thread_id"] == 42
    assert sent["document"].getvalue() == path.read_bytes()
    assert sent["filename"] == "spec.csv" and sent["caption"] == "Specs"


@pytest.mark.parametrize(
    "kind", ["outside", "symlink", "hardlink", "oversize", "empty", "fifo"]
)
def test_disallowed_uploads_never_read_secrets(file_topic, kind):
    tmp_path, _ = file_topic
    outbox = transfer.topic_directories(-10011, 42)["outbox"]
    outside = tmp_path / "secret.env"
    outside.write_text("private")
    path = outbox / "data.csv"
    if kind == "outside":
        path = outside
    elif kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.touch()
        if kind == "oversize":
            with path.open("wb") as f:
                f.truncate(transfer.MAX_OUTGOING + 1)
    with pytest.raises(ValueError):
        transfer.read_outgoing(path, outbox)


@pytest.mark.parametrize(
    "case", ["missing_owner", "wrong_owner", "ambiguous", "unbound", "general"]
)
def test_invalid_destination_fails_closed(file_topic, case):
    root, state = file_topic
    if case == "missing_owner":
        state["topic_owners"] = {}
    elif case == "wrong_owner":
        state["topic_owners"]["-10011:42"]["user_id"] = 999
    elif case == "ambiguous":
        state["thread_bindings"]["-10011"]["43"] = "@1"
        state["group_chat_ids"]["-10011:43"] = -10011
    elif case == "unbound":
        state["thread_bindings"] = {}
    else:
        state["thread_bindings"] = {"-10011": {"1": "@1"}}
        state["group_chat_ids"] = {"-10011:1": -10011}
    (root / "state.json").write_text(json.dumps(state))
    with pytest.raises(ValueError):
        transfer.current_topic()


@pytest.mark.asyncio
async def test_receive_keeps_bytes_with_unique_sanitized_name(file_topic):
    remote = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"x,y\n1,2\n"))
    )
    document = SimpleNamespace(
        file_size=8,
        file_name="../../secret.csv",
        get_file=AsyncMock(return_value=remote),
    )
    message = SimpleNamespace(document=document, photo=[])
    first = await transfer.receive_attachment(message, -10011, 42)
    second = await transfer.receive_attachment(message, -10011, 42)
    assert first != second
    assert first.parent == transfer.topic_directories(-10011, 42)["inbox"]
    assert first.read_bytes() == b"x,y\n1,2\n"
    assert first.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_oversized_document_is_rejected_before_network(file_topic):
    doc = SimpleNamespace(file_size=transfer.MAX_INCOMING + 1, get_file=AsyncMock())
    with pytest.raises(ValueError):
        await transfer.receive_attachment(SimpleNamespace(document=doc), -10011, 42)
    doc.get_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_document_caption_and_path_enter_normal_topic_flow(
    file_topic, monkeypatch
):
    root, _ = file_topic
    monkeypatch.setattr(config, "allowed_users", {111})
    monkeypatch.setattr(
        bot_module, "receive_attachment", AsyncMock(return_value=root / "input.csv")
    )
    forward = AsyncMock()
    monkeypatch.setattr(bot_module, "text_handler", forward)
    message = SimpleNamespace(message_thread_id=42, caption="Summarize this CSV")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        message=message,
        effective_chat=SimpleNamespace(id=-10011),
        callback_query=None,
    )
    await bot_module.document_handler(update, SimpleNamespace())
    text = forward.call_args.kwargs["forwarded_text"]
    assert "Summarize this CSV" in text
    assert str(root / "input.csv") in text
