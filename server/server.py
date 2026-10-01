"""Velia Multiplayer Playground WebSocket server — diagnostic build.

This version prints every startup step and every join/leave event so a failed
server can no longer disappear silently behind `pause`.
"""

import asyncio
import json
import os
import socket
import sys
import time
import uuid
from collections import defaultdict

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except Exception as exc:
    print("[FATAL] Could not import 'websockets'.", flush=True)
    print("[FATAL] Run: python -m pip install \"websockets>=14,<16\"", flush=True)
    print(f"[FATAL] {type(exc).__name__}: {exc}", flush=True)
    raise

HOST = os.environ.get("VELIA_HOST", "0.0.0.0")
PORT = int(os.environ.get("VELIA_PORT", "8765"))

ALLOWED_OPERATORS = {
    "lmun", "ishar-mla", "spuria", "gla-dia", "specter", "ulpianus",
}
ALLOWED_ANIMATIONS = {"move", "relax"}
rooms = defaultdict(dict)


def log(message):
    print(f"[Velia Server] {message}", flush=True)


def local_ipv4():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def clean_text(value, limit):
    return str(value or "").strip()[:limit]


def clamp(value, low, high, default):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


async def send_json(ws, payload):
    await ws.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


async def broadcast(room_id, payload, exclude=None):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    dead = []
    for ws in list(rooms.get(room_id, {})):
        if ws is exclude:
            continue
        try:
            await ws.send(encoded)
        except ConnectionClosed:
            dead.append(ws)
        except Exception as exc:
            log(f"broadcast error: {type(exc).__name__}: {exc}")
            dead.append(ws)
    for ws in dead:
        rooms[room_id].pop(ws, None)


def public_player(player):
    return {
        "player_id": player["player_id"],
        "username": player["username"],
        "operator": player["operator"],
        "x": player["x"],
        "depth": player["depth"],
        "facing": player["facing"],
        "animation": player["animation"],
    }


async def handler(ws, *legacy_args):
    room_id = None
    player = None
    peer = getattr(ws, "remote_address", None)
    log(f"connection opened from {peer}")

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
        packet = json.loads(raw)
        if not isinstance(packet, dict) or packet.get("type") != "join":
            await send_json(ws, {
                "type": "error",
                "message": "Expected join packet as first message.",
            })
            log(f"rejected {peer}: first packet was not join")
            return

        data = packet.get("data") or {}
        room_id = clean_text(data.get("room_id", "public-01"), 48) or "public-01"
        username = clean_text(data.get("username"), 24)
        operator = clean_text(data.get("operator"), 48).casefold()

        if not username:
            await send_json(ws, {"type": "error", "message": "Nickname is required."})
            return
        if operator not in ALLOWED_OPERATORS:
            await send_json(ws, {
                "type": "error",
                "message": f"Unknown operator: {operator}",
            })
            return

        player = {
            "player_id": uuid.uuid4().hex,
            "username": username,
            "operator": operator,
            "x": 0.50,
            "depth": 0.62,
            "facing": 1,
            "animation": "relax",
            "last_state": 0.0,
            "last_chat": 0.0,
        }

        existing = [public_player(p) for p in rooms[room_id].values()]
        rooms[room_id][ws] = player

        await send_json(ws, {
            "type": "welcome",
            "data": {
                "player_id": player["player_id"],
                "room_id": room_id,
                "players": existing,
            },
        })
        await broadcast(
            room_id,
            {"type": "player_joined", "data": public_player(player)},
            exclude=ws,
        )
        log(
            f"JOIN room={room_id} user={username!r} operator={operator} "
            f"players={len(rooms[room_id])}"
        )

        async for raw in ws:
            try:
                packet = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(packet, dict):
                continue

            kind = packet.get("type")
            data = packet.get("data") or {}
            now = time.monotonic()

            if kind == "state":
                if now - player["last_state"] < 0.03:
                    continue
                player["last_state"] = now
                player["x"] = clamp(data.get("x"), 0.07, 0.93, player["x"])
                player["depth"] = clamp(
                    data.get("depth"), 0.10, 0.93, player["depth"]
                )
                try:
                    facing = int(data.get("facing", 1))
                except (TypeError, ValueError):
                    facing = 1
                player["facing"] = -1 if facing < 0 else 1

                animation = clean_text(data.get("animation"), 16).lower()
                if animation not in ALLOWED_ANIMATIONS:
                    animation = "relax"
                player["animation"] = animation

                await broadcast(
                    room_id,
                    {"type": "state", "data": public_player(player)},
                    exclude=ws,
                )

            elif kind == "chat":
                if now - player["last_chat"] < 0.28:
                    continue
                player["last_chat"] = now
                message = clean_text(data.get("message"), 500)
                message_id = clean_text(data.get("message_id"), 96)
                if not message:
                    continue
                await broadcast(
                    room_id,
                    {
                        "type": "chat",
                        "data": {
                            "player_id": player["player_id"],
                            "username": player["username"],
                            "message": message,
                            "message_id": message_id,
                        },
                    },
                    exclude=None,
                )
                log(f"CHAT room={room_id} user={username!r}: {message[:60]!r}")

    except asyncio.TimeoutError:
        log(f"connection timeout from {peer}")
    except ConnectionClosed:
        pass
    except Exception as exc:
        log(f"client error {peer}: {type(exc).__name__}: {exc}")
        try:
            await send_json(ws, {
                "type": "error",
                "message": f"{type(exc).__name__}: {exc}"[:200],
            })
        except Exception:
            pass
    finally:
        if room_id and player:
            rooms[room_id].pop(ws, None)
            await broadcast(
                room_id,
                {"type": "player_left", "data": {"player_id": player["player_id"]}},
                exclude=ws,
            )
            log(
                f"LEAVE room={room_id} user={player['username']!r} "
                f"players={len(rooms.get(room_id, {}))}"
            )
            if not rooms.get(room_id):
                rooms.pop(room_id, None)


async def main():
    log(f"Python: {sys.version.split()[0]}")
    log(f"websockets: {getattr(websockets, '__version__', 'unknown')}")
    log(f"binding to {HOST}:{PORT}")

    try:
        server = await websockets.serve(
            handler,
            HOST,
            PORT,
            max_size=128 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )
    except OSError as exc:
        log(f"FATAL: could not bind port {PORT}: {exc}")
        log("If another server is already using 8765, close it first.")
        raise

    log("SERVER READY")
    log(f"Same-PC client URL: ws://127.0.0.1:{PORT}")
    log(f"LAN client URL:     ws://{local_ipv4()}:{PORT}")
    log("Keep this window open while using Multiplayer Room.")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("stopped by user")
    except Exception as exc:
        log(f"SERVER STOPPED: {type(exc).__name__}: {exc}")
        raise
