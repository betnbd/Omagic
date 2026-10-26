#!/usr/bin/env python3
"""Shared detection, settings, and Hyprland profile helpers for Omagic."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

HOME = Path.home()
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local" / "state")) / "omarchy" / "omagic"
TOGGLE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local" / "state")) / "omarchy" / "toggles" / "hypr"
PROFILE_PATH = TOGGLE_DIR / "zz-omagic.lua"
STATE_PATH = STATE_DIR / "state.json"
SETTINGS_PATH = STATE_DIR / "settings.json"
SHELL_JSON = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "omarchy" / "shell.json"

HID_SYS = Path("/sys/bus/hid/devices")
POWER_SYS = Path("/sys/class/power_supply")

KEYBOARD_NAME_HINTS = ("magic keyboard",)
APPLE_VENDOR = {"05ac", "004c"}

PLUS_ID = "davefano.trackpad-plus"
PULSE_ID = "nixfred.trackpad-pulse"

TIMEOUT = 8

DEFAULT_SETTINGS = {
    "tapToClick": False,
    "clickfinger": True,
    "naturalScroll": False,
    "scrollFactor": 0.4,
    "sensitivity": 0.0,
    "disableWhileTyping": True,
    "drag3fg": False,
    "workspaceSwipe": True,
    "pinchZoom": False,
    "notificationCenter": False,
    "launchpad": False,
    "macFnKeys": True,
}


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.is_file():
        try:
            loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update({k: v for k, v in loaded.items() if k in DEFAULT_SETTINGS})
                validate_settings(data)
        except (OSError, ValueError):
            return dict(DEFAULT_SETTINGS)
    return data


def validate_settings(data: dict) -> None:
    for key, value in data.items():
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"Unknown setting: {key}")
        if isinstance(DEFAULT_SETTINGS[key], bool):
            if type(value) is not bool:
                raise ValueError(f"{key} must be a boolean")
        else:
            low, high = (-0.6, 0.6) if key == "sensitivity" else (0.15, 1.2)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")


def private_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_DIR.chmod(0o700)


def save_settings(data: dict) -> None:
    private_state_dir()
    validate_settings(data)
    atomic_write(SETTINGS_PATH, json.dumps({**DEFAULT_SETTINGS, **data}, indent=2) + "\n")


def run(cmd: list[str], timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def load_saved() -> dict:
    if not STATE_PATH.is_file():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_saved(data: dict) -> None:
    private_state_dir()
    atomic_write(STATE_PATH, json.dumps(data, indent=2) + "\n")


def classify_name(name: str) -> str | None:
    n = (name or "").lower().replace("’", "'")
    if "magic trackpad" in n or ("apple" in n and "trackpad" in n):
        return "trackpad"
    if any(h in n for h in KEYBOARD_NAME_HINTS):
        return "keyboard"
    if "apple" in n and "keyboard" in n:
        return "keyboard"
    return None


def hypr_devices() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {"keyboard": [], "trackpad": []}
    try:
        proc = run(["hyprctl", "devices", "-j"], timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return found
    if proc.returncode != 0 or not proc.stdout.strip():
        return found
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return found
    for bucket in ("mice", "keyboards", "touch", "tablets"):
        for dev in data.get(bucket) or []:
            name = str(dev.get("name") or "")
            kind = classify_name(name.replace("-", " "))
            if kind and name not in found[kind]:
                found[kind].append(name)
    return found


def hid_devices() -> list[dict]:
    out: list[dict] = []
    if not HID_SYS.is_dir():
        return out
    for node in sorted(HID_SYS.iterdir()):
        uevent = read_text(node / "uevent")
        fields = {}
        for line in uevent.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k] = v
        hid_id = fields.get("HID_ID", "")
        parts = hid_id.split(":")
        vendor = parts[1][-4:].lower() if len(parts) >= 2 else ""
        product = parts[2][-4:].lower() if len(parts) >= 3 else ""
        name = fields.get("HID_NAME", "")
        kind = classify_name(name)
        if not kind:
            if vendor in APPLE_VENDOR and product in {
                "0265",
                "029a",
                "029c",
                "029f",
                "0320",
                "0321",
                "0322",
                "0324",
            }:
                kind = "trackpad" if product in {"0265", "0324"} else "keyboard"
            else:
                continue
        phys = fields.get("HID_PHYS", "")
        transport = {"0003": "usb", "0005": "bluetooth"}.get(parts[0], "unknown")
        driver = ""
        driver_link = node / "driver"
        if driver_link.is_symlink():
            driver = os.path.basename(os.path.realpath(driver_link))
        out.append(
            {
                "kind": kind,
                "name": name or kind,
                "serial": fields.get("HID_UNIQ") or "",
                "productId": product,
                "vendorId": vendor,
                "transport": transport,
                "phys": phys,
                "driver": driver,
                "sysfs": str(node),
            }
        )
    return out


def norm_serial(value: str) -> str:
    """Compare HID serials and Bluetooth addresses despite case or '-' vs ':'."""
    return (value or "").lower().replace("-", ":")


def power_supply_serial(name: str) -> str:
    """Serial from hid-<serial>-battery or hid-<serial>-battery-<report id>."""
    prefix = "hid-"
    marker = "-battery"
    if not name.startswith(prefix):
        return ""
    rest = name[len(prefix) :]
    idx = rest.rfind(marker)
    if idx <= 0:
        return ""
    tail = rest[idx + len(marker) :]
    # Older kernels omit the report id; current kernels append it (battery-144).
    if tail and not (tail.startswith("-") and tail[1:].isdigit()):
        return ""
    return rest[:idx]


def _kinds_by_serial(hid: list[dict] | None = None) -> dict[str, str]:
    kinds: dict[str, str] = {}
    for dev in hid_devices() if hid is None else hid:
        serial = norm_serial(dev.get("serial") or "")
        if serial and serial not in kinds:
            kinds[serial] = dev["kind"]
    return kinds


def batteries(hid: list[dict] | None = None) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if not POWER_SYS.is_dir():
        return result
    kinds_by_serial: dict[str, str] | None = None
    for node in POWER_SYS.iterdir():
        model = read_text(node / "model_name")
        serial = power_supply_serial(node.name)
        kind = classify_name(model)
        if not kind and serial:
            # model_name is the Bluetooth alias (the HID device name). A renamed
            # Magic Trackpad or keyboard no longer says "Magic", but the supply
            # is still hid-<serial>-battery[-<id>] for that device's HID_UNIQ.
            if kinds_by_serial is None:
                kinds_by_serial = _kinds_by_serial(hid)
            kind = kinds_by_serial.get(norm_serial(serial))
        if not kind:
            continue
        cap = read_text(node / "capacity")
        status = read_text(node / "status")
        try:
            capacity = int(cap)
        except ValueError:
            continue
        key = serial or model
        prev = result.get(key)
        if prev and prev.get("capacity") == capacity:
            continue
        result[key] = {
            "kind": kind,
            "model": model,
            "capacity": capacity,
            "status": status,
            "serial": serial,
        }
    return result


def parse_device_listing(text: str) -> list[tuple[str, str]]:
    rows = []
    for line in text.splitlines():
        # bluetoothctl may colorize; strip ANSI.
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).strip()
        parts = line.split(maxsplit=2)
        if len(parts) < 3 or parts[0] != "Device":
            continue
        rows.append((parts[1], parts[2]))
    return rows


def bt_info(mac: str) -> dict:
    info = {
        "address": mac,
        "alias": mac,
        "kind": None,
        "paired": False,
        "bonded": False,
        "trusted": False,
        "connected": False,
        "battery": None,
    }
    try:
        ip = run(["bluetoothctl", "info", mac], timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return info
    for iline in ip.stdout.splitlines():
        t = iline.strip()
        if t.startswith("Name:"):
            info["alias"] = t.split(":", 1)[-1].strip() or info["alias"]
        elif t.startswith("Alias:"):
            info["alias"] = t.split(":", 1)[-1].strip() or info["alias"]
        elif t.startswith("Paired:"):
            info["paired"] = t.endswith("yes")
        elif t.startswith("Bonded:"):
            info["bonded"] = t.endswith("yes")
        elif t.startswith("Trusted:"):
            info["trusted"] = t.endswith("yes")
        elif t.startswith("Connected:"):
            info["connected"] = t.endswith("yes")
        elif t.startswith("Battery Percentage:") or t.startswith("Battery:"):
            value = t.split(":", 1)[1].strip()
            decimal = re.search(r"\((\d+)\)", value)
            try:
                battery = int(decimal[1]) if decimal else int(value, 16 if value.startswith("0x") else 10)
                if 0 <= battery <= 100:
                    info["battery"] = battery
            except ValueError:
                pass
        elif t.startswith("Icon:"):
            info["icon"] = t.split(":", 1)[-1].strip().lower()
        elif t.startswith("Class:"):
            info["class"] = t.split(":", 1)[-1].strip().lower()
        elif t.startswith("Modalias:"):
            info["modalias"] = t.split(":", 1)[-1].strip().lower()
    info["kind"] = classify_bt(info)
    return info


def classify_bt(info: dict) -> str | None:
    kind = classify_name(info.get("alias") or "") or classify_name(info.get("name") or "")
    if kind:
        return kind
    mod = info.get("modalias") or ""
    # bluetooth:v004Cp0324d0319 — Apple vendor 004C, product IDs from hid-magicmouse/hid-apple
    if "v004c" in mod:
        prod = ""
        if "p" in mod:
            i = mod.find("p")
            prod = mod[i + 1 : i + 5]
        if prod in {"0324", "0265", "030e"}:
            return "trackpad"
        if prod in {"0321", "0320", "0322", "029a", "029c", "029f"}:
            return "keyboard"
    return None


def bluetooth_magic() -> list[dict]:
    seen: dict[str, dict] = {}
    for args in (["devices", "Paired"], ["devices"]):
        try:
            proc = run(["bluetoothctl", *args], timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        for mac, alias in parse_device_listing(proc.stdout):
            if mac in seen:
                continue
            info = bt_info(mac)
            if not info.get("kind"):
                info["kind"] = classify_name(alias)
            if not info.get("kind"):
                continue
            if alias and not classify_name(info.get("alias") or ""):
                info["alias"] = alias
            seen[mac] = info
    return list(seen.values())


def discover_magic(kind: str | None = None, seconds: int = 22) -> list[dict]:
    """Scan Dual, then BR/EDR, for Magic Keyboard / Trackpad advertisements."""
    run(["omarchy", "bluetooth", "power", "on"], timeout=8)
    scanners: list[subprocess.Popen] = []
    scan_cmd = ["bluetoothctl", "scan", "bredr"] if kind == "trackpad" else ["bluetoothctl", "scan", "on"]
    try:
        scanners.append(
            subprocess.Popen(scan_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        )
    except FileNotFoundError:
        return []
    found: dict[str, dict] = {}
    want = {kind} if kind in {"keyboard", "trackpad"} else {"keyboard", "trackpad"}
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                listing = run(["bluetoothctl", "devices"], timeout=4)
            except subprocess.TimeoutExpired:
                listing = None
            if listing:
                for mac, alias in parse_device_listing(listing.stdout):
                    info = bt_info(mac)
                    if not info.get("kind"):
                        info["kind"] = classify_name(alias)
                    if info.get("kind") not in want:
                        continue
                    named = classify_name(alias) or classify_name(info.get("alias") or "")
                    apple = "v004c" in (info.get("modalias") or "")
                    if named or apple:
                        found[mac] = info
            if {d["kind"] for d in found.values()} >= want:
                break
            time.sleep(1.0)
    finally:
        for scanner in scanners:
            if scanner.poll() is None:
                scanner.terminate()
                try:
                    scanner.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    scanner.kill()
                    scanner.wait()
    return list(found.values())


def enabled_plugin_ids() -> set[str]:
    ids: set[str] = set()
    try:
        proc = run(["omarchy", "plugin", "list", "--json"], timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            items = data if isinstance(data, list) else data.get("plugins") or []
            for p in items:
                if p.get("enabled") and p.get("id"):
                    ids.add(p["id"])
            return ids
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    if SHELL_JSON.is_file():
        try:
            shell = json.loads(SHELL_JSON.read_text(encoding="utf-8"))
            layout = (shell.get("bar") or {}).get("layout") or {}
            for section in layout.values():
                if not isinstance(section, list):
                    continue
                for item in section:
                    if isinstance(item, dict) and item.get("id"):
                        ids.add(item["id"])
                    elif isinstance(item, str):
                        ids.add(item)
        except (OSError, json.JSONDecodeError):
            pass
    return ids


def merge_devices() -> dict:
    hypr = hypr_devices()
    hid = hid_devices()
    bats = batteries(hid)
    bt = bluetooth_magic()

    def group(kind: str) -> list[dict]:
        buckets: dict[str, dict] = {}
        for h in hid:
            if h["kind"] != kind:
                continue
            key = h["serial"] or h["name"]
            rec = buckets.setdefault(
                key,
                {
                    "kind": kind,
                    "name": h["name"],
                    "serial": h["serial"],
                    "productId": h["productId"],
                    "transport": h["transport"],
                    "driver": h["driver"],
                    "hyprNames": [],
                    "battery": None,
                    "batteryStatus": None,
                    "bluetooth": None,
                },
            )
            if h["transport"] == "usb":
                rec["transport"] = "usb"
        for name in hypr.get(kind, []):
            if not buckets:
                buckets["_hypr"] = {
                    "kind": kind,
                    "name": name,
                    "serial": "",
                    "productId": "",
                    "transport": "unknown",
                    "driver": "",
                    "hyprNames": [name],
                    "battery": None,
                    "batteryStatus": None,
                    "bluetooth": None,
                }
            else:
                # Attach hypr names to every record of this kind; USB-C trackpads
                # expose several HID interfaces and several hypr names.
                for rec in buckets.values():
                    if name not in rec["hyprNames"]:
                        rec["hyprNames"].append(name)
        for serial, bat in bats.items():
            if bat["kind"] != kind:
                continue
            matched = buckets.get(serial) or buckets.get(norm_serial(serial))
            if not matched:
                for rec in buckets.values():
                    if norm_serial(rec.get("serial") or "") == norm_serial(serial):
                        matched = rec
                        break
            if not matched:
                for rec in buckets.values():
                    if not rec.get("serial"):
                        matched = rec
                        break
            if matched:
                matched["battery"] = bat["capacity"]
                matched["batteryStatus"] = bat["status"]
                if not matched["serial"]:
                    matched["serial"] = serial
        for b in bt:
            if b["kind"] != kind:
                continue
            target = next((rec for rec in buckets.values()
                           if norm_serial(rec["serial"]) == norm_serial(b["address"])), None)
            if target is None and b.get("connected"):
                candidates = [rec for rec in buckets.values()
                              if not rec["serial"] and not rec["bluetooth"]]
                if len(candidates) == 1:
                    target = candidates[0]
            if target is None:
                buckets[b["address"]] = {
                    "kind": kind,
                    "name": b["alias"],
                    "serial": b["address"],
                    "productId": "",
                    "transport": "bluetooth",
                    "driver": "",
                    "hyprNames": [],
                    "battery": b.get("battery"),
                    "batteryStatus": None,
                    "bluetooth": b,
                }
            else:
                target["bluetooth"] = b
                if b.get("battery") is not None and target["battery"] is None:
                    target["battery"] = b["battery"]
                if b.get("connected"):
                    target["transport"] = "wireless"
        for rec in buckets.values():
            wireless = rec.get("bluetooth") or {}
            if wireless.get("connected"):
                rec["link"] = "Wireless"
            elif rec.get("transport") == "usb":
                rec["link"] = "USB"
            else:
                rec["link"] = "Disconnected" if wireless else "Connected"
        return sorted(buckets.values(), key=lambda rec: rec["link"] == "Disconnected")

    keyboards = group("keyboard")
    trackpads = group("trackpad")
    enabled = enabled_plugin_ids()
    settings = load_settings()
    fnmode = read_text(Path("/sys/module/hid_apple/parameters/fnmode"))
    if fnmode in {"1", "2"}:
        settings["macFnKeys"] = fnmode == "1"
    return {
        "keyboard": {"present": any(d["link"] != "Disconnected" for d in keyboards), "devices": public_devices(keyboards)},
        "trackpad": {"present": any(d["link"] != "Disconnected" for d in trackpads), "devices": public_devices(trackpads)},
        "plugins": {"plus": PLUS_ID in enabled, "pulse": PULSE_ID in enabled},
        "profile": {
            "applied": PROFILE_PATH.is_file(),
        },
        "settings": settings,
    }


def public_devices(devices: list[dict]) -> list[dict]:
    """The panel and its IPC do not need hardware addresses or serial numbers."""
    return [{key: device[key] for key in ("link", "battery")} for device in devices]


def lua_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def _lua_bool(v: bool) -> str:
    return "true" if v else "false"


def render_profile(names: list[str], settings: dict | None = None) -> str:
    s = dict(DEFAULT_SETTINGS)
    if settings:
        s.update(settings)
    lines = [
        "-- Generated by Omagic. Use omagic.py revert-trackpad to remove.",
        "",
    ]
    scroll = float(s.get("scrollFactor") or 0.4)
    sens = float(s.get("sensitivity") or 0.0)
    drag = 1 if s.get("drag3fg") else 0
    space_fingers = 4 if s.get("drag3fg") and s.get("workspaceSwipe") else 3
    for name in names:
        lines += [
            "hl.device({",
            f'  name = "{lua_escape(name)}",',
            f"  tap_to_click = {_lua_bool(s['tapToClick'])},",
            f"  natural_scroll = {_lua_bool(bool(s.get('naturalScroll')))},",
            f"  clickfinger_behavior = {_lua_bool(bool(s.get('clickfinger')))},",
            f"  disable_while_typing = {_lua_bool(bool(s.get('disableWhileTyping')))},",
            f"  drag_3fg = {drag},",
            f"  scroll_factor = {scroll:.2f},",
            f"  sensitivity = {sens:.2f},",
            "})",
            "",
        ]
    if s.get("workspaceSwipe"):
        # Hyprland workspace swipe is inverted (natural) by default and does
        # not follow touchpad.natural_scroll. Tie them together here.
        lines += [
            "hl.config({",
            "  gestures = {",
            f"    workspace_swipe_invert = {_lua_bool(bool(s.get('naturalScroll')))},",
            "  },",
            "})",
        ]
        lines.append(
            f'hl.gesture({{ fingers = {space_fingers}, direction = "horizontal", action = "workspace" }})'
        )
    if s.get("pinchZoom"):
        lines.append(
            'hl.gesture({ fingers = 2, direction = "pinch", action = "cursor_zoom", zoom_level = 1, mode = "live" })'
        )
    if s.get("notificationCenter") and space_fingers != 4:
        lines.append(
            'hl.gesture({ fingers = 4, direction = "right", action = function() hl.exec_cmd("omarchy-shell notifications showHistory") end })'
        )
    if s.get("launchpad"):
        lines.append(
            'hl.gesture({ fingers = 4, direction = "up", action = function() hl.exec_cmd("omarchy-menu toggle apps") end })'
        )
    lines.append("")
    return "\n".join(lines)


def hypr_reload() -> tuple[bool, str]:
    try:
        reload = run(["hyprctl", "reload"], timeout=8)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if reload.returncode != 0:
        return False, (reload.stderr or reload.stdout or "hyprctl reload failed").strip()
    try:
        errors = run(["hyprctl", "configerrors"], timeout=5)
    except subprocess.TimeoutExpired:
        return False, "hyprctl configerrors timed out"
    if errors.returncode != 0:
        return False, (errors.stderr or errors.stdout or "Unable to check Hyprland configuration").strip()
    text = (errors.stdout or "").strip()
    if text and text.lower() not in {"nothing", "no errors", ""}:
        # hyprctl prints nothing or "nothing" when clean; anything else is an error.
        if "ok" not in text.lower() or len(text) > 4:
            return False, text
    return True, ""


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        Path(tmp_name).replace(path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def apply_fnmode(mac_fn: bool) -> tuple[bool, str]:
    """fnmode 1 = Mac (media row, Globe for F-keys). 2 = F-keys first."""
    value = "1" if mac_fn else "2"
    path = Path("/sys/module/hid_apple/parameters/fnmode")
    try:
        path.write_text(value)
        return True, ""
    except OSError:
        pass
    helper = Path("/usr/lib/omagic/set-fnmode")
    if not helper.is_file():
        return False, "Install the optional Fn-mode helper: sudo ./install-fnmode.sh"
    try:
        proc = run(["pkexec", str(helper), value], timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "fnmode failed").strip()
    return True, ""


def known_addresses(kind: str) -> list[str]:
    saved = load_saved()
    by_kind = saved.get("addressesByKind") or {}
    addrs = list(by_kind.get(kind) or [])
    for b in bluetooth_magic():
        if b.get("kind") == kind and b.get("address") and b["address"] not in addrs:
            addrs.append(b["address"])
    return addrs


def remember_address(kind: str, mac: str) -> None:
    saved = load_saved()
    addrs = list(dict.fromkeys((saved.get("addresses") or []) + [mac]))
    saved["addresses"] = addrs
    by_kind = dict(saved.get("addressesByKind") or {})
    kind_addrs = list(dict.fromkeys((by_kind.get(kind) or []) + [mac]))
    by_kind[kind] = kind_addrs
    saved["addressesByKind"] = by_kind
    save_saved(saved)


