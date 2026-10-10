import asyncio

import pytest

from custom_components.xiaoai_speaker.client import BridgeError
from custom_components.xiaoai_speaker.mpd import MPDClient, argument


@pytest.mark.parametrize("value", ["url\nclear", "url\rstop", "\0"])
def test_mpd_injection_is_rejected(value):
    with pytest.raises(ValueError):
        argument(value)


async def test_native_protocol_reads_and_routes_queue_without_openhome(socket_enabled):
    commands = []

    async def server(reader, writer):
        writer.write(b"OK MPD 0.23.5\n")
        await writer.drain()
        line = (await reader.readline()).decode().strip()
        commands.append(line)
        command = line.split(" ", 1)[0]
        responses = {
            "commands": "command: status\ncommand: currentsong\ncommand: addid\ncommand: clear\ncommand: playid\n",
            "status": "state: play\nvolume: 40\nsong: 0\n",
            "currentsong": "file: https://example.com/test.mp3\nTitle: Test\n",
            "addid": "Id: 12\n",
            "playlistinfo": "file: first\nId: 1\nfile: second\nId: 2\n",
        }
        writer.write((responses.get(command, "") + "OK\n").encode())
        await writer.drain()
        writer.close()

    listener = await asyncio.start_server(server, "127.0.0.1", 0)
    async with listener:
        client = MPDClient("127.0.0.1", listener.sockets[0].getsockname()[1])
        await client.discover()
        assert (await client.snapshot())["state"] == "play"
        await client.play_media("https://example.com/test.mp3")
        assert commands[-3:] == ["clear", 'addid "https://example.com/test.mp3"', 'playid "12"']
        assert len(await client.queue()) == 2
        before = len(commands)
        with pytest.raises(ValueError):
            await client.play_media("bad\nclear")
        assert len(commands) == before


async def test_mpd_ack_does_not_leak_server_message(socket_enabled):
    async def server(reader, writer):
        writer.write(b"OK MPD 0.23.5\n")
        await writer.drain()
        await reader.readline()
        writer.write(b"ACK [4@0] {password} secret-detail\n")
        await writer.drain()
        writer.close()

    listener = await asyncio.start_server(server, "127.0.0.1", 0)
    async with listener:
        client = MPDClient("127.0.0.1", listener.sockets[0].getsockname()[1])
        with pytest.raises(BridgeError, match="mpd_command_rejected"):
            await client.execute("status")
