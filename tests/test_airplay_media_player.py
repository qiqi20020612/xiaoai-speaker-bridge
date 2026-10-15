"""Existing HA player metadata, progress anchors and one bounded image cache."""

import asyncio
import copy
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.media_player import MediaPlayerEntityFeature

from custom_components.xiaoai_speaker.client import BridgeError
from custom_components.xiaoai_speaker.diagnostics import async_get_config_entry_diagnostics
from tests.test_airplay_metadata import ART
from tests.test_capabilities import setup
from tests.test_homeassistant import STATE
from tests.test_homeassistant import mock_clients as mock_clients

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic"


def state(**updates):
    airplay = {
        "active": True,
        "state": "Playing",
        "song": {
            "title": "Synthetic Song",
            "artist": ["Synthetic Artist", "Other Artist"],
            "album": "Synthetic Album",
            "file": "/org/gnome/ShairportSync/123",
        },
        "status": {"duration": 180, "elapsed": 10},
        "progress": "0/441000/7938000",
        "artwork_id": ART,
    } | updates
    return copy.deepcopy(STATE) | {
        "instance_id": "synthetic-instance",
        "active_source": "airplay",
        "airplay": airplay,
    }


async def publish(hass, coordinator, value):
    coordinator.async_set_updated_data(value)
    await hass.async_block_till_done()


async def test_existing_player_displays_metadata_via_native_proxy(hass, mock_clients, hass_client):
    entry, c = await setup(hass)
    player = hass.data["media_player"].get_entity("media_player.synthetic_speaker")
    try:
        await publish(hass, c, state())
        attrs = hass.states.get(player.entity_id).attributes
        assert attrs["media_title"] == "Synthetic Song"
        assert attrs["media_artist"] == "Synthetic Artist, Other Artist"
        assert attrs["media_album_name"] == "Synthetic Album"
        assert attrs["media_duration"] == 180
        assert attrs["media_position"] == 10
        assert player.media_content_id == "/org/gnome/ShairportSync/123"
        assert player.media_image_url is None
        assert player.entity_picture.startswith("/api/media_player_proxy/")
        assert entry.data["token"] not in player.entity_picture
        serialized = json.dumps(attrs, default=str)
        assert "file:" not in serialized and "/tmp/" not in serialized
        assert not player.supported_features & MediaPlayerEntityFeature.SEEK
        with patch.object(c.client, "artwork", AsyncMock(return_value=(PNG, "image/png"))) as fetch:
            client = await hass_client()
            response = await client.get(player.entity_picture)
            assert response.status == 200 and await response.read() == PNG
            assert response.content_type == "image/png"
            fetch.assert_awaited_once_with(ART)
        diagnostics = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
        assert "Synthetic Song" not in diagnostics and ART not in diagnostics
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cached_progress_is_not_reanchored_by_polls_jobs_or_volume_updates(
    hass, mock_clients, freezer
):
    entry, c = await setup(hass)
    player = hass.data["media_player"].get_entity("media_player.synthetic_speaker")
    clock = Mock(return_value=1000.0)
    try:
        with patch("custom_components.xiaoai_speaker.media_player.monotonic", clock):
            await publish(hass, c, state())
            initial = player.media_position_updated_at
            clock.return_value += 20
            freezer.tick(20)
            await publish(hass, c, state() | {"system": {"cpu_load": 0.1}})
            c.async_update_listeners()  # Existing job notifications also reach the player.
            assert player.media_position == 10 and player.media_position_updated_at == initial
            clock.return_value += 10
            freezer.tick(10)
            await publish(hass, c, state(state="Paused"))
            assert player.media_position == 40
            paused = player.media_position_updated_at
            clock.return_value += 30
            freezer.tick(30)
            await publish(hass, c, state(state="Paused"))
            assert player.media_position == 40 and player.media_position_updated_at == paused
            await publish(hass, c, state(state="Playing"))
            assert player.media_position == 40 and player.media_position_updated_at != paused
            # A new sender observation (including seek) replaces the old anchor.
            await publish(
                hass,
                c,
                state(progress="0/2646000/7938000", status={"duration": 180, "elapsed": 60}),
            )
            assert player.media_position == 60
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_track_switch_does_not_inherit_old_progress_or_artwork(hass, mock_clients):
    entry, c = await setup(hass)
    player = hass.data["media_player"].get_entity("media_player.synthetic_speaker")
    try:
        first = state()
        await publish(hass, c, first)
        previous = player.media_image_hash
        second = state()
        second["airplay"]["song"]["title"] = "Next Synthetic Song"
        second["airplay"]["song"]["file"] = "/org/gnome/ShairportSync/456"
        second["airplay"]["artwork_id"] = ART.replace("a", "b")
        await publish(hass, c, second)
        assert player.media_title == "Next Synthetic Song"
        assert player.media_image_hash != previous and player.media_position is None
        await publish(hass, c, second)
        assert player.media_position is None
        second["airplay"]["progress"] = "100/132400/7938100"
        second["airplay"]["status"]["elapsed"] = 3
        await publish(hass, c, second)
        assert player.media_position == 3
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "change",
    [
        {"active_source": "mpd"},
        {"active_source": "bluetooth"},
        {"state_stale": True},
        {"airplay": {"active": False, "state": "Stopped"}},
        {"airplay": {"active": True, "state": "Playing", "song": None, "status": None}},
    ],
)
async def test_missing_metadata_stop_source_change_and_stale_clear_image_cache(
    hass, mock_clients, change
):
    entry, c = await setup(hass)
    player = hass.data["media_player"].get_entity("media_player.synthetic_speaker")
    try:
        await publish(hass, c, state())
        with patch.object(c.client, "artwork", AsyncMock(return_value=(PNG, "image/png"))):
            assert await player.async_get_media_image() == (PNG, "image/png")
        await publish(hass, c, state() | change)
        assert player.media_image_hash is None
        assert await player.async_get_media_image() == (None, None)
        assert player._image_data == (None, None)
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_image_requests_share_fetch_expire_and_release_on_unload(hass, mock_clients):
    entry, c = await setup(hass)
    player = hass.data["media_player"].get_entity("media_player.synthetic_speaker")
    clock = Mock(return_value=1000.0)
    with patch("custom_components.xiaoai_speaker.media_player.monotonic", clock):
        await publish(hass, c, state())
        with patch.object(c.client, "artwork", AsyncMock(return_value=(PNG, "image/png"))) as fetch:
            assert (
                await asyncio.gather(*[player.async_get_media_image() for _ in range(5)])
                == [(PNG, "image/png")] * 5
            )
            assert fetch.await_count == 1
            clock.return_value += 299
            assert await player.async_get_media_image() == (PNG, "image/png")
            assert fetch.await_count == 1
            clock.return_value += 2
            c.async_update_listeners()
            assert player._image_data == (None, None)
            assert await player.async_get_media_image() == (PNG, "image/png")
            assert fetch.await_count == 2
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert player._image_data == (None, None)


