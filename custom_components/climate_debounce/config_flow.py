"""Config and options flow for Climate Debounce."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_DELAY,
    CONF_ONE_DECIMAL,
    CONF_PER_MINUTE,
    DEFAULT_DELAY,
    DEFAULT_ONE_DECIMAL,
    DEFAULT_PER_MINUTE,
    DOMAIN,
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DELAY, default=DEFAULT_DELAY): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.5,
                max=60,
                step=0.5,
                unit_of_measurement="s",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_PER_MINUTE, default=DEFAULT_PER_MINUTE): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=600, step=1, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(
            CONF_ONE_DECIMAL, default=DEFAULT_ONE_DECIMAL
        ): selector.BooleanSelector(),
    }
)


def _options(data: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_DELAY: float(data.get(CONF_DELAY, DEFAULT_DELAY)),
        CONF_PER_MINUTE: int(data.get(CONF_PER_MINUTE, DEFAULT_PER_MINUTE)),
        CONF_ONE_DECIMAL: bool(data.get(CONF_ONE_DECIMAL, DEFAULT_ONE_DECIMAL)),
    }


class ClimateDebounceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one buffered thermostat per entry."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a climate entity and tune the buffer."""
        if user_input is not None:
            entity_id = user_input[CONF_ENTITY_ID]
            await self.async_set_unique_id(entity_id)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._title(entity_id),
                data={CONF_ENTITY_ID: entity_id},
                options=_options(user_input),
            )
        schema = vol.Schema(
            {
                vol.Required(CONF_ENTITY_ID): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=CLIMATE_DOMAIN)
                )
            }
        ).extend(OPTIONS_SCHEMA.schema)
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Create or update an entry from YAML."""
        entity_id = import_data[CONF_ENTITY_ID]
        options = _options(import_data)
        await self.async_set_unique_id(entity_id)
        for entry in self._async_current_entries(include_ignore=False):
            if entry.unique_id == entity_id:
                if dict(entry.options) != options:
                    self.hass.config_entries.async_update_entry(entry, options=options)
                return self.async_abort(reason="already_configured")
        return self.async_create_entry(
            title=self._title(entity_id),
            data={CONF_ENTITY_ID: entity_id},
            options=options,
        )

    def _title(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        return state.name if state else entity_id

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return ClimateDebounceOptionsFlow()


class ClimateDebounceOptionsFlow(OptionsFlow):
    """Change the delay, the budget or the display precision."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form."""
        if user_input is not None:
            return self.async_create_entry(data=_options(user_input))
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
