from contextlib import nullcontext
import sys
import time
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    from sysmon import _windows
    from sysmon._windows_counters import _dedicated_memory, _disk_busy, _package_power


@unittest.skipUnless(sys.platform == "win32", "uses Windows PDH counter names")
class WindowsCountersTests(unittest.TestCase):
    def test_hwinfo_uses_live_cpu_package_temperature(self):
        values = {
            "Sensor0": "ASUS motherboard", "Label0": "CPU Package", "Value0": "90 °C", "ValueRaw0": "90",
            "Sensor1": "CPU [#0]: Intel CPU: DTS", "Label1": "CPU Package", "Value1": "140 °F", "ValueRaw1": "140",
            "Sensor2": "CPU [#0]: Intel CPU: Enhanced", "Label2": "CPU Package", "Value2": "52 W", "ValueRaw2": "52",
        }
        keys = list(values)
        now = int(time.time() * _windows._TICKS_PER_SECOND) + _windows._UNIX_EPOCH
        with patch.object(_windows.winreg, "OpenKey", return_value=nullcontext(None)), \
                patch.object(_windows.winreg, "EnumValue", side_effect=lambda key, i: (keys[i], values[keys[i]], _windows.winreg.REG_SZ)), \
                patch.object(_windows.winreg, "QueryInfoKey", return_value=(0, len(keys), now)) as info:
            for label in ("CPU Package", "CPU封裝"):
                with self.subTest(label=label):
                    values["Label1"] = label
                    self.assertEqual(_windows.cpu_temperature(), 60)
            info.return_value = (0, len(keys), now - 11 * _windows._TICKS_PER_SECOND)
            self.assertIsNone(_windows.cpu_temperature())

    def test_package_power_uses_milliwatts_without_counting_subdomains(self):
        values = [
            ("RAPL_Package0_PKG", 51000), ("rapl_package1_pkg", 43000),
            ("RAPL_Package0_PP0", 32000), ("RAPL_Package1_DRAM", 3500),
            ("RAPL_Package2_PKG", -1), ("_Total", 128000),
        ]
        self.assertEqual(_package_power(values), 94)
        self.assertIsNone(_package_power(values[2:]))
        self.assertEqual(_package_power([("RAPL_Package0_PKG", 0)]), 0)

    def test_physical_disk_busy_is_the_complement_of_idle_time(self):
        values = [("0 D:", 99.5), ("1 C: E:", 42), ("2", 101), ("_Total", 75)]
        self.assertEqual(_disk_busy(values), {
            "PhysicalDrive0": 0.5, "PhysicalDrive1": 58, "PhysicalDrive2": 0,
        })

    def test_vram_stays_with_the_adapter_and_physical_node(self):
        values = [
            ("pid_45_luid_0x00000000_0x00013B94_phys_0", 1000),
            ("pid_45_luid_0x00000000_0x00013b94_phys_0#1", 1000),
            ("pid_45_luid_0x00000000_0x00013B94_phys_1", 500),
            ("pid_45_luid_0x00000000_0x00014FD4_phys_0", 400),
            ("pid_45_luid_0x00000000_0x00019a1b_phys_0", -1),
            ("_Total", 10000),
        ]
        self.assertEqual(_dedicated_memory(values), {
            (0, 0x13B94, 0): {45: 1000}, (0, 0x13B94, 1): {45: 500},
            (0, 0x14FD4, 0): {45: 400},
        })
