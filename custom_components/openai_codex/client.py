"""ChatGPT device authorization and the Codex Responses transport.

Protocol reference: https://github.com/openai/codex/tree/main/codex-rs/login
This is an unofficial client of the subscription backend, not the OpenAI API.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

AUTH_URL = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
VERIFICATION_URL = f"{AUTH_URL}/codex/device"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class CodexError(Exception):
    """A safe, credential-free error to expose to Home Assistant."""


class AuthenticationError(CodexError):
    """The stored authorization is no longer usable."""


class LoginExpiredError(CodexError):
    """The device authorization window elapsed."""


@dataclass(frozen=True)
class DeviceCode:
    """Temporary login state; never saved to a config entry."""

    device_auth_id: str
    user_code: str
    interval: float
    expires_at: float


async def _json_request(
    session: ClientSession, path: str, payload: dict[str, Any], *, form: bool = False
) -> tuple[int, dict[str, Any]]:
    """Use fixed HTTPS destinations and never propagate provider error bodies."""
    try:
        async with session.post(
            f"{AUTH_URL}{path}",
            **({"data": payload} if form else {"json": payload}),
            timeout=ClientTimeout(total=30),
            allow_redirects=False,
        ) as response:
            raw = bytearray()
            async for chunk in response.content.iter_any():
                raw.extend(chunk)
                if len(raw) > 1024 * 1024:
                    raise CodexError("Authorization response is too large")
            try:
                data = json.loads(raw)
            except ValueError, UnicodeError:
                data = {}
            return response.status, data if isinstance(data, dict) else {}
    except (ClientError, TimeoutError) as err:
        raise CodexError("Cannot connect to OpenAI authorization") from err


async def async_start_device_login(session: ClientSession) -> DeviceCode:
    """Request a code to enter on OpenAI's own login page."""
    status, data = await _json_request(
        session, "/api/accounts/deviceauth/usercode", {"client_id": CLIENT_ID}
    )
    if status != 200:
        raise CodexError(f"Cannot start device sign-in (HTTP {status})")
    device_id = data.get("device_auth_id")
    user_code = data.get("user_code", data.get("usercode"))
    if (
        not isinstance(device_id, str)
        or not device_id
        or not isinstance(user_code, str)
        or not user_code
    ):
        raise CodexError("OpenAI returned an invalid device code")
    try:
        interval = float(data.get("interval", 5))
        if not math.isfinite(interval):
            raise ValueError
    except (ValueError, TypeError) as err:
        raise CodexError("OpenAI returned an invalid polling interval") from err
    return DeviceCode(device_id, user_code, max(1, interval), time.monotonic() + 900)


def _claims(token: str) -> dict[str, Any]:
    """Read metadata from a token received over the trusted OAuth connection."""
    try:
        encoded = token.split(".")[1]
        decoded = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        return decoded if isinstance(decoded, dict) else {}
    except ValueError, IndexError, UnicodeError:
        return {}


