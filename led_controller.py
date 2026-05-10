import asyncio
import traceback
import argparse
from aiohttp import web
from bleak import BleakClient, BleakScanner

# --- DEVICE DATABASE ---
LIGHTS = {
    "Set_1": {
        "name": "QHM (Green)",
        "address": "36:46:40:1F:54:0C",
        "uuid": "0000ffd9-0000-1000-8000-00805f9b34fb",
        "type": "triones",
        "cmd_on":  "cc2333",
        "cmd_off": "cc2433"
    },
    "Set_2": {
        "name": "Melk (Purple)",
        "address": "BE:28:05:00:09:3B",
        "uuid": "0000fff3-0000-1000-8000-00805f9b34fb",
        "type": "fff0",
        "cmd_on":  "7e00040100000000ef",
        "cmd_off": "7e00040000000000ef"
    }
}

PURPLE = (128, 0, 255)
GREEN  = (57, 255, 20)
RED    = (255, 0, 0)
BLACK  = (0, 0, 0)

clients         = {}
current_task    = None
current_pattern = "idle"
BOOT_PATTERN    = None   # set by argparse
_boot_triggered = False  # fires once on first connect


# ── HELPERS ────────────────────────────────────────────────

def get_hex(r, g, b, p_type):
    r, g, b = max(0,min(255,int(r))), max(0,min(255,int(g))), max(0,min(255,int(b)))
    if p_type == "triones":
        return f"56{r:02x}{g:02x}{b:02x}00f0aa"
    return f"7e000503{r:02x}{g:02x}{b:02x}00ef"

async def send_raw(key, hex_cmd):
    """Send a raw hex command to a device."""
    client = clients.get(key)
    if not client or not client.is_connected:
        return
    try:
        await client.write_gatt_char(
            LIGHTS[key]["uuid"],
            bytes.fromhex(hex_cmd),
            response=False
        )
    except Exception:
        pass

async def send_color(key, r, g, b):
    client = clients.get(key)
    if not client or not client.is_connected:
        return
    try:
        await client.write_gatt_char(
            LIGHTS[key]["uuid"],
            bytes.fromhex(get_hex(r, g, b, LIGHTS[key]["type"])),
            response=False
        )
    except Exception:
        pass

async def power_on_all():
    """Send power-on command to every connected light."""
    for key, cfg in LIGHTS.items():
        print(f"[{key}] Powering on...")
        await send_raw(key, cfg["cmd_on"])
    await asyncio.sleep(0.3)   # give them a moment to wake up

async def power_off_all():
    """Send power-off command to every connected light."""
    for key, cfg in LIGHTS.items():
        await send_raw(key, cfg["cmd_off"])

def any_connected():
    return any(c and c.is_connected for c in clients.values())

def connection_status():
    return {k: bool(clients.get(k) and clients[k].is_connected) for k in LIGHTS}


# ── PATTERNS ───────────────────────────────────────────────

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
            await send_color("Set_1", GREEN[0]*r,      GREEN[1]*r,      GREEN[2]*r)
            await send_color("Set_2", PURPLE[0]*(1-r), PURPLE[1]*(1-r), PURPLE[2]*(1-r))
            await asyncio.sleep(0.12)
        for i in range(steps):
            r = i / steps
            await send_color("Set_1", GREEN[0]*(1-r),  GREEN[1]*(1-r),  GREEN[2]*(1-r))
            await send_color("Set_2", PURPLE[0]*r,     PURPLE[1]*r,     PURPLE[2]*r)
            await asyncio.sleep(0.12)

async def pattern_emergency():
    print("EMERGENCY: BERSERK MODE")
    while True:
        await send_color("Set_1", *RED);   await send_color("Set_2", *RED)
        await asyncio.sleep(0.6)
        await send_color("Set_1", *BLACK); await send_color("Set_2", *BLACK)
        await asyncio.sleep(0.6)

async def pattern_static():
    print("Standard Setup Confirmed.")
    while True:
        await send_color("Set_1", *GREEN)
        await send_color("Set_2", *PURPLE)
        await asyncio.sleep(10)

PATTERN_MAP = {
    "synchro": pattern_synchro_test,
    "breath":  pattern_lilin_breath,
    "berserk": pattern_emergency,
    "static":  pattern_static,
}


# ── PATTERN SWITCHING ──────────────────────────────────────

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
    await asyncio.sleep(2)   # wait for both lights to finish connecting
    current_pattern = BOOT_PATTERN
    await power_on_all()
    await switch_to(PATTERN_MAP[BOOT_PATTERN])

# ── CONNECTION MANAGER ─────────────────────────────────────

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
                print(f"[{key}] Not found. Retrying in {retry}s...")
                await asyncio.sleep(retry)
                retry = min(retry * 2, 30)
                continue

            print(f"[{key}] Found: {found.name} — connecting...")
            client = BleakClient(found)
            clients[key] = client
            await client.connect(timeout=15)
            print(f"[{key}] Connected ✓")
            retry = 3

            # Trigger boot pattern once on first successful connection
            global _boot_triggered
            if BOOT_PATTERN and not _boot_triggered:
                _boot_triggered = True
                print(f"Boot pattern: {BOOT_PATTERN}")
                asyncio.create_task(_boot_activate())

            # If a pattern is active, power on and rejoin
            if current_pattern != "idle":
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


# ── CORS ───────────────────────────────────────────────────

def cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return r


# ── HTTP HANDLERS ──────────────────────────────────────────

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

    # Always power on before starting a pattern
    await power_on_all()
    await switch_to(PATTERN_MAP[name])

    return cors(web.json_response({"status": "ok", "pattern": name}))

async def handle_shutdown(request):
    global current_pattern

    if current_task and not current_task.done():
        current_task.cancel()
        try:    await current_task
        except: pass

    current_pattern = "idle"
    await power_off_all()
    print("System Offline.")
    return cors(web.json_response({"status": "ok", "pattern": "idle"}))


# ── MAIN ───────────────────────────────────────────────────

async def main():
    app = web.Application()
    app.router.add_route("OPTIONS", "/status",            handle_options)
    app.router.add_route("OPTIONS", "/pattern/{pattern}", handle_options)
    app.router.add_route("OPTIONS", "/shutdown",          handle_options)
    app.router.add_get( "/status",            handle_status)
    app.router.add_post("/pattern/{pattern}", handle_pattern)
    app.router.add_post("/shutdown",          handle_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "localhost", 8765).start()

    print("\n--- MAGI SYSTEM ONLINE ---")
    print("Server: http://localhost:8765")
    print("Open index.html — buttons unlock as lights connect.\n")

    for key in LIGHTS:
        asyncio.create_task(manage_device(key))

    while True:
        await asyncio.sleep(1)