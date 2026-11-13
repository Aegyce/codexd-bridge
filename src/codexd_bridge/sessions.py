"""Provider-scoped operations; model credentials remain with each daemon."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import Backend, ConfigError, load_backends
from .delegation import DelegationGuard, SessionRef
from .transport import AppServer, BridgeError


def nonempty(value: str, field: str, limit: int = 128) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise ValueError(f"{field} must contain 1..{limit} UTF-8 bytes")
    return value


def selection(model: str | None, effort: str | None) -> dict:
    result = {}
    if model is not None:
        result["model"] = nonempty(model, "model")
    if effort is not None:
        result["effort"] = nonempty(effort, "effort")
    return result


def prompt_input(prompt: str) -> list[dict]:
    return [{"type": "text", "text": nonempty(prompt, "prompt", 16_384)}]


def require_provider(backend: Backend, thread: dict) -> dict:
    if thread.get("modelProvider") != backend.model_provider:
        raise BridgeError(f"Backend {backend.name}: refusing a session from another provider")
    return thread


def metadata(client: AppServer, backend: Backend, thread_id: str) -> dict:
    nonempty(thread_id, "thread_id")
    return require_provider(
        backend,
        client.call(
            "thread/read",
            {
                "threadId": thread_id,
                "includeTurns": False,
            },
        )["thread"],
    )


def require_idle(thread: dict) -> None:
    status = thread.get("status")
    state = status.get("type") if isinstance(status, dict) else None
    if state not in ("idle", "notLoaded"):
        raise BridgeError("Session is not idle; read its current turn ID and use steer_session")


def validate_selection(client: AppServer, chosen: dict) -> None:
    """Use the selected daemon's catalog, including provider-specific effort values."""
    if "model" not in chosen:
        return
    cursor = None
    for _ in range(20):
        page = client.call("model/list", {"limit": 100, "cursor": cursor, "includeHidden": True})
        for model in page["data"]:
            if chosen["model"] not in (model.get("id"), model.get("model")):
                continue
            efforts = {option["reasoningEffort"] for option in model["supportedReasoningEfforts"]}
            if "effort" in chosen and chosen["effort"] not in efforts:
                raise BridgeError(
                    "Selected model does not advertise this reasoning effort; use list_models"
                )
            return
        cursor = page.get("nextCursor")
        if not cursor:
            raise BridgeError(
                "Model is absent from this backend's catalog; use list_models or update its catalog"
            )
    raise BridgeError("Model catalog pagination limit reached; no task was started")


def summary(backend: Backend, thread: dict) -> dict:
    require_provider(backend, thread)
    return {
        "backend": backend.name,
        **{
            key: thread.get(key)
            for key in (
                "id",
                "name",
                "modelProvider",
                "model",
                "reasoningEffort",
                "status",
                "cwd",
                "createdAt",
                "updatedAt",
            )
        },
    }


def text_messages(items: list[dict], budget: int) -> tuple[list[dict], int]:
    """Allocate the budget newest-first; hide tool output and private reasoning."""
    messages = []
    for item in items:
        if budget == 0:
            break
        if item.get("type") not in ("agentMessage", "userMessage"):
            continue
        text = item.get("text")
        if text is None:
            text = "\n".join(
                part.get("text", "")
                for part in item.get("content", [])
                if part.get("type") == "text"
            )
        if not text:
            continue
        displayed = text[:budget]
        budget -= len(displayed)
        messages.append(
            {
                "id": item.get("id"),
                "type": item["type"],
                "phase": item.get("phase"),
                "text": displayed,
                "truncated": len(displayed) < len(text),
            }
        )
    messages.reverse()
    return messages, budget


