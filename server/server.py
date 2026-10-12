"""Minimal Velia Playground room server. No characters, movement, or chat."""
import asyncio
import json
import os
import uuid
from collections import defaultdict
import websockets
from websockets.exceptions import ConnectionClosed

HOST=os.environ.get("VELIA_HOST","0.0.0.0")
PORT=int(os.environ.get("VELIA_PORT","8765"))
rooms=defaultdict(dict)

def clean(value,limit):
    return str(value or "").strip()[:limit]

async def send(ws,kind,data=None,message=None):
    msg={"type":kind}
    if data is not None:msg["data"]=data
    if message:msg["message"]=message
    await ws.send(json.dumps(msg,ensure_ascii=False))

async def broadcast(room,kind,data,exclude=None):
    for ws in list(rooms.get(room,{})):
        if ws is exclude:continue
        try:await send(ws,kind,data)
        except (ConnectionClosed,OSError): pass

async def handler(ws):
    room=None
    user=None
    try:
        raw=await asyncio.wait_for(ws.recv(),timeout=15)
        packet=json.loads(raw)
        if not isinstance(packet,dict) or packet.get("type")!="join":
            await send(ws,"error",message="First packet must be join")
            return
        data=packet.get("data") or {}
        room=clean(data.get("room_id"),48) or "public-01"
        name=clean(data.get("username"),24)
        if not name:
            await send(ws,"error",message="Nickname is required")
            return
        if any(member["username"]==name for member in rooms[room].values()):
            await send(ws,"error",message="Nickname already used in room")
            return
        user={"id":uuid.uuid4().hex,"username":name}
        previous=list(rooms[room].values())
        rooms[room][ws]=user
        await send(ws,"welcome",{"room_id":room,"members":previous})
        await broadcast(room,"member_joined",user,ws)
        print(f"JOIN {room}: {name}",flush=True)
        async for raw in ws:
            # Reserved for future protocol. No role state or messaging permitted.
            try:json.loads(raw)
            except (ValueError,TypeError):continue
    except (asyncio.TimeoutError,ConnectionClosed):
        pass
    except (ValueError,TypeError) as exc:
        try:await send(ws,"error",message=f"Invalid message: {exc}")
        except ConnectionClosed:pass
    finally:
        if room and user:
            rooms[room].pop(ws,None)
            await broadcast(room,"member_left",user)
            if not rooms[room]:rooms.pop(room,None)
            print(f"LEAVE {room}: {user['username']}",flush=True)

async def main():
    async with websockets.serve(handler,HOST,PORT,max_size=65536,ping_interval=20,ping_timeout=20):
        print(f"VELIA ROOM SERVER READY ws://127.0.0.1:{PORT}",flush=True)
        await asyncio.Future()

if __name__=="__main__":
    try:asyncio.run(main())
    except KeyboardInterrupt:pass
