"""Real-inference onboarding verification on a disposable probe thread.

'check_model' proves that a backend can actually run inference: catalog
presence alone is never treated as success. It bypasses the 'Backend.enabled'
gate on purpose (verification happens before a backend is enabled), but it never
enables a backend, edits the registry, or reads credentials. Exactly one
temporary thread is created on the target daemon and it is always deleted again
on that same daemon, whether verification succeeds, fails, or is interrupted. Inference is never
retried automatically, and results expose no reasoning text or tool output.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

from .config import Backend
from .sessions import Bridge, nonempty, prompt_input, selection, validate_selection
from .transport import AppServer, BridgeError

__all__ = ["PROBE_MARKER", "ProbeError", "check_model"]

PROBE_MARKER = "CODEXD_BRIDGE_PROBE_OK"
PROBE_THREAD_NAME = "codexd-bridge temporary model probe (safe to delete)"
PROBE_PROMPT = (
    "This is an automated codexd-bridge connectivity probe. "
    "Do not call any tools, run any commands, or read, create, modify, or delete any files. "
    f"Reply with exactly this marker and nothing else: {PROBE_MARKER}"
)

_POLL_INTERVAL = 0.5
_TERMINAL_TURN_STATES = frozenset(
    {"completed", "failed", "interrupted", "cancelled", "canceled", "error"}
)

# Indirection so tests can mock the clock without waiting in real time.
_clock = time.monotonic
_sleep = time.sleep


class ProbeError(BridgeError):
    """Model verification failed; no automatic retry or provider fallback."""


class _TurnState:
    """Cleanup bookkeeping for the one turn this probe started itself."""

    __slots__ = ("turn_id", "done")

    def __init__(self) -> None:
        self.turn_id: str | None = None
        self.done = False


def check_model(
    backend: Backend,
    *,
    model: str | None = None,
    effort: str | None = None,
    timeout: float = 60,
    client_factory=AppServer,
) -> dict:
    """Verify real inference on 'backend' and delete the temporary thread.

    'model'/'effort' fall back to the backend defaults; anything still unset is
    resolved by the target daemon and validated against its catalog before the
    turn starts. 'timeout' bounds the wait for the turn to complete. Returns
    secret-free evidence (provider, resolved model/effort, UTC 'checked_at',
    temporary 'thread_id', 'deleted') only after the marker reply was observed,
    the turn completed, and cleanup succeeded.
    """
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number of seconds")
    chosen = selection(
        model if model is not None else backend.default_model,
        effort if effort is not None else backend.default_effort,
    )
    try:
        with client_factory(backend.home) as client:
            return _probe(client, backend, chosen, float(timeout))
    except ProbeError:
        raise
    except BridgeError as error:
        raise ProbeError(f"Backend {backend.name}: {error}") from error


def _creation_unknown(backend: Backend, error: Exception | None) -> str:
    detail = f" {error}" if error is not None else ""
    return (
        f"Backend {backend.name}: thread/start did not return a usable thread ID; "
        f"the outcome is unknown and no cleanup was attempted.{detail} "
        "Inspect this backend's session list (list_sessions) for a leftover "
        f"'{PROBE_THREAD_NAME}' thread and delete it on that daemon if present."
    )


def _probe(client: AppServer, backend: Backend, chosen: dict, timeout: float) -> dict:
    validate_selection(client, chosen)
    params = {
        "modelProvider": backend.model_provider,
        "cwd": str(backend.home),
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "config": {"model_provider": backend.model_provider},
        "serviceName": "codexd-bridge-probe",
    }
    if "model" in chosen:
        params["model"] = chosen["model"]
    if "effort" in chosen:
        params["config"]["model_reasoning_effort"] = chosen["effort"]
    try:
        response = client.call("thread/start", params)
    except BridgeError as error:
        raise ProbeError(_creation_unknown(backend, error)) from error
    thread = response.get("thread") if isinstance(response, dict) else None
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ProbeError(_creation_unknown(backend, None))

    state = _TurnState()
    try:
        evidence = _verify_turn(client, backend, response, chosen, thread_id, timeout, state)
    except BaseException as error:
        problems = _cleanup(client, thread_id, state)
        if problems:
            raise ProbeError(
                f"{error} Cleanup failed for temporary thread {thread_id} on backend "
                f"{backend.name}: {'; '.join(problems)}. Delete it manually on that daemon."
            ) from error
        raise
    problems = _cleanup(client, thread_id, state)
    if problems:
        raise ProbeError(
            f"Backend {backend.name}: the probe reply was correct but temporary thread "
            f"{thread_id} could not be cleaned up: {'; '.join(problems)}. "
            "Delete it manually on that daemon."
        )
    return {
        "available": True,
        "backend": backend.name,
        "provider": backend.model_provider,
        "model": evidence["model"],
        "effort": evidence["effort"],
        "checked_at": evidence["checked_at"],
        "thread_id": thread_id,
        "turn_id": evidence["turn_id"],
        "deleted": True,
    }


def _verify_turn(
    client: AppServer,
    backend: Backend,
    response: dict,
    chosen: dict,
    thread_id: str,
    timeout: float,
    state: _TurnState,
) -> dict:
    where = f"Backend {backend.name}, temporary thread {thread_id}"
    try:
        Bridge.started(backend, response, chosen)
        effective = selection(response.get("model"), response.get("reasoningEffort"))
        if "model" not in effective:
            raise BridgeError("the daemon did not resolve a model for the probe")
        # Covers effort-only requests: validate the daemon-resolved pair too.
        validate_selection(client, effective)
    except (BridgeError, ValueError) as error:
        raise ProbeError(f"{where}: {error}") from error

    try:
        client.call("thread/name/set", {"threadId": thread_id, "name": PROBE_THREAD_NAME})
    except BridgeError:
        pass  # Naming is cosmetic; verification proceeds without it.

    params = {
        "threadId": thread_id,
        "input": prompt_input(PROBE_PROMPT),
        "model": effective["model"],
    }
    if "effort" in effective:
        params["effort"] = effective["effort"]
    try:
        turn = client.call("turn/start", params)["turn"]
        turn_id = nonempty(turn["id"], "turn_id")
        status = turn.get("status")
    except (BridgeError, KeyError, TypeError, ValueError) as error:
        raise ProbeError(f"{where}: turn/start failed; no inference result. {error}") from error
    state.turn_id = turn_id
    state.done = status in _TERMINAL_TURN_STATES

    deadline = _clock() + timeout
    while True:
        try:
            # Persisted turn summaries can report "interrupted" while the live
            # turn is still running. This disposable thread has only one turn;
            # read its authoritative live state, including the final reply.
            live = client.call("thread/read", {"threadId": thread_id, "includeTurns": True})
        except BridgeError as error:
            raise ProbeError(f"{where}: cannot read turn {turn_id}. {error}") from error
        thread = live.get("thread", {}) if isinstance(live, dict) else {}
        turn = _own_turn(thread, turn_id)
        if turn is not None:
            status = turn.get("status")
            if status in _TERMINAL_TURN_STATES:
                state.done = True
            failure = turn.get("error")
            if failure or (status in _TERMINAL_TURN_STATES and status != "completed"):
                detail = ""
                if isinstance(failure, dict) and isinstance(failure.get("message"), str):
                    detail = f": {failure['message'][:300]}"
                raise ProbeError(f"{where}: turn {turn_id} ended with status {status!r}{detail}")
            if status == "completed":
                if _has_marker_reply(turn):
                    return {
                        "model": effective["model"],
                        "effort": effective.get("effort"),
                        "turn_id": turn_id,
                        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    }
                raise ProbeError(
                    f"{where}: turn {turn_id} completed without the exact marker reply "
                    "(empty, refusal, or wrong answer)"
                )
        if _clock() >= deadline:
            raise ProbeError(
                f"{where}: timed out after {timeout:g}s waiting for turn {turn_id} to complete"
            )
        _sleep(min(_POLL_INTERVAL, max(0.0, deadline - _clock())))


def _own_turn(thread: dict, turn_id: str) -> dict | None:
    """Only the turn this probe started; other threads' turns are never used."""
    data = thread.get("turns") if isinstance(thread, dict) else None
    if not isinstance(data, list):
        return None
    for turn in data:
        if isinstance(turn, dict) and turn.get("id") == turn_id:
            return turn
    return None


def _has_marker_reply(turn: dict) -> bool:
    items = turn.get("items")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            content = item.get("content")
            text = "\n".join(
                part["text"]
                for part in (content if isinstance(content, list) else [])
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            )
        if text.strip() == PROBE_MARKER:
            return True
    return False


def _cleanup(client: AppServer, thread_id: str, state: _TurnState) -> list[str]:
    """Interrupt our own still-running turn, then delete our own thread.

    An interrupt problem only matters when the delete also fails; a successful
    delete removes the thread regardless. Delete errors are always reported.
    """
    interrupt_problem = None
    if state.turn_id is not None and not state.done:
        try:
            client.call("turn/interrupt", {"threadId": thread_id, "turnId": state.turn_id})
        except Exception as error:
            interrupt_problem = f"turn/interrupt failed: {error}"
    try:
        client.call("thread/delete", {"threadId": thread_id})
    except Exception as error:
        problems = [f"thread/delete failed: {error}"]
        if interrupt_problem:
            problems.insert(0, interrupt_problem)
        return problems
    return []
