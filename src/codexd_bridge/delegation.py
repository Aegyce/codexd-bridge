"""Persist directed session calls and reject calls back to an upstream session."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import tomllib
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from .config import _resolve_config_path, delegation_depth, load_backends
from .transport import AppServer, BridgeError

request_meta: ContextVar[dict | None] = ContextVar("bridge_request_meta", default=None)
MAX_STATE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class SessionRef:
    home: Path
    thread_id: str

    def __post_init__(self):
        if (not self.home.is_absolute() or not isinstance(self.thread_id, str)
                or not self.thread_id.strip() or len(self.thread_id) > 128
                or any(ord(c) < 32 for c in self.thread_id)):
            raise BridgeError("Invalid delegation session reference")

    @property
    def key(self) -> str:
        return json.dumps([str(self.home.resolve()), self.thread_id], separators=(",", ":"))


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
        values = [meta[k] for k in ("threadId", "sessionId") if k in meta]
        turn_meta = meta.get("x-codex-turn-metadata", {})
        if not isinstance(turn_meta, dict):
            raise BridgeError("Invalid caller metadata; no session operation was performed")
        values.extend(turn_meta[k] for k in ("thread_id", "session_id") if k in turn_meta)
        if (not values or any(not isinstance(v, str) or not v.strip() or len(v) > 128
                              or any(ord(c) < 32 for c in v) for v in values)
                or len(set(values)) != 1):
            raise BridgeError("Missing or conflicting caller session identity")
        source = Path(home).resolve()
        routes = [b for b in load_backends(self.registry).values() if b.home.resolve() == source]
        if not routes or len({b.model_provider for b in routes}) != 1:
            raise BridgeError("Caller daemon must have an unambiguous route in this registry")
        with self.connector(source) as client:
            thread = client.call("thread/read", {"threadId": values[0], "includeTurns": False})[
                "thread"
            ]
            if thread.get("id") != values[0] or thread.get("modelProvider") != routes[0].model_provider:
                raise BridgeError("Caller session does not match the configured daemon and provider")
            self._native_ancestry(client, source, thread)
        return SessionRef(source, values[0])

    def _native_ancestry(self, client, home: Path, thread: dict) -> None:
        """Retain native fork/subagent ancestry alongside bridge dispatch edges."""
        pending = [thread]
        seen = {thread["id"]}
        edges = []
        while pending:
            current = pending.pop()
            for field in ("parentThreadId", "forkedFromId"):
                parent_id = current.get(field)
                if parent_id is None:
                    continue
                parent = SessionRef(home, parent_id)
                edges.append((parent, SessionRef(home, current["id"])))
                if parent_id in seen:
                    continue
                if len(seen) >= 128:
                    raise BridgeError("Native session ancestry exceeds the inspection limit")
                ancestor = client.call("thread/read", {"threadId": parent_id, "includeTurns": False})["thread"]
                if ancestor.get("id") != parent_id:
                    raise BridgeError("Cannot verify native session ancestry")
                seen.add(parent_id)
                pending.append(ancestor)
        if edges:
            with self._locked() as links:
                for parent, child in edges:
                    children = links.setdefault(parent.key, [])
                    if child.key not in children:
                        children.append(child.key)
                self._depths(links)
                self._write(links)

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
                links: dict[str, list[str]] = {}
                if self.path.exists() or self.path.is_symlink():
                    fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd) as stream:
                        raw = stream.read(MAX_STATE_BYTES + 1)
                    if len(raw) > MAX_STATE_BYTES:
                        raise BridgeError("Delegation state exceeds the size limit")
                    data = json.loads(raw)
                    if not isinstance(data, dict) or data.get("version") != 1:
                        raise BridgeError("Unsupported delegation state; refusing session writes")
                    links = data.get("links")
                    if not isinstance(links, dict):
                        raise BridgeError("Invalid delegation state; refusing session writes")
                    for node, children in links.items():
                        if (not isinstance(children, list) or any(not isinstance(c, str)
                            for c in children) or len(children) != len(set(children))):
                            raise BridgeError("Invalid delegation links")
                        for key in (node, *children):
                            ref = json.loads(key)
                            if (not isinstance(ref, list) or len(ref) != 2
                                or not all(isinstance(v, str) and v for v in ref)
                                or not Path(ref[0]).is_absolute()
                                or SessionRef(Path(ref[0]), ref[1]).key != key):
                                raise BridgeError("Invalid delegation session reference")
                    self._depths(links)
                yield links
        except (OSError, ValueError, TypeError) as error:
            raise BridgeError("Cannot read or update delegation state; refusing session writes") from error

    def _write(self, links: dict[str, list[str]]) -> None:
        raw = json.dumps({"version": 1, "links": links}, ensure_ascii=False, indent=2) + "\n"
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

    def check(self, caller: SessionRef, target: SessionRef | None = None) -> None:
        """Check ancestry and depth before creating a daemon-side session."""
        with self._locked() as links:
            depths = self._depths(links)
            depth = depths.get(caller.key, 0)
            if target is not None:
                candidate = {key: list(value) for key, value in links.items()}
                if target.key not in candidate.setdefault(caller.key, []):
                    candidate[caller.key].append(target.key)
                self._depths(candidate)
                depth = max(depth, depths.get(target.key, 0))
            if depth >= self._max_depth():
                raise BridgeError("Maximum delegation depth reached; no child session was created")

    def record(self, caller: SessionRef, target: SessionRef, *, fork_from: SessionRef | None = None):
        """Reserve the call before execution; keep reservations after ambiguous failures."""
        with self._locked() as links:
            if fork_from is not None:
                candidate = {key: list(value) for key, value in links.items()}
                if fork_from.key not in candidate.setdefault(caller.key, []):
                    candidate[caller.key].append(fork_from.key)
                self._depths(candidate)
            for parent in (caller,) if fork_from is None else (caller, fork_from):
                children = links.setdefault(parent.key, [])
                if target.key not in children:
                    children.append(target.key)
            depths = self._depths(links)
            if max(depths.values(), default=0) > self._max_depth():
                raise BridgeError("Maximum delegation depth reached; no task was started")
            self._write(links)
