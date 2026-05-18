# Windows STA fix — MUST be before ALL other imports
import sys
if sys.platform == "win32":
    sys.coinit_flags = 0  # MTA threading — required for Bleak WinRT callbacks

import asyncio
import traceback
import argparse
import math
import random
import colorsys
import logging
from aiohttp import web
from bleak import BleakClient, BleakScanner

# =========================================================
# TERMINAL DOGMA — MAGI LIGHT CONTROL SYSTEM v4
# EVA-01 Inspired BLE Controller
# Adds cinematic Evangelion-inspired software patterns,
# HSV colour engine, runtime speed/brightness/intensity API.
# =========================================================

logging.basicConfig(level=logging.WARNING)

# ── DEVICE DATABASE ───────────────────────────────────────
LIGHTS = {
    "Set_1": {
        "name":    "QHM (Green)",
        "address": "36:46:40:1F:54:0C",
        "service": "0000ffd5-0000-1000-8000-00805f9b34fb",
        "uuid":    "0000ffd9-0000-1000-8000-00805f9b34fb",
        "type":    "triones",
        "cmd_on":  bytes([0xCC, 0x23, 0x33]),
        "cmd_off": bytes([0xCC, 0x24, 0x33]),
    },
    "Set_2": {
        "name":    "Melk (Purple)",
        "address": "BE:28:05:00:09:3B",
        "service": "0000fff0-0000-1000-8000-00805f9b34fb",
        "uuid":    "0000fff3-0000-1000-8000-00805f9b34fb",
        "type":    "fff0",
        "cmd_on":  bytes([0x7E, 0x00, 0x04, 0x01, 0x00, 0x00, 0x00, 0x00, 0xEF]),
        "cmd_off": bytes([0x7E, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0xEF]),
    }
}

# ── TRIONES HARDWARE MODES ────────────────────────────────
class HWMode:
    SMOOTH_RAINBOW  = 37
    PULSATING_RGB   = 97
    RGB_STROBE      = 98
    RED_STROBE      = 49
    HARD_RAINBOW    = 56

# ── COLOURS ───────────────────────────────────────────────
PURPLE = (128,   0, 255)
GREEN  = ( 57, 255,  20)
RED    = (255,   0,   0)
BLACK  = (  0,   0,   0)
OCEAN  = (  0, 180, 255)
WHITE  = (255, 255, 255)
ORANGE = (255, 102,   0)
GOLD   = (255, 215,   0)
MAGENTA= (255,   0, 255)
BLUE   = ( 20,  80, 255)

# ── STATE ─────────────────────────────────────────────────
clients         = {}
current_task    = None
current_pattern = "idle"
BOOT_PATTERN    = None
_boot_triggered = False

# Runtime controls. Update via POST /params.
PARAMS = {
    "speed": 1.0,
    "brightness": 1.0,
    "intensity": 1.0,
    "transition_ms": 120,
}

# ── COLOUR / MATH HELPERS ────────────────────────────────
def clamp(x, lo=0, hi=255):
    return max(lo, min(hi, int(x)))

def hsv(h, s=1.0, v=1.0):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)

def lerp(a, b, t):
    return a + (b - a) * t

def lerp_color(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(lerp(c1[i], c2[i], t)) for i in range(3))

def noise_wave(t):
    return (
        math.sin(t) * 0.5 +
        math.sin(t * 1.73 + 0.4) * 0.3 +
        math.sin(t * 2.31 + 1.2) * 0.2
    )

def spd(base):
    return max(0.02, base / max(0.05, float(PARAMS.get("speed", 1.0))))

# ── PROTOCOL HELPERS ──────────────────────────────────────
def color_cmd(cfg, r, g, b, brightness=0):
    r = clamp(r); g = clamp(g); b = clamp(b); w = clamp(brightness)
    if cfg["type"] == "triones":
        return bytes([0x56, r, g, b, w, 0xF0, 0xAA])
    return bytes([0x7E, 0x00, 0x05, 0x03, r, g, b, 0x00, 0xEF])

def mode_cmd(mode, speed=0x30):
    return bytes([0xBB, mode & 0xFF, speed & 0xFF, 0x44])