class Bridge:
    def __init__(
        self, config_path: Path | None = None, connector: Callable = AppServer,
        *, guard: DelegationGuard | None = None,
    ):
        self.config_path = config_path
        self.connector = connector
        self.guard = guard if guard is not None else DelegationGuard(config_path, connector=connector)

    def backend(self, name: str, *, require_enabled: bool = False) -> Backend:
        backends = load_backends(self.config_path)
        if name not in backends:
            raise ConfigError("Unknown backend; use list_backends to select a configured backend")
        if require_enabled and not backends[name].enabled:
            raise BridgeError(
                "Backend is disabled; verify its model and connect it before delegating"
            )
        return backends[name]

    def list_backends(self) -> dict:
        """List configured routes without starting or connecting to any daemon."""
        return {
            "backends": [
                {
                    "name": b.name,
                    "model_provider": b.model_provider,
                    "default_model": b.default_model,
                    "default_effort": b.default_effort,
                    "enabled": b.enabled,
                    "home_exists": b.home.is_dir(),
                    "socket_exists": b.socket_path.exists(),
                }
                for b in load_backends(self.config_path).values()
            ]
        }

    def list_models(self, backend: str, cursor: str | None = None, limit: int = 30) -> dict:
        """Read the backend's model catalog and supported reasoning efforts."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        target = self.backend(backend)
        return self._model_page(target, cursor, limit)

    def _model_page(self, target: Backend, cursor: str | None, limit: int) -> dict:
        with self.connector(target.home) as client:
            page = client.call("model/list", {"limit": limit, "cursor": cursor})
        keys = (
            "id",
            "model",
            "displayName",
            "description",
            "modelSpecialty",
            "isDefault",
            "defaultReasoningEffort",
            "supportedReasoningEfforts",
        )
        return {
            "backend": target.name,
            "model_provider": target.model_provider,
            "enabled": target.enabled,
            "source": "app-server/model/list",
            "availability_checked": False,
            "models": [{k: m.get(k) for k in keys} for m in page["data"]],
            "next_cursor": page.get("nextCursor"),
        }

    def list_provider_models(self, limit_per_backend: int = 20) -> dict:
        """Dynamically query each provider's daemon; advertised models are not inference proof."""
        if not 1 <= limit_per_backend <= 100:
            raise ValueError("limit_per_backend must be 1..100")
        targets = load_backends(self.config_path)
        results = {}
        pool = ThreadPoolExecutor(max_workers=min(8, len(targets)))
        futures = {
            pool.submit(self._model_page, target, None, limit_per_backend): target
            for target in targets.values()
        }
        try:
            for future in as_completed(futures, timeout=45):
                target = futures[future]
                try:
                    results[target.name] = {**future.result(), "reachable": True}
                except Exception as error:
                    results[target.name] = {
                        "backend": target.name,
                        "model_provider": target.model_provider,
                        "enabled": target.enabled,
                        "reachable": False,
                        "error": str(error)[:500],
                    }
        except TimeoutError:
            pass
        finally:
            for future, target in futures.items():
                if target.name not in results:
                    future.cancel()
                    results[target.name] = {
                        "backend": target.name,
                        "model_provider": target.model_provider,
                        "enabled": target.enabled,
                        "reachable": False,
                        "error": "Model discovery timed out; query this backend separately",
                    }
            pool.shutdown(wait=False, cancel_futures=True)
        return {"providers": [results[name] for name in targets]}

    def list_sessions(self, backend: str, cursor: str | None = None, limit: int = 20) -> dict:
        """List sessions for one explicitly selected backend and provider."""
        if not 1 <= limit <= 50:
            raise ValueError("limit must be 1..50")
        target = self.backend(backend)
        with self.connector(target.home) as client:
            page = client.call(
                "thread/list",
                {
                    "modelProviders": [target.model_provider],
                    "sourceKinds": [],
                    "sortKey": "updated_at",
                    "limit": limit,
                    "cursor": cursor,
                },
            )
        return {
            "sessions": [summary(target, t) for t in page["data"]],
            "next_cursor": page.get("nextCursor"),
        }

    def read_session(
        self,
        backend: str,
        thread_id: str,
        turn_limit: int = 2,
        text_limit: int = 6000,
        cursor: str | None = None,
    ) -> dict:
        """Read status and recent messages without resuming or interrupting the session."""
        if not 1 <= turn_limit <= 5 or not 256 <= text_limit <= 16_384:
            raise ValueError("turn_limit must be 1..5; text_limit must be 256..16384")
        target = self.backend(backend)
        with self.connector(target.home) as client:
            thread = metadata(client, target, thread_id)
            page = client.call(
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "limit": turn_limit,
                    "sortDirection": "desc",
                    "itemsView": "summary",
                    "cursor": cursor,
                },
            )
            # A persisted summary may call a running turn "interrupted" before
            # its completion is flushed. Resolve that ambiguity from live state.
            live_turns = {}
            if thread.get("status", {}).get("type") == "active" and any(
                turn.get("status") == "interrupted" for turn in page["data"]
            ):
                live = require_provider(
                    target,
                    client.call("thread/read", {"threadId": thread_id, "includeTurns": True})[
                        "thread"
                    ],
                )
                live_turns = {turn["id"]: turn for turn in live.get("turns", [])}
            turns = []
            budget = text_limit
            for turn in page["data"]:
                messages = []
                item_cursor = None
                if budget:
                    entries = client.call(
                        "thread/items/list",
                        {
                            "threadId": thread_id,
                            "turnId": turn["id"],
                            "limit": 24,
                            "sortDirection": "desc",
                        },
                    )
                    items = [entry["item"] for entry in entries["data"]]
                    messages, budget = text_messages(items, budget)
                    item_cursor = entries.get("nextCursor")
                    if not messages:
                        messages, budget = text_messages(
                            list(reversed(turn.get("items", []))), budget
                        )
                current = live_turns.get(turn["id"], turn)
                error = current.get("error")
                turns.append(
                    {
                        "id": turn["id"],
                        "status": current["status"],
                        "error": str(error.get("message", ""))[:1000]
                        if isinstance(error, dict)
                        else None,
                        "messages": messages,
                        "older_items_cursor": item_cursor,
                    }
                )
        return {
            "session": summary(target, thread),
            "turns": turns,
            "text_limit_reached": budget == 0,
            "next_cursor": page.get("nextCursor"),
        }

    def read_messages(
        self,
        backend: str,
        thread_id: str,
        turn_id: str,
        cursor: str | None = None,
        limit: int = 24,
        text_limit: int = 6000,
    ) -> dict:
        """Page through a turn's messages; tool output and private reasoning are omitted."""
        nonempty(turn_id, "turn_id")
        if not 1 <= limit <= 100 or not 256 <= text_limit <= 16_384:
            raise ValueError("limit must be 1..100; text_limit must be 256..16384")
        target = self.backend(backend)
        with self.connector(target.home) as client:
            metadata(client, target, thread_id)
            page = client.call(
                "thread/items/list",
                {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "cursor": cursor,
                    "limit": limit,
                    "sortDirection": "desc",
                },
            )
        messages, budget = text_messages([e["item"] for e in page["data"]], text_limit)
        return {
            "backend": backend,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "messages": messages,
            "text_limit_reached": budget == 0,
            "next_cursor": page.get("nextCursor"),
        }

    @staticmethod
    def started(target: Backend, response: dict, requested: dict) -> dict:
        thread = response["thread"]
        thread_id = thread.get("id", "unknown")
        if (
            thread.get("modelProvider") != target.model_provider
            or response.get("modelProvider") != target.model_provider
        ):
            raise BridgeError(
                f"Session {thread_id}: backend returned a different provider; no turn started"
            )
        for supplied, returned in (("model", "model"), ("effort", "reasoningEffort")):
            if supplied in requested and response.get(returned) != requested[supplied]:
                raise BridgeError(
                    f"Session {thread_id}: requested {supplied} was not applied; no turn started"
                )
        return response

    @staticmethod
    def start_turn(
        client: AppServer,
        target: Backend,
        response: dict,
        inputs: list[dict],
        title: str | None = None,
    ) -> dict:
        thread_id = response["thread"]["id"]
        step = "name session" if title is not None else "start turn"
        try:
            # A caller can specify only an effort, leaving the daemon to choose
            # its default model. Validate the effective pair before inference.
            validate_selection(
                client, selection(response.get("model"), response.get("reasoningEffort"))
            )
            if title is not None:
                client.call("thread/name/set", {"threadId": thread_id, "name": title})
            step = "start turn"
            params = {"threadId": thread_id, "input": inputs, "model": response["model"]}
            if response.get("reasoningEffort") is not None:
                params["effort"] = response["reasoningEffort"]
            turn = client.call("turn/start", params)["turn"]
            turn_id, status = turn["id"], turn["status"]
        except Exception as error:
            raise BridgeError(
                f"Backend {target.name}, session {thread_id}: failed to {step}. "
                f"Inspect this session before retrying. {error}"
            ) from error
        return {
            "backend": target.name,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "model_provider": response["modelProvider"],
            "model": response["model"],
            "effort": response.get("reasoningEffort"),
            "status": status,
        }

    def create_session(
        self,
        backend: str,
        prompt: str,
        title: str,
        cwd: str,
        model: str | None = None,
        effort: str | None = None,
        sandbox: str = "workspace-write",
    ) -> dict:
        """Create a task on the selected provider; optionally choose model and reasoning effort."""
        inputs = prompt_input(prompt)
        nonempty(title, "title", 256)
        directory = Path(cwd).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError("cwd must be an existing absolute directory")
        if sandbox not in ("read-only", "workspace-write"):
            raise ValueError("sandbox must be read-only or workspace-write")
        target = self.backend(backend, require_enabled=True)
        caller = self.guard.caller()
        self.guard.check(caller)
        chosen = selection(
            model if model is not None else target.default_model,
            effort if effort is not None else target.default_effort,
        )
        params = {
            "modelProvider": target.model_provider,
            "cwd": str(directory),
            "approvalPolicy": "never",
            "sandbox": sandbox,
            "config": {"model_provider": target.model_provider},
            "serviceName": "codexd-bridge",
        }
        if "model" in chosen:
            params["model"] = chosen["model"]
        if "effort" in chosen:
            params["config"]["model_reasoning_effort"] = chosen["effort"]
        with self.connector(target.home) as client:
            validate_selection(client, chosen)
            response = self.started(target, client.call("thread/start", params), chosen)
            self._record_created(client, caller, target, response)
            return self.start_turn(client, target, response, inputs, title)

    def _record_created(
        self, client, caller: SessionRef, target: Backend, response: dict,
        *, fork_from: SessionRef | None = None,
    ) -> None:
        thread_id = response["thread"]["id"]
        try:
            self.guard.record(caller, SessionRef(target.home, thread_id), fork_from=fork_from)
        except BridgeError as error:
            try:
                client.call("thread/delete", {"threadId": thread_id})
            except BridgeError as cleanup:
                raise BridgeError(
                    f"Session {thread_id} was not started; delegation recording failed and "
                    f"cleanup failed. Inspect this session. {cleanup}"
                ) from error
            raise

    def send_message(
        self,
        backend: str,
        thread_id: str,
        prompt: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict:
        """Continue an idle session, retaining its model/effort unless explicitly overridden."""
        inputs = prompt_input(prompt)
        chosen = selection(model, effort)
        target = self.backend(backend, require_enabled=True)
        caller = self.guard.caller()
        with self.connector(target.home) as client:
            thread = metadata(client, target, thread_id)
            require_idle(thread)
            chosen = self.inherited_selection(thread, chosen)
            validate_selection(client, chosen)
            params = self.resume_params(target, thread_id, chosen)
            self.guard.record(caller, SessionRef(target.home, thread_id))
            response = self.started(target, client.call("thread/resume", params), chosen)
            require_idle(response["thread"])
            return self.start_turn(client, target, response, inputs)

    @staticmethod
    def inherited_selection(thread: dict, chosen: dict) -> dict:
        # Explicit provider overrides can reapply daemon defaults on resume.
        # Carry the persisted per-thread selection forward instead.
        for key, field in (("model", "model"), ("effort", "reasoningEffort")):
            if key not in chosen and thread.get(field) is None:
                raise BridgeError(
                    f"Original {key} is unavailable; supply it explicitly to continue"
                )
        inherited = selection(thread.get("model"), thread.get("reasoningEffort"))
        return inherited | chosen

    @staticmethod
    def resume_params(target: Backend, thread_id: str, chosen: dict) -> dict:
        params = {
            "threadId": thread_id,
            "modelProvider": target.model_provider,
            "excludeTurns": True,
        }
        if "model" in chosen:
            params["model"] = chosen["model"]
        if "effort" in chosen:
            params["config"] = {"model_reasoning_effort": chosen["effort"]}
        return params

    def fork_session(
        self,
        backend: str,
        thread_id: str,
        prompt: str,
        title: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict:
        """Fork within the same provider; inherit model/effort unless explicitly overridden."""
        inputs = prompt_input(prompt)
        nonempty(title, "title", 256)
        chosen = selection(model, effort)
        target = self.backend(backend, require_enabled=True)
        caller = self.guard.caller()
        source = SessionRef(target.home, thread_id)
        self.guard.check(caller, source)
        with self.connector(target.home) as client:
            thread = metadata(client, target, thread_id)
            chosen = self.inherited_selection(thread, chosen)
            validate_selection(client, chosen)
            response = self.started(
                target,
                client.call("thread/fork", self.resume_params(target, thread_id, chosen)),
                chosen,
            )
            self._record_created(client, caller, target, response, fork_from=source)
            return self.start_turn(client, target, response, inputs, title)

    def steer_session(
        self, backend: str, thread_id: str, expected_turn_id: str, prompt: str
    ) -> dict:
        """Add instructions to a specific active turn without changing its provider or model."""
        inputs = prompt_input(prompt)
        nonempty(expected_turn_id, "expected_turn_id")
        target = self.backend(backend, require_enabled=True)
        caller = self.guard.caller()
        with self.connector(target.home) as client:
            metadata(client, target, thread_id)
            self.guard.record(caller, SessionRef(target.home, thread_id))
            result = client.call(
                "turn/steer",
                {"threadId": thread_id, "expectedTurnId": expected_turn_id, "input": inputs},
            )
        return {
            "backend": backend,
            "thread_id": thread_id,
            "model_provider": target.model_provider,
            "accepted": True,
            "result": result,
        }
