import asyncio, json, os, re, secrets, shutil, socket, ssl, subprocess, sys, time
from collections import deque
from aiohttp import web, WSMsgType
from evdev import UInput, AbsInfo, ecodes as e

BASE = os.path.dirname(os.path.abspath(__file__))
cfg = {"port": 4040, "max_players": 4, "persistent_pads": False, "leave_grace": 8,
       "stick_deadzone": 0.05, "stick_smoothing": 1.0}
try:
    cfg.update(json.load(open(os.path.join(BASE, "config.json"))))
except FileNotFoundError:
    pass
except Exception as ex:
    print("config.json problem, using defaults:", ex)
PORT, MAXP = int(cfg["port"]), max(1, min(16, int(cfg["max_players"])))
TOKEN = secrets.token_urlsafe(8)   # new secret every run, lives in the QR code
PERSIST = bool(cfg["persistent_pads"])   # True = make all pads at startup (for games that never notice new controllers)
GRACE = float(cfg["leave_grace"])        # seconds a disconnected phone keeps its pad, so Wi-Fi blips are harmless
DEADZONE = max(0.0, min(0.5, float(cfg["stick_deadzone"])))    # tiny, only to ignore finger jitter near the centre (the game adds its own)
SMOOTH = max(0.0, min(5.0, float(cfg["stick_smoothing"])))     # 0 = off, 1 = default, higher = smoother but laggier
MIN_HOLD = 0.025   # a pressed button stays down at least 25 ms, so games can't miss a tap
MIN_GAP = 0.02   # and stays up at least 20 ms, so two quick taps stay two taps
SILENCE = 1.5     # a connected phone that sends nothing for this long (asleep, Wi-Fi gone) lets go of everything

# One bit per control. The order is shared with pad.html, so don't reorder it.
KEYS = ["A", "B", "X", "Y", "LB", "RB", "SEL", "START", "HOME", "L3", "R3", "LT", "RT", "HU", "HD", "HL", "HR"]
KEY_CODE = {"A": e.BTN_A, "B": e.BTN_B, "X": e.BTN_X, "Y": e.BTN_Y, "LB": e.BTN_TL, "RB": e.BTN_TR,
            "SEL": e.BTN_SELECT, "START": e.BTN_START, "HOME": e.BTN_MODE, "L3": e.BTN_THUMBL, "R3": e.BTN_THUMBR}
TRIG = {"LT": e.ABS_Z, "RT": e.ABS_RZ}
NKEY, ALL = len(KEYS), (1 << len(KEYS)) - 1
HU, HD, HL, HR = (KEYS.index(k) for k in ("HU", "HD", "HL", "HR"))
AXES = (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY)


def _s():
    return AbsInfo(0, -32768, 32767, 0, 128, 0)


CAPS = {
    e.EV_KEY: list(KEY_CODE.values()),
    e.EV_ABS: [(e.ABS_X, _s()), (e.ABS_Y, _s()), (e.ABS_Z, AbsInfo(0, 0, 255, 0, 0, 0)),
               (e.ABS_RX, _s()), (e.ABS_RY, _s()), (e.ABS_RZ, AbsInfo(0, 0, 255, 0, 0, 0)),
               (e.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)), (e.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0))],
}
players = {}   # pid -> player dict, see new_player()
pads = {}      # slot -> UInput, only filled when persistent_pads is on


def make_pad(slot):  # looks like an Xbox 360 pad so every game accepts it
    return UInput(CAPS, name=f"MashPad {slot + 1}", vendor=0x045E, product=0x028E,
                  version=0x110, bustype=e.BUS_USB)


def new_player(slot, ui):
    return {"slot": slot, "ui": ui, "ws": None, "seen": 0, "rx": time.monotonic(), "quiet": False,
            "out": [False] * NKEY,                    # what the game currently sees, per control
            "t": [0.0] * NKEY,                        # when each control last changed
            "q": [deque() for _ in KEYS],             # changes waiting for their minimum hold time
            "tm": [None] * NKEY,                      # timer that will release the queue
            "ax": [0, 0, 0, 0]}                       # L-x, L-y, R-x, R-y as last written


def report():
    print(f"Players connected: {len(players)}/{MAXP}", flush=True)


def emit(p, i, on):
    """Write one control (button, trigger or d-pad direction) to the virtual pad."""
    ui, k, o = p["ui"], KEYS[i], p["out"]
    o[i], p["t"][i] = on, time.monotonic()
    if k in KEY_CODE:
        ui.write(e.EV_KEY, KEY_CODE[k], int(on))
    elif k in TRIG:
        ui.write(e.EV_ABS, TRIG[k], 255 if on else 0)
    else:
        ui.write(e.EV_ABS, e.ABS_HAT0X, int(o[HR]) - int(o[HL]))
        ui.write(e.EV_ABS, e.ABS_HAT0Y, int(o[HD]) - int(o[HU]))
    ui.syn()