async def write(key, data):
    client = clients.get(key)
    if not client or not client.is_connected:
        return
    try:
        await client.write_gatt_char(LIGHTS[key]["uuid"], data, response=False)
    except Exception:
        pass

async def send_color(key, r, g, b, brightness=0):
    scale = max(0.0, min(1.0, float(PARAMS.get("brightness", 1.0))))
    await write(key, color_cmd(LIGHTS[key], r * scale, g * scale, b * scale, brightness))

async def send_both(c1, c2=None):
    if c2 is None:
        c2 = c1
    await asyncio.gather(send_color("Set_1", *c1), send_color("Set_2", *c2))

async def send_mode(key, mode, speed=0x30):
    if LIGHTS[key]["type"] == "triones":
        await write(key, mode_cmd(mode, speed))

async def power_on_all():
    for key, cfg in LIGHTS.items():
        await write(key, cfg["cmd_on"])
    await asyncio.sleep(0.4)

async def power_off_all():
    for key, cfg in LIGHTS.items():
        await write(key, cfg["cmd_off"])

def any_connected():
    return any(c and c.is_connected for c in clients.values())

def connection_status():
    return {k: bool(clients.get(k) and clients[k].is_connected) for k in LIGHTS}

# ── ORIGINAL SOFTWARE PATTERNS ───────────────────────────
async def pattern_synchro_test():
    print("Initiating Synchro Test...")
    delay = 0.5
    while delay > 0.08:
        await send_both(GREEN, PURPLE); await asyncio.sleep(spd(delay))
        await send_both(PURPLE, GREEN); await asyncio.sleep(spd(delay))
        delay *= 0.9
    while True:
        await send_both(GREEN, PURPLE); await asyncio.sleep(spd(1.0))

async def pattern_lilin_breath():
    print("Running Pulse of the Lilin...")
    steps = 60
    while True:
        for i in range(steps):
            r = i / steps
            await send_both(tuple(GREEN[j]*r for j in range(3)), tuple(PURPLE[j]*(1-r) for j in range(3)))
            await asyncio.sleep(spd(0.10))
        for i in range(steps):
            r = i / steps
            await send_both(tuple(GREEN[j]*(1-r) for j in range(3)), tuple(PURPLE[j]*r for j in range(3)))
            await asyncio.sleep(spd(0.10))

async def pattern_emergency():
    print("EMERGENCY: BERSERK MODE")
    while True:
        await send_both(RED);   await asyncio.sleep(spd(0.5))
        await send_both(BLACK); await asyncio.sleep(spd(0.5))

async def pattern_static():
    print("Standard Setup Confirmed.")
    while True:
        await send_both(GREEN, PURPLE)
        await asyncio.sleep(10)

async def pattern_tidal_wave():
    print("Running Tidal Wave...")
    t, SPEED, PHASE = 0.0, 0.035, math.pi * 0.75
    def wave_color(w):
        if w < 0.5:
            return lerp_color(GREEN, OCEAN, w * 2)
        return lerp_color(OCEAN, PURPLE, (w - 0.5) * 2)
    while True:
        def mw(ph):
            raw = math.sin(t+ph)*0.65 + math.sin(t*1.8+ph+0.9)*0.35
            return (raw+1)/2
        await send_both(wave_color(mw(0)), wave_color(mw(PHASE)))
        t += SPEED * PARAMS["speed"]
        await asyncio.sleep(0.05)

async def pattern_evangelion():
    print("EVA-01 AWAKENING SEQUENCE")
    t = 0.0
    while True:
        env = math.sin(t * 0.8) ** 2
        intensity = 0.2 + 0.8 * env
        if env > 0.92:
            await send_both(WHITE)
        else:
            await send_both(tuple(int(c * intensity) for c in GREEN), tuple(int(c * intensity) for c in PURPLE))
        t += 0.07 * PARAMS["speed"]
        await asyncio.sleep(0.05)

