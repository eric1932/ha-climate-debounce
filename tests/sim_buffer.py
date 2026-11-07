"""Simulation tests for the command buffer, against a fake Nest-like cloud.

Needs Home Assistant importable (run it inside the HA container or a venv with
``homeassistant`` installed), from a directory that is not inside the HA
package (``cd /`` first in the container):

    python3 tests/sim_buffer.py

Time is scaled down: the cloud's rate window is 2 s instead of 60 s.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import sys
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from homeassistant.components.climate import (  # noqa: E402
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.climate_debounce.buffer import CommandBuffer, Settings  # noqa: E402
from custom_components.climate_debounce.patch import ClassPatch  # noqa: E402

WINDOW = 2.0
LIMIT = 5


def f_round(celsius: float) -> float:
    """A Nest showing °F stores whole °F."""
    return (round(celsius * 9 / 5 + 32) - 32) * 5 / 9


def near(a: float | None, b: float | None, tol: float = 0.01) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


@dataclass
class Call:
    t: float
    kind: str
    value: Any
    ok: bool
    entity: int = 0


class FakeCloud:
    """Nest-ish cloud: per-device rate limit, latency, delayed state reports."""

    def __init__(self, mode: HVACMode = HVACMode.COOL) -> None:
        self.device = {"mode": mode, "heat": f_round(20.0), "cool": f_round(23.4), "preset": PRESET_NONE}
        self.reported = dict(self.device)
        self.calls: list[Call] = []
        self.latency: list[float] = []  # per-call latency override, popped in order
        self.default_latency = 0.05
        self.report_delay = 0.25

    def used(self, now: float) -> int:
        return sum(1 for c in self.calls if c.ok and now - c.t < WINDOW)

    async def command(self, entity: Any, kind: str, value: Any) -> None:
        loop = asyncio.get_running_loop()
        await asyncio.sleep(self.latency.pop(0) if self.latency else self.default_latency)
        now = loop.time()
        if self.used(now) >= LIMIT:
            self.calls.append(Call(now, kind, value, False, id(entity)))
            raise HomeAssistantError(
                "Too Many Requests response from API (429): RESOURCE_EXHAUSTED"
            )
        self.calls.append(Call(now, kind, value, True, id(entity)))
        if kind in ("mode", "preset"):
            self.device[kind] = value
        elif kind == "range":
            self.device["heat"], self.device["cool"] = map(f_round, value)
        else:
            self.device[kind] = f_round(value)
        snapshot = dict(self.device)
        loop.call_later(self.report_delay, self._report, entity, snapshot)

    def _report(self, entity: Any, snapshot: dict[str, Any]) -> None:
        self.reported = snapshot
        entity.async_write_ha_state()

    def ok_calls(self) -> list[Call]:
        return [c for c in self.calls if c.ok]

    def max_per_window(self) -> int:
        ok = self.ok_calls()
        return max((sum(1 for d in ok if 0 <= c.t - d.t < WINDOW) for c in ok), default=0)


class FakeNest(ClimateEntity):
    """Mirrors the Nest integration's logic, including its stale-mode branch."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]
    _attr_preset_modes = [PRESET_NONE, PRESET_ECO]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.PRESET_MODE
    )

    def __init__(self, cloud: FakeCloud, entity_id: str) -> None:
        self.cloud = cloud
        self.entity_id = entity_id
        self.states: list[tuple[Any, ...]] = []

    @property
    def hvac_mode(self) -> HVACMode:
        return self.cloud.reported["mode"]

    @property
    def preset_mode(self) -> str:
        return self.cloud.reported["preset"]

    @property
    def target_temperature(self) -> float | None:
        if self.hvac_mode == HVACMode.HEAT:
            return self.cloud.reported["heat"]
        if self.hvac_mode == HVACMode.COOL:
            return self.cloud.reported["cool"]
        return None

    @property
    def target_temperature_high(self) -> float | None:
        return self.cloud.reported["cool"] if self.hvac_mode == HVACMode.HEAT_COOL else None

    @property
    def target_temperature_low(self) -> float | None:
        return self.cloud.reported["heat"] if self.hvac_mode == HVACMode.HEAT_COOL else None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.cloud.command(self, "mode", hvac_mode)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self.cloud.command(self, "preset", preset_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        hvac_mode = self.hvac_mode  # cached until the cloud reports back
        if kwargs.get(ATTR_HVAC_MODE) is not None:
            hvac_mode = kwargs[ATTR_HVAC_MODE]
            await self.async_set_hvac_mode(hvac_mode)
        low, high = kwargs.get(ATTR_TARGET_TEMP_LOW), kwargs.get(ATTR_TARGET_TEMP_HIGH)
        temp = kwargs.get(ATTR_TEMPERATURE)
        if hvac_mode == HVACMode.HEAT_COOL:
            if low and high:
                await self.cloud.command(self, "range", (low, high))
        elif hvac_mode == HVACMode.COOL and temp:
            await self.cloud.command(self, "cool", temp)
        elif hvac_mode == HVACMode.HEAT and temp:
            await self.cloud.command(self, "heat", temp)
        # OFF: silently nothing, like Nest

    def async_write_ha_state(self) -> None:
        self.states.append((self.hvac_mode, self.target_temperature))


class FakeHass:
    def __init__(self, unit: str = UnitOfTemperature.FAHRENHEIT) -> None:
        self.loop = asyncio.get_running_loop()
        self.is_stopping = False
        self.config = SimpleNamespace(units=SimpleNamespace(temperature_unit=unit))

    def async_create_background_task(self, coro: Any, name: str, eager_start: bool = True):
        return self.loop.create_task(coro, name=name)


async def settle(seconds: float = 3.0) -> None:
    await asyncio.sleep(seconds)


@dataclass
class Rig:
    cloud: FakeCloud
    entity: FakeNest
    patch: ClassPatch
    holder: list[Any]
    buffer: CommandBuffer | None


def setup(buffered: bool, settings: Settings, mode: HVACMode = HVACMode.COOL) -> Rig:
    """A fresh fake entity, optionally behind a buffer."""
    cloud = FakeCloud(mode)
    entity = FakeNest(cloud, "climate.fake")
    hass = FakeHass()
    entity.hass = hass
    holder: list[Any] = [entity]
    registry: dict[str, CommandBuffer] = {}
    patch = ClassPatch(FakeNest, lambda e: registry.get(e.entity_id))
    patch.apply()
    buf = None
    if buffered:
        buf = CommandBuffer(hass, entity.entity_id, settings, patch.originals, lambda: holder[0])
        registry[entity.entity_id] = buf
    return Rig(cloud, entity, patch, holder, buf)


FAST = Settings(delay=0.3, max_wait=1.0, per_minute=LIMIT, window=WINDOW, confirm_timeout=2.0)

# A real Apple Home drag sequence, in °C, gaps scaled by 0.25.
DRAG = [(0.0, 24.0), (0.25, 24.5), (0.5, 24.0), (0.05, 23.5), (0.86, 24.5),
        (0.03, 25.0), (0.65, 24.0), (0.03, 23.5), (0.82, 23.0)]


async def drag(entity: FakeNest) -> None:
    for gap, value in DRAG:
        await asyncio.sleep(gap)
        await entity.async_set_temperature(temperature=value)


async def concurrent_drag(entity: FakeNest) -> None:
    """HomeKit fires each write as its own task without waiting."""
    tasks = []
    for gap, value in DRAG:
        await asyncio.sleep(gap)
        tasks.append(asyncio.create_task(entity.async_set_temperature(temperature=value)))
    await asyncio.gather(*tasks, return_exceptions=True)


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def calls(rig: Rig) -> list[tuple[str, Any, bool]]:
    return [(c.kind, c.value, c.ok) for c in rig.cloud.calls]


# --------------------------------------------------------------------- basics

def test_patch_real_nest_class() -> None:
    try:
        from homeassistant.components.nest.climate import ThermostatEntity
    except ImportError as err:
        check("patch the real Nest class", True, f"skipped: {err}")
        return
    names = ("hvac_mode", "target_temperature", "precision", "async_set_temperature",
             "async_set_preset_mode")
    before = {k: ThermostatEntity.__dict__.get(k, "missing") for k in names}
    patch = ClassPatch(ThermostatEntity, lambda e: None)
    patch.apply()
    changed = all(ThermostatEntity.__dict__.get(k) is not v for k, v in before.items())
    patch.restore()
    same = all(ThermostatEntity.__dict__.get(k, "missing") is v for k, v in before.items())
    check("patch and restore the real Nest ThermostatEntity", changed and same)


async def test_unconfigured_passthrough_and_restore() -> None:
    original = FakeNest.__dict__["async_set_temperature"]
    rig = setup(False, FAST)
    await rig.entity.async_set_temperature(temperature=22.0)
    passthrough = len(rig.cloud.calls) == 1
    rig.patch.restore()
    restored = FakeNest.__dict__["async_set_temperature"] is original
    check("unconfigured entity unchanged; restore() undoes the patch", passthrough and restored)


async def test_subclass_of_patched_class() -> None:
    class SubNest(FakeNest):
        pass

    base = ClassPatch(FakeNest, lambda e: None)
    base.apply()
    try:
        sub = ClassPatch(SubNest, lambda e: None)
        sub.apply()
        ok = SubNest.__dict__["async_set_temperature"] is not FakeNest.__dict__["async_set_temperature"]
        sub.restore()
    except RuntimeError as err:
        ok = False
        print("   ", err)
    base.restore()
    check("a subclass of a patched class can be patched too", ok)


# ------------------------------------------------------------- main behaviour

async def test_drag_baseline_vs_buffered() -> None:
    rig = setup(False, FAST)
    await concurrent_drag(rig.entity)
    await settle()
    rig.patch.restore()
    rejected = sum(1 for c in rig.cloud.calls if not c.ok)
    check("baseline drag hits the limit", rejected > 0,
          f"{len(rig.cloud.calls)} calls, {rejected} rejected")

    rig = setup(True, FAST)
    await concurrent_drag(rig.entity)
    await settle()
    rig.patch.restore()
    rejected = sum(1 for c in rig.cloud.calls if not c.ok)
    check("buffered drag: last value lands, no 429",
          near(rig.cloud.device["cool"], f_round(23.0)) and rejected == 0,
          f"{len(rig.cloud.calls)} calls, {rejected} rejected, final {rig.cloud.device['cool']:.2f}")


async def test_tight_budget() -> None:
    settings = Settings(delay=0.3, max_wait=1.0, per_minute=2, window=WINDOW, confirm_timeout=2.0)
    rig = setup(True, settings)
    await drag(rig.entity)
    await settle(5)
    rig.patch.restore()
    worst = rig.cloud.max_per_window()
    check("budget of 2/window is respected and the last value still lands",
          near(rig.cloud.device["cool"], f_round(23.0)) and worst <= 2
          and all(c.ok for c in rig.cloud.calls),
          f"max {worst} per window, final {rig.cloud.device['cool']:.2f}")


async def test_mode_race() -> None:
    for buffered in (False, True):
        rig = setup(buffered, FAST, mode=HVACMode.OFF)
        await rig.entity.async_set_hvac_mode(HVACMode.HEAT)
        await asyncio.sleep(0.1)
        await rig.entity.async_set_temperature(temperature=23.0)
        await settle()
        rig.patch.restore()
        landed = rig.cloud.device["mode"] == HVACMode.HEAT and near(rig.cloud.device["heat"], f_round(23.0))
        if buffered:
            check("buffered: mode then setpoint both land", landed and len(rig.cloud.calls) == 2,
                  f"calls {calls(rig)}")
        else:
            check("baseline: setpoint right after a mode change is dropped", not landed,
                  f"calls {calls(rig)}")


async def test_siri_mode_and_temp() -> None:
    rig = setup(True, FAST, mode=HVACMode.COOL)
    await rig.entity.async_set_hvac_mode(HVACMode.HEAT)
    await rig.entity.async_set_temperature(hvac_mode=HVACMode.HEAT, temperature=22.0)
    await settle()
    rig.patch.restore()
    check("Siri-style mode + temp: two commands, no duplicate mode",
          [c.kind for c in rig.cloud.calls] == ["mode", "heat"], f"calls {calls(rig)}")


async def test_skip_duplicate() -> None:
    rig = setup(True, FAST)
    rig.cloud.device["cool"] = rig.cloud.reported["cool"] = f_round(23.9)  # 75 °F
    await rig.entity.async_set_temperature(temperature=24.0)  # 75.2 °F rounds to 75
    shown_before = rig.entity.target_temperature
    await settle(1)
    shown_after = rig.entity.target_temperature
    rig.patch.restore()
    check("duplicate setpoint: nothing sent",
          not rig.cloud.calls and shown_before == 24.0 and near(shown_after, f_round(23.9)),
          f"calls {len(rig.cloud.calls)}, shown {shown_before} then {shown_after:.2f}")


async def test_optimistic_state() -> None:
    rig = setup(True, FAST)
    await rig.entity.async_set_temperature(temperature=25.0)
    during_wait = rig.entity.target_temperature
    await asyncio.sleep(0.4)  # sent, not reported yet
    after_send = rig.entity.target_temperature
    old = f_round(23.4)
    await settle(1)
    after_report = rig.entity.target_temperature
    rig.patch.restore()
    snapped_back = any(near(t, old) for _, t in rig.entity.states)
    check("UI shows the requested value until the device confirms",
          during_wait == 25.0 and after_send == 25.0 and near(after_report, f_round(25.0))
          and not snapped_back,
          f"{during_wait} -> {after_send} -> {after_report:.2f}")


async def test_rate_limit_retry() -> None:
    rig = setup(True, FAST)
    now = asyncio.get_running_loop().time()
    rig.cloud.calls = [Call(now, "external", None, True) for _ in range(LIMIT)]
    await rig.entity.async_set_temperature(temperature=21.0)
    await settle(4)
    rig.patch.restore()
    mine = [c for c in rig.cloud.calls if c.kind != "external"]
    check("429 from someone else's quota: retried and landed",
          bool(mine) and mine[-1].ok and near(rig.cloud.device["cool"], f_round(21.0)),
          f"calls {[(c.kind, c.value, c.ok) for c in mine]}")


async def test_reorder() -> None:
    for buffered in (False, True):
        rig = setup(buffered, FAST)
        rig.cloud.latency = [0.4, 0.05]  # the first request is slower than the second
        t1 = asyncio.create_task(rig.entity.async_set_temperature(temperature=25.0))
        await asyncio.sleep(0.1)
        t2 = asyncio.create_task(rig.entity.async_set_temperature(temperature=21.0))
        await asyncio.gather(t1, t2)
        await settle()
        rig.patch.restore()
        last_wins = near(rig.cloud.device["cool"], f_round(21.0))
        if buffered:
            check("buffered: last write wins", last_wins, f"final {rig.cloud.device['cool']:.2f}")
        else:
            check("baseline: slow earlier request overwrites the later one", not last_wins,
                  f"final {rig.cloud.device['cool']:.2f}")


# ------------------------------------------------------- review regressions

async def test_back_to_original_before_confirm() -> None:
    settings = Settings(delay=0.2, max_wait=1.0, per_minute=LIMIT, window=WINDOW, confirm_timeout=5.0)
    rig = setup(True, settings)
    rig.cloud.report_delay = 2.0  # slow Pub/Sub event
    original = rig.cloud.device["cool"]
    await rig.entity.async_set_temperature(temperature=25.0)
    await asyncio.sleep(0.5)  # sent, device has not reported yet
    await rig.entity.async_set_temperature(temperature=original)
    await settle(3.5)
    rig.patch.restore()
    check("going back before the device confirms still sends the second command",
          near(rig.cloud.device["cool"], original) and len(rig.cloud.calls) == 2,
          f"calls {calls(rig)}")

    rig = setup(True, settings, mode=HVACMode.COOL)
    rig.cloud.report_delay = 2.0
    await rig.entity.async_set_hvac_mode(HVACMode.HEAT)
    await asyncio.sleep(0.5)
    await rig.entity.async_set_hvac_mode(HVACMode.COOL)
    await settle(3.5)
    rig.patch.restore()
    check("mode back before the device confirms: ends in the last mode",
          rig.cloud.device["mode"] == HVACMode.COOL and len(rig.cloud.calls) == 2,
          f"calls {calls(rig)}")


async def test_no_snapback_during_call() -> None:
    rig = setup(True, FAST, mode=HVACMode.COOL)
    rig.cloud.default_latency = 0.4  # the mode's event arrives while the setpoint is in flight
    await rig.entity.async_set_hvac_mode(HVACMode.HEAT)
    await rig.entity.async_set_temperature(hvac_mode=HVACMode.HEAT, temperature=22.0)
    await settle()
    rig.patch.restore()
    # Between the two writes the heat setpoint is legitimately the old one; once
    # 22 has been shown, nothing may pull it back.
    first = next((i for i, s in enumerate(rig.entity.states) if near(s[1], 22.0, 0.3)), None)
    after = rig.entity.states[first:] if first is not None else []
    wrong = [s for s in after if not (s[0] == HVACMode.HEAT and near(s[1], 22.0, 0.3))]
    check("no snap-back while commands are in flight", first is not None and not wrong,
          f"states {[(str(m), round(t, 2) if t else t) for m, t in rig.entity.states]}")


async def test_budget_never_exceeded() -> None:
    one = Settings(delay=0.2, max_wait=1.0, per_minute=1, window=WINDOW, confirm_timeout=5.0)
    rig = setup(True, one, mode=HVACMode.OFF)
    await rig.entity.async_set_hvac_mode(HVACMode.HEAT)
    await rig.entity.async_set_temperature(temperature=22.0)
    await settle(4.5)
    rig.patch.restore()
    check("budget 1: mode + setpoint go out one window apart",
          rig.cloud.max_per_window() <= 1 and len(rig.cloud.ok_calls()) == 2,
          f"max {rig.cloud.max_per_window()} per window, calls {calls(rig)}")

    two = Settings(delay=0.2, max_wait=1.0, per_minute=2, window=WINDOW, confirm_timeout=5.0)
    rig = setup(True, two)
    await rig.entity.async_set_temperature(temperature=24.0)
    await asyncio.sleep(0.4)
    await rig.entity.async_set_temperature(temperature=25.0)
    await asyncio.sleep(0.4)
    await rig.entity.async_set_temperature(temperature=21.0)  # must wait for the budget
    await asyncio.sleep(0.4)
    await rig.entity.async_set_hvac_mode(HVACMode.HEAT)  # arrives during that wait
    await rig.entity.async_set_temperature(temperature=21.0)
    preset = asyncio.create_task(rig.entity.async_set_preset_mode(PRESET_ECO))
    await settle(6)
    rig.patch.restore()
    check("budget 2 with a mode change and a preset during the wait",
          rig.cloud.max_per_window() <= 2 and preset.done() and all(c.ok for c in rig.cloud.calls)
          and rig.cloud.device["mode"] == HVACMode.HEAT and near(rig.cloud.device["heat"], f_round(21.0)),
          f"max {rig.cloud.max_per_window()} per window, calls {calls(rig)}")


async def test_mode_change_drops_old_setpoint() -> None:
    settings = Settings(delay=0.2, max_wait=1.0, per_minute=LIMIT, window=WINDOW, confirm_timeout=5.0)
    rig = setup(True, settings, mode=HVACMode.COOL)
    rig.cloud.report_delay = 1.5
    await rig.entity.async_set_temperature(temperature=21.0)
    await asyncio.sleep(0.4)  # cool 21 sent, not confirmed
    await rig.entity.async_set_hvac_mode(HVACMode.HEAT)
    shown = rig.entity.target_temperature
    await settle(3)
    rig.patch.restore()
    check("after a mode change the old mode's setpoint is not shown",
          shown is not None and near(shown, rig.cloud.device["heat"]),
          f"shown {shown}, heat setpoint {rig.cloud.device['heat']:.2f}")


async def test_expiry_rewrites_state() -> None:
    settings = Settings(delay=0.2, max_wait=1.0, per_minute=LIMIT, window=WINDOW, confirm_timeout=1.0)
    rig = setup(True, settings, mode=HVACMode.OFF)
    await rig.entity.async_set_temperature(temperature=22.0)  # Nest ignores setpoints in OFF
    await settle(2.5)
    rig.patch.restore()
    check("an ignored setpoint stops being shown after the timeout",
          rig.entity.states and rig.entity.states[-1][1] is None,
          f"last state {rig.entity.states[-1] if rig.entity.states else None}")


async def test_heat_cool_extra_temperature() -> None:
    rig = setup(True, FAST, mode=HVACMode.HEAT_COOL)
    low, high = rig.cloud.device["heat"], rig.cloud.device["cool"]
    await rig.entity.async_set_temperature(temperature=21.0, target_temp_low=low, target_temp_high=high)
    await settle(1)
    rig.patch.restore()
    check("heat_cool: unchanged range plus a stray temperature sends nothing",
          not rig.cloud.calls, f"calls {calls(rig)}")


async def test_reload_uses_new_entity() -> None:
    rig = setup(True, FAST)
    await rig.entity.async_set_temperature(temperature=25.0)
    new = FakeNest(rig.cloud, rig.entity.entity_id)  # the integration reloaded
    new.hass = rig.entity.hass
    rig.holder[0] = new
    await settle(1)
    rig.patch.restore()
    check("a pending flush goes through the reloaded entity object",
          rig.cloud.calls and rig.cloud.calls[-1].entity == id(new), f"calls {calls(rig)}")


async def test_one_decimal() -> None:
    plain = Settings(delay=0.3, per_minute=LIMIT, window=WINDOW)
    rig = setup(True, plain)
    whole = rig.entity.precision
    rig.buffer.settings = Settings(delay=0.3, per_minute=LIMIT, window=WINDOW, one_decimal=True)
    tenths = rig.entity.precision
    rig.patch.restore()
    check("one_decimal: precision 1.0 -> 0.1 on a °F system", whole == 1.0 and tenths == 0.1,
          f"{whole} -> {tenths}")


async def main() -> int:
    test_patch_real_nest_class()
    await test_unconfigured_passthrough_and_restore()
    await test_subclass_of_patched_class()
    await test_drag_baseline_vs_buffered()
    await test_tight_budget()
    await test_mode_race()
    await test_siri_mode_and_temp()
    await test_skip_duplicate()
    await test_optimistic_state()
    await test_rate_limit_retry()
    await test_reorder()
    await test_back_to_original_before_confirm()
    await test_no_snapback_during_call()
    await test_budget_never_exceeded()
    await test_mode_change_drops_old_setpoint()
    await test_expiry_rewrites_state()
    await test_heat_cool_extra_temperature()
    await test_reload_uses_new_entity()
    await test_one_decimal()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
