"""Monitor Changer: switch monitor inputs and power over DDC/CI from the Windows system tray.

Usage:
    python monitor_changer.py                          run in the system tray
    python monitor_changer.py --list                   list monitors, inputs and power state
    python monitor_changer.py --set MONITOR INPUT      e.g. --set 2 hdmi1
    python monitor_changer.py --power MONITOR on|off   e.g. --power all off

MONITOR is a number from --list, part of a monitor's name, or "all".
The tray needs pystray and Pillow (pip install pystray Pillow); the command
line options only need Python.

https://github.com/harlyvibes/Monitor-Changer
"""

import argparse
import ctypes
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import threading
import time
import winreg
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

try:  # Only the tray needs these; the command line options work without them.
    import pystray
    from PIL import Image, ImageDraw
except ImportError as exc:
    pystray = None
    TRAY_IMPORT_ERROR = exc
else:
    TRAY_IMPORT_ERROR = None

__version__ = "0.2.0"

APP_NAME = "Monitor Changer"
APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "MonitorChanger"
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "monitor_changer.log"

log = logging.getLogger("monitor_changer")


# --- MCCS: input names, capabilities strings, power modes --------------------

#: VCP code for "Input Select" (a.k.a. input source).
VCP_INPUT_SOURCE = 0x60

#: Input source values from the MCCS 2.2a spec, plus common vendor extensions.
INPUT_NAMES = {
    0x01: "VGA 1",
    0x02: "VGA 2",
    0x03: "DVI 1",
    0x04: "DVI 2",
    0x05: "Composite 1",
    0x06: "Composite 2",
    0x07: "S-Video 1",
    0x08: "S-Video 2",
    0x09: "Tuner 1",
    0x0A: "Tuner 2",
    0x0B: "Tuner 3",
    0x0C: "Component 1",
    0x0D: "Component 2",
    0x0E: "Component 3",
    0x0F: "DisplayPort 1",
    0x10: "DisplayPort 2",
    0x11: "HDMI 1",
    0x12: "HDMI 2",
    # Not in the spec, but widely used (Dell, HP, Lenovo, ...) for USB-C / Thunderbolt.
    0x1B: "USB-C",
}

#: Shown when a monitor answers DDC/CI but doesn't list its inputs in its capabilities.
FALLBACK_INPUTS = (0x0F, 0x10, 0x11, 0x12, 0x1B, 0x03, 0x01)

#: VCP code for "Power Mode", and its "on" value.
VCP_POWER_MODE = 0xD6
POWER_ON = 0x01
#: "Off" values in order of preference. DPM off (0x04), suspend and standby keep
#: the monitor listening for DDC/CI so it can be turned back on; 0x05 is like
#: pressing the power button and usually can't be undone remotely.
POWER_OFF_PREFERENCE = (0x04, 0x03, 0x02, 0x05)

# A VCP code, optionally followed by its value list (tolerating a truncated string).
_HEX_CODE = re.compile(r"([0-9A-Fa-f]{2})\s*(?:\(([^()]*)(?:\)|$))?")
_HEX_BYTE = re.compile(r"[0-9A-Fa-f]{2}")


def input_name(code):
    """Human readable name for an input source value."""
    return INPUT_NAMES.get(code, f"Input 0x{code:02X}")


def _squash(text):
    return re.sub(r"[\s_-]", "", text).casefold()


_INPUT_ALIASES = {_squash(name): code for code, name in INPUT_NAMES.items()}
_INPUT_ALIASES.update({f"dp{n}": 0x0E + n for n in (1, 2)})
_INPUT_ALIASES.update({"usbc": 0x1B, "typec": 0x1B, "thunderbolt": 0x1B})


def parse_input(text):
    """Parse an input given as a code ("0x11", "17") or a name ("HDMI 1", "hdmi1", "dp2")."""
    try:
        code = int(text, 0)
    except ValueError:
        code = _INPUT_ALIASES.get(_squash(text))
    if code is None or not 0 <= code <= 0xFF:
        raise ValueError(f"unknown input {text!r}")
    return code


def _top_level_groups(text):
    """Yield ``(key, body)`` for each ``key(body)`` group at the top level of *text*."""
    i, n = 0, len(text)
    while i < n:
        open_at = text.find("(", i)
        if open_at < 0:
            return
        key = text[i:open_at].strip()
        depth = 0
        for close_at in range(open_at, n):
            if text[close_at] == "(":
                depth += 1
            elif text[close_at] == ")":
                depth -= 1
                if depth == 0:
                    break
        else:
            # Unbalanced: treat the rest of the string as the body.
            close_at = n
        # Stray ')' characters from sloppy firmware end up in the key; drop them.
        yield key.replace(")", "").strip().lower(), text[open_at + 1:close_at]
        i = close_at + 1


def parse_capabilities(caps):
    """Parse a DDC/CI capabilities string.

    Returns a dict of the top-level groups (``model``, ``type``, ``mccs_ver``, ...)
    with the ``vcp`` group replaced by a dict mapping each VCP code to the list of
    values it supports (empty when the monitor didn't enumerate them).
    """
    caps = (caps or "").replace("\x00", "").strip()
    # The whole string is normally wrapped in one pair of parentheses.
    groups = list(_top_level_groups(caps))
    if groups and groups[0][0] == "":
        groups = list(_top_level_groups(groups[0][1])) + groups[1:]

    result = {}
    for key, body in groups:
        if key == "vcp":
            vcp = {}
            for match in _HEX_CODE.finditer(body):
                code = int(match.group(1), 16)
                values = match.group(2)
                vcp[code] = [int(v, 16) for v in _HEX_BYTE.findall(values)] if values else []
            result["vcp"] = vcp
        elif key:
            result[key] = body.strip()
    return result