# ── NEW MAGI v3 PATTERNS ────────────────────────────────
async def pattern_at_field():
    print("Deploying A.T. Field...")
    while True:
        for i in range(90):
            t = i / 90
            pulse = math.sin(t * math.pi)
            gold = (255, int(120 + pulse * 135), int(pulse * 40))
            edge = max(0, (pulse - 0.82) * 800)
            c = (clamp(gold[0] + edge), clamp(gold[1] + edge), clamp(gold[2] + edge))
            await send_both(c)
            await asyncio.sleep(spd(0.02))

async def pattern_dummy_plug():
    print("DUMMY SYSTEM ENGAGED")
    palette = [PURPLE, RED, ORANGE, WHITE, BLACK]
    while True:
        base1, base2 = random.choice(palette), random.choice(palette)
        flick1 = tuple(clamp(c + random.randint(-50, 50)) for c in base1)
        flick2 = tuple(clamp(c + random.randint(-80, 80)) for c in base2)
        if random.random() > 0.94: flick1 = WHITE
        if random.random() > 0.97: flick2 = RED
        await send_both(flick1, flick2)
        await asyncio.sleep(spd(random.uniform(0.02, 0.12)))

async def pattern_magi():
    print("MAGI COMPUTATION ACTIVE")
    colours = [GREEN, ORANGE, RED, OCEAN, WHITE, PURPLE]
    t = 0
    while True:
        i1 = int((math.sin(t) + 1) * 2.5)
        i2 = int((math.sin(t + 1.7) + 1) * 2.5)
        await send_both(colours[i1 % len(colours)], colours[i2 % len(colours)])
        t += 0.35 * PARAMS["speed"]
        await asyncio.sleep(0.06)

async def pattern_angel_arrival():
    print("ANGEL DETECTED")
    while True:
        for _ in range(10):
            await send_both(BLACK); await asyncio.sleep(spd(0.15))
        for i in range(100):
            intensity = (i / 100) ** 2
            c = (int(40 * intensity), int(120 * intensity), int(255 * intensity))
            await send_both(c)
            await asyncio.sleep(spd(0.03))
        for _ in range(4):
            await send_both(WHITE); await asyncio.sleep(spd(0.05))
        for i in range(80):
            pulse = (math.sin(i * 0.25) + 1) / 2
            c = (int(255 * pulse), 0, int(40 * pulse))
            await send_both(c)
            await asyncio.sleep(spd(0.04))

async def pattern_longinus():
    print("SPEAR OF LONGINUS")
    t = 0
    while True:
        h1 = (t * 0.008) % 1.0
        h2 = ((t * 0.008) + 0.18) % 1.0
        c1 = hsv(h1, 1.0, 1.0)
        c2 = hsv(h2, 1.0, 1.0)
        c1 = (255, int(c1[1] * 0.3), int(c1[2] * 0.5))
        c2 = (255, int(c2[1] * 0.3), int(c2[2] * 0.5))
        await send_both(c1, c2)
        t += PARAMS["speed"]
        await asyncio.sleep(0.03)

async def pattern_lcl():
    print("LCL IMMERSION")
    t = 0
    while True:
        w1 = (noise_wave(t) + 1) / 2
        w2 = (noise_wave(t + 2.4) + 1) / 2
        await send_both(lerp_color((255, 40, 0), ORANGE, w1), lerp_color((255, 40, 0), ORANGE, w2))
        t += 0.03 * PARAMS["speed"]
        await asyncio.sleep(0.04)

async def pattern_sync_ratio():
    print("SYNCHRONIZATION RATIO INCREASING")
    chaos = 1.0
    t = 0
    while True:
        phase_error = math.sin(t * 0.4) * chaos
        p1 = (math.sin(t) + 1) / 2
        p2 = (math.sin(t + phase_error) + 1) / 2
        await send_both(lerp_color(PURPLE, GREEN, p1), lerp_color(PURPLE, GREEN, p2))
        chaos *= 0.999
        t += 0.08 * PARAMS["speed"]
        await asyncio.sleep(0.04)

async def pattern_plasma_core():
    print("S² ENGINE PLASMA CORE")
    t = 0
    while True:
        c1 = hsv(0.72 + 0.04 * math.sin(t), 1.0, 0.45 + 0.55*((noise_wave(t)+1)/2))
        c2 = hsv(0.33 + 0.05 * math.sin(t+1.6), 1.0, 0.45 + 0.55*((noise_wave(t+2)+1)/2))
        await send_both(c1, c2)
        t += 0.05 * PARAMS["speed"]
        await asyncio.sleep(0.035)

