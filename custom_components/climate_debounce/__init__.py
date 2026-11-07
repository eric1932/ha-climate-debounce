"""Climate Debounce: coalesce and rate-limit commands to cloud thermostats.

Apple Home (through the HomeKit Bridge), Google Assistant and Alexa send one
``climate.set_temperature`` per step while you drag a slider. Cloud APIs such
as Google Nest SDM allow only a few commands per minute per thermostat, so the
later steps are rejected (HTTP 429) or arrive out of order. This integration
holds the writes for a configured entity until they stop, then sends only the
final values, in order, within a per-minute budget.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, ClimateEntity
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_ENTITY_ID, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.hass_dict import HassKey

from .buffer import CommandBuffer, Settings
from .const import (
    CONF_DELAY,
    CONF_ONE_DECIMAL,
    CONF_PER_MINUTE,
    DEFAULT_DELAY,
    DEFAULT_ONE_DECIMAL,
    DEFAULT_PER_MINUTE,
    DOMAIN,
    MIN_MAX_WAIT,
)
from .patch import ClassPatch

_LOGGER = logging.getLogger(__name__)

ENTRY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): cv.entity_domain(CLIMATE_DOMAIN),
        vol.Optional(CONF_DELAY, default=DEFAULT_DELAY): vol.All(
            vol.Coerce(float), vol.Range(min=0.5, max=60)
        ),
        vol.Optional(CONF_PER_MINUTE, default=DEFAULT_PER_MINUTE): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=600)
        ),
        vol.Optional(CONF_ONE_DECIMAL, default=DEFAULT_ONE_DECIMAL): cv.boolean,
    }
)
CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.All(cv.ensure_list, [ENTRY_SCHEMA])}, extra=vol.ALLOW_EXTRA
)

DATA_MANAGER: HassKey[Manager] = HassKey(DOMAIN)


def settings_from(entry: ConfigEntry) -> Settings:
    """Build buffer settings from a config entry."""
    delay = float(entry.options.get(CONF_DELAY, DEFAULT_DELAY))
    return Settings(
        delay=delay,
        max_wait=max(MIN_MAX_WAIT, 3 * delay),
        per_minute=int(entry.options.get(CONF_PER_MINUTE, DEFAULT_PER_MINUTE)),
        one_decimal=bool(entry.options.get(CONF_ONE_DECIMAL, DEFAULT_ONE_DECIMAL)),
    )


class Manager:
    """Attach buffers to entities and patch their classes."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        self.hass = hass
        self.buffers: dict[str, CommandBuffer] = {}
        self._classes: dict[str, type] = {}
        self._settings: dict[str, Settings] = {}
        self._patches: dict[type, ClassPatch] = {}
        self._waiting: dict[str, list[CALLBACK_TYPE]] = {}
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._stop)

    def lookup(self, entity: Any) -> CommandBuffer | None:
        """Return the buffer for an entity, if it is configured."""
        return self.buffers.get(getattr(entity, "entity_id", None) or "")

    def _entity(self, entity_id: str) -> Any:
        component = self.hass.data.get(CLIMATE_DOMAIN)
        return component.get_entity(entity_id) if component else None

    @callback
    def add(self, entity_id: str, settings: Settings) -> None:
        """Start buffering an entity now, or as soon as it shows up."""
        self._settings[entity_id] = settings
        if self._attach(entity_id):
            return
        _LOGGER.info("%s is not loaded yet; will attach when it appears", entity_id)

        @callback
        def _retry(_event: Event | None = None) -> None:
            if entity_id in self.buffers or not self._attach(entity_id):
                return
            for unsub in self._waiting.pop(entity_id, []):
                unsub()

        # Adding an entity writes its first state, which fires this.
        self._waiting[entity_id] = [
            async_track_state_change_event(self.hass, [entity_id], _retry)
        ]

    @callback
    def update(self, entity_id: str, settings: Settings) -> None:
        """Apply new options in place, keeping pending writes and the budget."""
        self._settings[entity_id] = settings
        if buffer := self.buffers.get(entity_id):
            buffer.settings = settings
            if entity := self._entity(entity_id):
                entity.async_write_ha_state()  # the one_decimal option changes the state

    @callback
    def remove(self, entity_id: str) -> None:
        """Stop buffering an entity; unpatch its class if nothing else uses it."""
        self._settings.pop(entity_id, None)
        for unsub in self._waiting.pop(entity_id, []):
            unsub()
        buffer = self.buffers.pop(entity_id, None)
        cls = self._classes.pop(entity_id, None)
        if buffer is None:
            return
        buffer.close()
        if cls not in self._classes.values() and (patch := self._patches.pop(cls, None)):
            patch.restore()
            _LOGGER.debug("restored %s", cls.__qualname__)

    @callback
    def _stop(self, _event: Event) -> None:
        # Writes still waiting are dropped: Home Assistant is going away.
        for buffer in self.buffers.values():
            buffer.close(write_state=False)

    def _attach(self, entity_id: str) -> bool:
        if entity_id not in self._settings:
            return False
        entity = self._entity(entity_id)
        if entity is None:
            return False
        if not isinstance(entity, ClimateEntity):
            _LOGGER.error("%s is not a climate entity", entity_id)
            return False
        cls = type(entity)
        patch = self._patches.get(cls)
        if patch is None:
            patch = ClassPatch(cls, self.lookup)
            patch.apply()
            self._patches[cls] = patch

        def resolve() -> Any:
            # A new object after the entity's integration reloads; same class.
            current = self._entity(entity_id)
            return current if type(current) is cls else None

        settings = self._settings[entity_id]
        self.buffers[entity_id] = CommandBuffer(
            self.hass, entity_id, settings, patch.originals, resolve
        )
        self._classes[entity_id] = cls
        _LOGGER.info(
            "buffering %s (%s): send %.1fs after the last change, at most %d commands/min%s",
            entity_id,
            f"{cls.__module__}.{cls.__qualname__}",
            settings.delay,
            settings.per_minute,
            ", one decimal" if settings.one_decimal else "",
        )
        if settings.one_decimal:
            entity.async_write_ha_state()
        return True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Import YAML entries (YAML wins over UI options on every start)."""
    for item in config.get(DOMAIN, []):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=dict(item)
            )
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start buffering the entry's entity."""
    manager = hass.data.get(DATA_MANAGER)
    if manager is None:
        manager = hass.data[DATA_MANAGER] = Manager(hass)
    entity_id: str = entry.data[CONF_ENTITY_ID]
    manager.add(entity_id, settings_from(entry))
    entry.async_on_unload(lambda: manager.remove(entity_id))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an entry (the on_unload callbacks do the work)."""
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    if manager := hass.data.get(DATA_MANAGER):
        manager.update(entry.data[CONF_ENTITY_ID], settings_from(entry))
