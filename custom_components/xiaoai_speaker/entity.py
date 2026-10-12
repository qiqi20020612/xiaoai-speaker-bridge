"""Stable device identity shared by all platforms."""

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class XiaoAIEntity(CoordinatorEntity):
    _attr_has_entity_name = True
    required_capability: tuple[str, ...] = ()

    @property
    def capability_available(self) -> bool:
        value = self.coordinator.info.get("capabilities", {})
        for key in self.required_capability:
            value = value.get(key, False) if isinstance(value, dict) else False
        return bool(value) if self.required_capability else True

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.capability_available
            and not (self.coordinator.data or {}).get("state_stale", False)
        )

    def require_available(self) -> None:
        if not self.available:
            raise HomeAssistantError("device_or_capability_unavailable")

    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.unique_id)},
            manufacturer="Xiaomi",
            model=coordinator.info.get("model", "LX06"),
            name=coordinator.entry.title,
            sw_version=coordinator.info.get("version"),
        )


def register_capability_entities(entry, async_add_entities, entities) -> None:
    """Add once when ready; retain registry identity while capabilities are absent."""
    coordinator = entry.runtime_data.coordinator
    added = set()

    def discover():
        if not coordinator.last_update_success:
            return
        ready = [
            entity
            for entity in entities
            if entity.unique_id not in added and entity.capability_available
        ]
        if ready:
            added.update(entity.unique_id for entity in ready)
            async_add_entities(ready)

    entry.async_on_unload(coordinator.async_add_listener(discover))
    discover()
