"""Wi-Fi remote for the XElectron C9A projector (HiSilicon Hi3751V350, Android 12).

Serves a phone-friendly web page and sends each button press to the projector
over ADB (port 5555). Python 3.9+ standard library only, plus adb.

    python remote.py                      # projector at 192.168.1.20, page on port 8765
    python remote.py --projector 192.168.1.42 --port 9000

Then open the printed http://<laptop-ip>:<port> address on your phone. If the
projector isn't at the given address, the remote searches the computer's network for it.
The page can only send what is listed in COMMANDS, plus Park & off, Connect speaker
and 4-point keystone values (clamped to the projector's own 0-200 range).
"""
import argparse
import concurrent.futures
import html
import ipaddress
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def key(code):
    return f"input keyevent {code}"


# All verified on an XElectron C9A (see README, "How it works").
COMMANDS = {
    "power": key(26),
    "home": key(3),
    "back": key(4),
    "menu": key(82),
    "up": key(19),
    "down": key(20),
    "left": key(21),
    "right": key(22),
    "ok": key(23),
    "vol_up": key(24),
    "vol_down": key(25),
    "mute": key(164),
    "focus_l": key(372),     # ELECTRIC_FOCUS_L, 5 motor steps per press
    "focus_r": key(373),     # ELECTRIC_FOCUS_R, 5 motor steps per press
    "auto_focus": key(375),  # AUTO_FOCUS, full sweep (~15 s)
    "auto_focus_direct": "am broadcast -a zvt.intent.action.ONCE_AUTO_FOCUS"
                         " -n com.zvt.zfocus/.receiver.FactoryReceiver",
    "keystone": "am start -n com.chijimlee.tyylauncher/.activity.ProjectModeActivity",
    "input": "am start -a com.hisilicon.tvsetting.action.quicksource",  # source list (HDMI, USB)
    "hdmi": key("TV_INPUT_HDMI_1"),  # straight to HDMI 1; Home goes back to Android
    "bt_settings": "am start -n com.android.tv.settings/.accessories.AccessoriesActivity",
}

DEFAULT_BT_SPEAKER = "MHC-V13"



def find_adb():
    """adb from a platform-tools folder next to this project's folder, else from PATH."""
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("adb.exe", "adb"):
        path = os.path.join(here, "..", "platform-tools", name)
        if os.path.isfile(path):
            return os.path.normpath(path)
    return shutil.which("adb")

# The lens range is 148 motor steps and each focus press moves 5.
LENS_MAX = 148
MAX_PARK_PRESSES = LENS_MAX // 5 + 3

# 4-point keystone, written the way the projector's own 4-point screen does it
# (TyyLauncher LRBTUtil): each corner is an inward offset of 0-200 steps, one step
# being 1920/800 px across and 1080/800 px down. Flipping the update flag
# false -> true makes /vendor/bin/hipluginserver apply it at once.
CORNERS = ("lt", "rt", "lb", "rb")
KEYSTONE_MAX = 200
PANEL_W, PANEL_H = 1920, 1080


def corner_pixels(corner, x, y):
    """Panel position (origin bottom-left) of a corner pulled in by (x, y) steps."""
    dx, dy = round(x * PANEL_W / 800), round(y * PANEL_H / 800)
    px = dx if corner[0] == "l" else PANEL_W - dx
    py = PANEL_H - dy if corner[1] == "t" else dy
    return px, py


