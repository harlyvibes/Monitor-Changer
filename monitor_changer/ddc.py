"""Windows DDC/CI access through the Monitor Configuration API (dxva2.dll).

Every call that talks to a monitor goes over the I2C bus in the video cable and
can take tens of milliseconds (capabilities strings take seconds), so callers
should keep these off the UI thread and serialize them.
"""

import ctypes
import logging
import time
from ctypes import wintypes
from dataclasses import dataclass, field

from . import mccs

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)

UINT32 = ctypes.c_uint32
MONITORINFOF_PRIMARY = 0x1
QDC_ONLY_ACTIVE_PATHS = 0x2
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
ERROR_SUCCESS = 0
ERROR_INSUFFICIENT_BUFFER = 122


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
    error: str | None = None

    @property
    def supported(self):
        return self.handle is not None and self.error is None

    def probe(self):
        """Query DDC/CI for the current input and supported inputs.

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

        parsed = mccs.parse_capabilities(self.capabilities)
        if parsed.get("model") and (not self.name or self.name.startswith("Generic ")):
            self.name = parsed["model"]
        self.inputs = list(dict.fromkeys(parsed.get("vcp", {}).get(mccs.VCP_INPUT_SOURCE, [])))
        if not self.inputs:
            log.info("%s: capabilities don't list inputs, using common defaults", self.name)
            self.inputs = list(mccs.FALLBACK_INPUTS)
        if self.current_input is not None and self.current_input not in self.inputs:
            self.inputs.append(self.current_input)
        log.info(
            "%s: inputs=%s current=%s",
            self.name,
            [mccs.input_name(c) for c in self.inputs],
            mccs.input_name(self.current_input) if self.current_input is not None else None,
        )

    def read_input(self):
        current, _ = _retry(lambda: get_vcp(self.handle, mccs.VCP_INPUT_SOURCE))
        # Some monitors put junk in the high byte; the input value is the low byte.
        return current & 0xFF

    def set_input(self, code):
        _retry(lambda: set_vcp(self.handle, mccs.VCP_INPUT_SOURCE, code))
        self.current_input = code

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
