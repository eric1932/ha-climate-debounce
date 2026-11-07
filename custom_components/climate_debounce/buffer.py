"""Per-thermostat command buffer.

Collects writes for one climate entity, waits until they stop for ``delay``
seconds, then sends only the latest values: one command at a time, never more
than ``per_minute`` commands in any rolling window, and nothing at all when the
device already has (or is about to have) the requested value.

While a value is waiting to be sent, or has been sent but the device has not
reported it back yet, the entity reports the requested value. UIs such as Apple
Home therefore do not snap back to the old setpoint in the meantime.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__package__)

MODE = ATTR_HVAC_MODE
TEMP = ATTR_TEMPERATURE
LOW = ATTR_TARGET_TEMP_LOW
HIGH = ATTR_TARGET_TEMP_HIGH
TEMP_KEYS = (TEMP, LOW, HIGH)

# Entity property that reports each buffered key.
PROPERTY = {
    MODE: "hvac_mode",
    TEMP: "target_temperature",
    LOW: "target_temperature_low",
    HIGH: "target_temperature_high",
}

MAX_RETRIES = 3  # rate-limit retries for one batch before giving up
_RATE_LIMITED = re.compile(
    r"\b429\b|rate.?limit|too many requests|resource.?exhausted", re.IGNORECASE
)

# Set (to the buffer) only inside the task that runs the integration's own
# set_temperature, so its internal reads of the current setpoints see the
# device's values. Everything else (state writes from other tasks while the
# request is in flight) keeps seeing the requested values.
_REAL_SETPOINTS: ContextVar[CommandBuffer | None] = ContextVar(
    "climate_debounce_real_setpoints", default=None
)

Step = tuple[dict[str, Any], str, Callable[[], Coroutine[Any, Any, Any]]]


@dataclass(frozen=True)
class Settings:
    """Tuning for one buffered entity."""

    delay: float = 5.0
    max_wait: float = 15.0
    per_minute: int = 5
    window: float = 60.0
    confirm_timeout: float = 30.0  # stop showing a sent value the device never reports
    one_decimal: bool = False  # report temperatures to 0.1° even when HA would round


@dataclass(frozen=True)
class Originals:
    """The entity class's own implementations, captured before patching."""

    props: dict[str, Any]
    set_temperature: Callable[..., Coroutine[Any, Any, None]]
    set_hvac_mode: Callable[..., Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class _Sent:
    value: Any
    at: float


def is_rate_limited(err: BaseException) -> bool:
    """Return True if an error (or its cause chain) looks like a 429."""
    exc: BaseException | None = err
    for _ in range(6):
        if exc is None:
            return False
        if _RATE_LIMITED.search(f"{type(exc).__name__}: {exc}"):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


class CommandBuffer:
    """Debounce, rate-limit and serialize commands for one climate entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        settings: Settings,
        originals: Originals,
        resolve: Callable[[], Any],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the buffer.

        ``resolve`` returns the live entity object for ``entity_id`` (it is a
        new object after its integration reloads), or None if it is gone.
        """
        self.hass = hass
        self.entity_id = entity_id
        self.settings = settings
        self._orig = originals
        self._resolve = resolve
        self._clock = clock
        self.desired: dict[str, Any] = {}  # what the user asked for, shown in state
        self.dirty: set[str] = set()  # not sent yet
        self.inflight: set[str] = set()  # being sent right now
        self.sent: dict[str, _Sent] = {}  # sent, waiting for the device to report it
        self._first_write: float | None = None
        self._writes = 0
        self._timer: asyncio.TimerHandle | None = None
        self._expiry: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._sends: deque[float] = deque()
        self._retries = 0
        self._closed = False

    # ------------------------------------------------------------------ state

    def close(self, write_state: bool = True) -> None:
        """Drop everything pending and stop showing requested values."""
        self._closed = True
        for handle in (self._timer, self._expiry):
            if handle:
                handle.cancel()
        self._timer = self._expiry = None
        for task in self._tasks:
            task.cancel()
        had_values = bool(self.desired)
        self.desired.clear()
        self.dirty.clear()
        self.inflight.clear()
        self.sent.clear()
        if write_state and had_values:
            self._write_state(self._resolve())

    def real(self, entity: Any, key: str) -> Any:
        """Value the integration itself reports for a key."""
        return self._orig.props[PROPERTY[key]].__get__(entity, type(entity))

    def shown(self, key: str, real: Any) -> Any:
        """Value the entity should report for a key."""
        if key not in self.desired:
            return real
        if key in TEMP_KEYS and _REAL_SETPOINTS.get() is self:
            return real
        if key in self.dirty or key in self.inflight:
            return self.desired[key]
        sent = self.sent.get(key)
        if sent is None or self._matches(key, real, sent.value) or self._expired(sent):
            # Confirmed by the device, or never will be: report the truth again.
            self.desired.pop(key, None)
            self.sent.pop(key, None)
            return real
        return self.desired[key]

    def _expired(self, sent: _Sent) -> bool:
        return self._clock() - sent.at > self.settings.confirm_timeout

    def _baseline(self, key: str, real: Any) -> Any:
        """What the device has, or will have once the last command lands."""
        sent = self.sent.get(key)
        if sent is not None and not self._expired(sent):
            return sent.value
        return real

    def _matches(self, key: str, real: Any, want: Any) -> bool:
        if key == MODE:
            return real == want
        if real is None or want is None:
            return False
        entity = self._resolve()
        unit = getattr(entity, "temperature_unit", UnitOfTemperature.CELSIUS)
        # Half a display step: devices round to 0.5 °C or to whole °F (0.56 °C).
        tolerance = 0.3 if unit == UnitOfTemperature.CELSIUS else 0.55
        return abs(float(real) - float(want)) <= tolerance

    @staticmethod
    def _write_state(entity: Any) -> None:
        if entity is not None and getattr(entity, "hass", None) is not None:
            entity.async_write_ha_state()

    # ----------------------------------------------------------------- writes

    def write_hvac_mode(self, entity: Any, mode: Any) -> None:
        """Queue a mode change."""
        self._queue_mode(entity, mode)
        self._schedule()

    def write_temperature(self, entity: Any, values: dict[str, Any]) -> None:
        """Queue the values of a set_temperature call."""
        if values.get(MODE) is not None:
            self._queue_mode(entity, values[MODE])
        for key in TEMP_KEYS:
            if values.get(key) is not None:
                self._want(key, values[key])
        self._schedule()

    def _queue_mode(self, entity: Any, mode: Any) -> None:
        current = self.desired.get(MODE, self.real(entity, MODE))
        if mode != current:
            # Setpoints requested for the old mode no longer apply (a single
            # setpoint means heat or cool depending on the mode; a range only
            # exists in heat_cool). Forget them, sent or not.
            for key in TEMP_KEYS:
                self.desired.pop(key, None)
                self.dirty.discard(key)
                self.inflight.discard(key)
                self.sent.pop(key, None)
        self._want(MODE, mode)

    def _want(self, key: str, value: Any) -> None:
        self.desired[key] = value
        self.dirty.add(key)
        self._writes += 1

    def _schedule(self, delay: float | None = None) -> None:
        if self._closed or self.hass.is_stopping:
            return
        now = self._clock()
        if self._first_write is None:
            self._first_write = now
        if delay is None:
            deadline = self._first_write + self.settings.max_wait
            delay = max(0.0, min(self.settings.delay, deadline - now))
        if self._timer:
            self._timer.cancel()
        self._timer = self.hass.loop.call_later(delay, self._kick)

    def _kick(self) -> None:
        self._timer = None
        if self._closed or self.hass.is_stopping:
            return
        task = self.hass.async_create_background_task(
            self._flush(), f"{DOMAIN} flush {self.entity_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------------------------------------------------------------- sending

    async def passthrough(self, call: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        """Run another command (preset, fan) in turn and within the budget."""
        async with self._lock:
            await self._wait_for_budget()
            self._sends.append(self._clock())
            return await call()

    async def _flush(self) -> None:
        async with self._lock:
            if self._closed or not self.dirty:
                return
            # Wait first, so the batch holds the newest values once we may send.
            await self._wait_for_budget()
            if self._closed or not self.dirty:
                return
            entity = self._resolve()
            if entity is None:
                _LOGGER.warning(
                    "%s is gone; dropping %s",
                    self.entity_id,
                    {key: self.desired.get(key) for key in self.dirty},
                )
                for key in self.dirty:
                    self.desired.pop(key, None)
                self.dirty.clear()
                return
            batch = {key: self.desired[key] for key in self.dirty}
            writes, self._writes = self._writes, 0
            self.dirty.clear()
            self._first_write = None
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self.inflight = set(batch)
            try:
                await self._send(entity, batch, writes)
            finally:
                self.inflight = set()

    def _prune(self) -> None:
        now = self._clock()
        while self._sends and now - self._sends[0] >= self.settings.window:
            self._sends.popleft()

    async def _wait_for_budget(self) -> None:
        """Wait until one more command fits in the rolling window."""
        while True:
            self._prune()
            if len(self._sends) < self.settings.per_minute:
                return
            wait = self.settings.window - (self._clock() - self._sends[0]) + 0.2
            _LOGGER.debug(
                "%s: %d commands in the last %.0fs, waiting %.1fs",
                self.entity_id,
                len(self._sends),
                self.settings.window,
                wait,
            )
            await asyncio.sleep(max(wait, 0.05))

    def _plan(self, entity: Any, batch: dict[str, Any]) -> list[Step]:
        """Turn a batch into the commands that actually need sending."""
        steps: list[Step] = []
        mode = batch.get(MODE)
        if mode is not None:
            real_mode = self.real(entity, MODE)
            if mode != self._baseline(MODE, real_mode):
                steps.append(
                    (
                        {MODE: mode},
                        f"hvac_mode={mode}",
                        lambda: self._orig.set_hvac_mode(entity, mode),
                    )
                )
            else:
                self._skip(MODE, real_mode)

        temps = {key: batch[key] for key in TEMP_KEYS if key in batch}
        if not temps:
            return steps
        # The device's own setpoints, for the mode it is about to be in.
        real = {key: self.real(entity, key) for key in TEMP_KEYS}
        if LOW in temps or HIGH in temps:
            send_keys = [key for key in (LOW, HIGH) if key in temps]
        else:
            send_keys = [TEMP]
        for key in temps:
            if key not in send_keys:  # a single setpoint sent alongside a range
                self.desired.pop(key, None)
                self.inflight.discard(key)
        changed = [
            key
            for key in send_keys
            if not self._matches(key, self._baseline(key, real[key]), temps[key])
        ]
        if not changed:
            for key in send_keys:
                self._skip(key, real[key])
            return steps
        if TEMP in send_keys:
            kwargs = {TEMP: temps[TEMP]}
        else:
            kwargs = {
                LOW: temps.get(LOW, self._baseline(LOW, real[LOW])),
                HIGH: temps.get(HIGH, self._baseline(HIGH, real[HIGH])),
            }
            kwargs = {key: value for key, value in kwargs.items() if value is not None}
        described = ", ".join(f"{key}={value:g}" for key, value in kwargs.items())
        values = {key: temps[key] for key in send_keys}
        steps.append((values, described, lambda: self._call_set_temperature(entity, kwargs)))
        return steps

    async def _call_set_temperature(self, entity: Any, kwargs: dict[str, Any]) -> None:
        # The integration may read its own current setpoints (e.g. Nest's
        # minimum range check); give it the device's values in this task only.
        token = _REAL_SETPOINTS.set(self)
        try:
            await self._orig.set_temperature(entity, **kwargs)
        finally:
            _REAL_SETPOINTS.reset(token)

    def _skip(self, key: str, real: Any) -> None:
        """Nothing to send for a key: already there, or already on its way."""
        self.inflight.discard(key)
        if self._matches(key, real, self.desired.get(key)):
            self.desired.pop(key, None)
            self.sent.pop(key, None)

    async def _send(self, entity: Any, batch: dict[str, Any], writes: int) -> None:
        steps = self._plan(entity, batch)
        if not steps:
            _LOGGER.debug(
                "%s: %d write(s) coalesced, nothing to send", self.entity_id, writes
            )
            self._write_state(entity)
            return
        sent: list[str] = []
        for index, (values, described, call) in enumerate(steps):
            if index:
                await self._wait_for_budget()
            self._sends.append(self._clock())
            try:
                await call()
            except Exception as err:  # noqa: BLE001 - whatever the integration raises
                failed = {key: value for step in steps[index:] for key, value in step[0].items()}
                self._failed(entity, failed, err)
                return
            now = self._clock()
            for key, value in values.items():
                if key in self.inflight:  # not dropped by a mode change meanwhile
                    self.sent[key] = _Sent(value, now)
                    self.inflight.discard(key)
            sent.append(described)
        self._retries = 0
        self._arm_expiry()
        _LOGGER.info(
            "%s: sent %s (%d write(s) coalesced)", self.entity_id, "; ".join(sent), writes
        )

    def _arm_expiry(self) -> None:
        """Re-publish state once unconfirmed values time out."""
        if self._expiry:
            self._expiry.cancel()
        self._expiry = self.hass.loop.call_later(
            self.settings.confirm_timeout + 0.5, self._expire
        )

    def _expire(self) -> None:
        self._expiry = None
        if self.sent and not self._closed:
            self._write_state(self._resolve())

    def _failed(self, entity: Any, failed: dict[str, Any], err: Exception) -> None:
        if is_rate_limited(err) and self._retries < MAX_RETRIES:
            self._retries += 1
            _LOGGER.warning(
                "%s: rate limited, will resend %s once the quota frees up (retry %d/%d)",
                self.entity_id,
                failed,
                self._retries,
                MAX_RETRIES,
            )
            # Something else used the quota: treat the window as full.
            now = self._clock()
            self._sends = deque([now] * self.settings.per_minute)
            for key in failed:
                self.inflight.discard(key)
                # Requeue unless a newer write replaced it or a mode change dropped it.
                if key in self.desired and key not in self.dirty:
                    self.dirty.add(key)
            self._schedule(delay=0)
            return
        self._retries = 0
        _LOGGER.error("%s: could not send %s: %s", self.entity_id, failed, err)
        for key in failed:
            self.inflight.discard(key)
            if key not in self.dirty:
                self.desired.pop(key, None)
                self.sent.pop(key, None)
        self._write_state(entity)
