"""UI configuration, reauthentication, reconfiguration and speaker options."""

from __future__ import annotations

import probatio as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import AuthenticationError, BridgeClient, BridgeError, validate_host
from .const import (
    DEFAULT_MPD_PORT,
    DEFAULT_POLL,
    DEFAULT_PORT,
    DOMAIN,
    SOURCE_PRIORITY,
)


def schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(CONF_TOKEN): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional("mpd_port", default=defaults.get("mpd_port", DEFAULT_MPD_PORT)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Optional("mpd_password", default=""): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


class XiaoAIConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, user_input: dict) -> dict:
        user_input[CONF_HOST] = validate_host(user_input[CONF_HOST])
        if len(user_input[CONF_TOKEN]) < 32 or any(c.isspace() for c in user_input[CONF_TOKEN]):
            raise ValueError("invalid_auth")
        client = BridgeClient(
            async_get_clientsession(self.hass),
            user_input[CONF_HOST],
            user_input[CONF_TOKEN],
            user_input[CONF_PORT],
        )
        return await client.info()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input:
            try:
                info = await self._validate(user_input)
                await self.async_set_unique_id(info["device_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=info.get("name", "XiaoAI Speaker"), data=user_input
                )
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except BridgeError as err:
                errors["base"] = (
                    "incompatible_protocol"
                    if err.code == "incompatible_protocol"
                    else "cannot_connect"
                )
            except ValueError:
                errors["base"] = "invalid_input"
        return self.async_show_form(
            step_id="user", data_schema=schema(user_input or {}), errors=errors
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input:
            try:
                info = await self._validate(user_input)
                if info["device_id"] != entry.unique_id:
                    return self.async_abort(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
            except BridgeError, ValueError:
                errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema(entry.data), errors=errors
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input:
            try:
                info = await self._validate({**entry.data, **user_input})
                if info["device_id"] != entry.unique_id:
                    return self.async_abort(reason="wrong_device")
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input, reason="reauth_successful"
                )
            except BridgeError, ValueError:
                errors["base"] = "invalid_auth"
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return XiaoAIOptionsFlow()


class XiaoAIOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        opts = {
            k: v
            for k, v in self.config_entry.options.items()
            if k not in ("agent_id", "idle_policy")
        }
        priority = opts.get("source_priority", list(SOURCE_PRIORITY))
        if (
            not isinstance(priority, list)
            or len(priority) != 3
            or any(not isinstance(source, str) for source in priority)
            or set(priority) != set(SOURCE_PRIORITY)
        ):
            priority = list(SOURCE_PRIORITY)
        defaults = {
            **opts,
            **{f"source_priority_{i + 1}": source for i, source in enumerate(priority)},
        }
        if user_input is not None:
            priority = [user_input.get(f"source_priority_{i}") for i in range(1, 4)]
            if any(source not in SOURCE_PRIORITY for source in priority) or len(set(priority)) != 3:
                errors["base"] = "invalid_source_priority"
                defaults.update(user_input)
            else:
                data = {**opts, **user_input, "source_priority": priority}
                for i in range(1, 4):
                    data.pop(f"source_priority_{i}", None)
                return self.async_create_entry(title="", data=data)
        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "poll_interval", default=defaults.get("poll_interval", DEFAULT_POLL)
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=300)),
                    **{
                        vol.Required(
                            f"source_priority_{i}", default=defaults[f"source_priority_{i}"]
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(options=list(SOURCE_PRIORITY))
                        )
                        for i in range(1, 4)
                    },
                    vol.Optional(
                        "announce_ducking", default=defaults.get("announce_ducking", False)
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        "announce_duck_volume", default=defaults.get("announce_duck_volume", 10)
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=100, step=1, mode=selector.NumberSelectorMode.SLIDER
                        )
                    ),
                }
            ),
        )
