# Double-click to start Monitor Changer in the system tray without a console window.
import ctypes
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIREMENTS = {"pystray", "PIL", "six"}  # top-level modules installed by requirements.txt
RETRY_ENV = "MONITOR_CHANGER_AFTER_INSTALL"

# Python doesn't always put the script's folder on the import path (isolated
# or safe-path mode, some launchers), so add it explicitly.
sys.path.insert(0, str(HERE))

MB_ICONERROR, MB_YESNO_QUESTION, IDYES = 0x10, 0x24, 6


def message_box(text, flags=MB_ICONERROR):
    # pythonw has no console, so errors have to be shown in a window.
    return ctypes.windll.user32.MessageBoxW(None, text, "Monitor Changer", flags)


def python_flags():
    """Interpreter flags that change where packages are imported from.

    In isolated mode (-I) or with -s, the per-user site-packages folder is
    skipped, so a package installed there is missing for the app even though
    plain pip reports it as installed. pip and the relaunch must use the same
    flags to see what the app sees.
    """
    if sys.flags.isolated:
        return ["-I"]
    if sys.flags.no_user_site:
        return ["-s"]
    return []


def missing_module(error):
    """Top-level name of the module that couldn't be found, if that's what went wrong."""
    # pystray re-raises a missing dependency as a generic "this platform is not
    # supported" ImportError, so the module name is only in the message.
    match = re.search(r"No module named '([\w.]+)'", str(error))
    return match.group(1).partition(".")[0] if match else None


def install_requirements(error):
    """Offer to pip install the requirements into this Python, then restart."""
    python = Path(sys.executable).with_name("python.exe")
    if not python.exists():
        python = Path(sys.executable)
    command = subprocess.list2cmdline(
        [str(python), *python_flags(), "-m", "pip", "install", "-r", str(HERE / "requirements.txt")]
    )
    answer = message_box(
        f"{error}\n\nMonitor Changer needs some Python packages that aren't installed. Install them now?\n\n{command}",
        MB_YESNO_QUESTION,
    )
    if answer != IDYES:
        return False
    if subprocess.run(command).returncode != 0:
        message_box(f"Installing failed. Run this in a terminal to see why:\n\n{command}")
        return False
    # A fresh process picks up the new packages without import cache surprises.
    # The marker stops it offering to install again if that didn't help.
    subprocess.Popen(
        [sys.executable, *python_flags(), str(Path(__file__).resolve())],
        env={**os.environ, RETRY_ENV: "1"},
    )
    return True


try:
    from monitor_changer.tray import main
except ImportError as exc:
    missing = missing_module(exc)
    if missing == "monitor_changer":
        message_box(
            f"{exc}\n\nThe monitor_changer folder must be next to MonitorChanger.pyw:\n{HERE / 'monitor_changer'}\n\n"
            "Download or clone the whole repository, not just this file."
        )
        raise SystemExit(1)
    if missing in REQUIREMENTS and not os.environ.get(RETRY_ENV):
        raise SystemExit(0 if install_requirements(exc) else 1)
    message_box(f"Monitor Changer couldn't start:\n\n{exc!r}\n\nPython: {sys.executable} {' '.join(python_flags())}")
    raise SystemExit(1)

main()
