# Windows STA fix — MUST be before ALL other imports
import sys
if sys.platform == "win32":
    sys.coinit_flags = 0  # MTA threading — required for Bleak WinRT callbacks

import asyncio
import traceback
import argparse
import math
import logging
from aiohttp import web
from bleak import BleakClient, BleakScanner

# =========================================================
# TERMINAL DOGMA — MAGI LIGHT CONTROL SYSTEM v2
# EVA-01 Inspired BLE Controller
# Source references:
#   github.com/kakopappa/ble-light-bulb (BLELight.h protocol)
#   github.com/madhead/saberlight (Triones protocol)
#   bleak.readthedocs.io (async BLE best practices)
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
# From BLELight.h — sent as single command, hardware-native (no BLE spam)
# Format: BB <mode> <speed> 44  |  speed: 0x01 fastest → 0xFF slowest
class HWMode:
    SMOOTH_RAINBOW  = 37   # slow colour cycling
    PULSATING_RGB   = 97   # slow RGB throb
    RGB_STROBE      = 98   # rapid RGB flash
    RED_STROBE      = 49
    HARD_RAINBOW    = 56   # snapping colour change

# ── COLOURS ───────────────────────────────────────────────
PURPLE = (128,   0, 255)
GREEN  = ( 57, 255,  20)
RED    = (255,   0,   0)
BLACK  = (  0,   0,   0)
OCEAN  = (  0, 180, 255)
WHITE  = (255, 255, 255)

# ── STATE ─────────────────────────────────────────────────
clients         = {}
current_task    = None
current_pattern = "idle"
BOOT_PATTERN    = None
_boot_triggered = False


# ── PROTOCOL HELPERS ──────────────────────────────────────

def color_cmd(cfg, r, g, b, brightness=0):
    """
    Build the colour write payload for a device.
    Triones: 56 RR GG BB WW F0 AA
      WW = warm-white channel (0 = off, use as brightness boost)
    fff0:    7E 00 05 03 RR GG BB 00 EF
    """
    r = max(0, min(255, int(r)))
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))
    w = max(0, min(255, int(brightness)))
    if cfg["type"] == "triones":
        return bytes([0x56, r, g, b, w, 0xF0, 0xAA])
    return bytes([0x7E, 0x00, 0x05, 0x03, r, g, b, 0x00, 0xEF])

def mode_cmd(mode, speed=0x30):
    """
    Hardware pattern command for Triones devices.
    Format: BB <mode> <speed> 44
    speed: 0x01 = fastest, 0xFF = slowest. 0x30 is a nice mid pace.
    Only works on triones — fff0 devices use software patterns only.
    """
    return bytes([0xBB, mode & 0xFF, speed & 0xFF, 0x44])


async def write(key, data):
    """Write raw bytes to one device. Silently skips if disconnected."""
    client = clients.get(key)
    if not client or not client.is_connected:
        return
    try:
        await client.write_gatt_char(LIGHTS[key]["uuid"], data, response=False)
    except Exception:
        pass

async def send_color(key, r, g, b, brightness=0):
    await write(key, color_cmd(LIGHTS[key], r, g, b, brightness))

async def send_mode(key, mode, speed=0x30):
    """Send hardware mode — only meaningful for triones."""
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


# ── SOFTWARE PATTERNS ─────────────────────────────────────
# Software patterns send colour packets continuously.
# Hardware patterns (below) send one command and let the device do the work.

async def pattern_synchro_test():
    print("Initiating Synchro Test...")
    delay = 0.5
    while delay > 0.08:
        await send_color("Set_1", *GREEN);  await send_color("Set_2", *PURPLE)
        await asyncio.sleep(delay)
        await send_color("Set_1", *PURPLE); await send_color("Set_2", *GREEN)
        await asyncio.sleep(delay)
        delay *= 0.9
    while True:
        await send_color("Set_1", *GREEN);  await send_color("Set_2", *PURPLE)
        await asyncio.sleep(1)

