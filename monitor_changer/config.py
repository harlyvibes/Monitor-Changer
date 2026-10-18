"""User settings stored in %APPDATA%\\MonitorChanger\\config.json."""

import json
import logging
import os
from pathlib import Path

from . import mccs

log = logging.getLogger(__name__)

APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "MonitorChanger"
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "monitor_changer.log"

DEFAULTS = {
    # How often to re-read each monitor's current input so the menu stays
    # correct when it's changed with the monitor's own buttons. 0 disables.
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
    """Parse an input code written as an int, "0x0F" or "15". Returns None if invalid."""
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
        self.data = {**DEFAULTS, **(data or {})}

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
            return DEFAULTS["poll_seconds"]

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
        return [(c, renamed.get(c) or mccs.input_name(c)) for c in codes if c not in hidden]


def write_template(monitors, path=CONFIG_PATH):
    """Create a config file pre-filled with the detected monitors so it's easy to edit."""
    data = json.loads(json.dumps(DEFAULTS))
    for monitor in monitors:
        if monitor.supported:
            data["monitors"][monitor.name] = {
                "label": "",
                "inputs": {f"0x{c:02X}": mccs.input_name(c) for c in sorted(monitor.inputs)},
                "hide": [],
            }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