# ── HARDWARE PATTERNS ───────────────────────────────────
async def pattern_hw_rainbow():
    print("Running NERV Rainbow (hardware on triones)...")
    await send_mode("Set_1", HWMode.SMOOTH_RAINBOW, speed=0x40)
    t = 0.0
    while True:
        raw = (math.sin(t)*0.65 + math.sin(t*1.8+0.9)*0.35 + 1) / 2
        c = lerp_color(OCEAN, PURPLE, raw)
        await send_color("Set_2", *c)
        t += 0.035 * PARAMS["speed"]
        await asyncio.sleep(0.05)

async def pattern_hw_rgb_strobe():
    print("Running SEELE Strobe (hardware on triones)...")
    await send_mode("Set_1", HWMode.RGB_STROBE, speed=0x10)
    while True:
        await send_color("Set_2", *RED);   await asyncio.sleep(spd(0.4))
        await send_color("Set_2", *BLACK); await asyncio.sleep(spd(0.4))

# ── PATTERN METADATA ─────────────────────────────────────
PATTERNS = {
    "synchro":    {"fn": pattern_synchro_test,  "label": "SYNCHRO TEST",      "theme": "green"},
    "breath":     {"fn": pattern_lilin_breath,  "label": "LILIN BREATH",      "theme": "purple"},
    "berserk":    {"fn": pattern_emergency,     "label": "BERSERK",           "theme": "red"},
    "static":     {"fn": pattern_static,        "label": "STATIC",            "theme": "orange"},
    "tidal":      {"fn": pattern_tidal_wave,    "label": "TIDAL WAVE",        "theme": "cyan"},
    "evangelion": {"fn": pattern_evangelion,    "label": "AWAKENING",         "theme": "white"},
    "rainbow":    {"fn": pattern_hw_rainbow,    "label": "RAINBOW",           "theme": "gold"},
    "strobe":     {"fn": pattern_hw_rgb_strobe, "label": "SEELE STROBE",      "theme": "pink"},
    "atfield":    {"fn": pattern_at_field,      "label": "A.T. FIELD",        "theme": "gold"},
    "dummy":      {"fn": pattern_dummy_plug,    "label": "DUMMY PLUG",        "theme": "red"},
    "magi":       {"fn": pattern_magi,          "label": "MAGI COMPUTATION",  "theme": "orange"},
    "angel":      {"fn": pattern_angel_arrival, "label": "ANGEL ARRIVAL",     "theme": "blue"},
    "longinus":   {"fn": pattern_longinus,      "label": "SPEAR OF LONGINUS", "theme": "red"},
    "lcl":        {"fn": pattern_lcl,           "label": "LCL IMMERSION",     "theme": "orange"},
    "sync_ratio": {"fn": pattern_sync_ratio,    "label": "SYNC RATIO",        "theme": "green"},
    "plasma":     {"fn": pattern_plasma_core,   "label": "S² ENGINE",         "theme": "purple"},
}

# Backwards-compatible alias for older code references.
PATTERN_MAP = {k: v["fn"] for k, v in PATTERNS.items()}

# ── PATTERN SWITCHING ─────────────────────────────────────
async def cancel_current_pattern():
    global current_task
    if current_task and not current_task.done():
        current_task.cancel()
        try:
            await current_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

async def switch_to(fn):
    global current_task
    await cancel_current_pattern()
    current_task = asyncio.create_task(fn())

async def _rejoin():
    await asyncio.sleep(1)
    if current_pattern in PATTERNS:
        await power_on_all()
        await switch_to(PATTERNS[current_pattern]["fn"])

async def _boot_activate():
    global current_pattern
    await asyncio.sleep(2)
    current_pattern = BOOT_PATTERN
    await power_on_all()
    await switch_to(PATTERNS[BOOT_PATTERN]["fn"])