async def pattern_lilin_breath():
    print("Running Pulse of the Lilin...")
    steps = 60
    while True:
        for i in range(steps):
            r = i / steps
            await send_color("Set_1", GREEN[0]*r,       GREEN[1]*r,       GREEN[2]*r)
            await send_color("Set_2", PURPLE[0]*(1-r),  PURPLE[1]*(1-r),  PURPLE[2]*(1-r))
            await asyncio.sleep(0.10)
        for i in range(steps):
            r = i / steps
            await send_color("Set_1", GREEN[0]*(1-r),   GREEN[1]*(1-r),   GREEN[2]*(1-r))
            await send_color("Set_2", PURPLE[0]*r,      PURPLE[1]*r,      PURPLE[2]*r)
            await asyncio.sleep(0.10)

async def pattern_emergency():
    print("EMERGENCY: BERSERK MODE")
    while True:
        await send_color("Set_1", *RED);   await send_color("Set_2", *RED)
        await asyncio.sleep(0.5)
        await send_color("Set_1", *BLACK); await send_color("Set_2", *BLACK)
        await asyncio.sleep(0.5)

async def pattern_static():
    print("Standard Setup Confirmed.")
    while True:
        await send_color("Set_1", *GREEN)
        await send_color("Set_2", *PURPLE)
        await asyncio.sleep(10)

async def pattern_tidal_wave():
    print("Running Tidal Wave...")
    t, SPEED, PHASE = 0.0, 0.035, math.pi * 0.75
    def wave_color(w):
        w = max(0.0, min(1.0, w))
        if w < 0.5:
            r = w * 2
            return tuple(GREEN[i]*(1-r) + OCEAN[i]*r for i in range(3))
        else:
            r = (w-0.5)*2
            return tuple(OCEAN[i]*(1-r) + PURPLE[i]*r for i in range(3))
    while True:
        def mw(ph):
            raw = math.sin(t+ph)*0.65 + math.sin(t*1.8+ph+0.9)*0.35
            return (raw+1)/2
        await send_color("Set_1", *wave_color(mw(0)))
        await send_color("Set_2", *wave_color(mw(PHASE)))
        t += SPEED
        await asyncio.sleep(0.05)

async def pattern_evangelion():
    """
    EVA-01 Awakening sequence:
    Both lights pulse white → surge to Unit-01 purple/green → hold.
    Loops with growing intensity.
    """
    print("EVA-01 AWAKENING SEQUENCE")
    t = 0.0
    while True:
        # Heartbeat-style surge on a sin² envelope
        env = math.sin(t * 0.8) ** 2
        intensity = 0.2 + 0.8 * env
        r1 = tuple(int(c * intensity) for c in GREEN)
        r2 = tuple(int(c * intensity) for c in PURPLE)
        # White flash at peak
        if env > 0.92:
            await send_color("Set_1", *WHITE); await send_color("Set_2", *WHITE)
        else:
            await send_color("Set_1", *r1);    await send_color("Set_2", *r2)
        t += 0.07
        await asyncio.sleep(0.05)


# ── HARDWARE PATTERNS (Triones only) ─────────────────────
# These send one command; the device handles animation internally.
# fff0 devices fall back to software equivalents.

async def pattern_hw_rainbow():
    """Hardware smooth rainbow on triones, software tidal on fff0."""
    print("Running NERV Rainbow (hardware on triones)...")
    await send_mode("Set_1", HWMode.SMOOTH_RAINBOW, speed=0x40)
    # fff0 fallback: software tidal
    t, SPEED, PHASE = 0.0, 0.035, math.pi * 0.75
    def wave_color(w):
        w = max(0.0, min(1.0, w))
        if w < 0.5:
            r = w * 2; return tuple(GREEN[i]*(1-r)+OCEAN[i]*r for i in range(3))
        else:
            r = (w-0.5)*2; return tuple(OCEAN[i]*(1-r)+PURPLE[i]*r for i in range(3))
    while True:
        raw = (math.sin(t)*0.65 + math.sin(t*1.8+0.9)*0.35 + 1) / 2
        await send_color("Set_2", *wave_color(raw))
        t += SPEED
        await asyncio.sleep(0.05)

async def pattern_hw_rgb_strobe():
    """Hardware RGB strobe on triones, software berserk on fff0."""
    print("Running SEELE Strobe (hardware on triones)...")
    await send_mode("Set_1", HWMode.RGB_STROBE, speed=0x10)
    while True:
        await send_color("Set_2", *RED);   await asyncio.sleep(0.4)
        await send_color("Set_2", *BLACK); await asyncio.sleep(0.4)


# ── PATTERN MAP ───────────────────────────────────────────

