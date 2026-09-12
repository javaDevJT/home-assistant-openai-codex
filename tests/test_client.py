"""Credential and transport regressions without contacting OpenAI."""

import asyncio
import base64
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from custom_components.openai_codex import client as api


def token(account="account-1", expiry=None):
    payload = {
        "exp": expiry or time.time() + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": account},
    }
    return (
        "header."
        + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        + ".signature"
    )


def credentials():
    return {
        "access_token": token(),
        "refresh_token": "secret-refresh",
        "account_id": "account-1",
        "expires_at": time.time() + 3600,
    }


class Response:
    def __init__(self, status=200, chunks=()):
        self.status = status
        self.chunks = chunks
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def iter_any(self):
        for chunk in self.chunks:
            yield chunk


class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return next(self.responses)


def completed():
    result = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "Hello"}]}],
    }
    return result, b"data: " + json.dumps(
        {"type": "response.completed", "response": result}
    ).encode() + b"\r\n\r\n"


class ClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_device_code_aliases_and_exchange(self):
        for key in ("user_code", "usercode"):
            with self.subTest(key=key):
                responses = [
                    (200, {"device_auth_id": "device", key: "CODE", "interval": "5"}),
                    (403, {}),
                    (404, {}),
                    (200, {"authorization_code": "grant", "code_verifier": "verifier"}),
                    (200, {"access_token": token(), "refresh_token": "refresh"}),
                ]
                with (
                    patch.object(api, "_json_request", AsyncMock(side_effect=responses)) as request,
                    patch.object(api.asyncio, "sleep", AsyncMock()) as sleep,
                ):
                    code = await api.async_start_device_login(None)
                    data = await api.async_complete_device_login(None, code)
                self.assertEqual(data["account_id"], "account-1")
                self.assertEqual(sleep.await_count, 2)
                exchange = request.await_args
                self.assertEqual(exchange.args[2]["code_verifier"], "verifier")
                self.assertEqual(
                    exchange.args[2]["redirect_uri"], api.AUTH_URL + "/deviceauth/callback"
                )
                self.assertTrue(exchange.kwargs["form"])

    async def test_device_expiry_and_cancel(self):
        with patch.object(api, "_json_request", AsyncMock(return_value=(404, {}))):
            with self.assertRaises(api.LoginExpiredError):
                await api.async_complete_device_login(
                    None, api.DeviceCode("id", "code", 1, time.monotonic() - 1)
                )
            task = asyncio.create_task(
                api.async_complete_device_login(
                    None, api.DeviceCode("id", "code", 1, time.monotonic() + 900)
                )
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_invalid_device_payloads(self):
        for data in (
            {},
            {"device_auth_id": "", "user_code": "code"},
            {"device_auth_id": "id", "user_code": "code", "interval": "NaN"},
        ):
            with (
                self.subTest(data=data),
                patch.object(api, "_json_request", AsyncMock(return_value=(200, data))),
            ):
                with self.assertRaises(api.CodexError):
                    await api.async_start_device_login(None)

    async def test_refresh_is_serialized_and_rotated_before_use(self):
        saved = AsyncMock()
        data = credentials()
        data["expires_at"] = 0
        client = api.CodexClient(None, data, saved)
        refreshed = {"access_token": token(expiry=time.time() + 7200), "refresh_token": "rotated"}
        with patch.object(
            api, "_json_request", AsyncMock(return_value=(200, refreshed))
        ) as request:
            tokens = await asyncio.gather(client._access_token(), client._access_token())
        self.assertEqual(tokens, [refreshed["access_token"]] * 2)
        request.assert_awaited_once()
        saved.assert_awaited_once()
        self.assertEqual(saved.await_args.args[0]["refresh_token"], "rotated")

    async def test_token_validation_and_account_boundary(self):
        old = credentials()
        self.assertEqual(
            api._token_data({"access_token": token()}, old)["refresh_token"], old["refresh_token"]
        )
        for data in (
            {"access_token": "invalid", "refresh_token": "r"},
            {"access_token": token("another-account"), "refresh_token": "r"},
        ):
            with self.subTest(data=data), self.assertRaises(api.AuthenticationError):
                api._token_data(data, old)
        old["expires_at"] = 0
        with patch.object(
            api,
            "_json_request",
            AsyncMock(return_value=(400, {"error": "invalid_grant", "detail": "secret-refresh"})),
        ):
            with self.assertRaises(api.AuthenticationError) as error:
                await api.CodexClient(None, old, AsyncMock()).async_validate()
            self.assertNotIn("secret-refresh", str(error.exception))

    async def test_stream_fragmentation_and_request_contract(self):
        expected, raw = completed()
        session = Session(Response(chunks=[raw[i : i + 3] for i in range(0, len(raw), 3)]))
        client = api.CodexClient(session, credentials(), AsyncMock())
        result = await client.async_request(
            "model", "instructions", [{"role": "user", "content": "Hello"}], []
        )
        self.assertEqual(result, expected)
        url, request = session.requests[0]
        self.assertEqual(url, api.RESPONSES_URL)
        self.assertFalse(request["allow_redirects"])
        self.assertFalse(request["json"]["store"])
        self.assertTrue(request["json"]["stream"])
        self.assertEqual(request["headers"]["ChatGPT-Account-Id"], "account-1")
        self.assertNotIn("max_output_tokens", request["json"])

    async def test_failed_incomplete_and_truncated_streams(self):
        bad = [
            b"data: nonsense\n\n",
            b"data: [DONE]\n\n",
            b'data: {"type":"response.failed"}\n\n',
            b'data: {"type":"response.incomplete"}\n\n',
            b'data: {"type":"response.completed","response":{"status":"incomplete","output":[]}}\n\n',
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(api.CodexError):
                await api._read_response(Response(chunks=[raw]))
        with patch.object(api, "MAX_RESPONSE_BYTES", 4), self.assertRaises(api.CodexError):
            await api._read_response(Response(chunks=[b"12345"]))

    async def test_401_refreshes_once_and_retries(self):
        _, raw = completed()
        session = Session(Response(status=401), Response(chunks=[raw]))
        client = api.CodexClient(session, credentials(), AsyncMock())
        new_token = token(expiry=time.time() + 7200)
        with patch.object(
            api,
            "_json_request",
            AsyncMock(return_value=(200, {"access_token": new_token, "refresh_token": "rotated"})),
        ) as refresh:
            await client.async_request("model", "instructions", [], [])
        refresh.assert_awaited_once()
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(session.requests[1][1]["headers"]["Authorization"], "Bearer " + new_token)

    async def test_error_statuses_do_not_leak_body_or_retry(self):
        for status in (403, 429, 500, 302):
            with self.subTest(status=status):
                session = Session(Response(status=status, chunks=[b"secret-token"]))
                with self.assertRaises(api.CodexError) as error:
                    await api.CodexClient(session, credentials(), AsyncMock()).async_request(
                        "model", "instructions", [], []
                    )
                self.assertEqual(len(session.requests), 1)
                self.assertNotIn("secret-token", str(error.exception))


if __name__ == "__main__":
    unittest.main()
