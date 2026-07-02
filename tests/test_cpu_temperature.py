"""Tests for the cached CPU temperature reader."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pc_optimizer_lite.cpu_temperature import CpuTemperatureReader

# psutil.sensors_temperatures() doesn't exist as an attribute at all on some
# platforms (e.g. Windows), so patches need create=True to attach it.
_SENSORS_TARGET = "pc_optimizer_lite.cpu_temperature.psutil.sensors_temperatures"
_CLOCK_TARGET = "pc_optimizer_lite.cpu_temperature.time.monotonic"
_WMI_CLIENT_TARGET = "pc_optimizer_lite.cpu_temperature._win32com_client"


def _entry(label: str, current: float) -> SimpleNamespace:
    return SimpleNamespace(label=label, current=current, high=None, critical=None)


def _wmi_zone(instance_name: str, tenth_kelvin: float) -> SimpleNamespace:
    return SimpleNamespace(InstanceName=instance_name, CurrentTemperature=tenth_kelvin)


class _FakeWin32Com:
    """Stand-in for win32com.client with a scripted GetObject() result."""

    def __init__(self, zones=None, get_object_error: Exception | None = None) -> None:
        self._zones = zones or []
        self._get_object_error = get_object_error

    def GetObject(self, moniker: str):
        if self._get_object_error is not None:
            raise self._get_object_error
        return SimpleNamespace(InstancesOf=lambda class_name: self._zones)


class CpuTemperatureReaderTests(unittest.TestCase):
    def _reader(self) -> CpuTemperatureReader:
        return CpuTemperatureReader(
            normal_interval_seconds=30.0,
            lite_interval_seconds=120.0,
            failure_cooldown_seconds=60.0,
        )

    # -- psutil backend --------------------------------------------------

    def test_no_sensors_reports_unavailable_without_raising(self) -> None:
        reader = self._reader()
        with patch(_SENSORS_TARGET, return_value={}, create=True), patch(_WMI_CLIENT_TARGET, None):
            info = reader.read()
        self.assertFalse(info.available)
        self.assertIsNone(info.value)
        self.assertIsNone(info.source)

    def test_missing_sensors_api_reports_unavailable(self) -> None:
        reader = self._reader()
        with patch(
            _SENSORS_TARGET,
            side_effect=AttributeError("not supported on this platform"),
            create=True,
        ), patch(_WMI_CLIENT_TARGET, None):
            info = reader.read()
        self.assertFalse(info.available)

    def test_prefers_package_label_over_individual_cores(self) -> None:
        reader = self._reader()
        sensors = {
            "coretemp": [
                _entry("Package id 0", 55.0),
                _entry("Core 0", 50.0),
                _entry("Core 1", 53.0),
            ]
        }
        with patch(_SENSORS_TARGET, return_value=sensors, create=True):
            info = reader.read()
        self.assertTrue(info.available)
        self.assertEqual(info.value, 55.0)
        self.assertEqual(info.source, "Package id 0")

    def test_falls_back_to_hottest_core_when_no_package_label(self) -> None:
        reader = self._reader()
        sensors = {
            "coretemp": [
                _entry("Core 0", 50.0),
                _entry("Core 1", 61.0),
            ]
        }
        with patch(_SENSORS_TARGET, return_value=sensors, create=True):
            info = reader.read()
        self.assertTrue(info.available)
        self.assertEqual(info.value, 61.0)
        self.assertEqual(info.source, "Core 1")

    # -- WMI ACPI fallback -------------------------------------------------

    def test_wmi_acpi_used_when_psutil_has_no_data(self) -> None:
        reader = self._reader()
        zones = [_wmi_zone("ACPI\\ThermalZone\\TZ00_0", 3232.0)]  # 323.2K -> 50.05C
        with patch(_SENSORS_TARGET, return_value={}, create=True), patch(
            _WMI_CLIENT_TARGET, _FakeWin32Com(zones=zones)
        ):
            info = reader.read()
        self.assertTrue(info.available)
        self.assertAlmostEqual(info.value, 50.05, places=2)
        self.assertEqual(info.source, "ACPI\\ThermalZone\\TZ00_0")

    def test_wmi_acpi_picks_hottest_zone(self) -> None:
        reader = self._reader()
        zones = [
            _wmi_zone("TZ00_0", 3000.0),  # 26.85C
            _wmi_zone("TZ00_1", 3300.0),  # 56.85C
        ]
        with patch(_SENSORS_TARGET, return_value={}, create=True), patch(
            _WMI_CLIENT_TARGET, _FakeWin32Com(zones=zones)
        ):
            info = reader.read()
        self.assertTrue(info.available)
        self.assertEqual(info.source, "TZ00_1")

    def test_wmi_acpi_unavailable_when_no_zones_reported(self) -> None:
        reader = self._reader()
        with patch(_SENSORS_TARGET, return_value={}, create=True), patch(
            _WMI_CLIENT_TARGET, _FakeWin32Com(zones=[])
        ):
            info = reader.read()
        self.assertFalse(info.available)

    def test_wmi_acpi_unavailable_without_win32com_installed(self) -> None:
        reader = self._reader()
        with patch(_SENSORS_TARGET, return_value={}, create=True), patch(_WMI_CLIENT_TARGET, None):
            info = reader.read()
        self.assertFalse(info.available)

    def test_wmi_acpi_com_error_does_not_raise(self) -> None:
        reader = self._reader()
        fake_client = _FakeWin32Com(get_object_error=OSError("COM error"))
        with patch(_SENSORS_TARGET, return_value={}, create=True), patch(_WMI_CLIENT_TARGET, fake_client):
            info = reader.read()
        self.assertFalse(info.available)

    def test_psutil_success_skips_wmi_fallback(self) -> None:
        reader = self._reader()
        sensors = {"coretemp": [_entry("Package id 0", 55.0)]}
        fake_client = _FakeWin32Com(zones=[_wmi_zone("TZ00_0", 3000.0)])
        with patch(_SENSORS_TARGET, return_value=sensors, create=True) as mocked_psutil, patch(
            _WMI_CLIENT_TARGET, fake_client
        ):
            info = reader.read()
        mocked_psutil.assert_called_once()
        self.assertEqual(info.source, "Package id 0")

    # -- caching / cooldown -------------------------------------------------

    def test_caches_result_within_interval(self) -> None:
        reader = self._reader()
        sensors = {"coretemp": [_entry("Package id 0", 55.0)]}
        with patch(_SENSORS_TARGET, return_value=sensors, create=True) as mocked, patch(
            _CLOCK_TARGET, return_value=1000.0
        ):
            reader.read()
            reader.read()
            reader.read()
        self.assertEqual(mocked.call_count, 1)

    def test_polls_again_after_normal_interval_elapses(self) -> None:
        reader = self._reader()
        sensors = {"coretemp": [_entry("Package id 0", 55.0)]}
        with patch(_SENSORS_TARGET, return_value=sensors, create=True) as mocked:
            with patch(_CLOCK_TARGET, return_value=1000.0):
                reader.read(lite_mode=False)
            with patch(_CLOCK_TARGET, return_value=1000.0 + 30.0):
                reader.read(lite_mode=False)
        self.assertEqual(mocked.call_count, 2)

    def test_lite_mode_uses_longer_interval(self) -> None:
        reader = self._reader()
        sensors = {"coretemp": [_entry("Package id 0", 55.0)]}
        with patch(_SENSORS_TARGET, return_value=sensors, create=True) as mocked:
            with patch(_CLOCK_TARGET, return_value=1000.0):
                reader.read(lite_mode=True)
            # past the normal interval but well short of the lite interval
            with patch(_CLOCK_TARGET, return_value=1000.0 + 40.0):
                cached = reader.read(lite_mode=True)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(cached.value, 55.0)

    def test_failed_read_enters_long_cooldown(self) -> None:
        reader = self._reader()
        with patch(_SENSORS_TARGET, return_value={}, create=True) as mocked, patch(
            _WMI_CLIENT_TARGET, None
        ):
            with patch(_CLOCK_TARGET, return_value=1000.0):
                reader.read(lite_mode=False)
            # normal interval (30s) elapsed, but failure cooldown (60s) has not
            with patch(_CLOCK_TARGET, return_value=1000.0 + 30.0):
                reader.read(lite_mode=False)
        self.assertEqual(mocked.call_count, 1)

    def test_polls_again_once_cooldown_expires(self) -> None:
        reader = self._reader()
        with patch(_SENSORS_TARGET, return_value={}, create=True) as mocked, patch(
            _WMI_CLIENT_TARGET, None
        ):
            with patch(_CLOCK_TARGET, return_value=1000.0):
                reader.read(lite_mode=False)
            with patch(_CLOCK_TARGET, return_value=1000.0 + 60.0):
                reader.read(lite_mode=False)
        self.assertEqual(mocked.call_count, 2)


if __name__ == "__main__":
    unittest.main()
