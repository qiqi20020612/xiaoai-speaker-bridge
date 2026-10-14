"""XiaoAI Speaker Bridge runs entirely inside Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .client import BridgeClient
from .const import DEFAULT_MPD_PORT, DOMAIN, PLATFORMS
from .coordinator import XiaoAICoordinator
from .mpd import MPDClient
from .services import register_services
from .views import register_views


@dataclass
class RuntimeData:
    coordinator: XiaoAICoordinator


type XiaoAIConfigEntry = ConfigEntry[RuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    register_services(hass)
    register_views(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: XiaoAIConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = BridgeClient(
        session, entry.data[CONF_HOST], entry.data[CONF_TOKEN], entry.data[CONF_PORT]
    )
    mpd = MPDClient(
        entry.data[CONF_HOST],
        entry.data.get("mpd_port", DEFAULT_MPD_PORT),
        entry.data.get("mpd_password", ""),
    )
    coordinator = XiaoAICoordinator(hass, entry, client, mpd)
    await coordinator.async_config_entry_first_refresh()
    # Remove only this entry's retired manual LED entity, including disabled ones.
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.domain == "light"
            and entity.platform == DOMAIN
            and entity.unique_id == f"{entry.unique_id}_led"
        ):
            registry.async_remove(entity.entity_id)
    entry.runtime_data = RuntimeData(coordinator)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    coordinator.start()
    return True


async def _options_updated(hass: HomeAssistant, entry: XiaoAIConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: XiaoAIConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.coordinator.close()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        return True
    return False


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    from homeassistant.helpers.storage import Store

    await Store(hass, 1, f"{DOMAIN}.{entry.unique_id}.volume").async_remove()
