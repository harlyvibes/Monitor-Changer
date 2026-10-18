"""Entry point: ``python -m monitor_changer [--list | --set MONITOR INPUT | --power MONITOR on|off]``."""

import argparse
import sys

from . import ddc, mccs
from .config import Config


def list_monitors():
    monitors = ddc.enumerate_monitors()
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
                print(f"     0x{code:02X}  {mccs.input_name(code)}{current}")
            if monitor.power_off_value is not None:
                state = {True: "on", False: "off", None: "unknown"}[monitor.power_on]
                print(f"     power: {state} (off = 0x{monitor.power_off_value:02X})")
    finally:
        ddc.close_all(monitors)
    return 0


def for_each_monitor(monitor_text, action):
    """Run ``action(monitor) -> message`` on the monitors matching *monitor_text*.

    *monitor_text* is a number from --list, part of a monitor's name, or "all".
    """
    monitors = ddc.enumerate_monitors()
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
        ddc.close_all(monitors)


def set_input(monitor_text, input_text):
    try:
        code = mccs.parse_input(input_text)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def switch(monitor):
        monitor.set_input(code)
        return f"switched to {mccs.input_name(code)}"

    return for_each_monitor(monitor_text, switch)


def set_power(monitor_text, state):
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

    return for_each_monitor(monitor_text, power)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="monitor_changer",
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
    args = parser.parse_args(argv)

    if args.list:
        return list_monitors()
    if args.set:
        return set_input(*args.set)
    if args.power:
        return set_power(*args.power)

    from .tray import main as run_tray
    run_tray()
    return 0


if __name__ == "__main__":
    sys.exit(main())
