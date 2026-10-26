"""Hardware-independent regressions; never change the desktop or pair devices."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import omagic_lib as om

spec = importlib.util.spec_from_file_location("cli", Path(__file__).resolve().parents[1] / "omagic.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class SettingsTests(unittest.TestCase):
    def test_invalid_settings(self):
        for data in ({"tapToClick": "false"}, {"sensitivity": True},
                     {"sensitivity": float("nan")}, {"scrollFactor": 0},
                     {"unknown": True}, {"sensitivity": 10}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                om.validate_settings(data)

    def test_corrupt_state_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with patch.object(om, "STATE_PATH", path), patch.object(om, "SETTINGS_PATH", path):
                path.write_text('[]')
                self.assertEqual(om.load_saved(), {})
                path.write_text('{"tapToClick":"yes"}')
                self.assertEqual(om.load_settings(), om.DEFAULT_SETTINGS)

    def test_atomic_writes_leave_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.object(om, "SETTINGS_PATH", path), patch.object(om, "STATE_DIR", Path(directory)):
                om.save_settings({"tapToClick": True})
                self.assertTrue(om.load_settings()["tapToClick"])
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_fn_failure_does_not_save_or_require_trackpad(self):
        with patch.object(om, "apply_fnmode", return_value=(False, "denied")), \
             patch.object(om, "save_settings") as save, patch.object(cli, "apply_trackpad") as apply:
            with self.assertRaisesRegex(RuntimeError, "denied"):
                cli.set_settings('{"macFnKeys":false}')
            save.assert_not_called()
            apply.assert_not_called()

    def test_state_permissions_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "private"
            with patch.object(om, "STATE_DIR", state), patch.object(om, "STATE_PATH", state / "state.json"):
                om.save_saved({"addresses": ["AA:BB:CC:DD:EE:FF"]})
                self.assertEqual(state.stat().st_mode & 0o777, 0o700)
                self.assertEqual(om.STATE_PATH.stat().st_mode & 0o777, 0o600)

    def test_public_status_excludes_identifiers(self):
        devices = om.public_devices([{"name": "Magic Trackpad", "link": "USB", "battery": 50,
                                      "serial": "private", "bluetooth": {"address": "private"}}])
        self.assertNotIn("private", json.dumps(devices))


class BluetoothTests(unittest.TestCase):
    def test_battery_hex_and_decimal(self):
        for value in ("0x62 (98)", "98", "0x62"):
            with patch.object(om, "run", return_value=subprocess.CompletedProcess([], 0, f"Battery Percentage: {value}\n", "")):
                self.assertEqual(om.bt_info("AA:BB:CC:DD:EE:FF")["battery"], 98)

    def test_no_generic_mouse_or_keyboard(self):
        for info in ({"icon": "input-mouse"}, {"icon": "input-keyboard"}, {"alias": "Logitech Trackpad"}):
            self.assertIsNone(om.classify_bt(info))
        self.assertEqual(om.classify_bt({"modalias": "bluetooth:v004cp0324d0319"}), "trackpad")

    def test_ansi_listing(self):
        self.assertEqual(om.parse_device_listing('\x1b[0;92mDevice AA:BB:CC:DD:EE:FF Magic Trackpad\x1b[0m'),
                         [("AA:BB:CC:DD:EE:FF", "Magic Trackpad")])

    def test_disconnected_devices_are_not_present_or_merged(self):
        devices = [{"kind": "trackpad", "address": mac, "alias": "Magic Trackpad", "connected": False}
                   for mac in ("AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02")]
        with patch.object(om, "hypr_devices", return_value={}), patch.object(om, "hid_devices", return_value=[]), \
             patch.object(om, "batteries", return_value={}), patch.object(om, "bluetooth_magic", return_value=devices), \
             patch.object(om, "enabled_plugin_ids", return_value=set()):
            snapshot = om.merge_devices()
            self.assertFalse(snapshot["trackpad"]["present"])
            self.assertEqual(len(snapshot["trackpad"]["devices"]), 2)

    def test_pair_without_connection_fails(self):
        with patch.object(om, "run"), patch.object(om, "known_addresses", return_value=[]), \
             patch.object(om, "discover_magic", return_value=[{"address": "AA:BB:CC:DD:EE:FF"}]), \
             patch.object(om, "bt_info", return_value={"paired": True, "connected": False}):
            with self.assertRaisesRegex(RuntimeError, "did not connect"):
                cli.setup_wireless("trackpad")

    def test_multiple_candidates_are_not_automatically_paired(self):
        with patch.object(om, "run") as run, patch.object(om, "known_addresses", return_value=[]), \
             patch.object(om, "discover_magic", return_value=[{}, {}]):
            with self.assertRaisesRegex(RuntimeError, "Multiple"):
                cli.setup_wireless("keyboard")
            self.assertEqual(run.call_count, 1)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _hid(node: Path, hid_id: str, name: str, uniq: str) -> None:
    _write(node / "uevent", f"HID_ID={hid_id}\nHID_NAME={name}\nHID_UNIQ={uniq}\nHID_PHYS=\n")


def _supply(node: Path, model: str, capacity: str, status: str) -> None:
    _write(node / "model_name", model)
    _write(node / "capacity", capacity)
    _write(node / "status", status)


class BatteryTests(unittest.TestCase):
    """Renamed Bluetooth aliases land in power_supply model_name, not 'Magic Trackpad'."""

    ALIAS = "James Caudill\u2019s Trackpad"

    def test_alias_is_not_classified_by_name(self):
        self.assertIsNone(om.classify_name(self.ALIAS))
        self.assertIsNone(om.classify_name("Study Keyboard"))

    def test_supply_serial_formats(self):
        cases = {
            "hid-04:4b:ed:eb:f5:10-battery-144": "04:4b:ed:eb:f5:10",
            "hid-04:4b:ed:eb:f5:10-battery": "04:4b:ed:eb:f5:10",
            "hid-04-4b-ed-eb-f5-10-battery-144": "04-4b-ed-eb-f5-10",
            "BAT0": "",
            "hid-battery-1": "",
            "hid-04:4b:ed:eb:f5:10-battery-extra": "",
        }
        for name, serial in cases.items():
            with self.subTest(name=name):
                self.assertEqual(om.power_supply_serial(name), serial)

    def test_renamed_trackpad_battery_uses_hid_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hid = root / "hid"
            power = root / "power"
            _hid(hid / "0005:004C:0265.0001", "0005:0000004C:00000265", self.ALIAS, "04:4b:ed:eb:f5:10")
            _hid(hid / "0005:004C:0320.0002", "0005:0000004C:00000320", "Study Keyboard", "aa:bb:cc:dd:ee:02")
            _hid(hid / "0003:046D:B035.0003", "0003:0000046D:0000B035", "Logitech MX Master", "11:22:33:44:55:66")
            _supply(power / "hid-04:4b:ed:eb:f5:10-battery-144", self.ALIAS, "38", "Discharging")
            _supply(power / "hid-aa:bb:cc:dd:ee:02-battery-9", "Study Keyboard", "64", "Charging")
            _supply(power / "hid-11:22:33:44:55:66-battery-1", "Logitech MX Master", "40", "Discharging")
            _supply(power / "hid-aa:bb:cc:dd:ee:11-battery-47", "Magic Keyboard", "91", "Full")
            _supply(power / "BAT0", "Internal Battery", "77", "Discharging")
            _supply(power / "hid-de:ad:00:00:00:01-battery-2", "Spare Trackpad", "10", "Discharging")
            with patch.object(om, "HID_SYS", hid), patch.object(om, "POWER_SYS", power):
                found = om.batteries()
        trackpad = found["04:4b:ed:eb:f5:10"]
        self.assertEqual(trackpad["kind"], "trackpad")
        self.assertEqual(trackpad["model"], self.ALIAS)
        self.assertEqual(trackpad["capacity"], 38)
        self.assertEqual(trackpad["status"], "Discharging")
        self.assertEqual(found["aa:bb:cc:dd:ee:02"]["kind"], "keyboard")
        self.assertEqual(found["aa:bb:cc:dd:ee:02"]["capacity"], 64)
        self.assertEqual(found["aa:bb:cc:dd:ee:11"], {
            "kind": "keyboard", "model": "Magic Keyboard", "capacity": 91,
            "status": "Full", "serial": "aa:bb:cc:dd:ee:11",
        })
        self.assertNotIn("11:22:33:44:55:66", found)
        self.assertNotIn("Internal Battery", found)
        self.assertNotIn("de:ad:00:00:00:01", found)
        self.assertNotIn("Spare Trackpad", found)

    def test_serial_match_ignores_case_separator_and_legacy_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hid = root / "hid"
            power = root / "power"
            _hid(hid / "trackpad", "0005:0000004C:00000265", self.ALIAS, "04:4b:ed:eb:f5:10")
            _hid(hid / "keyboard", "0005:000005AC:00000321", "Magic Keyboard", "AA-BB-CC-DD-EE-03")
            _supply(power / "hid-04-4B-ed-eb-f5-10-battery-144", self.ALIAS, "38", "Discharging")
            _supply(power / "hid-aa:bb:cc:dd:ee:03-battery", "Desk Keyboard", "55", "Discharging")
            with patch.object(om, "HID_SYS", hid), patch.object(om, "POWER_SYS", power):
                found = om.batteries()
        self.assertEqual(found["04-4B-ed-eb-f5-10"]["kind"], "trackpad")
        self.assertEqual(found["04-4B-ed-eb-f5-10"]["capacity"], 38)
        self.assertEqual(found["aa:bb:cc:dd:ee:03"]["kind"], "keyboard")
        self.assertEqual(found["aa:bb:cc:dd:ee:03"]["capacity"], 55)

    def test_renamed_trackpad_battery_is_shown_beside_keyboard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hid = root / "hid"
            power = root / "power"
            _hid(hid / "0005:004C:0265.0001", "0005:0000004C:00000265", self.ALIAS, "04:4b:ed:eb:f5:10")
            _hid(hid / "0005:004C:0320.0002", "0005:0000004C:00000320", "Magic Keyboard", "aa:bb:cc:dd:ee:11")
            _supply(power / "hid-04:4b:ed:eb:f5:10-battery-144", self.ALIAS, "38", "Discharging")
            _supply(power / "hid-aa:bb:cc:dd:ee:11-battery-47", "Magic Keyboard", "91", "Discharging")
            hypr = {"keyboard": ["magic-keyboard"], "trackpad": ["magic-trackpad"]}
            with patch.object(om, "HID_SYS", hid), patch.object(om, "POWER_SYS", power), \
                 patch.object(om, "hypr_devices", return_value=hypr), \
                 patch.object(om, "bluetooth_magic", return_value=[]), \
                 patch.object(om, "enabled_plugin_ids", return_value=set()), \
                 patch.object(om, "load_settings", return_value=dict(om.DEFAULT_SETTINGS)):
                snapshot = om.merge_devices()
        self.assertTrue(snapshot["trackpad"]["present"])
        self.assertEqual(snapshot["trackpad"]["devices"], [{"link": "Connected", "battery": 38}])
        self.assertTrue(snapshot["keyboard"]["present"])
        self.assertEqual(snapshot["keyboard"]["devices"], [{"link": "Connected", "battery": 91}])


class ProfileTests(unittest.TestCase):
    def test_tap_is_persistent_and_device_scoped(self):
        profile = om.render_profile(['a"b\\c\nd'], {"tapToClick": True, "workspaceSwipe": False})
        self.assertIn("tap_to_click = true", profile)
        self.assertIn('name = "a\\"b\\\\c\\nd"', profile)
        self.assertNotIn("input =", profile)

    def test_secondary_click_off_uses_button_areas(self):
        for value in (True, False):
            profile = om.render_profile(["magic-trackpad"], {"clickfinger": value})
            self.assertIn("clickfinger_behavior = " + str(value).lower(), profile)

    def test_drag_avoids_horizontal_gesture_conflict(self):
        profile = om.render_profile(["magic-trackpad"], {"drag3fg": True, "notificationCenter": True})
        self.assertIn('fingers = 4, direction = "horizontal"', profile)
        self.assertNotIn("notifications", profile)

    def test_failed_reload_restores_old_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.lua"
            path.write_text("old profile")
            with patch.object(om, "PROFILE_PATH", path), patch.object(om, "enabled_plugin_ids", return_value=set()), \
                 patch.object(om, "hypr_devices", return_value={"trackpad": ["magic-trackpad"]}), \
                 patch.object(om, "hypr_reload", side_effect=[(False, "bad Lua"), (True, "")]), \
                 patch.object(om, "save_settings") as save:
                with self.assertRaisesRegex(RuntimeError, "bad Lua"):
                    cli.set_settings('{"tapToClick":true}')
                self.assertEqual(path.read_text(), "old profile")
                save.assert_not_called()

    def test_first_failed_profile_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.lua"
            with patch.object(om, "PROFILE_PATH", path), patch.object(om, "enabled_plugin_ids", return_value=set()), \
                 patch.object(om, "hypr_devices", return_value={"trackpad": ["magic-trackpad"]}), \
                 patch.object(om, "hypr_reload", side_effect=[(False, "bad Lua"), (True, "")]):
                with self.assertRaises(RuntimeError):
                    cli.apply_trackpad()
                self.assertFalse(path.exists())

    def test_other_plugin_blocks_without_writes(self):
        with patch.object(om, "enabled_plugin_ids", return_value={om.PLUS_ID}), patch.object(om, "atomic_write") as write:
            with self.assertRaisesRegex(ValueError, "Trackpad Plus"):
                cli.apply_trackpad()
            write.assert_not_called()

    def test_failed_error_query_is_not_success(self):
        results = [subprocess.CompletedProcess([], 0, "ok", ""), subprocess.CompletedProcess([], 1, "", "offline")]
        with patch.object(om, "run", side_effect=results):
            self.assertEqual(om.hypr_reload(), (False, "offline"))


if __name__ == "__main__":
    unittest.main()
