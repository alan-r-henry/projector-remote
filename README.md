# Projector Remote

A Wi-Fi remote for the **XElectron C9A** Android projector, used from your phone's browser.

The C9A's original remote broke, and without it there was no way to work the focus motor. This project drives the projector over ADB instead. A small Python server runs on a computer, and any phone on the same Wi-Fi opens its page and gets a full remote:

- **Focus.** Step the lens ◀ / ▶, run auto focus, and drag a slider to send the lens to a position. A bar shows where the lens is.
- **Park & off.** Moves the lens to its start position, then powers off, so focus stays accurate next time ([why](#why-park--off)).
- **4-point keystone.** Drag the corners of the picture outline and the picture on the wall follows live.
- **Sources.** One tap to HDMI, or open the source list.
- **Bluetooth speaker.** One tap connects the projector to a paired speaker.
- **Basics.** Power, Home, Back, Menu, D-pad, OK, volume and mute.

It needs only Python's standard library and `adb`. Nothing is installed on the projector.

> Unofficial: not affiliated with XElectron. Made for and tested on one C9A (HiSilicon Hi3751V350, Android 12). Other projectors that use the same firmware may work. On other models, expect only the basic Android keys to work.

## What you need

- **The projector** on your Wi-Fi with network ADB on port 5555. On the C9A this was on out of the box. To check, run `adb connect <projector-ip>:5555` from a computer.
- **A computer** on the same network (Windows, macOS or Linux) with:
  - [Python 3.9+](https://www.python.org/downloads/)
  - `adb` from Google's [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools)
- **A phone** (or any browser) on the same network.

## Quick start

```bash
git clone https://github.com/alan-r-henry/projector-remote.git
cd projector-remote
python remote.py --projector 192.168.1.20
```

On Windows you can double-click `remote.cmd` instead.

The remote prints an address such as `http://192.168.1.8:8765`. Open it on your phone and add it to your home screen. The computer has to stay on while you use the remote.

**Windows firewall:** the first time, Windows asks whether Python may use the network. Allow it on **Private** networks. If the phone can't load the page, set your Wi-Fi to *Private* in Windows Settings → Network.

### Options

| Option | Default | |
|---|---|---|
| `--projector IP` | `192.168.1.20` | Projector address. Find it on the projector under Settings → Network. |
| `--port N` | `8765` | Port for the web page. |
| `--adb PATH` | see below | Path to `adb`. |
| `--bt-speaker NAME` | `MHC-V13` | Name of your paired Bluetooth speaker, exactly as the projector lists it. |
| `--no-scan` | off | Don't search the network if the projector isn't at `--projector`. |

`adb` is looked for in a `platform-tools` folder next to this project's folder, and then on PATH.

## Changing networks

The projector, the computer and the phone must all be on the **same network**. This doesn't work over the internet or between different networks, and that is on purpose: ADB gives full control of the projector. Guest Wi-Fi often stops devices from seeing each other, so it usually won't work there either.

If the projector isn't at the address you gave, the remote looks for it on its own. It searches the computer's network for a device with port 5555 open and the C9A's focus motor, and checks again every 30 s while it can't connect. After a move to new Wi-Fi it should just find the projector. If it doesn't, pass the new address with `--projector`.

## Focus

The lens has 148 motor positions, and each ◀ / ▶ press moves it 5. Hold a button to keep stepping. To jump, drag the slider (or tap the bar) and let go. The remote then steps the lens there. The green fill shows where the lens is now, and the thumb shows where it's heading. Each button is greyed out at its end of travel.

### Why Park & off

The projector counts the lens position with a counter that **resets to 0 at every power-on, while the lens itself stays where it was**. After that, the counter and the lens are out of step:

- ▶ grinds against the end stop.
- ◀ is refused, because the counter thinks it's already at 0.
- Auto focus lands blurry.

**Park & off** drives the lens to 0 before powering off. The counter's 0 at the next power-on then matches where the lens really is. If you've already got the grinding, run auto focus once to bring the counter back in step, then use Park & off from then on.

The projector's own *Auto Focus* switch, under Projection settings, refocuses by itself at times. If it keeps blurring a picture you've focused by hand, turn it off there.

## Keystone

Drag any of the four corner handles. The arrow buttons nudge the selected corner one step, **Undo changes** restores the corners from when the page opened, and **Reset to square** clears all four.

The remote writes the same settings the projector's own 4-point screen writes, so changes stay after a restart and show up in the projector's menu too.

## Bluetooth speaker

Android 12's shell has no command to connect a Bluetooth device. **Connect** therefore does what you would do by hand on the projector: it opens Settings → Remotes & accessories, taps your speaker's name, then taps **Connect**. It only ever taps those two labels, never *Unpair* or *Rename*, and backs out of the settings screens afterwards.

- Pair the speaker once yourself, under **Bluetooth settings**.
- Pass its name with `--bt-speaker`.
- The projector's interface must be in English, because the remote looks for the word "Connect".

## Screen mirroring (optional, Windows)

[scrcpy](https://github.com/Genymobile/scrcpy) shows the projector's screen on the computer and lets you control it with mouse and keyboard. There's a catch on this firmware: it deletes anything written to `/data/local/tmp` within a second, so scrcpy's helper never gets to run. `adb-sdcard-shim` is a tiny `adb` stand-in that sends the helper to `/sdcard` instead.

Build it once with the C# compiler that ships with Windows:

```bash
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe -nologo -out:adb-sdcard-shim\adb.exe adb-sdcard-shim\AdbShim.cs
```

Then run `mirror.cmd [projector-ip]`. It finds `adb` and `scrcpy` on PATH, or in `platform-tools` and `scrcpy-win64-*` folders next to this project's folder.

## How it works

Every button sends a shell command over a single ADB connection that stays open. Everything below was found on the C9A itself.

| Function | Command |
|---|---|
| Focus ◀ / ▶ | `input keyevent 372` / `373` (`ELECTRIC_FOCUS_L` / `_R`, 5 motor steps) |
| Auto focus | `input keyevent 375` (`AUTO_FOCUS`), or `am broadcast -a zvt.intent.action.ONCE_AUTO_FOCUS -n com.zvt.zfocus/.receiver.FactoryReceiver` |
| Lens position (read only) | `cat /sys/class/motor/index` (0–148) |
| HDMI | `input keyevent TV_INPUT_HDMI_1` |
| Source list | `am start -a com.hisilicon.tvsetting.action.quicksource` |
| Projection settings | `am start -n com.chijimlee.tyylauncher/.activity.ProjectModeActivity` |
| Bluetooth screen | `am start -n com.android.tv.settings/.accessories.AccessoriesActivity` |
| Keystone corner `c` (`lt` `rt` `lb` `rb`) | `setprop persist.zvt.<c> x,y` (steps 0–200), plus `persist.sys.keystone.<c>` and `persist.hisi.keystone.<c>` set to the corner's pixel position. Then set `persist.sys.keystone.update` to `false` and back to `true` to apply. |

The remote only reads the motor's settings and never writes them. It moves the lens with the same key presses as the original remote.

## Security

The page has no password. **Anyone on your network can open it and control the projector.** The projector's open ADB port is just as reachable without this remote, so use it only on a network you trust.

## License

[MIT](LICENSE)