def drain(p, i):
    """Apply queued changes for one control in order, never faster than MIN_HOLD / MIN_GAP."""
    p["tm"][i] = None
    q = p["q"][i]
    try:
        while q:
            wait = (MIN_HOLD if p["out"][i] else MIN_GAP) - (time.monotonic() - p["t"][i])
            if wait > 0:
                p["tm"][i] = asyncio.get_running_loop().call_later(wait, drain, p, i)
                return
            emit(p, i, q.popleft())
    except Exception:
        q.clear()


def set_key(p, i, on):
    q = p["q"][i]
    if (q[-1] if q else p["out"][i]) == on:
        return                                  # nothing new
    q.append(on)                                # every change is kept, in order: a quick tap is never lost
    if p["tm"][i] is None:
        drain(p, i)                             # a press from rest goes out immediately


def set_axes(p, vals):
    ui, changed = p["ui"], False
    for i, v in enumerate(vals):
        if v != p["ax"][i]:
            p["ax"][i] = v
            ui.write(e.EV_ABS, AXES[i], v)
            changed = True
    if changed:
        ui.syn()


def release_all(p):
    for i in range(NKEY):
        if p["tm"][i]:
            p["tm"][i].cancel()
            p["tm"][i] = None
        p["q"][i].clear()
        if p["out"][i]:
            emit(p, i, False)
    set_axes(p, [0, 0, 0, 0])


def feed(p, d):
    """d = 's,<held bitmask>,<lx>,<ly>,<rx>,<ry>': the phone's complete state, so a late or repeated message is harmless."""
    a = d.split(",")
    if len(a) != 6:
        return
    held = int(a[1]) & ALL
    set_axes(p, [max(-32767, min(32767, int(v))) for v in a[2:]])
    for i in range(NKEY):
        set_key(p, i, bool(held >> i & 1))


def remove(pid):
    p = players.pop(pid, None)
    if p:
        try:
            release_all(p)
        except Exception:
            pass
        if not PERSIST:
            try:
                p["ui"].close()   # unplugged: the game sees the controller disappear
            except Exception:
                pass
        print(f"Player {p['slot'] + 1} left", flush=True)
        report()


async def ws_play(request):
    ws = web.WebSocketResponse(heartbeat=15, compress=False, max_msg_size=256)   # tiny messages: compressing them only costs time
    await ws.prepare(request)
    pid = request.query.get("pid", "")[:32]
    if request.query.get("k") != TOKEN or not pid:
        await ws.close(code=4001)
        return ws
    p = players.get(pid)
    if p:
        if p["ws"] and not p["ws"].closed:
            await p["ws"].close(code=4002)
    else:
        used = {q["slot"] for q in players.values()}
        free = [i for i in range(MAXP) if i not in used]
        if not free:
            await ws.close(code=4003)
            return ws
        try:
            ui = pads.get(free[0]) or make_pad(free[0])   # plugged in now, the game sees a new controller
        except Exception as ex:
            print("Could not create a gamepad:", ex)
            await ws.close(code=4003)
            return ws
        p = players[pid] = new_player(free[0], ui)
        print(f"Player {free[0] + 1} joined", flush=True)
        report()
    p["ws"], p["seen"], p["rx"], p["quiet"] = ws, time.time(), time.monotonic(), False
    await ws.send_json({"t": "hi", "slot": p["slot"], "name": socket.gethostname(), "dz": DEADZONE, "sm": SMOOTH})
    bye = False
    try:
        async for msg in ws:
            d = msg.data
            if msg.type != WSMsgType.TEXT or not d or len(d) > 80:
                continue
            p["rx"], p["quiet"] = time.monotonic(), False
            if d[0] == "s":
                try:
                    feed(p, d)
                except Exception:
                    pass
            elif d[0] == "p" and len(d) <= 12:
                await ws.send_str(d)       # echo it back: the phone times the round trip (the ping display)
            elif d == "bye":
                bye = True
                break
    except ConnectionResetError:
        pass
    finally:
        if p["ws"] is ws:
            p["ws"], p["seen"] = None, time.time()
            try:
                release_all(p)
            except Exception:
                pass
            if bye:
                remove(pid)
    return ws


async def index(request):
    if request.query.get("k") != TOKEN:
        return web.Response(text="Scan the QR code shown in the laptop terminal.", status=403)
    with open(PADFILE, encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")


LIBS = {"qrcode.js", "jsQR.js"}


async def lib(request):
    n = request.match_info["name"]
    path = os.path.join(BASE, "js", n)
    if n not in LIBS or not os.path.exists(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})