def supported_inputs(caps):
    """Input source values a monitor advertises in its capabilities string."""
    return parse_capabilities(caps).get("vcp", {}).get(VCP_INPUT_SOURCE, [])


def best_power_off_value(values):
    """The best "off" value among the power modes a monitor lists, e.g. ``D6(01 04 05)``."""
    for value in POWER_OFF_PREFERENCE:
        if value in values:
            return value
    return POWER_OFF_PREFERENCE[0]


# --- Windows API --------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)

UINT32 = ctypes.c_uint32
MONITORINFOF_PRIMARY = 0x1
QDC_ONLY_ACTIVE_PATHS = 0x2
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
ERROR_SUCCESS = 0
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_HIDE = 0
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_APP_REFRESH_MENU = 0x8000 + 1  # WM_APP + 1
MB_ICONERROR, MB_YESNO_QUESTION, IDYES = 0x10, 0x24, 6


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", UINT32), ("Denominator", UINT32)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", UINT32),
        ("modeInfoIdx", UINT32),
        ("statusFlags", UINT32),
    ]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", UINT32),
        ("modeInfoIdx", UINT32),
        ("outputTechnology", UINT32),
        ("rotation", UINT32),
        ("scaling", UINT32),
        ("refreshRate", DISPLAYCONFIG_RATIONAL),
        ("scanLineOrdering", UINT32),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", UINT32),
    ]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags", UINT32),
    ]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    # We never read the mode union, so it's kept opaque (48 bytes, 8-byte aligned).
    _fields_ = [
        ("infoType", UINT32),
        ("id", UINT32),
        ("adapterId", LUID),
        ("_mode", ctypes.c_uint64 * 6),
    ]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("type", UINT32),
        ("size", UINT32),
        ("adapterId", LUID),
        ("id", UINT32),
    ]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("viewGdiDeviceName", wintypes.WCHAR * 32),
    ]


class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("flags", UINT32),
        ("outputTechnology", UINT32),
        ("edidManufactureId", ctypes.c_uint16),
        ("edidProductCodeId", ctypes.c_uint16),
        ("connectorInstance", UINT32),
        ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
        ("monitorDevicePath", wintypes.WCHAR * 128),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)


def _bind(dll, name, restype, *argtypes):
    func = getattr(dll, name)
    func.restype = restype
    func.argtypes = argtypes
    return func


_EnumDisplayMonitors = _bind(
    user32, "EnumDisplayMonitors", wintypes.BOOL,
    wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM,
)
_GetMonitorInfoW = _bind(
    user32, "GetMonitorInfoW", wintypes.BOOL, wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)
)
_GetDisplayConfigBufferSizes = _bind(
    user32, "GetDisplayConfigBufferSizes", wintypes.LONG,
    UINT32, ctypes.POINTER(UINT32), ctypes.POINTER(UINT32),
)
_QueryDisplayConfig = _bind(
    user32, "QueryDisplayConfig", wintypes.LONG,
    UINT32, ctypes.POINTER(UINT32), ctypes.POINTER(DISPLAYCONFIG_PATH_INFO),
    ctypes.POINTER(UINT32), ctypes.POINTER(DISPLAYCONFIG_MODE_INFO), ctypes.c_void_p,
)
_DisplayConfigGetDeviceInfo = _bind(
    user32, "DisplayConfigGetDeviceInfo", wintypes.LONG, ctypes.POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER)
)
_PostMessageW = _bind(
    user32, "PostMessageW", wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)
