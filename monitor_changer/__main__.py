"""Entry point: ``python -m monitor_changer [--list | --set MONITOR INPUT]``."""

import argparse
import sys

from . import ddc, mccs


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
    finally:
        ddc.close_all(monitors)
    return 0


def set_input(monitor_text, input_text):
    try:
        code = mccs.parse_input(input_text)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    monitors = ddc.enumerate_monitors()
    try:
        matches = [
            m for m in monitors
            if monitor_text == str(m.index) or monitor_text.casefold() in m.name.casefold()
        ]
        if len(matches) != 1:
            problem = "no monitor matches" if not matches else "more than one monitor matches"
            print(f"error: {problem} {monitor_text!r}; see --list", file=sys.stderr)
            return 2
        monitor = matches[0]
        if monitor.handle is None:
            print(f"error: {monitor.name}: {monitor.error}", file=sys.stderr)
            return 1
        try:
            monitor.set_input(code)
        except OSError as exc:
            print(f"error: {monitor.name}: {exc}", file=sys.stderr)
            return 1
        print(f"{monitor.name}: switched to {mccs.input_name(code)}")
        return 0
    finally:
        ddc.close_all(monitors)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="monitor_changer",
        description="Switch monitor inputs over DDC/CI. With no options, runs in the system tray.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="list monitors and their inputs, then exit")
    group.add_argument(
        "--set", nargs=2, metavar=("MONITOR", "INPUT"),
        help="switch MONITOR (number from --list, or part of its name) to INPUT "
             "(e.g. hdmi1, dp2, usbc, 0x11), then exit",
    )
    args = parser.parse_args(argv)

    if args.list:
        return list_monitors()
    if args.set:
        return set_input(*args.set)

    from .tray import main as run_tray
    run_tray()
    return 0


if __name__ == "__main__":
    sys.exit(main())
