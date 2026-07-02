"""Lightweight, cached CPU temperature reading.

psutil.sensors_temperatures() is the primary backend. It has no Windows
implementation at all (not just "no sensor found" -- the attribute doesn't
exist there), so on Windows we fall back to a direct WMI ACPI thermal-zone
query (root\\WMI -> MSAcpi_ThermalZoneTemperature), which needs no extra
dependency beyond the pywin32 the project already ships on Windows. That
zone is whatever the board's ACPI tables expose -- often the CPU package,
sometimes just a general system zone, sometimes nothing at all. Both
backends failing is a normal "no data" outcome, not an error, so callers
should treat CpuTemperatureInfo(available=False) as an expected steady
state rather than a failure to handle.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import psutil

try:
    import win32com.client as _win32com_client
except ModuleNotFoundError:  # pragma: no cover - optional Windows integration
    _win32com_client = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)

DEFAULT_NORMAL_INTERVAL_SECONDS = 45.0
DEFAULT_LITE_INTERVAL_SECONDS = 180.0
DEFAULT_FAILURE_COOLDOWN_SECONDS = 600.0

_CPU_SENSOR_KEYS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "acpitz")
_KELVIN_OFFSET_CELSIUS = 273.15


@dataclass(slots=True)
class CpuTemperatureInfo:
    """Result of one (possibly cached) temperature read."""

    value: float | None = None
    source: str | None = None
    available: bool = False


class CpuTemperatureReader:
    """Polls psutil, then a WMI ACPI fallback on Windows, at most once per interval.

    Real reads only happen after the configured interval elapses; every
    other call returns the cached result. A failed/empty read (no CPU
    sensor found on either backend) triggers a long cooldown so the reader
    stops trying on hardware that simply has no exposed sensor.
    """

    def __init__(
        self,
        normal_interval_seconds: float = DEFAULT_NORMAL_INTERVAL_SECONDS,
        lite_interval_seconds: float = DEFAULT_LITE_INTERVAL_SECONDS,
        failure_cooldown_seconds: float = DEFAULT_FAILURE_COOLDOWN_SECONDS,
    ) -> None:
        self._normal_interval = max(30.0, float(normal_interval_seconds))
        self._lite_interval = max(120.0, float(lite_interval_seconds))
        self._failure_cooldown = max(60.0, float(failure_cooldown_seconds))
        self._cached = CpuTemperatureInfo()
        self._last_poll_at = 0.0
        self._cooldown_until = 0.0

    def read(self, *, lite_mode: bool = False) -> CpuTemperatureInfo:
        """Return the current temperature, polling the sensor if the interval elapsed."""

        now = time.monotonic()
        if now < self._cooldown_until:
            return self._cached

        interval = self._lite_interval if lite_mode else self._normal_interval
        if now - self._last_poll_at < interval:
            return self._cached

        self._last_poll_at = now
        self._cached = self._poll()
        if not self._cached.available:
            self._cooldown_until = now + self._failure_cooldown
        return self._cached

    def _poll(self) -> CpuTemperatureInfo:
        info = self._poll_psutil()
        if info.available:
            return info
        return self._poll_wmi_acpi()

    @staticmethod
    def _poll_psutil() -> CpuTemperatureInfo:
        try:
            sensors = psutil.sensors_temperatures()
        except (AttributeError, NotImplementedError, OSError):
            LOGGER.debug("sensors_temperatures unavailable on this platform", exc_info=True)
            return CpuTemperatureInfo()
        except Exception:
            LOGGER.debug("Unexpected error reading CPU temperature", exc_info=True)
            return CpuTemperatureInfo()

        if not sensors:
            return CpuTemperatureInfo()
        return CpuTemperatureReader._select_reading(sensors)

    @staticmethod
    def _poll_wmi_acpi() -> CpuTemperatureInfo:
        """Query the built-in Windows ACPI thermal zone via WMI (no third-party tool)."""

        if _win32com_client is None:
            return CpuTemperatureInfo()

        try:
            wmi_service = _win32com_client.GetObject(r"winmgmts:\\.\root\WMI")
            zones = list(wmi_service.InstancesOf("MSAcpi_ThermalZoneTemperature"))
        except Exception:
            LOGGER.debug("WMI ACPI thermal zone query unavailable", exc_info=True)
            return CpuTemperatureInfo()

        readings: list[tuple[str, float]] = []
        for zone in zones:
            tenth_kelvin = getattr(zone, "CurrentTemperature", None)
            if tenth_kelvin is None:
                continue
            celsius = (float(tenth_kelvin) / 10.0) - _KELVIN_OFFSET_CELSIUS
            label = str(getattr(zone, "InstanceName", "") or "ACPI Thermal Zone")
            readings.append((label, celsius))

        if not readings:
            return CpuTemperatureInfo()

        label, value = max(readings, key=lambda item: item[1])
        return CpuTemperatureInfo(value=value, source=label, available=True)

    @staticmethod
    def _select_reading(sensors: dict[str, list]) -> CpuTemperatureInfo:
        """Pick the best CPU-package reading out of whatever psutil exposes."""

        for key in _CPU_SENSOR_KEYS:
            entries = sensors.get(key)
            if not entries:
                continue

            package = next(
                (entry for entry in entries if entry.label and "package" in entry.label.lower()),
                None,
            )
            if package is not None and package.current is not None:
                return CpuTemperatureInfo(value=float(package.current), source=package.label, available=True)

            core_entries = [entry for entry in entries if entry.current is not None]
            if core_entries:
                hottest = max(core_entries, key=lambda entry: entry.current)
                label = hottest.label.strip() if hottest.label else ""
                source = label or "Core max"
                return CpuTemperatureInfo(value=float(hottest.current), source=source, available=True)

        for key, entries in sensors.items():
            for entry in entries:
                label = (entry.label or "").lower()
                if entry.current is None:
                    continue
                if "cpu" in key.lower() or "cpu" in label:
                    return CpuTemperatureInfo(
                        value=float(entry.current), source=entry.label or key, available=True
                    )

        return CpuTemperatureInfo()
