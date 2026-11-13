"""Persist directed session calls and reject calls back to an upstream session."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import tomllib
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from .config import _resolve_config_path, delegation_depth, load_backends
from .transport import AppServer, BridgeError

request_meta: ContextVar[dict | None] = ContextVar("bridge_request_meta", default=None)
MAX_STATE_BYTES = 16 * 1024 * 1024


class ExistingSessionError(BridgeError):
    """A creation response identifies a known session; it must never be deleted."""


@dataclass(frozen=True)
class SessionRef:
    home: Path
    thread_id: str

    def __post_init__(self):
        if (not self.home.is_absolute() or not isinstance(self.thread_id, str)
                or not self.thread_id.strip() or len(self.thread_id) > 128
                or any(ord(c) < 32 for c in self.thread_id)):
            raise BridgeError("Invalid delegation session reference")

    def as_dict(self) -> dict:
        return {"home": str(self.home.resolve()), "thread_id": self.thread_id}

    @property
    def key(self) -> str:
        return json.dumps([str(self.home.resolve()), self.thread_id], separators=(",", ":"))


@dataclass
class DelegationState:
    links: dict[str, list[str]] = field(default_factory=dict)
    sessions: dict[str, dict] = field(default_factory=dict)


class DelegationGuard:
    def __init__(self, config_path: Path | None = None, *, connector=AppServer):
        self.registry = _resolve_config_path(config_path)[0].absolute()
        self.path = self.registry.resolve().with_name("delegations.json")
        self.connector = connector

    def caller(self) -> SessionRef:
        """Use daemon-injected MCP metadata, never model-supplied tool arguments."""
        home = os.environ.get("CODEXD_BRIDGE_CALLER_HOME", "")
        meta = request_meta.get()
        if not home or not Path(home).is_absolute() or not isinstance(meta, dict):
            raise BridgeError(
                "Caller identity is unavailable; reconnect this backend with codexd-bridge connect"
            )
        turn_meta = meta.get("x-codex-turn-metadata", {})
        if not isinstance(turn_meta, dict):
            raise BridgeError("Invalid caller metadata; no session operation was performed")
        identities = []
        for top, nested in (("threadId", "thread_id"), ("sessionId", "session_id")):
            values = ([meta[top]] if top in meta else []) + (
                [turn_meta[nested]] if nested in turn_meta else []
            )
            if (any(not isinstance(v, str) or not v.strip() or len(v) > 128
                    or any(ord(c) < 32 for c in v) for v in values)
                    or len(set(values)) > 1):
                raise BridgeError("Missing or conflicting caller session identity")
            identities.append(values[0] if values else None)
        thread_id, session_id = identities
        # Older daemons supply only sessionId as their thread identifier.
        thread_id = thread_id or session_id
        if thread_id is None:
            raise BridgeError("Missing or conflicting caller session identity")
        source = Path(home).resolve()
        routes = [b for b in load_backends(self.registry).values() if b.home.resolve() == source]
        if not routes or len({b.model_provider for b in routes}) != 1:
            raise BridgeError("Caller daemon must have an unambiguous route in this registry")
        with self.connector(source) as client:
            thread = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})[
                "thread"
            ]
            if thread.get("id") != thread_id or thread.get("modelProvider") != routes[0].model_provider:
                raise BridgeError("Caller session does not match the configured daemon and provider")
            if session_id is not None and session_id not in (
                thread_id, thread.get("sessionId")
            ):
                raise BridgeError("Caller session tree does not match the daemon thread")
            self.observe(client, source, thread)
        return SessionRef(source, thread_id)

    def observe(self, client, home: Path, thread: dict) -> None:
        """Retain native fork/subagent ancestry alongside bridge dispatch edges."""
        pending = [thread]
        seen = {thread["id"]}
        edges = []
        while pending:
            current = pending.pop()
            for parent_field in ("parentThreadId", "forkedFromId"):
                parent_id = current.get(parent_field)
                if parent_id is None:
                    continue
                parent = SessionRef(home, parent_id)
                edges.append((parent, SessionRef(home, current["id"])))
                if parent_id in seen:
                    continue
                if len(seen) >= 128:
                    raise BridgeError("Native session ancestry exceeds the inspection limit")
                ancestor = client.call("thread/read", {"threadId": parent_id, "includeTurns": False})["thread"]
                if (ancestor.get("id") != parent_id
                        or ancestor.get("modelProvider") != thread.get("modelProvider")):
                    raise BridgeError("Cannot verify native session ancestry")
                seen.add(parent_id)
                pending.append(ancestor)
        if edges:
            with self._locked() as state:
                links = state.links
                for parent, child in edges:
                    children = links.setdefault(parent.key, [])
                    if child.key not in children:
                        children.append(child.key)
                self._depths(links)
                self._write(state)

    def _max_depth(self) -> int:
        return delegation_depth(tomllib.loads(self.registry.read_text()))

    @staticmethod
    def _depths(links: dict[str, list[str]]) -> dict[str, int]:
        degrees = dict.fromkeys(links, 0)
        for children in links.values():
            for child in children:
                degrees[child] = degrees.get(child, 0) + 1
        ready = deque(node for node, degree in degrees.items() if degree == 0)
        depths = dict.fromkeys(degrees, 0)
        visited = 0
        while ready:
            node = ready.popleft()
            visited += 1
            for child in links.get(node, []):
                depths[child] = max(depths[child], depths[node] + 1)
                degrees[child] -= 1
                if degrees[child] == 0:
                    ready.append(child)
        if visited != len(degrees):
            raise BridgeError("Delegation would call itself or an upstream session")
        return depths

    @contextmanager
    def _locked(self):
        lock_path = self.path.with_suffix(".lock")
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                state = DelegationState()
                if self.path.exists() or self.path.is_symlink():
                    fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise BridgeError("Delegation state must be a regular file; refusing session writes")
                        raw = stream.read(MAX_STATE_BYTES + 1)
                    if len(raw) > MAX_STATE_BYTES:
                        raise BridgeError("Delegation state exceeds the size limit")
                    data = json.loads(raw)
                    if (not isinstance(data, dict) or type(data.get("version")) is not int
                            or data["version"] not in (1, 2)):
                        raise BridgeError("Unsupported delegation state; refusing session writes")
                    links = data.get("links")
                    if not isinstance(links, dict):
                        raise BridgeError("Invalid delegation state; refusing session writes")
                    for node, children in links.items():
                        if (not isinstance(children, list) or any(not isinstance(c, str)
                            for c in children) or len(children) != len(set(children))):
                            raise BridgeError("Invalid delegation links")
                        for key in (node, *children):
                            self._ref(key)
                    self._depths(links)
                    sessions = data.get("sessions", {})
                    if not isinstance(sessions, dict):
                        raise BridgeError("Invalid delegation session metadata")
                    for key, record in sessions.items():
                        ref = self._ref(key)
                        if (not isinstance(record, dict)
                                or record.get("session") != ref.as_dict()
                                or not isinstance(record.get("task_requirements"), str)
                                or not isinstance(record.get("session_type"), str)
                                or not isinstance(record.get("acceptance_criteria"), str)):
                            raise BridgeError("Invalid delegation session metadata")
                    state = DelegationState(links, sessions)
                yield state
        except (OSError, ValueError, TypeError) as error:
            raise BridgeError("Cannot read or update delegation state; refusing session writes") from error

    def _write(self, state: DelegationState) -> None:
        raw = json.dumps({"version": 2, "links": state.links, "sessions": state.sessions},
                         ensure_ascii=False, indent=2) + "\n"
        if len(raw.encode()) > MAX_STATE_BYTES:
            raise BridgeError("Delegation state exceeds the size limit")
        fd, name = tempfile.mkstemp(prefix=".delegations-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def _ref(key: str) -> SessionRef:
        value = json.loads(key)
        if not isinstance(value, list) or len(value) != 2 or not all(
            isinstance(part, str) for part in value
        ):
            raise BridgeError("Invalid delegation session reference")
        ref = SessionRef(Path(value[0]), value[1])
        if ref.key != key:
            raise BridgeError("Invalid delegation session reference")
        return ref

    @staticmethod
    def _check_source(links, caller, source):
        # Forking oneself creates a new child; it does not dispatch to oneself.
        if caller is not None and source is not None and caller.key != source.key:
            candidate = {key: list(value) for key, value in links.items()}
            if source.key not in candidate.setdefault(caller.key, []):
                candidate[caller.key].append(source.key)
            DelegationGuard._depths(candidate)

    def check(self, caller: SessionRef | None, target: SessionRef | None = None) -> None:
        """Check ancestry and depth before creating a daemon-side session."""
        with self._locked() as state:
            self._check_source(state.links, caller, target)
            depths = self._depths(state.links)
            depth = max((depths.get(ref.key, 0) for ref in (caller, target)
                         if ref is not None), default=-1)
            if depth >= self._max_depth():
                raise BridgeError("Maximum delegation depth reached; no child session was created")

    def record(self, caller: SessionRef | None, target: SessionRef, *,
               fork_from: SessionRef | None = None, assignment: dict | None = None):
        """Reserve execution and its assignment atomically, retaining ambiguous failures."""
        with self._locked() as state:
            previous_depths = self._depths(state.links)
            if assignment is not None and (
                target.key in previous_depths or target.key in state.sessions
                or any(parent is not None and target.key == parent.key for parent in (caller, fork_from))
            ):
                raise ExistingSessionError(
                    f"Daemon returned an existing session {target.thread_id}; no task started or session deleted"
                )
            self._check_source(state.links, caller, fork_from)
            for parent in (caller, fork_from):
                if parent is None:
                    continue
                children = state.links.setdefault(parent.key, [])
                if target.key not in children:
                    children.append(target.key)
            depths = self._depths(state.links)
            limit = self._max_depth()
            # Native ancestry or a lowered limit may leave an existing deep tree.
            # Reject only operations that make a path exceed the limit further.
            if any(depth > limit and depth > previous_depths.get(node, 0)
                   for node, depth in depths.items()):
                raise BridgeError("Maximum delegation depth reached; no task was started")
            record = None
            if assignment is not None:
                parent = fork_from or caller
                predecessors: dict[str, set[str]] = {}
                for node, children in state.links.items():
                    for child in children:
                        predecessors.setdefault(child, set()).add(node)
                roots, seen, pending = set(), set(), [target.key]
                while pending:
                    node = pending.pop()
                    if node in seen:
                        continue
                    seen.add(node)
                    parents = predecessors.get(node, set())
                    if not parents:
                        roots.add(node)
                    pending.extend(parents)
                record = {
                    **assignment,
                    "session": target.as_dict(),
                    "parent": parent.as_dict() if parent else None,
                    "requested_by": caller.as_dict() if caller else None,
                    "fork_source": fork_from.as_dict() if fork_from else None,
                    "root_sessions": [self._ref(key).as_dict() for key in sorted(roots)],
                    "parent_delegation_id": state.sessions.get(
                        parent.key if parent else "", {}
                    ).get("delegation_id"),
                    "depth": depths.get(target.key, 0),
                }
                state.sessions[target.key] = record
            self._write(state)
            return record

    def describe(self, refs: list[SessionRef]) -> list[dict | None]:
        """Read assignment metadata without copying ancestor task text."""
        with self._locked() as state:
            return [state.sessions.get(ref.key) for ref in refs]


class OperatorGuard(DelegationGuard):
    """Human TUI actions have no calling agent, but still retain fork ancestry."""

    def caller(self):
        return None
