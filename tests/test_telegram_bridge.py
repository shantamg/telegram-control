import io
import signal
import socket
import threading
import time
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import telegram_bridge
import voice_responses


VALID_TOKEN = "123456789:" + ("A" * 35)


class FakeHTTPBody:
    def __init__(self, body):
        self._payload = io.BytesIO(body)

    def read(self, size):
        return self._payload.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        return False


def http_error(body):
    return urllib.error.HTTPError(
        "https://api.telegram.org/bot/method",
        400,
        "Bad Request",
        None,
        io.BytesIO(body),
    )


class FakeChild:
    def __init__(self, stdout="", returncode=0):
        self._stdout = stdout
        self.returncode = returncode
        self.killed = False
        self.communicate_calls = 0
        self.timeout_on_first_communicate = False
        self.sent_input = None

    def communicate(self, input=None, timeout=None):
        self.communicate_calls += 1
        if self.timeout_on_first_communicate and self.communicate_calls == 1:
            raise telegram_bridge.subprocess.TimeoutExpired(
                "api-exec",
                timeout,
            )
        if input is not None:
            self.sent_input = input
        return self._stdout, ""

    def kill(self):
        self.killed = True


class PerformApiCallTests(unittest.TestCase):
    """In-process behavior of the helper's request implementation."""

    def test_success_body_is_parsed(self):
        with mock.patch.object(
            telegram_bridge.urllib.request,
            "urlopen",
            return_value=FakeHTTPBody(b'{"ok":true,"result":[1,2]}'),
        ):
            self.assertEqual(
                telegram_bridge.perform_api_call("token", "getUpdates", {}),
                [1, 2],
            )

    def test_error_description_is_reported(self):
        with mock.patch.object(
            telegram_bridge.urllib.request,
            "urlopen",
            side_effect=http_error(
                b'{"ok":false,"description":"Bad Request: chat not found"}'
            ),
        ):
            with self.assertRaisesRegex(
                telegram_bridge.BridgeError,
                "Bad Request: chat not found",
            ):
                telegram_bridge.perform_api_call(
                    "token",
                    "sendMessage",
                    {"chat_id": 1},
                )

    def test_malformed_error_bodies_fall_back(self):
        for body in (b"not json", b"[]", b'{"description":null}'):
            with mock.patch.object(
                telegram_bridge.urllib.request,
                "urlopen",
                side_effect=http_error(body),
            ):
                with self.assertRaisesRegex(
                    telegram_bridge.BridgeError,
                    "Telegram rejected the request.",
                ):
                    telegram_bridge.perform_api_call(
                        "token",
                        "sendMessage",
                        {"chat_id": 1},
                    )

    def test_malformed_success_bodies_raise_bridge_error(self):
        for body in (b"garbage", b"[]", b'{"ok":true}'):
            with mock.patch.object(
                telegram_bridge.urllib.request,
                "urlopen",
                return_value=FakeHTTPBody(body),
            ):
                with self.assertRaisesRegex(
                    telegram_bridge.BridgeError,
                    "unreadable response",
                ):
                    telegram_bridge.perform_api_call("token", "getUpdates", {})

    def test_incomplete_chunked_read_maps_to_bridge_error(self):
        class TruncatedBody(FakeHTTPBody):
            def read(self, _size):
                raise telegram_bridge.http.client.IncompleteRead(b"partial")

        with mock.patch.object(
            telegram_bridge.urllib.request,
            "urlopen",
            return_value=TruncatedBody(b""),
        ):
            with self.assertRaisesRegex(
                telegram_bridge.BridgeError,
                "Could not reach Telegram",
            ):
                telegram_bridge.perform_api_call("token", "getUpdates", {})

    def test_non_boolean_ok_is_rejected(self):
        for body in (
            b'{"ok":"false","result":7}',
            b'{"ok":1,"result":7}',
        ):
            with mock.patch.object(
                telegram_bridge.urllib.request,
                "urlopen",
                return_value=FakeHTTPBody(body),
            ):
                with self.assertRaises(telegram_bridge.BridgeError):
                    telegram_bridge.perform_api_call("token", "getUpdates", {})

    def test_reflected_token_is_redacted_from_descriptions(self):
        token = "12345:SECRETSECRETSECRET"
        body = (
            '{"ok":false,"description":"Not Found: /bot'
            + token
            + '/getMe"}'
        ).encode("utf-8")
        with mock.patch.object(
            telegram_bridge.urllib.request,
            "urlopen",
            side_effect=http_error(body),
        ):
            with self.assertRaises(telegram_bridge.BridgeError) as caught:
                telegram_bridge.perform_api_call(token, "getMe", {})
        self.assertNotIn(token, str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))

    def test_response_size_boundary_is_inclusive(self):
        body = b'{"ok": true, "result": 7}'
        padded = body + b" " * (32 - len(body))
        with mock.patch.object(telegram_bridge, "API_MAX_RESPONSE_BYTES", 32):
            with mock.patch.object(
                telegram_bridge.urllib.request,
                "urlopen",
                return_value=FakeHTTPBody(padded),
            ):
                # A body of exactly the cap is accepted...
                self.assertEqual(
                    telegram_bridge.perform_api_call("token", "getUpdates", {}),
                    7,
                )
            with mock.patch.object(
                telegram_bridge.urllib.request,
                "urlopen",
                return_value=FakeHTTPBody(padded + b" "),
            ):
                # ...and one byte more is rejected.
                with self.assertRaisesRegex(
                    telegram_bridge.BridgeError,
                    "size limit",
                ):
                    telegram_bridge.perform_api_call("token", "getUpdates", {})

    def test_voice_upload_uses_bounded_multipart_request(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            speech_dir = Path(temporary_directory) / "speech"
            speech_dir.mkdir(mode=0o700)
            voice_path = speech_dir / "reply.ogg"
            voice_path.write_bytes(b"opus-audio")
            voice_path.chmod(0o600)
            captured = {}

            def urlopen(request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return FakeHTTPBody(
                    b'{"ok":true,"result":{"message_id":42}}'
                )

            with mock.patch.object(
                voice_responses,
                "SPEECH_DIR",
                speech_dir,
            ), mock.patch.object(
                telegram_bridge.urllib.request,
                "urlopen",
                side_effect=urlopen,
            ):
                result = telegram_bridge.perform_api_call(
                    "token",
                    "sendVoice",
                    {
                        "chat_id": 123,
                        "message_thread_id": 62,
                        "__voice_file_path": str(voice_path),
                        "caption": "Lovely",
                    },
                )

        self.assertEqual(result, {"message_id": 42})
        request = captured["request"]
        self.assertTrue(
            request.headers["Content-type"].startswith(
                "multipart/form-data; boundary="
            )
        )
        self.assertIn(b'name="chat_id"\r\n\r\n123\r\n', request.data)
        self.assertIn(b'name="voice"; filename="voice.ogg"', request.data)
        self.assertIn(b"Content-Type: audio/ogg\r\n\r\nopus-audio", request.data)
        self.assertNotIn(b"__voice_file_path", request.data)

    def test_voice_file_is_rejected_for_non_voice_method(self):
        with self.assertRaisesRegex(
            telegram_bridge.BridgeError,
            "only be used with sendVoice",
        ):
            telegram_bridge.perform_api_call(
                "token",
                "sendMessage",
                {"__voice_file_path": "/tmp/not-used.ogg"},
            )

    def test_chat_photo_upload_uses_bounded_multipart_request(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            photo_path = Path(temporary_directory) / "icon.png"
            photo_path.write_bytes(b"\x89PNG\r\n\x1a\nimage-bytes")
            captured = {}

            def urlopen(request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return FakeHTTPBody(b'{"ok":true,"result":true}')

            with mock.patch.object(
                telegram_bridge.urllib.request,
                "urlopen",
                side_effect=urlopen,
            ):
                result = telegram_bridge.perform_api_call(
                    "token",
                    "setChatPhoto",
                    {
                        "chat_id": -100123,
                        "__photo_file_path": str(photo_path),
                    },
                )

        self.assertIs(result, True)
        request = captured["request"]
        self.assertTrue(
            request.headers["Content-type"].startswith(
                "multipart/form-data; boundary="
            )
        )
        self.assertIn(b'name="chat_id"\r\n\r\n-100123\r\n', request.data)
        self.assertIn(
            b'name="photo"; filename="chat-photo.png"',
            request.data,
        )
        self.assertIn(
            b"Content-Type: image/png\r\n\r\n"
            b"\x89PNG\r\n\x1a\nimage-bytes",
            request.data,
        )
        self.assertNotIn(b"__photo_file_path", request.data)

    def test_chat_photo_file_is_rejected_for_other_methods(self):
        with self.assertRaisesRegex(
            telegram_bridge.BridgeError,
            "only be used with setChatPhoto",
        ):
            telegram_bridge.perform_api_call(
                "token",
                "sendPhoto",
                {"__photo_file_path": "/tmp/not-used.png"},
            )


class ApiCallSubprocessTests(unittest.TestCase):
    """Parent-side helper-subprocess boundary."""

    def test_round_trip_through_real_helper_subprocess(self):
        # No network: the helper child fails fast on a malformed base_url,
        # proving stdin/stdout wiring and error mapping end to end.
        with mock.patch.object(
            telegram_bridge,
            "API_BASE_URL",
            "http://127.0.0.1:1",
        ):
            with self.assertRaisesRegex(
                telegram_bridge.BridgeError,
                "Could not reach Telegram",
            ):
                telegram_bridge.api_call("token", "getMe")

    def test_deadline_kills_real_blocked_helper(self):
        # A real TCP server that accepts the request and stays silent: the
        # helper child blocks in a genuine socket recv until the parent
        # kills it at the total deadline.
        server = socket.socket()
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
        except OSError:
            server.close()
            self.skipTest("local sockets are unavailable in this sandbox")
        port = server.getsockname()[1]
        accepted = []

        def _serve():
            try:
                connection, _ = server.accept()
            except OSError:
                return
            accepted.append(connection)

        serve_thread = threading.Thread(target=_serve, daemon=True)
        serve_thread.start()
        try:
            with mock.patch.dict(
                telegram_bridge.os.environ,
                {"no_proxy": "*", "NO_PROXY": "*"},
            ):
                with mock.patch.object(
                    telegram_bridge,
                    "API_BASE_URL",
                    f"http://127.0.0.1:{port}",
                ):
                    start = time.monotonic()
                    with self.assertRaisesRegex(
                        telegram_bridge.BridgeError,
                        "total deadline",
                    ):
                        telegram_bridge.api_call(
                            "token",
                            "getUpdates",
                            total_deadline_seconds=2.0,
                        )
                    # The kill bounds the call; it must not wait out the
                    # 70-second socket timeout.
                    self.assertLess(time.monotonic() - start, 20.0)
            serve_thread.join(timeout=5)
            # Hermeticity: the request actually reached our server, not a
            # system proxy.
            self.assertEqual(len(accepted), 1)
        finally:
            for connection in accepted:
                connection.close()
            server.close()
            serve_thread.join(timeout=5)

    def test_orphaned_helper_self_terminates_at_its_own_deadline(self):
        # Even if the parent's kill never comes (worker torn down), the
        # helper exits by itself shortly after its deadline.
        server = socket.socket()
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
        except OSError:
            server.close()
            self.skipTest("local sockets are unavailable in this sandbox")
        port = server.getsockname()[1]
        payload = {
            "token": "token",
            "method": "getUpdates",
            "params": {},
            "base_url": f"http://127.0.0.1:{port}",
            "total_deadline_seconds": 0.5,
        }
        try:
            import json as json_module
            import subprocess as subprocess_module
            import sys as sys_module

            child = subprocess_module.Popen(
                [
                    sys_module.executable,
                    str(telegram_bridge.SCRIPT_PATH),
                    "api-exec",
                ],
                stdin=subprocess_module.PIPE,
                stdout=subprocess_module.PIPE,
                stderr=subprocess_module.PIPE,
                text=True,
            )
            child.stdin.write(json_module.dumps(payload))
            child.stdin.close()
            # No parent-side kill: the self-watchdog must fire on its own.
            returncode = child.wait(timeout=20)
            self.assertEqual(returncode, 70)
        finally:
            server.close()

    def test_helper_exits_when_parent_dies(self):
        # An intermediate process spawns the helper against a silent server
        # (60s deadline, so only orphan detection can end it) and exits
        # immediately; the reparented helper must exit on its own.
        server = socket.socket()
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
        except OSError:
            server.close()
            self.skipTest("local sockets are unavailable in this sandbox")
        port = server.getsockname()[1]
        intermediate_code = (
            "import json, os, subprocess, sys\n"
            "payload = {\n"
            "    'token': 'token',\n"
            "    'method': 'getUpdates',\n"
            "    'params': {},\n"
            f"    'base_url': 'http://127.0.0.1:{port}',\n"
            "    'total_deadline_seconds': 60,\n"
            "    'parent_pid': os.getpid(),\n"
            "}\n"
            "child = subprocess.Popen(\n"
            f"    [sys.executable, {str(telegram_bridge.SCRIPT_PATH)!r},"
            " 'api-exec'],\n"
            "    stdin=subprocess.PIPE,\n"
            "    stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL,\n"
            "    text=True,\n"
            ")\n"
            "child.stdin.write(json.dumps(payload))\n"
            "child.stdin.close()\n"
            "print(child.pid, flush=True)\n"
        )
        try:
            import os as os_module
            import subprocess as subprocess_module
            import sys as sys_module

            intermediate = subprocess_module.run(
                [sys_module.executable, "-c", intermediate_code],
                capture_output=True,
                text=True,
                timeout=15,
            )
            helper_pid = int(intermediate.stdout.strip())
            deadline = time.monotonic() + 15
            gone = False
            while time.monotonic() < deadline:
                try:
                    os_module.kill(helper_pid, 0)
                except OSError:
                    gone = True
                    break
                time.sleep(0.2)
            self.assertTrue(
                gone,
                "orphaned helper kept running after its parent died",
            )
        finally:
            server.close()

    def test_helper_crash_maps_to_bridge_error(self):
        child = FakeChild(stdout="", returncode=1)
        with mock.patch.object(
            telegram_bridge.subprocess,
            "Popen",
            return_value=child,
        ):
            with self.assertRaisesRegex(
                telegram_bridge.BridgeError,
                "helper exited unexpectedly",
            ):
                telegram_bridge.api_call("token", "getMe")

    def test_helper_timeout_kills_and_reaps_child(self):
        child = FakeChild(stdout='{"ok":true,"result":true}')
        child.timeout_on_first_communicate = True
        with mock.patch.object(
            telegram_bridge.subprocess,
            "Popen",
            return_value=child,
        ):
            with self.assertRaisesRegex(
                telegram_bridge.BridgeError,
                "total deadline",
            ):
                telegram_bridge.api_call("token", "getMe")
        self.assertTrue(child.killed)
        self.assertEqual(child.communicate_calls, 2)

    def test_helper_token_never_appears_in_arguments(self):
        recorded = {}

        def _capture(command, **keywords):
            recorded["command"] = command
            recorded["keywords"] = keywords
            return FakeChild(stdout='{"ok":true,"result":true}')

        with mock.patch.object(
            telegram_bridge.subprocess,
            "Popen",
            side_effect=_capture,
        ):
            self.assertTrue(telegram_bridge.api_call("secret-token", "getMe"))
        self.assertNotIn(
            "secret-token",
            " ".join(str(part) for part in recorded["command"]),
        )
        self.assertEqual(recorded["keywords"]["pass_fds"], ())

    def test_delivery_lock_descriptor_is_inherited_by_helper(self):
        recorded = {}

        def _capture(command, **keywords):
            recorded["keywords"] = keywords
            child = FakeChild(stdout='{"ok":true,"result":true}')
            recorded["child"] = child
            return child

        with mock.patch.object(
            telegram_bridge.subprocess,
            "Popen",
            side_effect=_capture,
        ):
            self.assertTrue(
                telegram_bridge.api_call(
                    "token",
                    "editMessageText",
                    delivery_lock_fd=42,
                    chat_id=1,
                )
            )
        self.assertEqual(recorded["keywords"]["pass_fds"], (42,))
        # The reserved parameter never leaks into the Telegram request.
        import json as json_module

        helper_payload = json_module.loads(recorded["child"].sent_input)
        self.assertNotIn("delivery_lock_fd", helper_payload["params"])

    def test_inherited_flock_fences_helper_across_parent_sigkill(self):
        # The ordering proof: a sender parent holds the delivery flock,
        # spawns a real blocked api-exec helper that inherits the locked
        # descriptor, and is then SIGKILLed. The lock must stay held —
        # blocking any competing sender — until the helper itself exits.
        server = socket.socket()
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
        except OSError:
            server.close()
            self.skipTest("local sockets are unavailable in this sandbox")
        port = server.getsockname()[1]
        accepted = []

        def _serve():
            try:
                connection, _ = server.accept()
            except OSError:
                return
            accepted.append(connection)

        serve_thread = threading.Thread(target=_serve, daemon=True)
        serve_thread.start()

        import fcntl as fcntl_module
        import os as os_module
        import signal as signal_module
        import subprocess as subprocess_module
        import sys as sys_module
        import tempfile

        lock_dir = tempfile.mkdtemp(prefix="send-lock-test-")
        lock_path = os_module.path.join(lock_dir, "controller.send-lock")
        parent_code = (
            "import fcntl, json, os, subprocess, sys, time\n"
            f"lock_fd = os.open({lock_path!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
            "fcntl.flock(lock_fd, fcntl.LOCK_EX)\n"
            "payload = {\n"
            "    'token': 'token',\n"
            "    'method': 'getUpdates',\n"
            "    'params': {},\n"
            f"    'base_url': 'http://127.0.0.1:{port}',\n"
            "    'total_deadline_seconds': 60,\n"
            "    'parent_pid': os.getpid(),\n"
            "}\n"
            "child = subprocess.Popen(\n"
            f"    [sys.executable, {str(telegram_bridge.SCRIPT_PATH)!r},"
            " 'api-exec'],\n"
            "    stdin=subprocess.PIPE,\n"
            "    stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL,\n"
            "    text=True,\n"
            "    pass_fds=(lock_fd,),\n"
            ")\n"
            "child.stdin.write(json.dumps(payload))\n"
            "child.stdin.close()\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(3600)\n"
        )

        def _lock_is_free():
            probe = os_module.open(lock_path, os_module.O_RDWR)
            try:
                try:
                    fcntl_module.flock(
                        probe,
                        fcntl_module.LOCK_EX | fcntl_module.LOCK_NB,
                    )
                except BlockingIOError:
                    return False
                fcntl_module.flock(probe, fcntl_module.LOCK_UN)
                return True
            finally:
                os_module.close(probe)

        parent = subprocess_module.Popen(
            [sys_module.executable, "-c", parent_code],
            stdout=subprocess_module.PIPE,
            text=True,
        )
        helper_pid = None
        try:
            import select as select_module

            ready, _, _ = select_module.select([parent.stdout], [], [], 15)
            self.assertTrue(ready, "lock-holding parent never reported a PID")
            helper_pid = int(parent.stdout.readline().strip())
            # Wait until the helper's request actually reached the server.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not accepted:
                time.sleep(0.05)
            self.assertTrue(accepted, "helper never connected")
            self.assertFalse(_lock_is_free())
            # Freeze the helper completely — its watchdog cannot run, which
            # models the post-wake window where the helper has not been
            # scheduled — then kill the parent outright. Only the inherited
            # kernel flock can hold the fence now.
            os_module.kill(helper_pid, signal_module.SIGSTOP)
            os_module.kill(parent.pid, signal_module.SIGKILL)
            parent.wait(timeout=10)
            hold_until = time.monotonic() + 1.5
            while time.monotonic() < hold_until:
                self.assertFalse(
                    _lock_is_free(),
                    "flock released while the helper was still in flight",
                )
                time.sleep(0.25)
            # Resume the helper; its orphan watchdog ends it, releasing the
            # lock. Close the server side too so the request cannot linger.
            os_module.kill(helper_pid, signal_module.SIGCONT)
            accepted[0].close()
            release_deadline = time.monotonic() + 15
            released = False
            while time.monotonic() < release_deadline:
                if _lock_is_free():
                    released = True
                    break
                time.sleep(0.2)
            self.assertTrue(
                released,
                "flock was not released after the helper exited",
            )
            # The helper process itself is gone.
            helper_deadline = time.monotonic() + 10
            helper_gone = False
            while time.monotonic() < helper_deadline:
                try:
                    os_module.kill(helper_pid, 0)
                except OSError:
                    helper_gone = True
                    break
                time.sleep(0.2)
            self.assertTrue(helper_gone)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=10)
            if parent.stdout is not None:
                parent.stdout.close()
            if helper_pid is not None:
                # Never leave a stopped helper behind on assertion failure.
                for signal_number in (
                    signal_module.SIGCONT,
                    signal_module.SIGKILL,
                ):
                    try:
                        os_module.kill(helper_pid, signal_number)
                    except OSError:
                        pass
            for connection in accepted:
                try:
                    connection.close()
                except OSError:
                    pass
            server.close()
            serve_thread.join(timeout=5)
            try:
                os_module.unlink(lock_path)
                os_module.rmdir(lock_dir)
            except OSError:
                pass


class ProcessUpdateAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "chat_id": 123,
            "owner_user_id": 123,
            "handler_path": str(telegram_bridge.SCRIPT_PATH),
        }

    @staticmethod
    def update(chat, sender_id=123):
        return {
            "update_id": 10,
            "message": {
                "message_id": 20,
                "from": {"id": sender_id, "username": "owner"},
                "chat": chat,
                "message_thread_id": 62,
                "text": "hello",
            },
        }

    @staticmethod
    def callback_update(chat, sender_id=123):
        return {
            "update_id": 11,
            "callback_query": {
                "id": "callback-11",
                "from": {"id": sender_id, "username": "owner"},
                "data": "a:opaque",
                "message": {
                    "message_id": 21,
                    "chat": chat,
                    "message_thread_id": 62,
                },
            },
        }

    def test_paired_private_chat_is_still_accepted(self):
        update = self.update({"id": 123, "type": "private"})
        completed = mock.Mock(returncode=0)
        with mock.patch.object(
            telegram_bridge.subprocess,
            "run",
            return_value=completed,
        ) as run:
            telegram_bridge.process_update(self.config, update)

        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["TELEGRAM_CHAT_ID"], "123")
        self.assertEqual(environment["TELEGRAM_CHAT_TYPE"], "private")

    def test_owner_is_accepted_in_a_private_forum_group(self):
        update = self.update(
            {
                "id": -100777,
                "type": "supergroup",
                "title": "Life",
                "is_forum": True,
            }
        )
        completed = mock.Mock(returncode=0)
        with mock.patch.object(
            telegram_bridge.subprocess,
            "run",
            return_value=completed,
        ) as run:
            telegram_bridge.process_update(self.config, update)

        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["TELEGRAM_CHAT_ID"], "-100777")
        self.assertEqual(environment["TELEGRAM_CHAT_TYPE"], "supergroup")
        self.assertEqual(environment["TELEGRAM_CHAT_TITLE"], "Life")
        self.assertEqual(environment["TELEGRAM_FROM_ID"], "123")

    def test_other_users_are_ignored_in_an_authorized_private_group(self):
        update = self.update(
            {
                "id": -100777,
                "type": "supergroup",
                "title": "Life",
                "is_forum": True,
            },
            sender_id=456,
        )
        with mock.patch.object(telegram_bridge.subprocess, "run") as run:
            telegram_bridge.process_update(self.config, update)
        run.assert_not_called()

    def test_public_groups_are_not_accepted_as_control_surfaces(self):
        update = self.update(
            {
                "id": -100777,
                "type": "supergroup",
                "title": "Public Life",
                "username": "public_life",
                "is_forum": True,
            }
        )
        with mock.patch.object(telegram_bridge.subprocess, "run") as run:
            telegram_bridge.process_update(self.config, update)
        run.assert_not_called()

    def test_non_forum_groups_are_not_accepted_as_control_surfaces(self):
        for chat in (
            {
                "id": -777,
                "type": "group",
                "title": "Private Group",
            },
            {
                "id": -100777,
                "type": "supergroup",
                "title": "Private Supergroup",
            },
        ):
            with self.subTest(chat_type=chat["type"]):
                update = self.update(chat)
                with mock.patch.object(
                    telegram_bridge.subprocess,
                    "run",
                ) as run:
                    telegram_bridge.process_update(self.config, update)
                run.assert_not_called()

    def test_owner_callback_is_accepted_in_exact_private_forum_topic(self):
        update = self.callback_update(
            {
                "id": -100777,
                "type": "supergroup",
                "title": "Life",
                "is_forum": True,
            }
        )
        completed = mock.Mock(returncode=0)
        with mock.patch.object(
            telegram_bridge.subprocess,
            "run",
            return_value=completed,
        ) as run:
            telegram_bridge.process_update(self.config, update)

        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["TELEGRAM_CHAT_ID"], "-100777")
        self.assertEqual(environment["TELEGRAM_MESSAGE_THREAD_ID"], "62")
        self.assertEqual(environment["TELEGRAM_FROM_ID"], "123")
        self.assertEqual(environment["TELEGRAM_CALLBACK_QUERY_ID"], "callback-11")

    def test_foreign_callback_is_rejected_in_private_forum(self):
        update = self.callback_update(
            {
                "id": -100777,
                "type": "supergroup",
                "title": "Life",
                "is_forum": True,
            },
            sender_id=456,
        )
        with mock.patch.object(telegram_bridge.subprocess, "run") as run:
            telegram_bridge.process_update(self.config, update)
        run.assert_not_called()

    def test_callback_is_rejected_in_public_or_non_forum_group(self):
        chats = (
            {
                "id": -100777,
                "type": "supergroup",
                "title": "Public Life",
                "username": "public_life",
                "is_forum": True,
            },
            {
                "id": -100778,
                "type": "supergroup",
                "title": "Private Non-forum",
            },
        )
        for chat in chats:
            with self.subTest(title=chat["title"]):
                update = self.callback_update(chat)
                with mock.patch.object(
                    telegram_bridge.subprocess,
                    "run",
                ) as run:
                    telegram_bridge.process_update(self.config, update)
                run.assert_not_called()

    def test_an_unpaired_private_chat_is_ignored(self):
        update = self.update({"id": 999, "type": "private"})
        with mock.patch.object(telegram_bridge.subprocess, "run") as run:
            telegram_bridge.process_update(self.config, update)
        run.assert_not_called()