class Projector:
    """Keeps one `adb shell` open and runs commands through it one at a time."""

    def __init__(self, adb, host, scan, bt_speaker=DEFAULT_BT_SPEAKER):
        self.adb, self.host, self.scan, self.bt_speaker = adb, host, scan, bt_speaker
        self.lock = threading.RLock()
        self.proc = None
        self.lines = None
        self.seq = 0
        self.model = None
        self.last_scan = -1e9
        self.lens_lock = threading.Lock()
        self.lens_target = None  # set by the slider; anything else that moves the lens clears it

    @property
    def serial(self):
        return f"{self.host}:5555"

    def _adb(self, *args, timeout=8):
        return subprocess.run([self.adb, *args], capture_output=True, text=True, timeout=timeout)

    def _try_connect(self, host):
        try:
            out = self._adb("connect", f"{host}:5555").stdout.strip()
            if not out.startswith(("connected", "already connected")):
                return False
            state = self._adb("-s", f"{host}:5555", "get-state").stdout.strip()
        except subprocess.TimeoutExpired:  # nothing answering there (e.g. off, or other Wi-Fi)
            return False
        return state == "device"

    def _is_projector(self, host):
        """True if the adb device at host has this projector's focus motor."""
        out = self._adb("-s", f"{host}:5555", "shell", "test -e /sys/class/motor/index && echo yes").stdout
        return out.strip() == "yes"

    def _connect(self):
        if self._try_connect(self.host):
            return
        # Search this computer's own network (and the old address's), at most every 30 s,
        # so the remote still finds the projector after a move to another Wi-Fi.
        if self.scan and time.monotonic() - self.last_scan > 30:
            self.last_scan = time.monotonic()
            near = [self.host]
            try:
                near.insert(0, lan_ip(self.host))
            except OSError:
                pass
            for host in scan_for_adb(*near):
                if host == self.host or not self._try_connect(host):
                    continue
                if not self._is_projector(host):  # some other Android device with adb open
                    self._adb("disconnect", f"{host}:5555")
                    continue
                print(f"Projector found at new address {host}")
                self.host = host
                return
        raise ConnectionError(f"Projector not reachable on {self.host}:5555")

    def _open_shell(self):
        self._close_shell()
        self._connect()
        # Binary pipes: text mode on Windows would send \r\n, which Android's sh rejects.
        self.proc = subprocess.Popen(
            [self.adb, "-s", self.serial, "shell"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc, self.lines), daemon=True).start()

    @staticmethod
    def _pump(proc, lines):
        for raw in proc.stdout:
            lines.put(raw.decode("utf-8", "replace").rstrip("\r\n"))
        lines.put(None)

    def _close_shell(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None

    def run(self, cmd, timeout=12):
        """Run one shell command; return (exit_code, output)."""
        with self.lock:
            for attempt in (1, 2):
                try:
                    if not self.proc or self.proc.poll() is not None:
                        self._open_shell()
                    self.seq += 1
                    marker = f"__done_{self.seq}__"
                    self.proc.stdin.write(f"{cmd} 2>&1; echo {marker} $?\n".encode())
                    self.proc.stdin.flush()
                    output, deadline = [], time.monotonic() + timeout
                    while True:
                        line = self.lines.get(timeout=max(0.1, deadline - time.monotonic()))
                        if line is None:
                            raise ConnectionError("adb shell closed")
                        if line.startswith(marker):
                            return int(line.split()[-1]), "\n".join(output)
                        output.append(line)
                except (queue.Empty, OSError, ConnectionError, subprocess.TimeoutExpired) as e:
                    self._close_shell()
                    if attempt == 2:
                        raise ConnectionError(str(e) or type(e).__name__) from e

    def motor_index(self):
        return int(self.run("cat /sys/class/motor/index", timeout=5)[1].strip())

    def wait_for_move(self, before, timeout=2.5):
        """Poll the lens counter until it changes and then holds still."""
        idx, changed_at = before, None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.15)
            now = self.motor_index()
            if now != idx:
                idx, changed_at = now, time.monotonic()
            elif changed_at and time.monotonic() - changed_at > 0.4:
                break
        return idx

    def lens_to(self, target):
        """Tap Focus ◀/▶ until the lens counter is within half a press of target.

        A newer target (or a focus button press) takes over: this one then returns
        early with whatever position the lens had reached.
        """
        target = max(0, min(LENS_MAX, target))
        self.lens_target = target
        with self.lens_lock:
            idx = self.motor_index()
            for _ in range(2 * MAX_PARK_PRESSES):
                if self.lens_target != target or abs(target - idx) < 3:
                    break
                self.run(COMMANDS["focus_r" if target > idx else "focus_l"])
                new = self.wait_for_move(idx, timeout=1.2)
                if new == idx:  # refused: at an end, or counter out of step
                    return {"ok": False, "lens": idx, "error": f"Lens stopped at {idx}, didn't reach {target}."}
                idx = new
            return {"ok": True, "lens": idx}

    def park_and_power_off(self):
        """Tap Focus ◀ until the lens counter reads 0, then press Power.

        The projector resets its lens counter to 0 at every boot without moving the
        lens, so parking at the 0 end first keeps counter and lens in step next time.
        Powers off only if the counter really reached 0.
        """
        start = idx = self.motor_index()
        for _ in range(MAX_PARK_PRESSES):
            if idx == 0:
                break
            self.run(COMMANDS["focus_l"])
            new = self.wait_for_move(idx)
            if new >= idx:  # refused, or moved the wrong way: don't guess further
                break
            idx = new
        if idx != 0:
            return {"ok": False, "error": f"Lens stopped at {idx} (started at {start}), not 0. "
                                          "Projector left on."}
        self.run(COMMANDS["power"])
        return {"ok": True, "message": f"Lens parked ({start} → 0). Powering off."}

    def _screen_texts(self):
        """{label: (x, y)} centre of every labelled element on the projector's screen."""
        out = self._adb("-s", self.serial, "exec-out", "uiautomator", "dump", "/dev/tty", timeout=20).stdout
        nodes = {}
        for m in re.finditer(r'text="([^"]+)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', out):
            x1, y1, x2, y2 = map(int, m.group(2, 3, 4, 5))
            nodes.setdefault(html.unescape(m.group(1)), ((x1 + x2) // 2, (y1 + y2) // 2))
        return nodes

    def _wait_for_texts(self, wanted, timeout=8):
        deadline = time.monotonic() + timeout
        while True:
            nodes = self._screen_texts()
            if any(w in nodes for w in wanted) or time.monotonic() > deadline:
                return nodes
            time.sleep(0.5)

    def bt_connect(self):
        """Connect the paired Bluetooth speaker, by doing what you'd do by hand in
        Settings > Remotes & accessories > <speaker> > Connect (Android 12's shell has
        no connect command). Only ever taps the speaker's name and "Connect"."""
        name = self.bt_speaker
        with self.lock:
            try:
                self.run(COMMANDS["bt_settings"])
                nodes = self._wait_for_texts([name])
                if name not in nodes:
                    return {"ok": False, "error": f"{name} isn't in the paired list. Pair it once in Bluetooth settings."}
                self.run("input tap %d %d" % nodes[name])
                nodes = self._wait_for_texts(["Connect", "Disconnect"], timeout=5)
                if "Disconnect" in nodes:
                    return {"ok": True, "message": f"{name} is already connected."}
                if "Connect" not in nodes:
                    return {"ok": False, "error": f"Couldn't find the Connect option for {name}."}
                self.run("input tap %d %d" % nodes["Connect"])
                for _ in range(20):
                    time.sleep(0.5)
                    state = self.run("dumpsys bluetooth_manager | grep -m1 ConnectionState")[1]
                    if "STATE_CONNECTED" in state:
                        return {"ok": True, "message": f"Connected to {name}."}
                return {"ok": False, "error": f"{name} didn't connect. Is it switched on and in Bluetooth mode?"}
            finally:
                # Back out of the settings screens only, back to whatever was showing.
                for _ in range(3):
                    focus = self.run("dumpsys window | grep -m1 mCurrentFocus")[1]
                    if "accessories" not in focus:
                        break
                    self.run("input keyevent KEYCODE_BACK; sleep 0.5")

    def keystone(self):
        """Current corner offsets, e.g. {"lt": [1, 2], "rt": [19, 6], ...}."""
        out = self.run("; ".join(f"getprop persist.zvt.{c}" for c in CORNERS), timeout=5)[1]
        corners = {}
        for c, line in zip(CORNERS, out.splitlines() + [""] * 4):
            try:
                x, y = (int(v) for v in line.strip().split(","))
            except ValueError:
                x = y = 0
            corners[c] = [x, y]
        return corners

    def set_keystone(self, corners):
        cmds = []
        for c, (x, y) in corners.items():
            px, py = corner_pixels(c, x, y)
            cmds += [f"setprop persist.zvt.{c} {x},{y}",
                     f"setprop persist.sys.keystone.{c} {px},{py}",
                     f"setprop persist.hisi.keystone.{c} {px},{py}"]
        cmds += ["setprop persist.sys.keystone.update false", "sleep 0.2",
                 "setprop persist.sys.keystone.update true"]
        code, output = self.run("; ".join(cmds))
        return {"ok": code == 0, "output": output[-300:]}

    def status(self):
        try:
            if self.model is None:
                self.model = self.run("getprop ro.product.model", timeout=5)[1].strip()
            return {"connected": True, "host": self.host, "model": self.model, "lens": self.motor_index()}
        except ConnectionError as e:
            return {"connected": False, "host": self.host, "error": str(e)}


def scan_for_adb(*near_hosts):
    """Yield LAN addresses (the /24 around each of near_hosts) that have port 5555 open."""
    nets = dict.fromkeys(ipaddress.ip_network(f"{h}/24", strict=False) for h in near_hosts)
    hosts = [ip for net in nets for ip in net.hosts()]

    def probe(ip):
        with socket.socket() as s:
            s.settimeout(0.5)
            return str(ip) if s.connect_ex((str(ip), 5555)) == 0 else None

    with concurrent.futures.ThreadPoolExecutor(64) as pool:
        yield from filter(None, pool.map(probe, hosts))


def lan_ip(towards):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((towards, 5555))
        return s.getsockname()[0]


def make_handler(projector):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.replace("{{BT_NAME}}", html.escape(projector.bt_speaker)), "text/html")
            elif self.path == "/api/status":
                self._send(200, json.dumps(projector.status()))
            elif self.path == "/api/keystone":
                try:
                    self._send(200, json.dumps({"ok": True, "corners": projector.keystone()}))
                except ConnectionError as e:
                    self._send(200, json.dumps({"ok": False, "error": f"Projector not reachable: {e}"}))
            else:
                self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            if self.path == "/api/keystone":
                self._post_keystone()
                return
            if self.path == "/api/lens":
                self._post_lens()
                return
            name =self.path.removeprefix("/api/cmd/") if self.path.startswith("/api/cmd/") else None
            if name not in COMMANDS and name not in ("park_power", "bt_connect"):
                self._send(404, json.dumps({"ok": False, "error": "unknown command"}))
                return
            if name in ("focus_l", "focus_r", "auto_focus", "auto_focus_direct", "park_power"):
                projector.lens_target = None  # stop any slider move in progress
            started = time.monotonic()
            try:
                if name == "park_power":
                    body = projector.park_and_power_off()
                elif name == "bt_connect":
                    body = projector.bt_connect()
                elif name in ("focus_l", "focus_r"):
                    # Answer only once the lens has settled, so the page shows the new
                    # position and a held button can't outrun the motor.
                    before = projector.motor_index()
                    code, output = projector.run(COMMANDS[name])
                    body = {"ok": code == 0, "output": output[-300:],
                            "lens": projector.wait_for_move(before, timeout=1.2)}
                else:
                    code, output = projector.run(COMMANDS[name])
                    body = {"ok": code == 0, "output": output[-300:]}
            except (ConnectionError, subprocess.TimeoutExpired) as e:
                body = {"ok": False, "error": f"Projector not reachable: {e}"}
            body["ms"] = round((time.monotonic() - started) * 1000)
            self._send(200, json.dumps(body))

        def _post_keystone(self):
            """Body: {"rt": [x, y], ...}; any subset of corners, steps clamped to 0-200."""
            try:
                raw = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                corners = {c: [max(0, min(KEYSTONE_MAX, int(v))) for v in raw[c]][:2]
                           for c in CORNERS if c in raw}
                if not corners or any(len(v) != 2 for v in corners.values()):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                self._send(400, json.dumps({"ok": False, "error": "bad keystone values"}))
                return
            try:
                body = projector.set_keystone(corners)
            except ConnectionError as e:
                body = {"ok": False, "error": f"Projector not reachable: {e}"}
            self._send(200, json.dumps(body))

        def _post_lens(self):
            """Body: {"target": 0-148}. Answers once the lens gets there (or gives way)."""
            try:
                raw = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                target = int(raw["target"])
            except (ValueError, TypeError, KeyError):
                self._send(400, json.dumps({"ok": False, "error": "bad lens target"}))
                return
            try:
                body = projector.lens_to(target)
            except (ConnectionError, subprocess.TimeoutExpired) as e:
                body = {"ok": False, "error": f"Projector not reachable: {e}"}
            self._send(200, json.dumps(body))

        def log_message(self, fmt, *args):
            if self.command == "POST":
                print(f"{time.strftime('%H:%M:%S')}  {self.client_address[0]}  {self.path}")

    return Handler


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f1115">
<title>Projector Remote</title>
<style>
  :root {
    --bg: #0f1115; --panel: #181b22; --key: #242833; --key-hi: #313646;
    --text: #e8eaf0; --muted: #8b91a1; --accent: #4c8dff; --focus: #2fbf8f;
    --danger: #e5484d; --radius: 18px;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin: 0; background: var(--bg); color: var(--text);
    font: 16px/1.3 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  body { padding: max(14px, env(safe-area-inset-top)) 16px max(20px, env(safe-area-inset-bottom));
    max-width: 440px; margin: 0 auto; user-select: none; -webkit-user-select: none;
    -webkit-touch-callout: none; touch-action: manipulation; }
  header { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
  header h1 { font-size: 17px; font-weight: 600; margin: 0; flex: 1; }
  #dot { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); }
  #dot.on { background: var(--focus); box-shadow: 0 0 8px var(--focus); }
  #dot.off { background: var(--danger); }
  #where { color: var(--muted); font-size: 13px; }
  section { background: var(--panel); border-radius: var(--radius); padding: 12px; margin-bottom: 12px; }
  section h2 { margin: 0 0 10px 4px; font-size: 12px; letter-spacing: .08em;
    text-transform: uppercase; color: var(--muted); font-weight: 600; }
  .row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .row + .row { margin-top: 10px; }
  .row.two { grid-template-columns: 1fr 1fr; }
  button { appearance: none; border: 0; border-radius: 14px; background: var(--key); color: var(--text);
    font: inherit; font-weight: 600; min-height: 58px; padding: 6px; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 2px; cursor: pointer; transition: background .08s, transform .08s; }
  button small { font-size: 11px; font-weight: 500; color: var(--muted); }
  button:active, button.held { background: var(--key-hi); transform: scale(.97); }
  button:disabled { opacity: .35; cursor: default; }
  button.power { background: #3a1d20; color: #ffb4b6; }
  .lens { display: flex; align-items: center; gap: 22px; margin: 4px 4px 16px; font-size: 13px; color: var(--muted); }
  .lens .track { flex: 1; height: 6px; border-radius: 3px; background: var(--key); position: relative; }
  .lens .fill { position: absolute; inset: 0 auto 0 0; border-radius: 3px; background: var(--focus); width: 0; transition: width .25s; }
  /* The slider overhangs the track by half a thumb each side so the thumb's centre spans 0-100%. */
  .lens input { position: absolute; left: -13px; top: -14px; width: calc(100% + 26px); height: 34px; margin: 0;
    background: none; -webkit-appearance: none; appearance: none; touch-action: pan-y; cursor: pointer; }
  .lens input::-webkit-slider-runnable-track { height: 34px; background: none; }
  .lens input::-webkit-slider-thumb { -webkit-appearance: none; width: 26px; height: 26px; margin-top: 4px;
    border-radius: 50%; background: var(--text); border: 4px solid var(--focus); box-sizing: border-box; }
  .lens input::-moz-range-track { background: none; }
  .lens input::-moz-range-thumb { width: 26px; height: 26px; border-radius: 50%; background: var(--text);
    border: 4px solid var(--focus); box-sizing: border-box; }
  .lens input:disabled { cursor: default; opacity: .4; }
  .lens b { color: var(--text); font-variant-numeric: tabular-nums; min-width: 5.5em; text-align: right; font-weight: 600; }
  button.focus { background: #183a30; color: #a6f0d4; }
  button.wide { grid-column: 1 / -1; }
  .pad { display: grid; grid-template-columns: repeat(3, 1fr); grid-template-rows: repeat(3, 72px);
    gap: 8px; max-width: 300px; margin: 0 auto; }
  .pad button { min-height: 0; font-size: 22px; }
  .pad .ok { background: var(--accent); color: #fff; border-radius: 50%; font-size: 17px;
    width: 72px; justify-self: center; }
  .pad .blank { visibility: hidden; }
  .ks { position: relative; width: calc(100% - 32px); margin: 20px auto 6px; aspect-ratio: 16 / 9; touch-action: none; }
  .nudge + .row { margin-top: 10px; }
  .ks svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
  .ks .frame { fill: none; stroke: var(--muted); stroke-dasharray: 4 4; stroke-width: 1; }
  .ks .quad { fill: rgba(76, 141, 255, .18); stroke: var(--accent); stroke-width: 2; }
  .ks .handle { fill: var(--key-hi); stroke: var(--accent); stroke-width: 2; cursor: grab; }
  .ks .handle.sel { fill: var(--accent); stroke: #fff; }
  .ks-info { display: flex; justify-content: space-between; margin: 10px 4px; font-size: 13px; color: var(--muted); }
  .ks-info b { color: var(--text); font-variant-numeric: tabular-nums; }
  .nudge { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
  .nudge button { min-height: 44px; font-size: 18px; }
  #toast { position: fixed; left: 50%; bottom: max(18px, env(safe-area-inset-bottom)); transform: translateX(-50%);
    background: #2a2e3a; color: var(--text); padding: 10px 16px; border-radius: 12px; font-size: 14px;
    opacity: 0; transition: opacity .2s; pointer-events: none; max-width: calc(100% - 32px); }
  #toast.show { opacity: 1; }
</style>
</head>
<body>
<header>
  <span id="dot"></span>
  <h1>Projector</h1>
  <span id="where">connecting...</span>
</header>

<section>
  <div class="row">
    <button class="power" data-cmd="park_power" data-toast="Parking lens, then powering off..."
      data-confirm="Move the lens to its start position and turn the projector off? The remote can't turn it back on.">⏻<small>Park &amp; off</small></button>
    <button data-cmd="home">⌂<small>Home</small></button>
    <button data-cmd="input">⇄<small>All sources</small></button>
  </div>
  <div class="row">
    <button data-cmd="hdmi" data-toast="Switching to HDMI (Home goes back)">▭<small>HDMI</small></button>
    <button data-cmd="bt_connect" data-toast="Connecting to {{BT_NAME}}...">ᛒ<small>Connect {{BT_NAME}}</small></button>
    <button data-cmd="bt_settings">⚙<small>Bluetooth settings</small></button>
  </div>
</section>

<section>
  <h2>Focus</h2>
  <div class="lens">Lens <span class="track"><span class="fill" id="lensFill"></span><input type="range" id="lensSlider"
    min="0" max="148" step="1" value="0" disabled aria-label="Lens position"></span><b id="lensText">–</b></div>
  <div class="row">
    <button class="focus" id="focusL" data-cmd="focus_l" data-repeat="600">◀<small>Focus</small></button>
    <button class="focus" data-cmd="auto_focus" data-toast="Auto focus running (about 15 s)">AF<small>Auto focus</small></button>
    <button class="focus" id="focusR" data-cmd="focus_r" data-repeat="600">▶<small>Focus</small></button>
  </div>
  <div class="row two">
    <button data-cmd="keystone">⏢<small>Projection menu</small></button>
    <button data-cmd="auto_focus_direct" data-toast="Auto focus running (about 15 s)">AF<small>Auto focus (direct)</small></button>
  </div>
</section>

<section>
  <h2>Keystone (4-point)</h2>
  <div class="ks" id="ks">
    <svg id="ksSvg" preserveAspectRatio="none">
      <rect class="frame" x="0" y="0" width="100%" height="100%"></rect>
      <polygon class="quad" id="ksQuad"></polygon>
    </svg>
  </div>
  <div class="ks-info"><span>Drag a corner. Selected: <b id="ksSel">–</b></span><b id="ksVal"></b></div>
  <div class="nudge">
    <button data-nudge="-1,0">◀</button><button data-nudge="0,-1">▲</button>
    <button data-nudge="0,1">▼</button><button data-nudge="1,0">▶</button>
  </div>
  <div class="row two">
    <button id="ksUndo">↶<small>Undo changes</small></button>
    <button id="ksReset">▢<small>Reset to square</small></button>
  </div>
</section>

<section>
  <div class="pad">
    <span class="blank"></span><button data-cmd="up" data-repeat>▲</button><span class="blank"></span>
    <button data-cmd="left" data-repeat>◀</button><button class="ok" data-cmd="ok">OK</button><button data-cmd="right" data-repeat>▶</button>
    <span class="blank"></span><button data-cmd="down" data-repeat>▼</button><span class="blank"></span>
  </div>
  <div class="row two" style="margin-top:12px">
    <button data-cmd="back">↩<small>Back</small></button>
    <button data-cmd="menu">☰<small>Menu</small></button>
  </div>
</section>

<section>
  <div class="row">
    <button data-cmd="vol_down" data-repeat>−<small>Volume</small></button>
    <button data-cmd="mute">🔇<small>Mute</small></button>
    <button data-cmd="vol_up" data-repeat>+<small>Volume</small></button>
  </div>
  <div class="row">
    <button class="power wide" data-cmd="power"
      data-confirm="Turn the projector off WITHOUT parking the lens? Focus may be out of step after the next power-on.">⏻<small>Power off without parking</small></button>
  </div>
</section>

<div id="toast"></div>

<script>
const dot = document.getElementById('dot');
const where = document.getElementById('where');
const toastEl = document.getElementById('toast');
let toastTimer;

function toast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2500);
}

// Lens counter: 0 = the end Focus ◀ moves toward, 148 = the Focus ▶ end.
// The projector refuses to go past either end, so grey out that button there.
const LENS_MAX = 148;
const focusL = document.getElementById('focusL');
const focusR = document.getElementById('focusR');
const slider = document.getElementById('lensSlider');
let lensNow = null, lensAim = null, lensGen = 0, sliding = false;

// The green fill is where the lens is; the slider thumb is where it's going.
function setLens(n) {
  if (typeof n !== 'number') return;
  lensNow = n;
  const aim = sliding ? Number(slider.value) : lensAim;
  document.getElementById('lensText').textContent = aim === null ? n + ' / ' + LENS_MAX : n + ' → ' + aim;
  document.getElementById('lensFill').style.width = (100 * n / LENS_MAX) + '%';
  if (!sliding && lensAim === null) slider.value = n;
  slider.disabled = false;
  focusL.disabled = n <= 0;
  focusR.disabled = n >= LENS_MAX;
}

// Drag (or tap) the slider: on release the lens steps there, 5 per press, about
// a second each. Releasing again, or pressing a focus button, takes over.
async function moveLens(target) {
  const mine = ++lensGen;
  lensAim = target;
  setLens(lensNow);
  try {
    const j = await (await fetch('/api/lens', { method: 'POST', body: JSON.stringify({ target }) })).json();
    if (mine !== lensGen) return;
    lensAim = null;
    if (!j.ok) toast(j.error || 'Lens move failed');
    setLens(typeof j.lens === 'number' ? j.lens : lensNow);
  } catch (e) {
    if (mine === lensGen) { lensAim = null; toast('Laptop not reachable'); }
  }
}

slider.addEventListener('input', () => { sliding = true; setLens(lensNow); });
slider.addEventListener('change', () => { sliding = false; moveLens(Number(slider.value)); });
['pointerup', 'pointercancel'].forEach(t => slider.addEventListener(t, () => { sliding = false; }));

async function send(cmd) {
  if (/^(focus_|auto_focus|park_power)/.test(cmd)) { lensGen++; lensAim = null; }  // takes over from the slider
  try {
    const r = await fetch('/api/cmd/' + cmd, { method: 'POST' });
    const j = await r.json();
    setLens(j.lens);
    if (!j.ok) { toast(j.error || j.output || 'Command failed'); setStatus(false); }
    else { setStatus(true); if (j.message) toast(j.message); }
    return j.ok;
  } catch (e) {
    toast('Laptop not reachable');
    setStatus(false);
    return false;
  }
}

function setStatus(ok, text) {
  dot.className = ok ? 'on' : 'off';
  if (text) where.textContent = text;
}

async function poll() {
  try {
    const s = await (await fetch('/api/status')).json();
    setStatus(s.connected, s.connected ? s.host : 'offline (' + s.host + ')');
    setLens(s.lens);
  } catch (e) { setStatus(false, 'laptop offline'); }
}

// Tap = one press. Hold a [data-repeat] button = keep pressing, one at a time,
// each waiting for the previous to finish and at most one per data-repeat ms
// (default 250), so presses never pile up. Stops when the button greys out.
const sleep = ms => new Promise(r => setTimeout(r, ms));

document.querySelectorAll('button[data-cmd]').forEach(btn => {
  let holding = false, timer = null;
  const stop = () => { holding = false; clearTimeout(timer); btn.classList.remove('held'); };
  const every = Number(btn.dataset.repeat) || 250;

  btn.addEventListener('pointerdown', async ev => {
    ev.preventDefault();
    if (btn.disabled) return;
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
    if (navigator.vibrate) navigator.vibrate(12);
    if (btn.dataset.toast) toast(btn.dataset.toast);
    holding = true;
    btn.classList.add('held');
    const first = send(btn.dataset.cmd);
    if (!('repeat' in btn.dataset)) { await first; stop(); return; }
    timer = setTimeout(async () => {
      if (!await first) return stop();
      while (holding && !btn.disabled) {
        const [ok] = await Promise.all([send(btn.dataset.cmd), sleep(every)]);
        if (!ok) break;
      }
      stop();
    }, Math.max(350, every));
  });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(t => btn.addEventListener(t, stop));
  btn.addEventListener('contextmenu', e => e.preventDefault());
});

// Keystone: each corner is an inward offset of 0-200 steps; one step is 2.4 px
// across and 1.35 px down on the 1920x1080 picture (so 200 steps = a quarter).
const KS = { max: 200, sx: 2.4 / 1920, sy: 1.35 / 1080 };
const ORDER = ['lt', 'rt', 'rb', 'lb'];
const NAMES = { lt: 'top-left', rt: 'top-right', lb: 'bottom-left', rb: 'bottom-right' };
const ksBox = document.getElementById('ks');
const ksSvg = document.getElementById('ksSvg');
const ksQuad = document.getElementById('ksQuad');
const handles = {};
let ks = null, ksOriginal = null, ksSel = 'lt', ksBusy = false, ksPending = false;

const clampStep = v => Math.max(0, Math.min(KS.max, Math.round(v)));
const copy = o => JSON.parse(JSON.stringify(o));

function ksPoint(c) {
  const w = ksBox.clientWidth, h = ksBox.clientHeight;
  const dx = ks[c][0] * KS.sx * w, dy = ks[c][1] * KS.sy * h;
  return [c[0] === 'l' ? dx : w - dx, c[1] === 't' ? dy : h - dy];
}

function ksDraw() {
  if (!ks) return;
  ksQuad.setAttribute('points', ORDER.map(c => ksPoint(c).join(',')).join(' '));
  for (const c of ORDER) {
    const [x, y] = ksPoint(c);
    handles[c].setAttribute('cx', x);
    handles[c].setAttribute('cy', y);
    handles[c].classList.toggle('sel', c === ksSel);
  }
  document.getElementById('ksSel').textContent = NAMES[ksSel];
  document.getElementById('ksVal').textContent = ks[ksSel].join(', ');
}

// One request in flight at a time; while dragging, only the newest values are sent.
async function ksPush() {
  if (ksBusy) { ksPending = true; return; }
  ksBusy = true;
  do {
    ksPending = false;
    try {
      const r = await fetch('/api/keystone', { method: 'POST', body: JSON.stringify(ks) });
      const j = await r.json();
      if (!j.ok) toast(j.error || 'Keystone change failed');
    } catch (e) { toast('Laptop not reachable'); }
  } while (ksPending);
  ksBusy = false;
}

for (const c of ORDER) {
  const h = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  h.setAttribute('r', 14);
  h.classList.add('handle');
  ksSvg.appendChild(h);
  handles[c] = h;
  let grab = null;
  h.addEventListener('pointerdown', ev => {
    if (!ks) return;
    ev.preventDefault();
    h.setPointerCapture(ev.pointerId);
    const [x, y] = ksPoint(c), r = ksBox.getBoundingClientRect();
    grab = [ev.clientX - r.left - x, ev.clientY - r.top - y];
    ksSel = c;
    ksDraw();
  });
  h.addEventListener('pointermove', ev => {
    if (!grab) return;
    const r = ksBox.getBoundingClientRect();
    const px = ev.clientX - r.left - grab[0], py = ev.clientY - r.top - grab[1];
    const dx = c[0] === 'l' ? px : r.width - px, dy = c[1] === 't' ? py : r.height - py;
    const next = [clampStep(dx / (KS.sx * r.width)), clampStep(dy / (KS.sy * r.height))];
    if (next[0] === ks[c][0] && next[1] === ks[c][1]) return;
    ks[c] = next;
    ksDraw();
    ksPush();
  });
  ['pointerup', 'pointercancel'].forEach(t => h.addEventListener(t, () => { grab = null; }));
}

// Arrows move the selected corner one step in that direction on the wall.
document.querySelectorAll('[data-nudge]').forEach(b => b.addEventListener('click', () => {
  if (!ks) return;
  const [mx, my] = b.dataset.nudge.split(',').map(Number);
  const [x, y] = ks[ksSel];
  ks[ksSel] = [clampStep(x + (ksSel[0] === 'l' ? mx : -mx)), clampStep(y + (ksSel[1] === 't' ? my : -my))];
  ksDraw();
  ksPush();
}));

document.getElementById('ksUndo').addEventListener('click', () => {
  if (!ksOriginal) return;
  ks = copy(ksOriginal);
  ksDraw();
  ksPush();
  toast('Keystone restored to how it was when this page opened');
});

document.getElementById('ksReset').addEventListener('click', () => {
  if (!ks || !confirm('Reset all four corners to a full, square picture?')) return;
  for (const c of ORDER) ks[c] = [0, 0];
  ksDraw();
  ksPush();
});

async function ksLoad() {
  try {
    const j = await (await fetch('/api/keystone')).json();
    if (!j.ok) return toast(j.error || 'Could not read keystone');
    ks = j.corners;
    ksOriginal = copy(ks);
    ksDraw();
  } catch (e) { toast('Laptop not reachable'); }
}
window.addEventListener('resize', ksDraw);
ksLoad();

poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Wi-Fi remote for the XElectron C9A projector")
    ap.add_argument("--projector", default="192.168.1.20", help="projector IP (default 192.168.1.20)")
    ap.add_argument("--port", type=int, default=8765, help="web page port (default 8765)")
    ap.add_argument("--adb", default=find_adb(),
                    help="path to adb (default: ../platform-tools/adb next to this folder, else adb on PATH)")
    ap.add_argument("--bt-speaker", default=DEFAULT_BT_SPEAKER,
                    help=f"name of the paired Bluetooth speaker (default {DEFAULT_BT_SPEAKER})")
    ap.add_argument("--no-scan", action="store_true",
                    help="don't search the LAN if the projector's address has changed")
    args = ap.parse_args()
    if not args.adb:
        raise SystemExit("adb not found: pass --adb C:\\path\\to\\adb.exe")

    projector = Projector(args.adb, args.projector, scan=not args.no_scan, bt_speaker=args.bt_speaker)
    print(f"Connecting to projector at {args.projector} ...")
    s = projector.status()
    print(f"  {'connected: ' + s['model'] if s['connected'] else 'not connected yet: ' + s['error']}")

    # On Windows, address reuse lets a second copy bind the same port silently and
    # steal requests from the first; refuse instead so a stale copy can't linger.
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    try:
        server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(projector))
    except OSError:
        raise SystemExit(f"Port {args.port} is already in use: the remote is probably already "
                         "running in another window. Close it first, or use --port.")
    print(f"\nOpen on your phone:  http://{lan_ip(projector.host)}:{args.port}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        projector._close_shell()


if __name__ == "__main__":
    main()