# ── CONNECTION MANAGER ────────────────────────────────────
async def manage_device(key):
    cfg     = LIGHTS[key]
    address = cfg["address"].upper()
    retry   = 3

    while True:
        try:
            client = clients.get(key)
            if client and client.is_connected:
                await asyncio.sleep(2)
                continue

            print(f"[{key}] Scanning for {cfg['name']}...")
            found = None
            discovered = await BleakScanner.discover(timeout=8)
            for d in discovered:
                if d.address.upper() == address:
                    found = d
                    break

            if not found:
                print(f"[{key}] Not found — retry in {retry}s")
                await asyncio.sleep(retry)
                retry = min(retry * 2, 30)
                continue

            print(f"[{key}] Found: {found.name} — connecting...")
            client = BleakClient(found)
            clients[key] = client
            await client.connect(timeout=15)
            print(f"[{key}] Connected ✓  ({cfg['type']})")
            retry = 3

            global _boot_triggered
            if BOOT_PATTERN and not _boot_triggered:
                _boot_triggered = True
                asyncio.create_task(_boot_activate())
            elif current_pattern != "idle":
                asyncio.create_task(_rejoin())

            while client.is_connected:
                await asyncio.sleep(2)

            print(f"[{key}] Dropped — will retry.")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[{key}] Error: {e}")
            traceback.print_exc()
            await asyncio.sleep(retry)
            retry = min(retry * 2, 30)

# ── CORS ──────────────────────────────────────────────────
def cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return r


# ── CINEMATIC SCENES ─────────────────────────────────────
async def scene_step(c1, c2=None, duration=0.25):
    await send_both(c1, c2)
    await asyncio.sleep(max(0.01, duration))

async def scene_eva_launch():
    print("EVA LAUNCH SEQUENCE")
    await cancel_current_pattern()
    await power_on_all()
    for _ in range(2):
        await scene_step(RED, duration=0.18)
        await scene_step(BLACK, duration=0.12)
    await scene_step(WHITE, duration=0.08)
    await scene_step(PURPLE, GREEN, duration=0.8)
    await scene_step(GREEN, PURPLE, duration=1.2)

async def scene_angel_alert():
    print("ANGEL ALERT SEQUENCE")
    await cancel_current_pattern()
    await power_on_all()
    for _ in range(3):
        await scene_step(BLUE, duration=0.25)
        await scene_step(BLACK, duration=0.12)
    await scene_step(WHITE, duration=0.08)
    for _ in range(6):
        await scene_step(RED, duration=0.16)
        await scene_step(BLACK, duration=0.10)

async def scene_blackout():
    print("BLACKOUT SEQUENCE")
    await cancel_current_pattern()
    # stepped fade avoids harsh BLE spam while looking intentional
    for level in range(10, -1, -1):
        c = tuple(int(x * level / 10) for x in ORANGE)
        await scene_step(c, duration=0.08)
    await power_off_all()

SCENES = {
    "launch": scene_eva_launch,
    "angel_alert": scene_angel_alert,
    "blackout": scene_blackout,
}

# ── HTTP HANDLERS ─────────────────────────────────────────
async def handle_options(request):
    return cors(web.Response(status=204))

async def handle_status(request):
    return cors(web.json_response({
        "pattern":   current_pattern,
        "connected": any_connected(),
        "devices":   connection_status(),
        "params":    PARAMS,
        "patterns":  {k: {"label": v["label"], "theme": v["theme"]} for k, v in PATTERNS.items()}
    }))

async def handle_patterns(request):
    return cors(web.json_response({
        "patterns": {k: {"label": v["label"], "theme": v["theme"]} for k, v in PATTERNS.items()},
        "scenes": list(SCENES.keys())
    }))

async def handle_scene(request):
    global current_pattern
    if not any_connected():
        return cors(web.json_response({"error": "no devices connected"}, status=503))
    name = request.match_info["scene"]
    if name not in SCENES:
        return cors(web.json_response({"error": "unknown scene"}, status=404))
    current_pattern = "scene:" + name
    await SCENES[name]()
    if name == "blackout":
        current_pattern = "idle"
    return cors(web.json_response({"status": "ok", "scene": name, "pattern": current_pattern}))

