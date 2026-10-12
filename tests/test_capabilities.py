"""Real HA entity/registry/service behavior across device capability transitions."""

import copy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaoai_speaker.client import BridgeError
from custom_components.xiaoai_speaker.const import DOMAIN
from tests.test_homeassistant import DATA, INFO, STATE
from tests.test_homeassistant import mock_clients as mock_clients


async def setup(hass, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Synthetic speaker",
        data=DATA,
        unique_id="synthetic-speaker",
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, entry.runtime_data.coordinator


async def test_late_capabilities_loss_recovery_and_offline_keep_identity(hass, mock_clients):
    info = copy.deepcopy(INFO)
    info["capabilities"]["conversation"] = False
    info["capabilities"]["volume"]["airplay"] = False
    state = {**STATE, "capabilities": info["capabilities"]}
    with patch(
        "custom_components.xiaoai_speaker.client.BridgeClient.info", AsyncMock(return_value=info)
    ):
        with patch(
            "custom_components.xiaoai_speaker.client.BridgeClient.state",
            AsyncMock(return_value=state),
        ):
            entry, c = await setup(hass)
    registry = er.async_get(hass)
    uid = "synthetic-speaker_reset_context"
    assert registry.async_get_entity_id("button", DOMAIN, uid) is None
    caps = copy.deepcopy(INFO["capabilities"])
    caps["volume"]["airplay"] = True
    with patch.object(c.client, "state", AsyncMock(return_value={**STATE, "capabilities": caps})):
        await c.async_refresh()
        await hass.async_block_till_done()
    button = registry.async_get_entity_id("button", DOMAIN, uid)
    assert button and hass.states.get(button).state != "unavailable"
    assert registry.async_get_entity_id("number", DOMAIN, "synthetic-speaker_volume_airplay")
    size = len(registry.entities)
    caps["conversation"] = False
    with patch.object(c.client, "state", AsyncMock(return_value={**STATE, "capabilities": caps})):
        await c.async_refresh()
    assert hass.states.get(button).state == "unavailable"
    with patch.object(c.jobs, "submit", AsyncMock()) as submit:
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                DOMAIN,
                "send_text",
                {"entity_id": "media_player.synthetic_speaker", "text": "test"},
                blocking=True,
            )
        submit.assert_not_called()
    caps["conversation"] = True
    with patch.object(c.client, "state", AsyncMock(return_value={**STATE, "capabilities": caps})):
        await c.async_refresh()
        await c.async_refresh()
        await hass.async_block_till_done()
    assert registry.async_get_entity_id("button", DOMAIN, uid) == button
    assert len(registry.entities) == size
    with patch.object(c.client, "state", AsyncMock(side_effect=BridgeError("connection_failed"))):
        await c.async_refresh()
    assert hass.states.get(button).state == "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)
