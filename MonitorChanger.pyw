# Double-click to start Monitor Changer in the system tray without a console window.
import sys
from pathlib import Path

# Python doesn't always put the script's folder on the import path (isolated
# or safe-path mode, some launchers), so add it explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from monitor_changer.tray import main
except ImportError as exc:
    # pythonw has no console, so show the error rather than silently exiting.
    import ctypes

    python = Path(sys.executable).with_name("python.exe")
    ctypes.windll.user32.MessageBoxW(
        None,
        f"{exc}\n\nInstall the requirements with:\n\"{python}\" -m pip install -r requirements.txt",
        "Monitor Changer",
        0x10,  # MB_ICONERROR
    )
    raise SystemExit(1)

main()
