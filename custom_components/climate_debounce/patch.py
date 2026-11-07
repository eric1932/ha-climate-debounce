"""Wrap a climate entity class so selected entities go through a CommandBuffer.

The class is patched in place (no new entity, nothing to re-expose to HomeKit,
Google or Alexa). Entities of the same class that are not configured keep the
original behaviour: every wrapper looks up a buffer by entity_id first and
falls through to the original when there is none. ``restore()`` puts the
original attributes back.
"""

from __future__ import annotations

from collections.abc import Callable
import inspect
from typing import Any

from homeassistant.const import PRECISION_TENTHS

from .buffer import PROPERTY, CommandBuffer, Originals

# Other commands that cost quota on cloud thermostats. They are not debounced,
# only sent in turn and within the budget.
PASSTHROUGH = ("async_set_preset_mode", "async_set_fan_mode")

_MISSING = object()
MARKER = "__climate_debounce__"
ORIGINAL = "__climate_debounce_original__"

Lookup = Callable[[Any], CommandBuffer | None]


def _ours(value: Any) -> bool:
    return bool(getattr(value, MARKER, False) or getattr(getattr(value, "fget", None), MARKER, False))


def _unwrap(value: Any) -> Any:
    """Skip our own wrappers inherited from an already patched base class."""
    for _ in range(10):
        inner = getattr(value, ORIGINAL, None)
        if inner is None:
            inner = getattr(getattr(value, "fget", None), ORIGINAL, None)
        if inner is None:
            return value
        value = inner
    return value


class ClassPatch:
    """Patch one entity class."""

    NAMES = (*PROPERTY.values(), "precision", "async_set_temperature", "async_set_hvac_mode")

    def __init__(self, cls: type, lookup: Lookup) -> None:
        """Capture the class's current implementations."""
        self.cls = cls
        self._lookup = lookup
        self._saved: dict[str, Any] = {}
        for name in (*self.NAMES, *PASSTHROUGH):
            if _ours(cls.__dict__.get(name)):
                raise RuntimeError(f"{cls.__qualname__}.{name} is already patched")
        self._static = {
            name: _unwrap(inspect.getattr_static(cls, name, None))
            for name in (*self.NAMES, *PASSTHROUGH)
        }
        self.originals = Originals(
            props={name: self._static[name] for name in PROPERTY.values()},
            set_temperature=self._static["async_set_temperature"],
            set_hvac_mode=self._static["async_set_hvac_mode"],
        )

    def apply(self) -> None:
        """Install the wrappers."""
        for key, name in PROPERTY.items():
            self._set(name, self._property(key, self._static[name]))
        if self._static["precision"] is not None:
            self._set("precision", self._precision(self._static["precision"]))
        self._set("async_set_temperature", self._set_temperature())
        self._set("async_set_hvac_mode", self._set_hvac_mode())
        for name in PASSTHROUGH:
            if self._static[name] is not None:
                self._set(name, self._passthrough(self._static[name]))

    def restore(self) -> None:
        """Put the original attributes back."""
        for name, value in self._saved.items():
            if value is _MISSING:
                delattr(self.cls, name)
            else:
                setattr(self.cls, name, value)
        self._saved.clear()

    def _set(self, name: str, value: Any) -> None:
        self._saved.setdefault(name, self.cls.__dict__.get(name, _MISSING))
        setattr(self.cls, name, value)

    @staticmethod
    def _mark(func: Any, original: Any) -> None:
        setattr(func, MARKER, True)
        setattr(func, ORIGINAL, original)

    def _property(self, key: str, original: Any) -> property:
        lookup = self._lookup

        def fget(entity: Any) -> Any:
            real = original.__get__(entity, type(entity))
            buffer = lookup(entity)
            return real if buffer is None else buffer.shown(key, real)

        self._mark(fget, original)
        return property(fget, doc=getattr(original, "__doc__", None))

    def _precision(self, original: Any) -> property:
        lookup = self._lookup

        def fget(entity: Any) -> Any:
            buffer = lookup(entity)
            if buffer is not None and buffer.settings.one_decimal:
                return PRECISION_TENTHS
            return original.__get__(entity, type(entity))

        self._mark(fget, original)
        return property(fget, doc=getattr(original, "__doc__", None))

    def _set_temperature(self) -> Callable[..., Any]:
        lookup = self._lookup
        original = self.originals.set_temperature

        async def async_set_temperature(entity: Any, **kwargs: Any) -> None:
            buffer = lookup(entity)
            if buffer is None:
                await original(entity, **kwargs)
                return
            buffer.write_temperature(entity, kwargs)
            entity.async_write_ha_state()

        self._mark(async_set_temperature, original)
        return async_set_temperature

    def _set_hvac_mode(self) -> Callable[..., Any]:
        lookup = self._lookup
        original = self.originals.set_hvac_mode

        async def async_set_hvac_mode(entity: Any, hvac_mode: Any) -> None:
            buffer = lookup(entity)
            if buffer is None:
                await original(entity, hvac_mode)
                return
            buffer.write_hvac_mode(entity, hvac_mode)
            entity.async_write_ha_state()

        self._mark(async_set_hvac_mode, original)
        return async_set_hvac_mode

    def _passthrough(self, original: Callable[..., Any]) -> Callable[..., Any]:
        lookup = self._lookup

        async def wrapper(entity: Any, *args: Any, **kwargs: Any) -> Any:
            buffer = lookup(entity)
            if buffer is None:
                return await original(entity, *args, **kwargs)
            return await buffer.passthrough(lambda: original(entity, *args, **kwargs))

        wrapper.__name__ = getattr(original, "__name__", "wrapper")
        self._mark(wrapper, original)
        return wrapper
