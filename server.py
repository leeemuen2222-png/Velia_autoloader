"""Velia LAN room server, compatible with installed websockets 10–16."""
import asyncio
import json
import os
import sys
import uuid
import time
import websockets
from websockets.exceptions import ConnectionClosed

HOST = '0.0.0.0'
PORT = int(os.environ.get('VELIA_ROOM_PORT', '8765'))
INVITE = os.environ.get('VELIA_ROOM_CODE', '')
ROOM_NAME = os.environ.get('VELIA_ROOM_NAME', 'Velia LAN Room')
users = {}
host_id = None
host_entered = False
actor_states = {}

def clean(text, limit=32):
    return str(text or '').strip()[:limit]

async def send(ws, typ, data=None, message=None):
    payload = {'type': typ}
    if data is not None:
        payload['data'] = data
    if message:
        payload['message'] = message
    await ws.send(json.dumps(payload, ensure_ascii=False))

async def broadcast(typ, data=None):
    for ws in list(users):
        try:
            await send(ws, typ, data)
        except (ConnectionClosed, OSError):
            pass

def roster():
    return [{'id': u['id'], 'username': u['username'], 'is_host': u['is_host'], 'entered': u.get('entered', False)} for u in users.values()]

async def handler(ws):
    global host_id, host_entered, ROOM_NAME
    joined = False
    try:
        packet = json.loads(await asyncio.wait_for(ws.recv(), 20))
        if not isinstance(packet, dict) or packet.get('type') != 'join':
            await send(ws, 'error', message='First packet must be join')
            return
        data = packet.get('data') or {}
        if clean(data.get('room_id'), 32) != INVITE or not INVITE:
            await send(ws, 'error', message='Invalid invitation key')
            return
        is_host = bool(data.get('host'))
        if is_host and host_id is not None:
            await send(ws, 'error', message='Room already has a host')
            return
        name = clean(data.get('username'), 24) or '未知玩家'
        if is_host:
            ROOM_NAME = clean(data.get('room_name'), 48) or ROOM_NAME
        uid = uuid.uuid4().hex
        if is_host:
            host_id = uid
        users[ws] = {'id': uid, 'username': name, 'is_host': is_host, 'entered': False, 'last_actor': 0.0}
        joined = True
        await send(ws, 'welcome', {'room_id': INVITE, 'members': roster(), 'room_name': ROOM_NAME, 'host_entered': host_entered, 'is_host': is_host, 'your_id': uid, 'actors': actor_states})
        await broadcast('roster', {'members': roster(), 'host_entered': host_entered, 'room_name': ROOM_NAME})
        async for raw in ws:
            try:
                message = json.loads(raw)
            except (ValueError, TypeError):
                continue
            typ = message.get('type') if isinstance(message, dict) else ''
            if typ == 'host_entered' and is_host:
                host_entered = True
                await broadcast('host_state', {'host_entered': True})
            elif typ == 'entered' and (is_host or host_entered):
                users[ws]['entered'] = True
                await broadcast('roster', {'members': roster(), 'host_entered': host_entered, 'room_name': ROOM_NAME})
            elif typ == 'actor' and (is_host or host_entered):
                data = message.get('data') or {}
                if not isinstance(data, dict):
                    continue
                op = clean(data.get('operator'), 48)
                if not op or len(json.dumps(data)) > 1300:
                    continue
                now = time.monotonic()
                if now - users[ws]['last_actor'] < 0.035:
                    continue
                users[ws]['last_actor'] = now
                try:
                    x, z = float(data.get('x')), float(data.get('z'))
                except (TypeError, ValueError):
                    continue
                if not (0 <= x <= 10 and 0 <= z <= 10):
                    continue
                state = {'operator':op,'x':x,'z':z,
                         'skin':data.get('skin',1),'moving':bool(data.get('moving')),
                         'left':bool(data.get('left'))}
                actor_states[users[ws]['id']] = state
                await broadcast('actor', {'id':users[ws]['id'], 'state':state})
            elif typ == 'actor_clear' and users[ws].get('entered'):
                actor_states.pop(users[ws]['id'], None)
                await broadcast('actor_left', {'id':users[ws]['id']})
            elif typ == 'chat' and users[ws].get('entered'):
                text = clean((message.get('data') or {}).get('text'), 500)
                if text:
                    await broadcast('chat', {'id':users[ws]['id'],
                                             'username':users[ws]['username'], 'text':text})
            elif typ == 'action' and users[ws].get('entered'):
                expression = clean((message.get('data') or {}).get('expression'), 5000)
                if expression:
                    await broadcast('action', {'id':users[ws]['id'], 'expression':expression})
            elif typ == 'rename':
                newname = clean((message.get('data') or {}).get('username'), 24) or '未知玩家'
                users[ws]['username'] = newname
                await broadcast('roster', {'members': roster(), 'host_entered': host_entered, 'room_name': ROOM_NAME})
            elif typ == 'rename_room' and is_host:
                ROOM_NAME = clean((message.get('data') or {}).get('room_name'), 48) or ROOM_NAME
                await broadcast('roster', {'members': roster(), 'host_entered': host_entered, 'room_name': ROOM_NAME})
    except (asyncio.TimeoutError, ConnectionClosed, ValueError, TypeError):
        pass
    finally:
        if joined:
            info = users.pop(ws, None)
            if info:
                actor_states.pop(info['id'], None)
                await broadcast('actor_left', {'id': info['id']})
            if info and info['id'] == host_id:
                host_id = None
                host_entered = False
                await broadcast('host_state', {'host_entered': False})
            await broadcast('roster', {'members': roster(), 'host_entered': host_entered, 'room_name': ROOM_NAME})

async def main():
    async with websockets.serve(handler, HOST, PORT, max_size=65536, ping_interval=20, ping_timeout=20):
        print(f'VELIA_LAN_READY:{PORT}', flush=True)
        await asyncio.Future()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except OSError as e:
        print(f'VELIA_LAN_ERROR:{e}', file=sys.stderr, flush=True)
        sys.exit(1)
