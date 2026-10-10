"""Stable device identity shared by all platforms."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class XiaoAIEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        return super().available and not self.coordinator.data.get("state_stale", False)

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
