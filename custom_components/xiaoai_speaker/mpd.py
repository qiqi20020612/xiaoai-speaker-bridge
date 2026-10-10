"""Bounded asyncio implementation of the MPD protocol, independent of OpenHome."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from .client import BridgeError, validate_host


def argument(value: Any) -> str:
    value = str(value)
    if any(c in value for c in ("\n", "\r", "\x00")):
        raise ValueError("invalid_mpd_argument")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pairs(lines: list[str]) -> dict:
    data: dict = {}
    for line in lines:
        if ": " in line:
            key, value = line.split(": ", 1)
            if key in data:
                if not isinstance(data[key], list):
                    data[key] = [data[key]]
                data[key].append(value)
            else:
                data[key] = value
    return data


class MPDClient:
    def __init__(self, host: str, port: int = 6600, password: str = "") -> None:
        self.host = validate_host(host)
        self.port = port
        self._password = password
        self._lock = asyncio.Lock()
        self.commands: set[str] = set()

    async def execute(self, command: str, *args: Any) -> list[str]:
        if not command.replace("_", "").isalpha():
            raise ValueError("invalid_mpd_command")
        async with self._lock:
            writer = None
            try:
                async with asyncio.timeout(10):
                    reader, writer = await asyncio.open_connection(
                        self.host, self.port, limit=65536
                    )
                    if not (await reader.readline()).startswith(b"OK MPD "):
                        raise BridgeError("invalid_mpd_greeting")
                    if self._password:
                        writer.write(("password " + argument(self._password) + "\n").encode())
                        await writer.drain()
                        await self._response(reader)
                    writer.write(
                        (
                            command + (" " if args else "") + " ".join(map(argument, args)) + "\n"
                        ).encode()
                    )
                    await writer.drain()
                    return await self._response(reader)
            except OSError, TimeoutError, ValueError, asyncio.IncompleteReadError:
                raise BridgeError("mpd_unavailable") from None
            finally:
                if writer:
                    writer.close()
                    with suppress(OSError, TimeoutError):
                        async with asyncio.timeout(1):
                            await writer.wait_closed()

    @staticmethod
    async def _response(reader: asyncio.StreamReader) -> list[str]:
        lines = []
        size = 0
        while True:
            raw = await reader.readline()
            if not raw:
                raise BridgeError("mpd_disconnected")
            size += len(raw)
            if size > 262144 or len(lines) >= 4096:
                raise BridgeError("mpd_response_too_large")
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "OK":
                return lines
            if line.startswith("ACK"):
                raise BridgeError("mpd_command_rejected")
            lines.append(line)

    async def discover(self) -> set[str]:
        self.commands = {
            line[9:] for line in await self.execute("commands") if line.startswith("command: ")
        }
        return self.commands

    async def snapshot(self) -> dict:
        status = pairs(await self.execute("status"))
        song = pairs(await self.execute("currentsong")) if "currentsong" in self.commands else {}
        return {
            "online": True,
            "state": status.get("state", "stop"),
            "status": status,
            "song": song,
            "actions": sorted(self.commands),
        }

    async def queue(self) -> list[dict]:
        entries = []
        current: dict = {}
        for line in await self.execute("playlistinfo"):
            if ": " not in line:
                continue
            key, value = line.split(": ", 1)
            if key == "file" and current:
                entries.append(current)
                current = {}
            current[key] = value
        if current:
            entries.append(current)
        return entries

    async def play_media(self, uri: str, enqueue: str = "replace") -> None:
        if enqueue not in ("replace", "add", "next", "play"):
            raise ValueError("invalid_enqueue")
        # Validate before destructive queue changes.
        argument(uri)
        if not uri or len(uri) > 2048:
            raise ValueError("invalid_media_uri")
        if enqueue == "replace":
            await self.execute("clear")
        result = pairs(await self.execute("addid", uri))
        song_id = result.get("Id")
        if enqueue == "next" and song_id is not None:
            status = pairs(await self.execute("status"))
            await self.execute("moveid", song_id, int(status.get("song", -1)) + 1)
        if enqueue in ("replace", "play"):
            if song_id is not None:
                await self.execute("playid", song_id)
            else:
                await self.execute("play")