async def handle_power(request):
    action = request.match_info["action"]
    if action == "on":
        await power_on_all()
        return cors(web.json_response({"status": "ok", "power": "on"}))
    if action == "off":
        await power_off_all()
        return cors(web.json_response({"status": "ok", "power": "off"}))
    return cors(web.json_response({"error": "unknown power action"}, status=404))

async def handle_pattern(request):
    global current_pattern
    if not any_connected():
        return cors(web.json_response({"error": "no devices connected"}, status=503))
    name = request.match_info["pattern"]
    if name not in PATTERNS:
        return cors(web.json_response({"error": "unknown pattern"}, status=404))
    print(f"Switching to: {name}")
    current_pattern = name
    await power_on_all()
    await switch_to(PATTERNS[name]["fn"])
    return cors(web.json_response({"status": "ok", "pattern": name, "label": PATTERNS[name]["label"]}))

async def handle_color(request):
    global current_pattern
    if not any_connected():
        return cors(web.json_response({"error": "no devices connected"}, status=503))
    try:
        body = await request.json()
        r = int(body.get("r", 255)); g = int(body.get("g", 255)); b = int(body.get("b", 255))
    except Exception:
        return cors(web.json_response({"error": "bad body"}, status=400))

    await cancel_current_pattern()
    current_pattern = "custom"
    await power_on_all()
    await send_both((r, g, b))
    return cors(web.json_response({"status": "ok", "r": r, "g": g, "b": b}))

async def handle_params(request):
    try:
        body = await request.json()
        for k in ("speed", "brightness", "intensity"):
            if k in body:
                PARAMS[k] = max(0.05, min(3.0, float(body[k])))
        if "transition_ms" in body:
            PARAMS["transition_ms"] = max(0, min(2000, int(body["transition_ms"])))
        PARAMS["brightness"] = max(0.0, min(1.0, PARAMS["brightness"]))
        return cors(web.json_response({"status": "ok", "params": PARAMS}))
    except Exception as e:
        return cors(web.json_response({"error": str(e)}, status=400))

async def handle_shutdown(request):
    global current_pattern
    await cancel_current_pattern()
    current_pattern = "idle"
    await power_off_all()
    print("System Offline.")
    return cors(web.json_response({"status": "ok", "pattern": "idle"}))

# ── MAIN ──────────────────────────────────────────────────
async def main():
    app = web.Application()
    for method in ("OPTIONS", "GET", "POST"):
        app.router.add_route(method, "/status",   handle_options if method=="OPTIONS" else handle_status)
        app.router.add_route(method, "/shutdown", handle_options if method=="OPTIONS" else handle_shutdown)
    app.router.add_route("GET",     "/patterns",          handle_patterns)
    app.router.add_route("OPTIONS", "/scene/{scene}",     handle_options)
    app.router.add_route("POST",    "/scene/{scene}",     handle_scene)
    app.router.add_route("OPTIONS", "/power/{action}",    handle_options)
    app.router.add_route("POST",    "/power/{action}",    handle_power)
    app.router.add_route("OPTIONS", "/pattern/{pattern}", handle_options)
    app.router.add_route("POST",    "/pattern/{pattern}", handle_pattern)
    app.router.add_route("OPTIONS", "/color",             handle_options)
    app.router.add_route("POST",    "/color",             handle_color)
    app.router.add_route("OPTIONS", "/params",            handle_options)
    app.router.add_route("POST",    "/params",            handle_params)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "localhost", 8765).start()

    print("\n══════════════════════════════════")
    print("  TERMINAL DOGMA — MAGI v4 ONLINE")
    print("  Server: http://localhost:8765")
    print("  Patterns:", ", ".join(PATTERNS.keys()))
    print("══════════════════════════════════\n")

    for key in LIGHTS:
        asyncio.create_task(manage_device(key))

    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MAGI BLE Light Controller v4")
    parser.add_argument("--pattern", choices=list(PATTERNS.keys()), default=None,
                        help="Pattern to activate on boot")
    args = parser.parse_args()
    BOOT_PATTERN = args.pattern

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nManual Override Engaged.")
