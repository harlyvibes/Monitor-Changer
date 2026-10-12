# Monitor Changer

A small Windows system tray app that finds your monitors, reads which inputs each one has, and lets you switch between them (DisplayPort, HDMI, USB-C, …) from the tray. You no longer need the monitor's buttons and on-screen menu.

It talks to the monitors over **DDC/CI** using the Windows Monitor Configuration API (`dxva2.dll`), so no drivers or vendor software are needed.

```
 ┌──────────────────────────────────────────┐
 │ DELL U2720Q (1)        DisplayPort 1   ▸ │──┐ ● DisplayPort 1
 │ DELL U2720Q (2)        HDMI 1          ▸ │  │   HDMI 1
 │ Built-in  (no DDC/CI)                    │  └   USB-C
 ├──────────────────────────────────────────┤
 │ Refresh monitors                         │
 │ Edit config…                             │
 │ Open log                                 │
 │ ✓ Start with Windows                     │
 ├──────────────────────────────────────────┤
 │ Quit                                     │
 └──────────────────────────────────────────┘
```

## Features

- Lives in the system tray. Left or right click opens the menu.
- Detects every connected monitor and the inputs it reports, and marks the current one.
- One click switches input. With a single monitor, its inputs are listed directly in the menu.
- The tooltip shows each monitor's current input.
- Picks up input changes made with the monitor's own buttons by polling every 30 seconds (configurable).
- Lets you rename inputs ("Work laptop", "Gaming PC"), hide inputs you don't use, or add inputs a monitor doesn't report.
- Includes a "Start with Windows" toggle.
- Has a command line mode for scripts and hotkey tools such as AutoHotkey.

## Requirements

- Windows 10 or 11
- Python 3.10+
- Monitors with **DDC/CI enabled**. This is usually a setting in the monitor's on-screen menu, often under "Other" or "System". Most external monitors support it, but laptop panels and most TVs don't.

## Install and run

```bash
git clone https://github.com/harlyvibes/Monitor-Changer.git
```

```bash
cd Monitor-Changer
```

```bash
pip install -r requirements.txt
```

Then double-click **`MonitorChanger.pyw`** to start it without a console window. You can also run:

```bash
python -m monitor_changer
```

To start it automatically at login, use **Start with Windows** in the tray menu. This adds an entry under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.

You can also install it as a package. This gives you a `monitor-changer` command that starts the tray app:

```bash
pip install .
```

## Command line

```bash
python -m monitor_changer --list
```

```
1: DELL U2720Q  (\\.\DISPLAY1, primary)
     0x0F  DisplayPort 1  <- current
     0x11  HDMI 1
     0x1B  USB-C
2: LG ULTRAGEAR  (\\.\DISPLAY2)
     0x0F  DisplayPort 1
     0x11  HDMI 1  <- current
     0x12  HDMI 2
```

```bash
python -m monitor_changer --set 2 dp1
```

`--set MONITOR INPUT` takes the monitor's number from `--list` or part of its name. The input can be a name (`hdmi1`, `HDMI 2`, `dp1`, `displayport2`, `usbc`, `vga1`, `dvi1`) or a raw code (`0x11`, `17`). Use it to bind inputs to global hotkeys with AutoHotkey or PowerToys.

## Configuration

Choose **Edit config…** in the tray menu. The first time, this creates `%APPDATA%\MonitorChanger\config.json` pre-filled with your detected monitors and their inputs. The app reloads the file automatically when you save it.

```json
{
  "poll_seconds": 30,
  "hide_unsupported": false,
  "monitors": {
    "DELL U2720Q": {
      "label": "Left monitor",
      "inputs": {
        "0x0F": "Desktop",
        "0x1B": "Work laptop",
        "0x11": "PS5"
      },
      "hide": ["0x12"]
    }
  }
}
```

| Key | Meaning |
| --- | --- |
| `poll_seconds` | How often to re-read the current inputs (minimum 5). Set it to `0` to turn polling off. |
| `hide_unsupported` | Leave monitors without DDC/CI, such as a laptop panel, out of the menu. |
| `monitors.<name>` | Settings for a monitor, keyed by the name shown in the menu (not case-sensitive). The settings apply to every monitor with that name. |
| `label` | The name to show in the menu instead of the monitor's own name. |
| `inputs` | Renames inputs. It also adds any listed input the monitor didn't report, which helps with monitors whose capabilities string is incomplete. |
| `hide` | Input codes to leave out of the menu. |

Input codes use the MCCS standard. The table below lists them, and `--list` shows what each monitor reports.

| Code | Input | Code | Input |
| --- | --- | --- | --- |
| `0x01` | VGA 1 | `0x0F` | DisplayPort 1 |
| `0x03` | DVI 1 | `0x10` | DisplayPort 2 |
| `0x04` | DVI 2 | `0x11` | HDMI 1 |
| `0x0C` | Component 1 | `0x12` | HDMI 2 |
| | | `0x1B` | USB-C (vendor extension) |

## Troubleshooting

- **"(no DDC/CI)" next to a monitor:** turn on DDC/CI in the monitor's on-screen menu, then choose **Refresh monitors**. Some docks, KVM switches and adapters don't pass DDC/CI through, so try a direct cable. Laptop panels, virtual machines and remote desktop sessions don't support DDC/CI.
- **The monitor stops responding after switching away:** some monitors only answer DDC/CI on the active input, so a PC that isn't being shown can't switch it back. Switch back from the device that is being shown, or use the monitor's buttons.
- **An input is missing or has a strange name:** some monitors report incomplete or non-standard capabilities. Add the input under `inputs` in the config. Some LG models use non-standard codes, and those may not work.
- **Something else:** choose **Open log** in the tray menu. The log is at `%APPDATA%\MonitorChanger\monitor_changer.log`.

## Development

```bash
python -m unittest discover -s tests -v
```

| File | What it does |
| --- | --- |
| `monitor_changer/ddc.py` | Monitor enumeration and DDC/CI through `ctypes` and `dxva2.dll` |
| `monitor_changer/mccs.py` | Capabilities-string parser and input names. Pure Python, unit tested. |
| `monitor_changer/config.py` | The JSON config |
| `monitor_changer/tray.py` | The tray app, built on [pystray](https://github.com/moses-palmer/pystray) |
| `monitor_changer/autostart.py` | The "Start with Windows" registry entry |

## License

MIT
