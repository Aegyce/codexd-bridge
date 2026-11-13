"""Transport failure tests use mock sockets and never contact a daemon."""

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from websockets.exceptions import ConnectionClosed, InvalidHandshake

from codexd_bridge.transport import AppServer, BridgeError


class RpcTests(unittest.TestCase):
    def client(self):
        client = AppServer.__new__(AppServer)
        client.next_id = 0
        client.timeout = 25
        client.socket = Mock()
        return client

    def test_server_request_with_same_id_is_not_our_response(self):
        client = self.client()
        client.socket.recv.side_effect = [
            json.dumps(
                {
                    "id": 1,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "must not execute"},
                }
            ),
            json.dumps({"method": "thread/status/changed", "params": {}}),
            json.dumps({"id": 99, "result": {"unrelated": True}}),
            json.dumps({"id": 1, "result": {"accepted": True}}),
        ]

        self.assertEqual(client.call("thread/read", {}), {"accepted": True})
        sent = [json.loads(call.args[0]) for call in client.socket.send.call_args_list]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["method"], "thread/read")
        self.assertEqual(sent[1]["id"], 1)
        self.assertEqual(sent[1]["error"]["code"], -32601)
        self.assertNotIn("result", sent[1])

    def test_disconnect_and_receive_timeout_do_not_retry_mutations(self):
        for failure in (ConnectionClosed(None, None), TimeoutError(), OSError("socket failed")):
            with self.subTest(failure=type(failure).__name__):
                client = self.client()
                client.socket.recv.side_effect = failure
                with self.assertRaisesRegex(BridgeError, "turn/start: outcome unknown"):
                    client.call("turn/start", {"threadId": "known-thread"})
                client.socket.send.assert_called_once()
                client.socket.recv.assert_called_once()

    def test_send_failure_is_ambiguous_and_not_retried(self):
        client = self.client()
        client.socket.send.side_effect = ConnectionClosed(None, None)
        with self.assertRaisesRegex(BridgeError, "outcome unknown.*inspect session"):
            client.call("thread/fork", {"threadId": "source"})
        client.socket.send.assert_called_once()
        client.socket.recv.assert_not_called()

    def test_notifications_cannot_extend_the_request_deadline(self):
        client = self.client()
        client.socket.recv.return_value = json.dumps({"method": "progress", "params": {}})
        with patch("codexd_bridge.transport.time.monotonic", side_effect=[100, 101, 126]):
            with self.assertRaisesRegex(BridgeError, "outcome unknown"):
                client.call("thread/start", {})
        client.socket.recv.assert_called_once_with(timeout=24)
        client.socket.send.assert_called_once()

    def test_invalid_json_is_reported_without_echoing_frame_contents(self):
        client = self.client()
        client.socket.recv.return_value = '{"secret-sentinel": invalid json'
        with self.assertRaises(BridgeError) as raised:
            client.call("thread/start", {})
        self.assertIn("outcome unknown", str(raised.exception))
        self.assertNotIn("secret-sentinel", str(raised.exception))

    def test_non_object_frames_are_ambiguous(self):
        for payload in ([], None, "unexpected", 42):
            with self.subTest(payload=payload):
                client = self.client()
                client.socket.recv.return_value = json.dumps(payload)
                with self.assertRaisesRegex(BridgeError, "outcome unknown.*invalid JSON-RPC frame"):
                    client.call("thread/start", {})

    def test_missing_or_non_object_results_are_ambiguous(self):
        for response in ({"id": 1}, {"id": 1, "result": None}, {"id": 1, "result": []}):
            with self.subTest(response=response):
                client = self.client()
                client.socket.recv.return_value = json.dumps(response)
                with self.assertRaisesRegex(BridgeError, "outcome unknown.*missing result"):
                    client.call("thread/start", {})

    def test_malformed_error_response_is_ambiguous(self):
        for error in ("bad", {}, {"message": None}):
            with self.subTest(error=error):
                client = self.client()
                client.socket.recv.return_value = json.dumps({"id": 1, "error": error})
                with self.assertRaisesRegex(BridgeError, "outcome unknown.*invalid error"):
                    client.call("thread/start", {})

    def test_server_rejection_is_bounded_and_not_retried(self):
        client = self.client()
        client.socket.recv.return_value = json.dumps(
            {
                "id": 1,
                "error": {
                    "code": -32600,
                    "message": "provider mismatch " + "x" * 3000,
                },
            }
        )
        with self.assertRaises(BridgeError) as raised:
            client.call("thread/resume", {})
        self.assertTrue(str(raised.exception).startswith("thread/resume: provider mismatch"))
        self.assertLessEqual(len(str(raised.exception)), len("thread/resume: ") + 2000)
        client.socket.send.assert_called_once()


class ConnectionTests(unittest.TestCase):
    def test_missing_or_invalid_home_cannot_default_to_current_directory(self):
        for hello in (
            {},
            {"codexHome": None},
            {"codexHome": ""},
            {"codexHome": "."},
            {"codexHome": 17},
        ):
            with self.subTest(hello=hello):
                socket = Mock()
                with (
                    patch("codexd_bridge.transport.unix_connect", return_value=socket),
                    patch.object(AppServer, "call", return_value=hello),
                ):
                    with self.assertRaisesRegex(BridgeError, "unexpected configuration home"):
                        AppServer(Path.cwd())
                socket.close.assert_called_once()
                socket.send.assert_not_called()

    def test_unexpected_home_closes_the_socket(self):
        socket = Mock()
        with (
            patch("codexd_bridge.transport.unix_connect", return_value=socket),
            patch.object(AppServer, "call", return_value={"codexHome": "/different-home"}),
        ):
            with self.assertRaisesRegex(BridgeError, "unexpected configuration home"):
                AppServer(Path("/expected-home"))
        socket.close.assert_called_once()
        socket.send.assert_not_called()

    def test_initialization_failure_closes_the_socket(self):
        socket = Mock()
        with (
            patch("codexd_bridge.transport.unix_connect", return_value=socket),
            patch.object(AppServer, "call", side_effect=BridgeError("initialization failed")),
        ):
            with self.assertRaisesRegex(BridgeError, "initialization failed"):
                AppServer(Path("/expected-home"))
        socket.close.assert_called_once()

    def test_connection_failures_have_a_consistent_error_type(self):
        for failure in (InvalidHandshake("invalid upgrade"), TimeoutError(), OSError("offline")):
            with self.subTest(failure=type(failure).__name__):
                with patch("codexd_bridge.transport.unix_connect", side_effect=failure) as connect:
                    with self.assertRaisesRegex(BridgeError, "Cannot connect to App Server"):
                        AppServer(Path("/expected-home"))
                connect.assert_called_once()

    def test_context_exit_closes_socket_even_when_operation_raises(self):
        socket = Mock()
        with (
            patch("codexd_bridge.transport.unix_connect", return_value=socket),
            patch.object(AppServer, "call", return_value={"codexHome": "/expected-home"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "caller failed"):
                with AppServer(Path("/expected-home")):
                    raise RuntimeError("caller failed")
        socket.close.assert_called_once()
        self.assertEqual(json.loads(socket.send.call_args.args[0]), {"method": "initialized"})


if __name__ == "__main__":
    unittest.main()