_MessageBoxW = _bind(user32, "MessageBoxW", ctypes.c_int, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)
_ShowWindow = _bind(user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_GetConsoleWindow = _bind(kernel32, "GetConsoleWindow", wintypes.HWND)
_GetConsoleProcessList = _bind(kernel32, "GetConsoleProcessList", wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD)
_OpenProcess = _bind(kernel32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_QueryFullProcessImageNameW = _bind(
    kernel32, "QueryFullProcessImageNameW", wintypes.BOOL,
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
)
_CloseHandle = _bind(kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_CreateMutexW = _bind(kernel32, "CreateMutexW", wintypes.HANDLE, ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
_GetNumberOfPhysicalMonitorsFromHMONITOR = _bind(
    dxva2, "GetNumberOfPhysicalMonitorsFromHMONITOR", wintypes.BOOL,
    wintypes.HMONITOR, ctypes.POINTER(wintypes.DWORD),
)
_GetPhysicalMonitorsFromHMONITOR = _bind(
    dxva2, "GetPhysicalMonitorsFromHMONITOR", wintypes.BOOL,
    wintypes.HMONITOR, wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR),
)
_DestroyPhysicalMonitor = _bind(dxva2, "DestroyPhysicalMonitor", wintypes.BOOL, wintypes.HANDLE)
_GetCapabilitiesStringLength = _bind(
    dxva2, "GetCapabilitiesStringLength", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
)
_CapabilitiesRequestAndCapabilitiesReply = _bind(
    dxva2, "CapabilitiesRequestAndCapabilitiesReply", wintypes.BOOL,
    wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD,
)
_GetVCPFeatureAndVCPFeatureReply = _bind(
    dxva2, "GetVCPFeatureAndVCPFeatureReply", wintypes.BOOL,
    wintypes.HANDLE, ctypes.c_ubyte, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
)
_SetVCPFeature = _bind(dxva2, "SetVCPFeature", wintypes.BOOL, wintypes.HANDLE, ctypes.c_ubyte, wintypes.DWORD)


def _check(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


# --- DDC/CI ---------------------------------------------------------------------
#
# Every call that talks to a monitor goes over the I2C bus in the video cable
# and can take tens of milliseconds (capabilities strings take seconds), so the
# tray keeps these off the UI thread and runs them one at a time.

def _retry(func, attempts=3, delay=0.15):
    """DDC/CI is a flaky bus; most transient failures go away on a retry."""
    for attempt in range(attempts):
        try:
            return func()
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def get_vcp(handle, code):
    """Return ``(current, maximum)`` for a VCP feature."""
    current, maximum = wintypes.DWORD(), wintypes.DWORD()
    _check(_GetVCPFeatureAndVCPFeatureReply(handle, code, None, ctypes.byref(current), ctypes.byref(maximum)))
    return current.value, maximum.value


def set_vcp(handle, code, value):
    _check(_SetVCPFeature(handle, code, value))


def get_capabilities(handle):
    length = wintypes.DWORD()
    _check(_GetCapabilitiesStringLength(handle, ctypes.byref(length)))
    buffer = ctypes.create_string_buffer(length.value)
    _check(_CapabilitiesRequestAndCapabilitiesReply(handle, buffer, length))
    return buffer.value.decode("ascii", errors="replace")


@dataclass(eq=False)
class Monitor:
    """A physical monitor and what we know about it over DDC/CI."""

    name: str
    device: str  # GDI device name, e.g. \\.\DISPLAY1
    position: tuple  # top-left corner on the virtual desktop
    primary: bool
    handle: int | None  # physical monitor handle; None when there isn't one
    index: int = 0  # 1-based, ordered left to right
    capabilities: str = ""
    inputs: list = field(default_factory=list)  # input source values the monitor supports
    current_input: int | None = None
    power_off_value: int | None = None  # power mode value for "off"; None if power control isn't offered
    power_on: bool | None = None  # None when unknown
    error: str | None = None

    @property
    def supported(self):
        return self.handle is not None and self.error is None

    def probe(self):
        """Query DDC/CI for the current and supported inputs, and power control.

        Slow (capabilities requests regularly take 1-2 seconds per monitor).
        Failures are recorded on ``error`` rather than raised.
        """
        if self.handle is None:
            self.error = self.error or "no physical monitor handle"
            return
        try:
            self.current_input = self.read_input()
        except OSError as exc:
            self.error = f"DDC/CI not available: {exc.strerror or exc}"
            log.info("%s: %s", self.name, self.error)
            return

        try:
            self.capabilities = _retry(lambda: get_capabilities(self.handle))
        except OSError as exc:
            log.warning("%s: could not read capabilities: %s", self.name, exc)

        parsed = parse_capabilities(self.capabilities)
        if parsed.get("model") and (not self.name or self.name.startswith("Generic ")):
            self.name = parsed["model"]
        self.inputs = list(dict.fromkeys(parsed.get("vcp", {}).get(VCP_INPUT_SOURCE, [])))
        if not self.inputs:
            log.info("%s: capabilities don't list inputs, using common defaults", self.name)
            self.inputs = list(FALLBACK_INPUTS)
        if self.current_input is not None and self.current_input not in self.inputs:
            self.inputs.append(self.current_input)

        vcp = parsed.get("vcp")
        # Offer power control when it's advertised, or when we can't tell.
        if not vcp or VCP_POWER_MODE in vcp:
            self.power_off_value = best_power_off_value((vcp or {}).get(VCP_POWER_MODE, []))
            try:
                self.power_on = self.read_power()
            except OSError as exc:
                log.info("%s: could not read power mode: %s", self.name, exc)

        log.info(
            "%s: inputs=%s current=%s power_on=%s power_off_value=%s",
            self.name,
            [input_name(c) for c in self.inputs],
            input_name(self.current_input) if self.current_input is not None else None,
            self.power_on,
            f"0x{self.power_off_value:02X}" if self.power_off_value is not None else None,
        )

    def read_input(self):
        current, _ = _retry(lambda: get_vcp(self.handle, VCP_INPUT_SOURCE))
        # Some monitors put junk in the high byte; the input value is the low byte.
        return current & 0xFF

    def set_input(self, code):
        _retry(lambda: set_vcp(self.handle, VCP_INPUT_SOURCE, code))
        self.current_input = code

    def read_power(self):
        current, _ = _retry(lambda: get_vcp(self.handle, VCP_POWER_MODE))
        return (current & 0xFF) == POWER_ON

    def set_power(self, on, off_value=None):
        """Turn the monitor on, or off with *off_value* (default: the detected best value)."""
        if on:
            value = POWER_ON
        else:
            value = off_value or self.power_off_value or POWER_OFF_PREFERENCE[0]
        # A monitor that's off can be slow to answer, so try harder when waking it.
        _retry(
            lambda: set_vcp(self.handle, VCP_POWER_MODE, value),
            attempts=5 if on else 3,
            delay=0.3 if on else 0.15,
        )
        self.power_on = on

    def close(self):
        if self.handle is not None:
            _DestroyPhysicalMonitor(self.handle)
            self.handle = None


def _display_monitors():
    handles = []

    @MONITORENUMPROC
    def callback(hmonitor, hdc, rect, lparam):
        handles.append(hmonitor)
        return True

    _check(_EnumDisplayMonitors(None, None, callback, 0))
    return handles


def _monitor_info(hmonitor):
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(info)
    _check(_GetMonitorInfoW(hmonitor, ctypes.byref(info)))
    return info


def _physical_monitors(hmonitor):
    count = wintypes.DWORD()
    _check(_GetNumberOfPhysicalMonitorsFromHMONITOR(hmonitor, ctypes.byref(count)))
    if not count.value:
        return []
    monitors = (PHYSICAL_MONITOR * count.value)()
    _check(_GetPhysicalMonitorsFromHMONITOR(hmonitor, count.value, monitors))
    return list(monitors)


def _friendly_names():
    """Map GDI device names (\\\\.\\DISPLAY1) to the EDID names of the monitors showing them."""
    names = {}
    num_paths, num_modes = UINT32(), UINT32()
    for _ in range(5):  # the topology can change between the two calls
        if _GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(num_paths), ctypes.byref(num_modes)):
            return names
        paths = (DISPLAYCONFIG_PATH_INFO * num_paths.value)()
        modes = (DISPLAYCONFIG_MODE_INFO * num_modes.value)()
        rc = _QueryDisplayConfig(
            QDC_ONLY_ACTIVE_PATHS, ctypes.byref(num_paths), paths, ctypes.byref(num_modes), modes, None
        )
        if rc == ERROR_SUCCESS:
            break
        if rc != ERROR_INSUFFICIENT_BUFFER:
            return names
    else:
        return names

    for path in paths[: num_paths.value]:
        source = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
        source.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
        source.header.size = ctypes.sizeof(source)
        source.header.adapterId = path.sourceInfo.adapterId
        source.header.id = path.sourceInfo.id
        target = DISPLAYCONFIG_TARGET_DEVICE_NAME()
        target.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
        target.header.size = ctypes.sizeof(target)
        target.header.adapterId = path.targetInfo.adapterId
        target.header.id = path.targetInfo.id
        if _DisplayConfigGetDeviceInfo(ctypes.byref(source.header)) == ERROR_SUCCESS and \
                _DisplayConfigGetDeviceInfo(ctypes.byref(target.header)) == ERROR_SUCCESS:
            names.setdefault(source.viewGdiDeviceName, []).append(target.monitorFriendlyDeviceName)
    return names


def enumerate_monitors():
    """List the physical monitors attached to the desktop, ordered left to right.

    This is fast; call :meth:`Monitor.probe` on each to talk DDC/CI to it.
    Call :meth:`Monitor.close` on each when done.
    """
    try:
        friendly = _friendly_names()
    except OSError as exc:
        log.warning("Could not read monitor names: %s", exc)
        friendly = {}

    monitors = []
    for hmonitor in _display_monitors():
        info = _monitor_info(hmonitor)
        common = dict(
            device=info.szDevice,
            position=(info.rcMonitor.left, info.rcMonitor.top),
            primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
        )
        edid_names = friendly.get(info.szDevice, [])
        try:
            physical = _physical_monitors(hmonitor)
        except OSError as exc:
            log.warning("%s: no physical monitors: %s", info.szDevice, exc)
            physical = []
        if not physical:
            monitors.append(Monitor(
                name=edid_names[0] if edid_names else info.szDevice,
                handle=None,
                error="Windows exposes no physical monitor for this display",
                **common,
            ))
        for i, pm in enumerate(physical):
            name = edid_names[i] if i < len(edid_names) and edid_names[i] else pm.szPhysicalMonitorDescription
            # Physical monitor handles are small integers and 0 is valid, but
            # ctypes reads a NULL HANDLE as None.
            monitors.append(Monitor(name=name, handle=pm.hPhysicalMonitor or 0, **common))

    monitors.sort(key=lambda m: m.position)
    for index, monitor in enumerate(monitors, start=1):
        monitor.index = index
    return monitors


def close_all(monitors):
    for monitor in monitors:
        monitor.close()


# --- Config: %APPDATA%\MonitorChanger\config.json --------------------------------

CONFIG_DEFAULTS = {
    # How often to re-read each monitor's current input and power state so the
    # menu stays correct when they're changed with the monitor's own buttons.
    # 0 disables.
    "poll_seconds": 30,
    # Leave monitors that don't answer DDC/CI (e.g. laptop panels) out of the menu.
    "hide_unsupported": False,
    # Per-monitor overrides, keyed by monitor name as shown in the menu:
    #   "label":  name to show instead of the monitor's own
    #   "inputs": {"0x0F": "Desktop"} renames inputs, and adds any the monitor doesn't report
    #   "hide":   ["0x12"] removes inputs from the menu
    #   "power_off": "0x05" power mode value to send for "Power off" (default: detected)
    "monitors": {},
}


def parse_code(value):
    """Parse a code written as an int, "0x0F" or "15". Returns None if invalid."""
    if isinstance(value, int):
        code = value
    else:
        try:
            code = int(str(value).strip(), 0)
        except ValueError:
            return None
    return code if 0 <= code <= 0xFF else None


class Config:
    def __init__(self, data=None):
        self.data = {**CONFIG_DEFAULTS, **(data or {})}

    @classmethod
    def load(cls, path=CONFIG_PATH):
        """Load the config file. Returns ``(config, error_message_or_None)``."""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("top level must be a JSON object")
            return cls(data), None
        except FileNotFoundError:
            return cls(), None
        except (OSError, ValueError) as exc:
            log.error("Ignoring invalid config %s: %s", path, exc)
            return cls(), f"Config file is invalid and was ignored: {exc}"

    @property
    def poll_seconds(self):
        try:
            return max(0.0, float(self.data.get("poll_seconds") or 0))
        except (TypeError, ValueError):
            return CONFIG_DEFAULTS["poll_seconds"]

    @property
    def hide_unsupported(self):
        return bool(self.data.get("hide_unsupported"))

    def _monitor_settings(self, monitor):
        monitors = self.data.get("monitors")
        if not isinstance(monitors, dict):
            return {}
        wanted = monitor.name.casefold()
        for name, settings in monitors.items():
            if name.casefold() == wanted and isinstance(settings, dict):
                return settings
        return {}

    def label_for(self, monitor):
        return str(self._monitor_settings(monitor).get("label") or monitor.name)

    def power_off_value(self, monitor):
        """The configured power mode value for "off", or None to use the detected one."""
        value = self._monitor_settings(monitor).get("power_off")
        code = None if value is None else parse_code(value)
        if value is not None and not code:
            log.warning("%s: ignoring invalid power_off %r in config", monitor.name, value)
        return code or None

    def inputs_for(self, monitor):
        """The ``(code, label)`` pairs to offer for a monitor, sorted by code."""
        settings = self._monitor_settings(monitor)
        renamed = {}
        for key, label in (settings.get("inputs") or {}).items():
            code = parse_code(key)
            if code is None:
                log.warning("%s: ignoring invalid input code %r in config", monitor.name, key)
            else:
                renamed[code] = str(label)
        hidden = {parse_code(c) for c in (settings.get("hide") or [])}
        codes = sorted(set(monitor.inputs) | set(renamed))
        return [(c, renamed.get(c) or input_name(c)) for c in codes if c not in hidden]


def write_template(monitors, path=CONFIG_PATH):
    """Create a config file pre-filled with the detected monitors so it's easy to edit."""
    data = json.loads(json.dumps(CONFIG_DEFAULTS))
    for monitor in monitors:
        if monitor.supported:
            data["monitors"][monitor.name] = {
                "label": "",
                "inputs": {f"0x{c:02X}": input_name(c) for c in sorted(monitor.inputs)},
                "hide": [],
            }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# --- Start with Windows (per-user Run registry key) -----------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "MonitorChanger"


def autostart_command():
    """The command line that starts the tray app without a console window."""
    if getattr(sys, "frozen", False):  # e.g. a PyInstaller build
        return f'"{sys.executable}"'
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    if pythonw.exists():
        python = pythonw
    return f'"{python}" "{Path(__file__).resolve()}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except FileNotFoundError:
        return False


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


# --- System tray -----------------------------------------------------------------

class TrayIcon(pystray.Icon if pystray else object):
    """pystray's Win32 icon, changed to open the menu on left click as well, and
    to accept menu rebuilds from worker threads without breaking an open menu."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._menu_open = False
        self._refresh_pending = False
        self._message_handlers[WM_APP_REFRESH_MENU] = self._on_refresh_menu

    def refresh_menu(self):
        """Rebuild the menu on the tray thread. Safe to call from any thread."""
        if self._hwnd:
            _PostMessageW(self._hwnd, WM_APP_REFRESH_MENU, 0, 0)

    def _on_refresh_menu(self, wparam, lparam):
        # The popup's modal loop dispatches our messages too. Rebuilding then
        # would destroy the menu under the user and remap item IDs, so wait.
        if self._menu_open:
            self._refresh_pending = True
        else:
            self.update_menu()

    def _on_notify(self, wparam, lparam):
        if lparam == WM_LBUTTONUP:
            lparam = WM_RBUTTONUP
        if lparam != WM_RBUTTONUP:
            return super()._on_notify(wparam, lparam)
        self._menu_open = True
        try:
            return super()._on_notify(wparam, lparam)
        finally:
            self._menu_open = False
            if self._refresh_pending:
                self._refresh_pending = False
                self.update_menu()


def make_icon_image(size=64):
    """Draw the tray icon: a monitor with switch arrows."""
    s = size / 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    stand = (148, 163, 184, 255)
    white = (255, 255, 255, 255)
    draw.rectangle([28 * s, 44 * s, 36 * s, 54 * s], fill=stand)
    draw.rounded_rectangle([16 * s, 52 * s, 48 * s, 59 * s], radius=3 * s, fill=stand)
    draw.rounded_rectangle(
        [3 * s, 5 * s, 61 * s, 46 * s], radius=7 * s,
        fill=(37, 99, 235, 255), outline=(191, 219, 254, 255), width=max(1, round(3 * s)),
    )
    arrow = max(1, round(5 * s))
    draw.line([14 * s, 19 * s, 42 * s, 19 * s], fill=white, width=arrow)
    draw.polygon([(40 * s, 11 * s), (51 * s, 19 * s), (40 * s, 27 * s)], fill=white)
    draw.line([22 * s, 33 * s, 50 * s, 33 * s], fill=white, width=arrow)
    draw.polygon([(24 * s, 25 * s), (13 * s, 33 * s), (24 * s, 41 * s)], fill=white)
    return image


def _menu_text(text):
    # A lone '&' marks a keyboard accelerator in Win32 menus.
    return str(text).replace("&", "&&")


def _open_file(path):
    try:
        os.startfile(path)
    except OSError:
        # No program associated with the extension.
        subprocess.Popen(["notepad.exe", str(path)])


class App:
    def __init__(self):
        self.monitors = []
        self.busy = True  # detecting monitors
        self._config_mtime = None
        self._config_error = None
        self._load_config()
        self._ddc = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ddc")
        self._stopping = threading.Event()
        self._poll_queued = False
        self._quiet_until = 0.0
        self.icon = TrayIcon("MonitorChanger", make_icon_image(), APP_NAME, menu=pystray.Menu(self._menu_items))

    # --- lifecycle -------------------------------------------------------

    def run(self):
        try:
            self.icon.run(setup=self._setup)
        finally:
            self._stopping.set()
            self._ddc.shutdown(wait=True, cancel_futures=True)
            close_all(self.monitors)
            log.info("Stopped")

    def _setup(self, icon):
        icon.visible = True
        if self._config_error:
            self._notify(self._config_error)
        self.refresh()
        threading.Thread(target=self._background_loop, name="background", daemon=True).start()

    def quit(self):
        self.icon.stop()

    def _submit(self, func, *args):
        def run():
            try:
                func(*args)
            except Exception:
                log.exception("Error in %s", func.__name__)
        self._ddc.submit(run)

    def _changed(self):
        self.icon.title = self._tooltip()
        self.icon.refresh_menu()

    def _notify(self, message):
        try:
            self.icon.notify(message[:255], APP_NAME)
        except Exception:
            log.exception("Could not show notification")

    # --- config ------------------------------------------------------------

    def _load_config(self):
        try:
            self._config_mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            self._config_mtime = None
        self.config, self._config_error = Config.load()

    def _reload_config_if_changed(self):
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._config_mtime:
            log.info("Config changed, reloading")
            self._load_config()
            if self._config_error:
                self._notify(self._config_error)
            self._changed()

    def open_config(self):
        try:
            if not CONFIG_PATH.exists():
                write_template(self.monitors)
            _open_file(CONFIG_PATH)
        except OSError as exc:
            log.error("Could not open config: %s", exc)
            self._notify(f"Couldn't open {CONFIG_PATH}: {exc}")

    def open_log(self):
        try:
            _open_file(LOG_PATH)
        except OSError as exc:
            self._notify(f"Couldn't open {LOG_PATH}: {exc}")

    def toggle_autostart(self):
        try:
            set_autostart(not autostart_enabled())
        except OSError as exc:
            log.error("Could not change autostart: %s", exc)
            self._notify(f"Couldn't change the startup setting: {exc}")

    # --- DDC/CI work (always on the single DDC thread) ---------------------

    def refresh(self):
        self.busy = True
        self.icon.refresh_menu()
        self._submit(self._detect)

    def _detect(self):
        log.info("Detecting monitors")
        monitors = []
        try:
            monitors = enumerate_monitors()
            for monitor in monitors:
                monitor.probe()
        finally:
            old, self.monitors = self.monitors, monitors
            close_all(old)
            self.busy = False
            self._changed()
        log.info("Found %d monitor(s), %d with DDC/CI", len(monitors), sum(m.supported for m in monitors))

    def _switch(self, monitor, code):
        if self.busy or monitor not in self.monitors or not monitor.supported:
            return  # clicked in a menu built before a refresh
        name, label = self.config.label_for(monitor), self._input_label(monitor, code)
        try:
            monitor.set_input(code)
        except OSError as exc:
            log.error("%s: switching to %s failed: %s", monitor.name, label, exc)
            self._notify(f"Couldn't switch {name} to {label}.\n{exc.strerror or exc}")
            return
        log.info("%s: switched to %s (0x%02X)", monitor.name, label, code)
        # Monitors take a few seconds to switch; don't let a poll read the old input meanwhile.
        self._quiet_until = time.monotonic() + 8
        self._changed()

    def _power(self, monitors, on):
        state = "on" if on else "off"
        failed = []
        for monitor in monitors:
            if self.busy or monitor not in self.monitors or not monitor.supported:
                continue  # clicked in a menu built before a refresh
            try:
                monitor.set_power(on, self.config.power_off_value(monitor))
            except OSError as exc:
                log.error("%s: turning %s failed: %s", monitor.name, state, exc)
                failed.append(self.config.label_for(monitor))
            else:
                log.info("%s: turned %s", monitor.name, state)
        self._quiet_until = time.monotonic() + 8
        self._changed()
        if failed:
            hint = "\nSome monitors stop listening for DDC/CI while off; use the power button." if on else ""
            self._notify(f"Couldn't turn {state} {', '.join(failed)}.{hint}")

    def _poll(self):
        self._poll_queued = False
        changed = False
        for monitor in self.monitors:
            if self.busy or time.monotonic() < self._quiet_until:
                return
            if not monitor.supported:
                continue
            readers = {"current_input": monitor.read_input}
            if self._can_power(monitor):
                readers["power_on"] = monitor.read_power
            for attr, read in readers.items():
                try:
                    value = read()
                except OSError:
                    continue  # asleep, showing another input, ...
                if value != getattr(monitor, attr):
                    log.info("%s: %s changed to %r", monitor.name, attr, value)
                    setattr(monitor, attr, value)
                    changed = True
        if changed:
            self._changed()

    def _background_loop(self):
        last_poll = time.monotonic()
        while not self._stopping.wait(2):
            self._reload_config_if_changed()
            interval = self.config.poll_seconds
            now = time.monotonic()
            if interval and now - last_poll >= max(interval, 5):
                last_poll = now
                if not (self.busy or self._poll_queued or now < self._quiet_until):
                    self._poll_queued = True
                    self._submit(self._poll)

    # --- menu ----------------------------------------------------------------

    def _input_label(self, monitor, code):
        return dict(self.config.inputs_for(monitor)).get(code) or input_name(code)

    def _display_names(self, monitors):
        """Monitor names for the UI, numbered when several monitors share one."""
        names = [self.config.label_for(m) for m in monitors]
        return [f"{name} ({m.index})" if names.count(name) > 1 else name for m, name in zip(monitors, names)]

    def _can_power(self, monitor):
        return monitor.supported and bool(monitor.power_off_value or self.config.power_off_value(monitor))

    def _status(self, monitor):
        """Short state shown next to a monitor's name: "Off" or its current input."""
        if monitor.power_on is False:
            return "Off"
        if monitor.current_input is None:
            return ""
        return self._input_label(monitor, monitor.current_input)

    def _tooltip(self):
        lines = [APP_NAME]
        for monitor, name in zip(self.monitors, self._display_names(self.monitors)):
            if monitor.supported and self._status(monitor):
                lines.append(f"{name}: {self._status(monitor)}")
        text = "\n".join(lines)
        return text if len(text) <= 127 else text[:126] + "…"

    def _switch_action(self, monitor, code):
        def action():
            self._submit(self._switch, monitor, code)
        return action

    def _power_action(self, monitors, on):
        def action():
            self._submit(self._power, monitors, on)
        return action

    @staticmethod
    def _is_current(monitor, code):
        return lambda item: monitor.current_input == code

    def _menu_items(self):
        Item = pystray.MenuItem
        config = self.config
        monitors = [m for m in self.monitors if m.supported or not config.hide_unsupported]

        if self.busy:
            yield Item("Detecting monitors…", None, enabled=False)
        elif not monitors:
            yield Item("No monitors found", None, enabled=False)

        for monitor, name in zip(monitors, self._display_names(monitors)):
            name = _menu_text(name)
            if not monitor.supported:
                yield Item(f"{name}  (no DDC/CI)", None, enabled=False)
                continue
            items = [
                Item(
                    _menu_text(label), self._switch_action(monitor, code),
                    checked=self._is_current(monitor, code), radio=True, enabled=not self.busy,
                )
                for code, label in config.inputs_for(monitor)
            ]
            if self._can_power(monitor):
                items += [
                    pystray.Menu.SEPARATOR,
                    Item("Power on", self._power_action([monitor], True), enabled=not self.busy),
                    Item("Power off", self._power_action([monitor], False), enabled=not self.busy),
                ]
            title = f"{name}\t{_menu_text(self._status(monitor))}"
            if len(monitors) == 1:
                # One monitor: list everything directly, one click to switch.
                yield Item(title, None, enabled=False)
                yield from items
            else:
                yield Item(title, pystray.Menu(*items))

        powerable = [m for m in monitors if self._can_power(m)]
        if len(powerable) > 1:
            yield pystray.Menu.SEPARATOR
            yield Item("Power on all monitors", self._power_action(powerable, True), enabled=not self.busy)
            yield Item("Power off all monitors", self._power_action(powerable, False), enabled=not self.busy)

        yield pystray.Menu.SEPARATOR
        yield Item("Refresh monitors", self.refresh, enabled=not self.busy)
        yield Item("Edit config…", self.open_config)
        yield Item("Open log", self.open_log)
        yield Item("Start with Windows", self.toggle_autostart, checked=lambda item: autostart_enabled())
        yield pystray.Menu.SEPARATOR
        yield Item("Quit", self.quit)


# --- Starting the tray -------------------------------------------------------------

TRAY_PACKAGES = ["pystray", "Pillow"]
TRAY_MODULES = {"pystray", "PIL", "six"}  # top-level modules those packages provide
RETRY_ENV = "MONITOR_CHANGER_AFTER_INSTALL"


def _message_box(text, flags=MB_ICONERROR):
    # Under pythonw there's no console, so problems have to be shown in a window.
    return _MessageBoxW(None, text, APP_NAME, flags)


def _python_flags():
    """Interpreter flags that change where packages are imported from.

    In isolated mode (-I) or with -s, the per-user site-packages folder is
    skipped, so a package installed there is missing for us even though plain
    pip reports it as installed. pip and the restart must use the same flags.
    """
    if sys.flags.isolated:
        return ["-I"]
    if sys.flags.no_user_site:
        return ["-s"]
    return []


def _missing_module(error):
    """Top-level name of the module that couldn't be found, if that's what went wrong."""
    # pystray re-raises a missing dependency as a generic "this platform is not
    # supported" ImportError, so the module name is only in the message.
    match = re.search(r"No module named '([\w.]+)'", str(error))
    return match.group(1).partition(".")[0] if match else None


def _offer_install(error):
    """Offer to pip install the tray's packages into this Python, then restart.

    Returns True if a new instance was started.
    """
    if _missing_module(error) not in TRAY_MODULES or os.environ.get(RETRY_ENV):
        # Not a missing package, or installing already didn't help: don't loop.
        _message_box(f"Monitor Changer couldn't start:\n\n{error!r}\n\nPython: {sys.executable} {' '.join(_python_flags())}")
        return False
    python = Path(sys.executable).with_name("python.exe")
    if not python.exists():
        python = Path(sys.executable)
    command = [str(python), *_python_flags(), "-m", "pip", "install", *TRAY_PACKAGES]
    answer = _message_box(
        f"{error}\n\nThe tray needs some Python packages that aren't installed. Install them now?\n\n"
        f"{subprocess.list2cmdline(command)}",
        MB_YESNO_QUESTION,
    )
    if answer != IDYES:
        return False
    if subprocess.run(command).returncode != 0:
        _message_box(f"Installing failed. Run this in a terminal to see why:\n\n{subprocess.list2cmdline(command)}")
        return False
    # A fresh process picks up the new packages without import cache surprises.
    # The marker stops it offering to install again if that didn't help.
    subprocess.Popen(
        [sys.executable, *_python_flags(), str(Path(__file__).resolve()), *sys.argv[1:]],
        env={**os.environ, RETRY_ENV: "1"},
    )
    return True


def _process_name(pid):
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(260)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value).name
    finally:
        _CloseHandle(handle)


def _hide_own_console():
    """Hide the console window if it only exists for us, e.g. after double-clicking
    the .py file. (Closing that window would also close the tray app.) A console
    shared with a shell means we were started from a terminal, so leave it alone.
    Returns True if the window was hidden.
    """
    hwnd = _GetConsoleWindow()
    if not hwnd:
        return False  # pythonw: no console
    pids = (wintypes.DWORD * 16)()
    count = _GetConsoleProcessList(pids, len(pids))
    if not 0 < count <= len(pids):
        return False
    for pid in pids[:count]:
        name = _process_name(pid)
        # Ours: python.exe, pythonw.exe, py.exe, and other Python launchers.
        if not name or not name.lower().startswith("py"):
            return False
    _ShowWindow(hwnd, SW_HIDE)
    return True


def setup_logging():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=1, encoding="utf-8")]
    if sys.stderr is not None:  # None under pythonw
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, handlers=handlers,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def run_tray():
    if TRAY_IMPORT_ERROR is not None:
        return 0 if _offer_install(TRAY_IMPORT_ERROR) else 1
    hidden = _hide_own_console()
    setup_logging()
    # Single instance: the mutex lives until the process exits.
    ctypes.set_last_error(0)
    _CreateMutexW(None, False, "Local\\MonitorChanger")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        log.info("Already running, exiting")
        return 0
    log.info("Starting %s%s", __version__, " (console hidden)" if hidden else "")
    App().run()
    return 0


