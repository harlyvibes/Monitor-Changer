# Monitor Changer

A small Windows system tray app that finds your monitors, reads which inputs each one has, and lets you switch between them (DisplayPort, HDMI, USB-C, …) or turn monitors on and off from the tray. You no longer need the monitor's buttons and on-screen menu.

It talks to the monitors over **DDC/CI** using the Windows Monitor Configuration API (`dxva2.dll`), so no drivers or vendor software are needed.

```
 ┌──────────────────────────────────────────┐
 │ DELL U2720Q (1)        DisplayPort 1   ▸ │──┐ ● DisplayPort 1
 │ DELL U2720Q (2)        Off             ▸ │  │   HDMI 1
 │ Built-in  (no DDC/CI)                    │  │   USB-C
 ├──────────────────────────────────────────┤  │ ─────────────
 │ Power on all monitors                    │  │   Power on
 │ Power off all monitors                   │  └   Power off
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
- Turns monitors on and off, one at a time or all at once (see [Power on and off](#power-on-and-off)).
- The tooltip shows each monitor's current input, or "Off".
- Picks up input and power changes made with the monitor's own buttons by polling every 30 seconds (configurable).
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
     power: on (off = 0x04)
2: LG ULTRAGEAR  (\\.\DISPLAY2)
     0x0F  DisplayPort 1
     0x11  HDMI 1  <- current
     0x12  HDMI 2
     power: on (off = 0x05)
```

```bash
python -m monitor_changer --set 2 dp1
```

```bash
python -m monitor_changer --power all off
```

`MONITOR` is the monitor's number from `--list`, part of its name, or `all`.

- `--set MONITOR INPUT`: the input can be a name (`hdmi1`, `HDMI 2`, `dp1`, `displayport2`, `usbc`, `vga1`, `dvi1`) or a raw code (`0x11`, `17`).
- `--power MONITOR on|off`: turns monitors on or off. "Off" reads each monitor's capabilities first to pick the right value, which takes a second or two.

Use these to bind inputs or power to global hotkeys with AutoHotkey or PowerToys.

## Power on and off

**Power on** and **Power off** are in each monitor's menu. With two or more monitors there are also **Power on all monitors** and **Power off all monitors**. They use the monitor's DDC/CI power mode setting (VCP code `0xD6`).

Monitors can support several "off" modes. The app uses the first one the monitor lists, in this order:

| Code | Mode | Can be turned back on from the app? |
| --- | --- | --- |
| `0x04` | Off (DPM) | Usually |
| `0x03` | Suspend | Usually |
| `0x02` | Standby | Usually |
| `0x05` | Off, same as the power button | Rarely |

Turning **off** works on most monitors. Turning back **on** only works if the monitor keeps listening for DDC/CI while it's off, and many don't. If **Power on** fails, use the monitor's power button. You can test a monitor with `--power N off` and then `--power N on`.

If "off" only dims the monitor or does nothing, set `power_off` for it in the config (for example `"0x05"`).

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
      "hide": ["0x12"],
      "power_off": "0x04"
    }
  }
}
```

| Key | Meaning |
| --- | --- |
| `poll_seconds` | How often to re-read the current inputs and power state (minimum 5). Set it to `0` to turn polling off. |
| `hide_unsupported` | Leave monitors without DDC/CI, such as a laptop panel, out of the menu. |
| `monitors.<name>` | Settings for a monitor, keyed by the name shown in the menu (not case-sensitive). The settings apply to every monitor with that name. |
| `label` | The name to show in the menu instead of the monitor's own name. |
| `inputs` | Renames inputs. It also adds any listed input the monitor didn't report, which helps with monitors whose capabilities string is incomplete. |
| `hide` | Input codes to leave out of the menu. |
| `power_off` | The power mode value **Power off** sends (see [Power on and off](#power-on-and-off)). It also adds the power items for a monitor that doesn't advertise power control. |

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
- **Power on doesn't work, or the monitor shows "(no DDC/CI)" while it's off:** the monitor stops listening for DDC/CI when it's off. Turn it on with its power button. DisplayPort monitors that disconnect when off also disappear from Windows, so the app can't reach them until they're back on.
- **An input is missing or has a strange name:** some monitors report incomplete or non-standard capabilities. Add the input under `inputs` in the config. Some LG models use non-standard codes, and those may not work.
- **Something else:** choose **Open log** in the tray menu. The log is at `%APPDATA%\MonitorChanger\monitor_changer.log`.

## Development

```bash
python -m unittest discover -s tests -v
```

| File | What it does |
| --- | --- |
| `monitor_changer/ddc.py` | Monitor enumeration and DDC/CI through `ctypes` and `dxva2.dll` |
| `monitor_changer/mccs.py` | Capabilities-string parser, input names and power modes. Pure Python, unit tested. |
| `monitor_changer/config.py` | The JSON config |
| `monitor_changer/tray.py` | The tray app, built on [pystray](https://github.com/moses-palmer/pystray) |
| `monitor_changer/autostart.py` | The "Start with Windows" registry entry |

## License

MIT