def make_ctx(ip):  # self-signed cert, only used when config.json has "https": true
    cert, key = os.path.join(BASE, "cert.pem"), os.path.join(BASE, "key.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                        "-days", "825", "-subj", "/CN=mashpad", "-addext", f"subjectAltName=IP:{ip}"],
                       check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


async def reaper():
    while True:
        await asyncio.sleep(0.5)
        now, mono = time.time(), time.monotonic()
        for pid, p in list(players.items()):
            if p["ws"] is None:
                if now - p["seen"] > GRACE:
                    remove(pid)
            elif mono - p["rx"] > SILENCE and not p["quiet"]:
                p["quiet"] = True          # phone went quiet while connected: don't leave a button or stick stuck down
                try:
                    release_all(p)
                except Exception:
                    pass


async def bg(app):
    t = asyncio.create_task(reaper())
    yield
    t.cancel()
    for pid in list(players):
        remove(pid)
    for ui in pads.values():
        ui.close()


SKIP_IFACES = ("docker", "veth", "br-", "virbr", "tun", "tailscale", "wg")


def lan_ip():
    for target in ("10.255.255.255", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 1))
                ip = s.getsockname()[0]
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass
    # no route out (happens when the laptop is the hotspot), so read the interfaces directly
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=2).stdout
        for ifc, addr in re.findall(r"^\d+:\s+(\S+)\s+inet (\d+\.\d+\.\d+\.\d+)/", out, re.M):
            if not ifc.startswith(SKIP_IFACES) and not addr.startswith("127."):
                return addr
    except Exception:
        pass
    return "127.0.0.1"


def wifi_hint(ip):
    """Best effort: warn if this laptop's Wi-Fi card is in power-save mode, the classic cause of random lag spikes."""
    try:
        if not (shutil.which("iw") and shutil.which("ip")):
            return
        run = lambda *c: subprocess.run(c, capture_output=True, text=True, timeout=2).stdout
        for ifc in re.findall(r"Interface\s+(\S+)", run("iw", "dev")):
            if f"inet {ip}/" in run("ip", "-4", "-o", "addr", "show", "dev", ifc) \
                    and "power save: on" in run("iw", "dev", ifc, "get", "power_save").lower():
                print(f"!! Wi-Fi power saving is ON for {ifc}. It makes the card doze between packets, which feels like\n"
                      f"   random freezes. Turn it off now:   sudo iw dev {ifc} set power_save off\n"
                      f"   and for good: put these two lines in /etc/NetworkManager/conf.d/wifi-powersave.conf\n"
                      f"       [connection]\n       wifi.powersave = 2\n   then: sudo systemctl restart NetworkManager\n")
    except Exception:
        pass


PADFILE = os.path.join(BASE, "assets", "pad.html")

if __name__ == "__main__":
    missing = [p for p in [PADFILE] + [os.path.join(BASE, "js", n) for n in LIBS] if not os.path.exists(p)]
    if missing:
        sys.exit("Missing files (did you unzip the WHOLE folder?):\n  " + "\n  ".join(missing))
    try:
        if PERSIST:
            for i in range(MAXP):
                pads[i] = make_pad(i)
        else:
            make_pad(0).close()   # permission check only
    except Exception as ex:
        sys.exit(f"Can't create virtual gamepads ({ex}).\nTry: sudo modprobe uinput  (and check /dev/uinput permissions)")
    ip, ctx = lan_ip(), None
    if cfg.get("https"):
        try:
            ctx = make_ctx(ip)
        except Exception as ex:
            print("HTTPS setup failed, using plain http:", ex)
    url = f"{'https' if ctx else 'http'}://{ip}:{PORT}/?k={TOKEN}"
    if PERSIST:
        print(f"\n{MAXP} virtual pads created. Start the GAME AFTER this app, not before.")
    else:
        print("\nA pad appears when a phone joins and disappears when it leaves.")
    print("Scan this with your phone (same Wi-Fi):\n")
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(url)
        q.print_ascii(invert=True)   # unreadable? flip to invert=False
    except ImportError:
        print("(pip install qrcode  to get a QR code here)")
    print(f"\n{url}\n")
    wifi_hint(ip)
    app = web.Application()
    app.cleanup_ctx.append(bg)
    app.add_routes([web.get("/", index), web.get("/ws", ws_play), web.get("/js/{name}", lib)])
    web.run_app(app, host="0.0.0.0", port=PORT, print=None, ssl_context=ctx, access_log=None)