# --- Command line -------------------------------------------------------------------

def cli_list():
    monitors = enumerate_monitors()
    try:
        for monitor in monitors:
            monitor.probe()
            primary = ", primary" if monitor.primary else ""
            print(f"{monitor.index}: {monitor.name}  ({monitor.device}{primary})")
            if not monitor.supported:
                print(f"     {monitor.error}")
                continue
            for code in sorted(monitor.inputs):
                current = "  <- current" if code == monitor.current_input else ""
                print(f"     0x{code:02X}  {input_name(code)}{current}")
            if monitor.power_off_value is not None:
                state = {True: "on", False: "off", None: "unknown"}[monitor.power_on]
                print(f"     power: {state} (off = 0x{monitor.power_off_value:02X})")
    finally:
        close_all(monitors)
    return 0


def _for_each_monitor(monitor_text, action):
    """Run ``action(monitor) -> message`` on the monitors matching *monitor_text*.

    *monitor_text* is a number from --list, part of a monitor's name, or "all".
    """
    monitors = enumerate_monitors()
    try:
        if monitor_text.casefold() == "all":
            targets = [m for m in monitors if m.handle is not None]
        else:
            targets = [
                m for m in monitors
                if monitor_text == str(m.index) or monitor_text.casefold() in m.name.casefold()
            ]
            if len(targets) > 1:
                print(f"error: more than one monitor matches {monitor_text!r}; see --list", file=sys.stderr)
                return 2
        if not targets:
            print(f"error: no monitor matches {monitor_text!r}; see --list", file=sys.stderr)
            return 2

        status = 0
        for monitor in targets:
            if monitor.handle is None:
                print(f"error: {monitor.name}: {monitor.error}", file=sys.stderr)
                status = 1
                continue
            try:
                print(f"{monitor.name}: {action(monitor)}")
            except OSError as exc:
                print(f"error: {monitor.name}: {exc}", file=sys.stderr)
                status = 1
        return status
    finally:
        close_all(monitors)