class TokenValidationTests(unittest.TestCase):
    def test_accepts_valid_token(self):
        self.assertEqual(
            telegram_bridge.clean_and_validate_token(f"  {VALID_TOKEN}\n"),
            VALID_TOKEN,
        )

    def test_removes_bracketed_paste_escape_prefix(self):
        pasted = f"\x1b[200~{VALID_TOKEN}\x1b[201~"
        self.assertEqual(
            telegram_bridge.clean_and_validate_token(pasted),
            VALID_TOKEN,
        )

    def test_rejects_control_character_inside_token(self):
        with self.assertRaises(telegram_bridge.BridgeError):
            telegram_bridge.clean_and_validate_token(
                VALID_TOKEN[:10] + "\n" + VALID_TOKEN[10:]
            )

    def test_rejects_non_token_text(self):
        with self.assertRaises(telegram_bridge.BridgeError):
            telegram_bridge.clean_and_validate_token("not a token")


class MessageDescriptionTests(unittest.TestCase):
    def test_describes_sender_without_mutating_message(self):
        message = {
            "from": {
                "first_name": "Test",
                "last_name": "Person",
                "username": "tester",
            },
            "chat": {"id": 1234},
        }
        snapshot = {
            "from": dict(message["from"]),
            "chat": dict(message["chat"]),
        }

        description = telegram_bridge.describe_message(message)

        self.assertEqual(
            description,
            "Test Person (@tester), chat ID 1234",
        )
        self.assertEqual(message, snapshot)

    def test_ignores_automatic_topic_root_reply(self):
        message = {
            "message_id": 43,
            "message_thread_id": 62,
            "is_topic_message": True,
            "reply_to_message": {
                "message_id": 42,
                "message_thread_id": 62,
                "forum_topic_created": {"name": "Stage 2 Test"},
            },
        }

        self.assertEqual(
            telegram_bridge.explicit_reply_message_id(message),
            "",
        )

    def test_preserves_explicit_reply_inside_topic(self):
        message = {
            "message_id": 45,
            "message_thread_id": 62,
            "is_topic_message": True,
            "reply_to_message": {
                "message_id": 44,
                "message_thread_id": 62,
                "text": "bot response",
            },
        }

        self.assertEqual(
            telegram_bridge.explicit_reply_message_id(message),
            "44",
        )


if __name__ == "__main__":
    unittest.main()
