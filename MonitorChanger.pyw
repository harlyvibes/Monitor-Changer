# Double-click to start Monitor Changer in the system tray without a console window.
import ctypes
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Python doesn't always put the script's folder on the import path (isolated
# or safe-path mode, some launchers), so add it explicitly.
sys.path.insert(0, str(HERE))

MB_ICONERROR, MB_YESNO_QUESTION, IDYES = 0x10, 0x24, 6


def message_box(text, flags):
    return ctypes.windll.user32.MessageBoxW(None, text, "Monitor Changer", flags)


def install_requirements(error):
    """Offer to pip install the requirements into this Python, then restart."""
    # pythonw has no console, so ask with a message box rather than failing silently.
    python = Path(sys.executable).with_name("python.exe")
    if not python.exists():
        python = Path(sys.executable)
    command = subprocess.list2cmdline([str(python), "-m", "pip", "install", "-r", str(HERE / "requirements.txt")])
    answer = message_box(
        f"{error}\n\nMonitor Changer needs some Python packages that aren't installed. Install them now?\n\n{command}",
        MB_YESNO_QUESTION,
    )
    if answer != IDYES:
        return False
    if subprocess.run(command).returncode != 0:
        message_box(f"Installing failed. Run this in a terminal to see why:\n\n{command}", MB_ICONERROR)
        return False
    # A fresh process picks up the new packages without import cache surprises.
    subprocess.Popen([sys.executable, str(Path(__file__).resolve())])
    return True


try:
    from monitor_changer.tray import main
except ImportError as exc:
    raise SystemExit(0 if install_requirements(exc) else 1)

main()
