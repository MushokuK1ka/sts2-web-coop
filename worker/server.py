"""Local browser gateway for independent STS2 game instances."""
import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass, field
import json
from pathlib import Path
import secrets
import struct
import time

from aiohttp import web, WSMsgType

BASE = Path(__file__).resolve().parent
CONFIG = BASE / 'access.json'
if os.getenv('WEB_ACCESS_KEYS'):
    CONFIG.write_text(os.environ['WEB_ACCESS_KEYS'], 'utf-8')
if not CONFIG.exists():
    CONFIG.write_text(json.dumps({s:secrets.token_urlsafe(24) for s in ('A','B')},indent=2), 'utf-8')
KEYS = json.loads(CONFIG.read_text('utf-8'))

@dataclass
class Seat:
    writer: object = None
    status: dict = field(default_factory=dict)
    frame: bytes = b''
    seq: int = 0
    updated: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    controller: object = None

seats = {s:Seat() for s in os.getenv('WEB_SEATS', 'A,B').split(',')}

async def send_bridge(seat, message):
    data = b'\x03' + json.dumps(message, separators=(',',':')).encode()
    async with seat.lock:
        writer = seat.writer
        if writer is None: return False
        try:
            writer.write(struct.pack('!I',len(data)) + data)
            await writer.drain()
            return True
        except (ConnectionError, OSError): return False

async def bridge(reader, writer):
    seat = None
    try:
        while True:
            size = struct.unpack('!I', await reader.readexactly(4))[0]
            if not 1 < size < 5_000_000: raise ValueError('invalid frame')
            packet = await reader.readexactly(size)
            if packet[0] == 2:
                status = json.loads(packet[1:])
                name = status.get('seat')
                if name not in seats: raise ValueError('invalid seat')
                if seat is None:
                    seat = seats[name]
                    if seat.writer and seat.writer is not writer: seat.writer.close()
                    seat.writer = writer
                    await send_bridge(seat, {'type':'watch','enabled':True})
                    print(f'Game {name} connected: PID {status.get("pid")}', flush=True)
                seat.status = status
                seat.updated = time.monotonic()
            elif packet[0] == 1 and seat:
                seat.frame = packet[1:]
                seat.seq += 1
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, OSError): pass
    finally:
        if seat and seat.writer is writer:
            seat.writer = None
        writer.close()
        with suppress(Exception): await writer.wait_closed()

def authorized(request):
    name = request.match_info['seat']
    token = request.headers.get('X-Access-Key') or request.query.get('key','')
    if name not in KEYS or not secrets.compare_digest(KEYS[name],token):
        raise web.HTTPForbidden(text='访问链接无效')
    return seats[name]

async def player(request):
    if request.match_info['seat'] not in seats: raise web.HTTPNotFound()
    return web.FileResponse(BASE/'web/player.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})

async def status(request):
    seat = authorized(request)
    return web.json_response({**seat.status,'connected':seat.writer is not None,
                              'frames':seat.seq,'age':round(time.monotonic()-seat.updated,2)})

async def frame(request):
    seat = authorized(request)
    if not seat.frame: raise web.HTTPServiceUnavailable(text='等待游戏画面')
    return web.Response(body=seat.frame,content_type='image/jpeg',headers={'Cache-Control':'no-store'})

def validate_input(e):
    t = e.get('type')
    if t in ('move','down','up','wheel'):
        for key in ('x','y'):
            if type(e.get(key)) not in (float,int) or not 0 <= e[key] <= 1: return False
        if t in ('down','up') and e.get('button',0) not in (0,2): return False
        if t == 'wheel' and type(e.get('delta')) not in (float,int): return False
    elif t == 'key':
        if e.get('key') not in ['Escape','Enter',' ','m','M','d','D','a','A','s','S','e','E','1','2','3','4','5','6','7','8','9','0']: return False
        if type(e.get('pressed')) is not bool: return False
    elif t != 'release': return False
    return True

async def websocket(request):
    seat = authorized(request)
    # Cross-site pages must not acquire a player's controller.
    origin = request.headers.get('Origin')
    if origin and origin.split('://',1)[-1] != request.host: raise web.HTTPForbidden()
    if seat.controller is not None and not seat.controller.closed:
        raise web.HTTPConflict(text='这个席位已在其他页面连接，请先关闭原页面')
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4096)
    await ws.prepare(request)
    seat.controller = ws
    ack = asyncio.Event(); ack.set()
    async def push_frames():
        seq = -1
        last_status = 0
        while not ws.closed:
            now = time.monotonic()
            if now-last_status > 1:
                await ws.send_json({'type':'status','connected':seat.writer is not None,'seat':request.match_info['seat']})
                last_status = now
            if seat.frame and seq != seat.seq and ack.is_set():
                ack.clear()
                seq = seat.seq
                await ws.send_bytes(seat.frame)
            await asyncio.sleep(0.008)
    sender = asyncio.create_task(push_frames())
    try:
        count, start = 0, time.monotonic()
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try: e = json.loads(msg.data)
                except (ValueError,TypeError): continue
                if not isinstance(e,dict): continue
                if e.get('type') == 'ack': ack.set(); continue
                now = time.monotonic()
                if now-start >= 1: count,start = 0,now
                count += 1
                if count <= 160 and validate_input(e): await send_bridge(seat,e)
            elif msg.type == WSMsgType.ERROR: break
    finally:
        sender.cancel()
        with suppress(asyncio.CancelledError, ConnectionError): await sender
        await send_bridge(seat,{'type':'release'})
        if seat.controller is ws: seat.controller = None
    return ws

async def index(request):
    return web.Response(text='尖塔网页联机测试服务已启动。请使用主机提供的玩家专属链接。',content_type='text/plain',charset='utf-8')

async def main():
    server = await asyncio.start_server(bridge,'127.0.0.1',7861)
    app = web.Application(client_max_size=4096)
    app.add_routes([web.get('/',index),web.get('/player/{seat}',player),
                    web.get('/api/{seat}/status',status),web.get('/api/{seat}/frame',frame),web.get('/ws/{seat}',websocket)])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner,'0.0.0.0',7860).start()
    print('Web gateway ready on port 7860; private bridge 127.0.0.1:7861',flush=True)
    async with server: await server.serve_forever()

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