async def test_missing_image_negative_cache_and_track_change_during_fetch(hass, mock_clients):
    entry, c = await setup(hass)
    player = hass.data["media_player"].get_entity("media_player.synthetic_speaker")
    clock = Mock(return_value=1000.0)
    try:
        with patch("custom_components.xiaoai_speaker.media_player.monotonic", clock):
            await publish(hass, c, state())
            with patch.object(
                c.client, "artwork", AsyncMock(side_effect=BridgeError("artwork_unavailable"))
            ) as fetch:
                assert await player.async_get_media_image() == (None, None)
                assert await player.async_get_media_image() == (None, None)
                assert fetch.await_count == 1
                clock.return_value += 31
                assert await player.async_get_media_image() == (None, None)
                assert fetch.await_count == 2
            clock.return_value += 31
            began, release = asyncio.Event(), asyncio.Event()

            async def delayed(_):
                began.set()
                await release.wait()
                return PNG, "image/png"

            with patch.object(c.client, "artwork", delayed):
                task = asyncio.create_task(player.async_get_media_image())
                await began.wait()
                second = state(artwork_id=ART.replace("a", "b"))
                await publish(hass, c, second)
                release.set()
                assert await task == (None, None)
                assert player._image_data == (None, None)
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_image_fetch_cannot_repopulate_cache_after_unload(hass, mock_clients):
    entry, c = await setup(hass)
    player = hass.data["media_player"].get_entity("media_player.synthetic_speaker")
    await publish(hass, c, state())
    began, release = asyncio.Event(), asyncio.Event()

    async def delayed(_):
        began.set()
        await release.wait()
        return PNG, "image/png"

    with patch.object(c.client, "artwork", delayed):
        task = asyncio.create_task(player.async_get_media_image())
        await began.wait()
        assert await hass.config_entries.async_unload(entry.entry_id)
        release.set()
        assert await task == (None, None)
        assert player._image_data == (None, None)


async def test_failed_metadata_does_not_reanchor_old_counter_when_it_returns(hass, mock_clients):
    entry, c = await setup(hass)
    player = hass.data["media_player"].get_entity("media_player.synthetic_speaker")
    try:
        await publish(hass, c, state())
        await publish(hass, c, state(song={}, status={}, progress=None))
        assert player.media_position is None
        await publish(hass, c, state())
        assert player.media_position is None
        await publish(
            hass, c, state(progress="0/882000/7938000", status={"duration": 180, "elapsed": 20})
        )
        assert player.media_position == 20
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
