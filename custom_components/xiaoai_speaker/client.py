"""Authenticated LX06 client; ambiguous mutations are never blindly retried."""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import DEFAULT_PORT, PROTOCOL_VERSION

ARTWORK_LIMIT = 2 * 1024 * 1024
ARTWORK_ID = re.compile(r"cover-[0-9a-f]{32}\.(?:jpg|png)")


class BridgeError(Exception):
    """A safe, machine-readable error without device response bodies."""

    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


class AuthenticationError(BridgeError):
    """Bridge authentication failed."""


def validate_host(host: str) -> str:
    host = host.strip()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?", host):
            raise ValueError("invalid_host") from None
    return host


class BridgeClient:
    def __init__(
        self, session: aiohttp.ClientSession, host: str, token: str, port: int = DEFAULT_PORT
    ) -> None:
        host = validate_host(host)
        if ":" in host:
            host = f"[{host}]"
        self.base_url = f"http://{host}:{int(port)}"
        self._session = session
        self._headers = {"Authorization": f"Bearer {token}"}

    async def request(
        self, method: str, path: str, data: dict | None = None, *, timeout: float = 10
    ) -> dict[str, Any]:
        try:
            async with self._session.request(
                method,
                self.base_url + path,
                json=data,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise AuthenticationError("auth_failed")
                if 300 <= response.status < 400:
                    raise BridgeError("unexpected_redirect", uncertain=method != "GET")
                if response.content_length and response.content_length > 131072:
                    raise BridgeError("response_too_large", uncertain=method != "GET")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    raw.extend(chunk)
                    if len(raw) > 131072:
                        raise BridgeError("response_too_large", uncertain=method != "GET")

                try:
                    payload = json.loads(raw)
                except ValueError, UnicodeError:
                    raise BridgeError("invalid_response", uncertain=method != "GET") from None
                if not isinstance(payload, dict):
                    raise BridgeError("invalid_response", uncertain=method != "GET")
                if response.status >= 400:
                    code = payload.get("error", "request_failed")
                    # Only expose the protocol's bounded identifier, never arbitrary server text.
                    if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,48}", code):
                        code = "request_failed"
                    raise BridgeError(code)
                return payload
        except aiohttp.ClientError, TimeoutError, OSError:
            raise BridgeError("connection_failed", uncertain=method != "GET") from None

    async def info(self) -> dict:
        info = await self.request("GET", "/api/info")
        if (
            type(info.get("protocol")) is not int
            or info["protocol"] != PROTOCOL_VERSION
            or not isinstance(info.get("device_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", info["device_id"])
            or not isinstance(info.get("capabilities"), dict)
        ):
            raise BridgeError("incompatible_protocol")
        return info

    async def state(self) -> dict:
        return await self.request("GET", "/api/state")

    async def artwork(self, image_id: str) -> tuple[bytes | None, str | None]:
        """Fetch only an opaque image ID through the authenticated device API."""
        if not isinstance(image_id, str) or not ARTWORK_ID.fullmatch(image_id):
            raise BridgeError("invalid_artwork_id")
        try:
            async with self._session.get(
                self.base_url + "/api/airplay/artwork/" + image_id,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=5),
                allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise AuthenticationError("auth_failed")
                if response.status == 404:
                    return None, None
                if response.status != 200:
                    raise BridgeError("artwork_unavailable")
                mime = response.content_type
                if mime not in ("image/jpeg", "image/png"):
                    raise BridgeError("invalid_artwork")
                if response.content_length and response.content_length > ARTWORK_LIMIT:
                    raise BridgeError("response_too_large")
                data = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    data.extend(chunk)
                    if len(data) > ARTWORK_LIMIT:
                        raise BridgeError("response_too_large")
                if not (
                    mime == "image/jpeg"
                    and data.startswith(b"\xff\xd8\xff")
                    or mime == "image/png"
                    and data.startswith(b"\x89PNG\r\n\x1a\n")
                ):
                    raise BridgeError("invalid_artwork")
                return bytes(data), mime
        except aiohttp.ClientError, TimeoutError, OSError:
            raise BridgeError("connection_failed") from None

    async def events(self, since: int) -> dict:
        return await self.request("GET", f"/api/events?since={since}", timeout=35)

    async def submit(self, kind: str, payload: dict) -> dict:
        path = {"announce": "/api/tts/announce", "send_text": "/api/conversation/text"}[kind]
        return await self.request("POST", path, payload)

    async def job(self, request_id: str) -> dict:
        return await self.request("GET", "/api/jobs/" + quote(request_id, safe=""))

    async def cancel(self, request_id: str) -> dict:
        return await self.request("POST", "/api/jobs/" + quote(request_id, safe="") + "/cancel", {})

    async def volume(self, target: str, level: float, request_id: str) -> dict:
        return await self.request(
            "POST",
            "/api/volume",
            {
                "target": target,
                "level": level,
                "request_id": request_id,
            },
        )

    async def control(self, source: str, action: str, **fields: Any) -> dict:
        return await self.request(
            "POST",
            "/api/playback/control",
            {
                "source": source,
                "action": action,
                **fields,
            },
        )