def cli_set_input(monitor_text, input_text):
    try:
        code = parse_input(input_text)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def switch(monitor):
        monitor.set_input(code)
        return f"switched to {input_name(code)}"

    return _for_each_monitor(monitor_text, switch)


def cli_set_power(monitor_text, state):
    state = state.casefold()
    if state not in ("on", "off"):
        print(f"error: power state must be 'on' or 'off', not {state!r}", file=sys.stderr)
        return 2
    config, _ = Config.load()

    def power(monitor):
        off_value = config.power_off_value(monitor)
        if state == "off" and off_value is None:
            monitor.probe()  # reads the capabilities to pick the right "off" value (slow)
        monitor.set_power(state == "on", off_value)
        return f"turned {state}"

    return _for_each_monitor(monitor_text, power)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="monitor_changer.py",
        description="Switch monitor inputs and power over DDC/CI. With no options, runs in the system tray. "
                    "MONITOR is a number from --list, part of a monitor's name, or 'all'.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="list monitors, their inputs and power state, then exit")
    group.add_argument(
        "--set", nargs=2, metavar=("MONITOR", "INPUT"),
        help="switch MONITOR to INPUT (e.g. hdmi1, dp2, usbc, 0x11), then exit",
    )
    group.add_argument(
        "--power", nargs=2, metavar=("MONITOR", "on|off"),
        help="turn MONITOR on or off, then exit",
    )
    group.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if args.list:
        return cli_list()
    if args.set:
        return cli_set_input(*args.set)
    if args.power:
        return cli_set_power(*args.power)
    return run_tray()


if __name__ == "__main__":
    sys.exit(main())
