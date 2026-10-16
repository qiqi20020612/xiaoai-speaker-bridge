import asyncio
import json

import aiohttp
import pytest
from aiohttp import web

from custom_components.xiaoai_speaker.client import ARTWORK_LIMIT, BridgeClient, BridgeError


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


@pytest.mark.parametrize(
    "mode",
    [
        "valid",
        "missing",
        "redirect",
        "unauthorized",
        "type",
        "magic",
        "oversized",
        "chunked-oversized",
    ],
)
async def test_artwork_is_authenticated_bounded_and_never_follows_redirects(socket_enabled, mode):
    calls = []
    app = web.Application()
    image_id = "cover-" + "a" * 32 + ".jpg"

    async def image(request):
        calls.append(request.path)
        assert request.headers["Authorization"] == "Bearer synthetic-art-token"
        if mode == "missing":
            return web.Response(status=404)
        if mode == "unauthorized":
            return web.Response(status=401)
        if mode == "redirect":
            return web.Response(status=302, headers={"Location": "/forbidden"})
        body = b"\xff\xd8\xff" + b"x" * 40
        if "oversized" in mode:
            body += b"x" * ARTWORK_LIMIT
        if mode == "chunked-oversized":
            response = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
            await response.prepare(request)
            await response.write(body)
            await response.write_eof()
            return response
        return web.Response(
            body=b"invalid" if mode == "magic" else body,
            content_type="text/html" if mode == "type" else "image/jpeg",
        )

    async def forbidden(request):
        calls.append(request.path)
        return web.Response(body=b"not reached")

    app.router.add_get("/api/airplay/artwork/{image_id}", image)
    app.router.add_get("/forbidden", forbidden)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as session:
            client = BridgeClient(session, "127.0.0.1", "synthetic-art-token", port)
            with pytest.raises(BridgeError, match="invalid_artwork_id"):
                await client.artwork("../config.json")
            assert calls == []
            if mode == "valid":
                assert await client.artwork(image_id) == (b"\xff\xd8\xff" + b"x" * 40, "image/jpeg")
            elif mode == "missing":
                assert await client.artwork(image_id) == (None, None)
            else:
                with pytest.raises(BridgeError):
                    await client.artwork(image_id)
            assert calls == ["/api/airplay/artwork/" + image_id]
    finally:
        await runner.cleanup()
