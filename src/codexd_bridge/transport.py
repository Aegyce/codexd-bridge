"""Bounded JSON-RPC over an existing App Server's local WebSocket socket."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import unix_connect

from . import __version__


class BridgeError(RuntimeError):
    """An operation failed without an automatic retry or provider fallback."""


class AppServer:
    def __init__(self, home: Path, timeout: float = 25):
        self.home = home.resolve()
        self.timeout = timeout
        self.next_id = 0
        try:
            self.socket = unix_connect(
                str(self.home / "app-server-control/app-server-control.sock"),
                uri="ws://localhost",
                open_timeout=5,
                max_size=64 * 1024 * 1024,
            )
        except (OSError, TimeoutError, InvalidHandshake) as error:
            raise BridgeError(
                f"Cannot connect to App Server at {self.home}; start it first"
            ) from error
        try:
            hello = self.call(
                "initialize",
                {
                    "clientInfo": {"name": "codexd_bridge", "version": __version__},
                    "capabilities": {"experimentalApi": True},
                },
            )
            actual_home = hello.get("codexHome")
            if (
                not isinstance(actual_home, str)
                or not actual_home
                or not Path(actual_home).is_absolute()
                or Path(actual_home).resolve() != self.home
            ):
                raise BridgeError("App Server returned an unexpected configuration home")
            self.socket.send(json.dumps({"method": "initialized"}))
        except BaseException:
            self.socket.close()
            raise

    def call(self, method: str, params: dict | None) -> dict[str, Any]:
        self.next_id += 1
        request_id = self.next_id
        uncertain = f"{method}: outcome unknown; inspect session state before retrying"
        deadline = time.monotonic() + self.timeout
        try:
            self.socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError(uncertain)
                response = json.loads(self.socket.recv(timeout=remaining))
                if not isinstance(response, dict):
                    raise BridgeError(uncertain + "; invalid JSON-RPC frame")
                if "id" in response and "method" in response:
                    # Server requests use their own ID space. Never interpret
                    # an approval request as the response to our own request.
                    self.socket.send(
                        json.dumps(
                            {
                                "id": response["id"],
                                "error": {
                                    "code": -32601,
                                    "message": "This client does not implement interactive approvals",
                                },
                            }
                        )
                    )
                    continue
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    error = response["error"]
                    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                        raise BridgeError(uncertain + "; invalid error response")
                    raise BridgeError(f"{method}: {error['message'][:2000]}")
                if not isinstance(response.get("result"), dict):
                    raise BridgeError(uncertain + "; missing result object")
                return response["result"]
        except (OSError, TimeoutError, ConnectionClosed, json.JSONDecodeError) as error:
            raise BridgeError(uncertain) from error

    def close(self) -> None:
        self.socket.close()

    def __enter__(self) -> AppServer:
        return self

    def __exit__(self, *_args) -> None:
        self.close()