PATTERN_MAP = {
    "synchro":    pattern_synchro_test,
    "breath":     pattern_lilin_breath,
    "berserk":    pattern_emergency,
    "static":     pattern_static,
    "tidal":      pattern_tidal_wave,
    "evangelion": pattern_evangelion,
    "rainbow":    pattern_hw_rainbow,
    "strobe":     pattern_hw_rgb_strobe,
}


# ── PATTERN SWITCHING ─────────────────────────────────────

async def switch_to(fn):
    global current_task
    if current_task and not current_task.done():
        current_task.cancel()
        try:    await current_task
        except: pass
    current_task = asyncio.create_task(fn())

async def _rejoin():
    await asyncio.sleep(1)
    if current_pattern in PATTERN_MAP:
        await power_on_all()
        await switch_to(PATTERN_MAP[current_pattern])

async def _boot_activate():
    global current_pattern
    await asyncio.sleep(2)
    current_pattern = BOOT_PATTERN
    await power_on_all()
    await switch_to(PATTERN_MAP[BOOT_PATTERN])


# ── CONNECTION MANAGER ────────────────────────────────────
# One independent task per device.
# Scans → connects with BLEDevice object (most reliable on Windows) → monitors.

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


# ── HTTP HANDLERS ─────────────────────────────────────────

async def handle_options(request):
    return cors(web.Response(status=204))

async def handle_status(request):
    return cors(web.json_response({
        "pattern":   current_pattern,
        "connected": any_connected(),
        "devices":   connection_status()
    }))

async def handle_pattern(request):
    global current_pattern
    if not any_connected():
        return cors(web.json_response({"error": "no devices connected"}, status=503))
    name = request.match_info["pattern"]
    if name not in PATTERN_MAP:
        return cors(web.json_response({"error": "unknown pattern"}, status=404))
    print(f"Switching to: {name}")
    current_pattern = name
    await power_on_all()
    await switch_to(PATTERN_MAP[name])
    return cors(web.json_response({"status": "ok", "pattern": name}))

async def handle_color(request):
    """Set a static custom colour on both lights."""
    global current_pattern
    if not any_connected():
        return cors(web.json_response({"error": "no devices connected"}, status=503))
    try:
        body = await request.json()
        r = int(body.get("r", 255))
        g = int(body.get("g", 255))
        b = int(body.get("b", 255))
    except Exception:
        return cors(web.json_response({"error": "bad body"}, status=400))

    if current_task and not current_task.done():
        current_task.cancel()
        try: await current_task
        except: pass
    current_pattern = "custom"
    await power_on_all()
    await send_color("Set_1", r, g, b)
    await send_color("Set_2", r, g, b)
    return cors(web.json_response({"status": "ok", "r": r, "g": g, "b": b}))

async def handle_shutdown(request):
    global current_pattern
    if current_task and not current_task.done():
        current_task.cancel()
        try: await current_task
        except: pass
    current_pattern = "idle"
    await power_off_all()
    print("System Offline.")
    return cors(web.json_response({"status": "ok", "pattern": "idle"}))


# ── MAIN ──────────────────────────────────────────────────

async def main():
    app = web.Application()
    for method in ("OPTIONS", "GET", "POST"):
        app.router.add_route(method, "/status",            handle_options if method=="OPTIONS" else handle_status)
        app.router.add_route(method, "/shutdown",          handle_options if method=="OPTIONS" else handle_shutdown)
    app.router.add_route("OPTIONS", "/pattern/{pattern}", handle_options)
    app.router.add_route("POST",    "/pattern/{pattern}", handle_pattern)
    app.router.add_route("OPTIONS", "/color",             handle_options)
    app.router.add_route("POST",    "/color",             handle_color)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "localhost", 8765).start()

    print("\n══════════════════════════════════")
    print("  TERMINAL DOGMA — MAGI v2 ONLINE")
    print("  Server: http://localhost:8765")
    print("  Patterns:", ", ".join(PATTERN_MAP.keys()))
    print("══════════════════════════════════\n")

    for key in LIGHTS:
        asyncio.create_task(manage_device(key))

    while True:
        await asyncio.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MAGI BLE Light Controller")
    parser.add_argument("--pattern", choices=list(PATTERN_MAP.keys()), default=None,
                        help="Pattern to activate on boot")
    args = parser.parse_args()
    BOOT_PATTERN = args.pattern

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nManual Override Engaged.")
