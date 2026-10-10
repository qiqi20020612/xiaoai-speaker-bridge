import asyncio
import json

import aiohttp
import pytest
from aiohttp import web

from custom_components.xiaoai_speaker.client import BridgeClient, BridgeError


async def test_chunked_http_response_is_collected_with_a_bound(socket_enabled):
    app = web.Application()

    async def fragmented(request):
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        payload = json.dumps({"protocol": 1, "device_id": "synthetic", "capabilities": {}}).encode()
        await response.write(payload[:15])
        await asyncio.sleep(0.01)
        await response.write(payload[15:])
        await response.write_eof()
        return response

    async def oversized(request):
        return web.Response(body=b"x" * 131073)

    app.router.add_get("/api/info", fragmented)
    app.router.add_post("/too-large", oversized)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as session:
            client = BridgeClient(session, "127.0.0.1", "synthetic", port)
            assert (await client.info())["device_id"] == "synthetic"
            with pytest.raises(BridgeError, match="response_too_large") as error:
                await client.request("POST", "/too-large", {})
            assert error.value.uncertain
    finally:
        await runner.cleanup()
