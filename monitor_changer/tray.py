"""The system tray app."""

import ctypes
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

import pystray
from PIL import Image, ImageDraw

from . import autostart, ddc, mccs
from .config import CONFIG_PATH, LOG_PATH, Config, write_template

log = logging.getLogger(__name__)

APP_NAME = "Monitor Changer"

WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_APP_REFRESH_MENU = 0x8000 + 1  # WM_APP + 1
ERROR_ALREADY_EXISTS = 183

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_PostMessageW = _user32.PostMessageW
_PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_PostMessageW.restype = wintypes.BOOL

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_CreateMutexW = _kernel32.CreateMutexW
_CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
_CreateMutexW.restype = wintypes.HANDLE


class TrayIcon(pystray.Icon):
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
            ddc.close_all(self.monitors)
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
            autostart.set_enabled(not autostart.is_enabled())
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
            monitors = ddc.enumerate_monitors()
            for monitor in monitors:
                monitor.probe()
        finally:
            old, self.monitors = self.monitors, monitors
            ddc.close_all(old)
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
        return dict(self.config.inputs_for(monitor)).get(code) or mccs.input_name(code)

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
        yield Item("Start with Windows", self.toggle_autostart, checked=lambda item: autostart.is_enabled())
        yield pystray.Menu.SEPARATOR
        yield Item("Quit", self.quit)


def _open_file(path):
    try:
        os.startfile(path)
    except OSError:
        # No program associated with the extension.
        subprocess.Popen(["notepad.exe", str(path)])


def setup_logging():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=1, encoding="utf-8")]
    if sys.stderr is not None:  # None under pythonw
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, handlers=handlers,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main():
    setup_logging()
    # Single instance: the mutex lives until the process exits.
    ctypes.set_last_error(0)
    _CreateMutexW(None, False, "Local\\MonitorChanger")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        log.info("Already running, exiting")
        return
    log.info("Starting")
    App().run()