def _token_data(
    response: Mapping[str, Any], previous: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Validate and retain only the credentials needed by the integration."""
    previous = previous or {}
    access_token = response.get("access_token")
    refresh_token = response.get("refresh_token", previous.get("refresh_token"))
    if not isinstance(access_token, str) or not access_token:
        raise AuthenticationError("OpenAI did not return an access token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise AuthenticationError("OpenAI did not return a refresh token")
    claims = _claims(access_token)
    id_token = response.get("id_token", "")
    identity = _claims(id_token) if isinstance(id_token, str) else {}
    auth = claims.get("https://api.openai.com/auth", {})
    id_auth = identity.get("https://api.openai.com/auth", {})
    account_id = (
        (auth.get("chatgpt_account_id") if isinstance(auth, dict) else None)
        or (id_auth.get("chatgpt_account_id") if isinstance(id_auth, dict) else None)
        or previous.get("account_id")
    )
    if not isinstance(account_id, str) or not account_id:
        raise AuthenticationError("The login has no ChatGPT account identifier")
    if previous.get("account_id") and previous["account_id"] != account_id:
        raise AuthenticationError("The refreshed token belongs to another account")
    try:
        expires_at = float(claims.get("exp") or time.time() + float(response["expires_in"]))
        if not math.isfinite(expires_at) or expires_at <= time.time():
            raise ValueError
    except (KeyError, TypeError, ValueError) as err:
        raise AuthenticationError("OpenAI returned an invalid token expiration") from err
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": account_id,
        "expires_at": expires_at,
    }


async def async_complete_device_login(session: ClientSession, code: DeviceCode) -> dict[str, Any]:
    """Poll until approved, cancelled by HA, or the 15-minute window expires."""
    try:
        async with asyncio.timeout(max(0, code.expires_at - time.monotonic())):
            while True:
                status, data = await _json_request(
                    session,
                    "/api/accounts/deviceauth/token",
                    {"device_auth_id": code.device_auth_id, "user_code": code.user_code},
                )
                if status == 200:
                    authorization_code = data.get("authorization_code")
                    verifier = data.get("code_verifier")
                    if not isinstance(authorization_code, str) or not isinstance(verifier, str):
                        raise CodexError("OpenAI returned an invalid authorization grant")
                    status, tokens = await _json_request(
                        session,
                        "/oauth/token",
                        {
                            "grant_type": "authorization_code",
                            "client_id": CLIENT_ID,
                            "code": authorization_code,
                            "code_verifier": verifier,
                            "redirect_uri": f"{AUTH_URL}/deviceauth/callback",
                        },
                        form=True,
                    )
                    if status != 200:
                        raise AuthenticationError(f"Sign-in exchange failed (HTTP {status})")
                    return _token_data(tokens)
                if status not in (403, 404):
                    raise CodexError(f"Cannot check device sign-in (HTTP {status})")
                await asyncio.sleep(code.interval)
    except TimeoutError as err:
        raise LoginExpiredError("The device code expired; start sign-in again") from err


async def _read_response(response: Any) -> dict[str, Any]:
    """Read a bounded SSE response, including events split across TCP chunks."""
    buffer = bytearray()
    data_lines: list[bytes] = []
    received = 0
    async for chunk in response.content.iter_any():
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            raise CodexError("OpenAI response exceeded the size limit")
        buffer.extend(chunk)
        while (newline := buffer.find(b"\n")) != -1:
            line = bytes(buffer[:newline]).rstrip(b"\r")
            del buffer[: newline + 1]
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip(b" "))
            elif not line and data_lines:
                raw = b"\n".join(data_lines)
                data_lines.clear()
                if raw == b"[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeError) as err:
                    raise CodexError("OpenAI returned an invalid stream event") from err
                if not isinstance(event, dict):
                    raise CodexError("OpenAI returned an invalid stream event")
                kind = event.get("type")
                if kind in ("response.completed", "response.done"):
                    result = event.get("response")
                    if (
                        not isinstance(result, dict)
                        or result.get("status") != "completed"
                        or not isinstance(result.get("output"), list)
                    ):
                        raise CodexError("OpenAI did not complete the response")
                    return result
                if kind in ("error", "response.failed", "response.incomplete"):
                    raise CodexError("OpenAI could not complete the response")
    raise CodexError("OpenAI disconnected before completing the response")


class CodexClient:
    """Per-entry credentials with serialized refresh and no shared chat state."""

    def __init__(
        self,
        session: ClientSession,
        data: Mapping[str, Any],
        async_save: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._session = session
        self._data = dict(data)
        self._async_save = async_save
        self._refresh_lock = asyncio.Lock()

    async def _access_token(self, rejected_token: str | None = None) -> str:
        async with self._refresh_lock:
            token = self._data.get("access_token")
            expires_at = self._data.get("expires_at", 0)
            if (
                isinstance(token, str)
                and isinstance(expires_at, (int, float))
                and expires_at > time.time() + 60
                and token != rejected_token
            ):
                return token
            refresh_token = self._data.get("refresh_token")
            if not refresh_token:
                raise AuthenticationError("Sign in to OpenAI again")
            status, result = await _json_request(
                self._session,
                "/oauth/token",
                {
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": refresh_token,
                },
            )
            if status in (400, 401, 403):
                raise AuthenticationError("OpenAI authorization expired; sign in again")
            if status != 200:
                raise CodexError(f"Cannot refresh OpenAI authorization (HTTP {status})")
            self._data = _token_data(result, self._data)
            await self._async_save(dict(self._data))
            return self._data["access_token"]

    async def async_validate(self) -> None:
        """Ensure usable local credentials, refreshing when necessary."""
        if not self._data.get("account_id"):
            raise AuthenticationError("Sign in to OpenAI again")
        await self._access_token()

    async def async_request(
        self, model: str, instructions: str, messages: list[dict], tools: list[dict]
    ) -> dict[str, Any]:
        """Request a completed response; retry an authentication failure once."""
        payload = {
            "model": model,
            "instructions": instructions,
            "input": messages,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        token = await self._access_token()
        for attempt in range(2):
            try:
                async with self._session.post(
                    RESPONSES_URL,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "ChatGPT-Account-Id": self._data["account_id"],
                        "originator": "home_assistant_openai_codex",
                        "User-Agent": "home-assistant-openai-codex/0.1.0",
                        "Accept": "text/event-stream",
                    },
                    timeout=ClientTimeout(total=120, sock_read=60),
                    allow_redirects=False,
                ) as response:
                    if response.status == 401:
                        if attempt:
                            raise AuthenticationError("OpenAI rejected the login; sign in again")
                    elif response.status == 429:
                        raise CodexError("OpenAI usage limit reached; try again later")
                    elif response.status == 403:
                        raise CodexError("This account cannot access the selected Codex model")
                    elif response.status != 200:
                        raise CodexError(
                            f"OpenAI request failed (HTTP {response.status}); check the model setting"
                        )
                    else:
                        return await _read_response(response)
            except (ClientError, TimeoutError) as err:
                raise CodexError("Cannot connect to OpenAI; try again later") from err
            token = await self._access_token(rejected_token=token)
        raise AuthenticationError("Sign in to OpenAI again")
