"""Bounded message pages with continuations for partially displayed native items."""

from __future__ import annotations

import base64
import hashlib
import json

from .transport import BridgeError

PREFIX = "bridge-messages-v1:"


def visible_text(item: dict) -> str:
    if item.get("type") not in ("agentMessage", "userMessage"):
        return ""
    return item.get("text") or "\n".join(
        part.get("text", "") for part in item.get("content", []) if part.get("type") == "text"
    )


def scope_key(home, thread_id, turn_id):
    return hashlib.sha256(json.dumps([str(home.resolve()), thread_id, turn_id]).encode()).hexdigest()


def decode_cursor(cursor, scope):
    if cursor is None or not cursor.startswith(PREFIX):
        return cursor, None
    try:
        if len(cursor) > 24_000:
            raise ValueError
        state = json.loads(base64.b64decode(cursor[len(PREFIX):], altchars=b"-_", validate=True))
        if (not isinstance(state, dict) or state.get("scope") != scope
                or not isinstance(state.get("item"), str) or not state["item"]
                or type(state.get("offset")) is not int or not 0 <= state["offset"] <= 16_777_216
                or not isinstance(state.get("prefix"), str)):
            raise ValueError
        native = state["cursor"]
        if native is not None and not (isinstance(native, str) or (
            isinstance(native, dict) and native.get("type") == "item"
            and isinstance(native.get("itemId"), str)
        )):
            raise ValueError
        return native, state
    except (ValueError, KeyError, TypeError):
        raise ValueError("Invalid message cursor for this session and turn; read again without a cursor") from None


def _continuation(scope, cursor, item, offset, text):
    if not item.get("id"):
        raise BridgeError("Daemon returned a message without an ID; cannot safely continue this page")
    state = {"scope": scope, "cursor": cursor, "item": item["id"], "offset": offset,
             "prefix": hashlib.sha256(text[:offset].encode()).hexdigest()}
    return PREFIX + base64.urlsafe_b64encode(json.dumps(state, separators=(",", ":")).encode()).decode()


def message_page(page, budget, *, scope, native_cursor=None, continuation=None):
    items = [entry["item"] for entry in page["data"]]
    offset = 0
    if continuation:
        start = next((i for i, item in enumerate(items) if item.get("id") == continuation["item"]), None)
        if start is None:
            raise BridgeError("Message history changed beyond this page; read again without a cursor")
        items = items[start:]
        offset = continuation["offset"]
        text = visible_text(items[0])
        if len(text) < offset or hashlib.sha256(text[:offset].encode()).hexdigest() != continuation["prefix"]:
            raise BridgeError("Message text changed; read again without a cursor")
    messages = []
    next_cursor = page.get("nextCursor")
    previous = None
    for item in items:
        text = visible_text(item)
        if text:
            displayed = text[offset:offset + budget]
            if displayed:
                messages.append({"id": item.get("id"), "type": item["type"], "phase": item.get("phase"),
                                 "text": displayed, "text_offset": offset,
                                 "truncated": offset + len(displayed) < len(text)})
                budget -= len(displayed)
            if offset + len(displayed) < len(text):
                anchor = {"type": "item", "itemId": previous} if previous else native_cursor
                next_cursor = _continuation(scope, anchor, item, offset + len(displayed), text)
                break
        offset = 0
        previous = item.get("id")
    messages.reverse()
    return messages, budget, next_cursor
