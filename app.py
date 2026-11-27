"""Room lobby and browser gateway for Docker and native Windows workers."""
import asyncio
from contextlib import suppress
import functools
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import sqlite3
import time

from aiohttp import web, ClientSession, WSMsgType

BASE = Path(__file__).resolve().parent
LETTERS = 'ABCD'


class Rooms:
    def __init__(self, path, max_rooms=4):
        self.db = sqlite3.connect(str(path))
        self.db.execute('CREATE TABLE IF NOT EXISTS rooms (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.rooms = {rid: json.loads(data) for rid, data in self.db.execute('SELECT id,data FROM rooms')}
        self.max_rooms = max_rooms

    def save(self, room):
        self.db.execute('INSERT OR REPLACE INTO rooms VALUES (?,?)', (room['id'], json.dumps(room)))
        self.db.commit()

    def mine(self, user):
        return [r for r in self.rooms.values() if r['state'] != 'closed' and any(m['user'] == user for m in r['members'])]

    def create(self, user, name, capacity):
        if type(capacity) is not int or not 2 <= capacity <= 4:
            raise web.HTTPBadRequest(text='人数必须为 2 至 4 人')
        if self.mine(user):
            raise web.HTTPConflict(text='请先关闭或离开当前房间')
        if sum(r['state'] != 'closed' for r in self.rooms.values()) >= self.max_rooms:
            raise web.HTTPConflict(text='服务器房间已满')
        room = {'id': secrets.token_hex(8), 'code': secrets.token_hex(3).upper(), 'owner': user,
                'capacity': capacity, 'state': 'waiting', 'created': time.time(), 'members': [], 'worker': None}
        while any(r['code'] == room['code'] for r in self.rooms.values()):
            room['code'] = secrets.token_hex(3).upper()
        self.rooms[room['id']] = room
        self.add(room, user, name)
        return room

    def add(self, room, user, name):
        if any(m['user'] == user for m in room['members']):
            return room
        if room['state'] != 'waiting':
            raise web.HTTPConflict(text='房间已进入游戏，请使用原来的浏览器重连')
        if len(room['members']) >= room['capacity']:
            raise web.HTTPConflict(text='房间已满')
        if self.mine(user):
            raise web.HTTPConflict(text='你已在另一个房间中')
        used = {m['seat'] for m in room['members']}
        seat = next(s for s in LETTERS if s not in used)
        room['members'].append({'user': user, 'name': str(name).strip()[:24] or '玩家',
                                'seat': seat, 'key': secrets.token_urlsafe(24)})
        self.save(room)
        return room

    @staticmethod
    def public(room, user):
        me = next((m for m in room['members'] if m['user'] == user), None)
        return {'id': room['id'], 'code': room['code'], 'capacity': room['capacity'], 'state': room['state'],
                'owner': room['owner'] == user, 'members': [{'name': m['name'], 'seat': m['seat'],
                'owner': m['user'] == room['owner']} for m in room['members']],
                'my_seat': me['seat'] if me else None,
                'play_url': '/play/{}/{}'.format(room['id'], me['seat']) if me and room['state'] == 'playing' else None}


class DockerWorkers:
    def __init__(self, state):
        self.state = state
        self.image = os.getenv('WORKER_IMAGE', 'sts2-web-worker:0.3')
        self.game = os.getenv('GAME_ROOT', str(state / 'game-linux'))
        self.network = os.getenv('WORKER_NETWORK', 'sts2-web-coop')
        self.root = os.getenv('DOCKER_STATE_ROOT', str(state))

    def client(self):
        import docker
        return docker.from_env(timeout=15)

    def preflight(self):
        try:
            client = self.client()
            client.ping()
        except Exception:
            return {'ready': False, 'reason': '容器运行权限尚未就绪'}
        candidates = [Path(self.game) / name for name in ('SlayTheSpire2', 'SlayTheSpire2.x86_64', 'SlayTheSpire2.exe')]
        executable = next((p for p in candidates if p.is_file()), None)
        if executable is None:
            return {'ready': False, 'reason': '等待配置匹配版本的游戏文件'}
        with executable.open('rb') as handle:
            magic = handle.read(4)
            if magic != b'\x7fELF' and not (executable.suffix == '.exe' and magic[:2] == b'MZ'):
                return {'ready': False, 'reason': '游戏可执行文件格式不正确'}
        try:
            version = json.loads((Path(self.game) / 'release_info.json').read_text('utf-8'))['version']
        except (OSError, ValueError, KeyError):
            return {'ready': False, 'reason': '无法确认游戏版本'}
        if version.lstrip('v') != '0.107.1':
            return {'ready': False, 'reason': '当前桥接包需要 0.107.1 版游戏'}
        try:
            client.images.get(self.image)
        except Exception:
            return {'ready': False, 'reason': '等待构建游戏运行镜像'}
        return {'ready': True, 'reason': ''}

    def start(self, room):
        check = self.preflight()
        if not check['ready']:
            raise RuntimeError(check['reason'])
        client = self.client()
        try:
            client.networks.get(self.network)
        except Exception:
            client.networks.create(self.network, driver='bridge')
        # The gateway sees /state; Docker bind sources are paths on the host.
        (self.state / 'rooms' / room['id']).mkdir(parents=True, exist_ok=True)
        save_dir = PurePosixPath(self.root) / 'rooms' / room['id']
        name = 'sts2-room-' + room['id']
        keys = {m['seat']: m['key'] for m in room['members']}
        members = sorted(room['members'], key=lambda m: m['seat'])
        kwargs = dict(image=self.image, name=name, detach=True, network=self.network,
            labels={'sts2.webcoop': 'worker', 'sts2.room': room['id']},
            environment={'WEB_SEATS': ','.join(m['seat'] for m in members), 'WEB_ACCESS_KEYS': json.dumps(keys),
                         'ROOM_MEMBERS': json.dumps([{'seat': m['seat'], 'name': m['name']} for m in members]),
                         'SOFTWARE_RENDER': os.getenv('SOFTWARE_RENDER', '1'),
                         'GAME_RUNTIME': os.getenv('GAME_RUNTIME', 'auto')},
            volumes={self.game: {'bind': '/opt/game', 'mode': 'ro'},
                     str(save_dir): {'bind': '/data', 'mode': 'rw'}},
            shm_size='1g', mem_limit=os.getenv('ROOM_MEMORY_LIMIT', '12g'),
            nano_cpus=int(float(os.getenv('ROOM_CPU_LIMIT', '8')) * 1e9),
            security_opt=['no-new-privileges:true'], cap_drop=['ALL'],
            pids_limit=1024, restart_policy={'Name': 'no'})
        if os.getenv('SOFTWARE_RENDER', '1') != '1':
            driver_root = os.getenv('GPU_DRIVER_ROOT')
            if not driver_root:
                raise RuntimeError('GPU 驱动目录尚未配置')
            kwargs['volumes'][driver_root] = {'bind': '/opt/nvidia', 'mode': 'ro'}
            kwargs['devices'] = ['/dev/nvidia0:/dev/nvidia0:rw', '/dev/nvidiactl:/dev/nvidiactl:rw']
        try:
            container = client.containers.run(**kwargs)
            container.reload()
            address = container.attrs['NetworkSettings']['Networks'][self.network]['IPAddress']
            return {'id': container.id, 'url': 'http://' + address + ':7860'}
        except Exception:
            # Roll back only a container belonging to this exact launch attempt.
            with suppress(Exception):
                orphan = client.containers.get(name)
                if orphan.labels.get('sts2.room') == room['id']:
                    orphan.remove(force=True)
            raise
        finally:
            client.close()

    def stop(self, room):
        if not room.get('worker'):
            return
        import docker
        client = self.client()
        try:
            try:
                container = client.containers.get(room['worker']['id'])
            except docker.errors.NotFound:
                return
            if container.labels.get('sts2.room') != room['id']:
                raise RuntimeError('容器身份检查失败')
            container.stop(timeout=15)
            container.remove()
        finally:
            client.close()


async def background_call(function, *args):
    return await asyncio.get_running_loop().run_in_executor(None, functools.partial(function, *args))


def create_app(state=None, workers=None, password=None):
    state = Path(state or os.getenv('STATE_DIR', BASE / 'state'))
    state.mkdir(parents=True, exist_ok=True)
    secret_file = state / 'server.key'
    if not secret_file.exists():
        secret_file.write_text(secrets.token_hex(32), encoding='utf-8')
        secret_file.chmod(0o600)
    secret = secret_file.read_text('utf-8').strip().encode()
    password_file = state / 'access-password.txt'
    if password is None:
        password = os.getenv('SITE_PASSWORD')
        if not password:
            if not password_file.exists():
                password_file.write_text(secrets.token_urlsafe(12), 'utf-8')
                password_file.chmod(0o600)
            password = password_file.read_text('utf-8').strip()
    if workers is None and os.name == 'nt':
        from windows_worker import WindowsWorkers
        workers = WindowsWorkers(state)
    runner = workers or DockerWorkers(state)
    room_limit = getattr(runner, 'max_rooms', int(os.getenv('MAX_ROOMS', '4')))
    rooms = Rooms(state / 'rooms.sqlite', room_limit)
    app = web.Application(client_max_size=16 * 1024)
    app['rooms'], app['workers'], app['tasks'] = rooms, runner, set()

    def csrf(user):
        return hmac.new(secret, ('csrf:' + user).encode(), hashlib.sha256).hexdigest()

    def token(user):
        signature = hmac.new(secret, user.encode(), hashlib.sha256).hexdigest()
        return user + '.' + signature

    def user(request):
        cookie = request.cookies.get('sts2_session', '')
        try:
            ident, signature = cookie.split('.')
        except ValueError:
            raise web.HTTPUnauthorized(text='请先输入访问口令')
        if len(ident) != 32 or not hmac.compare_digest(token(ident), cookie):
            raise web.HTTPUnauthorized(text='请重新登录')
        return ident

    def mutation(request):
        ident = user(request)
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf(ident)):
            raise web.HTTPForbidden(text='请求校验失败，请刷新页面')
        return ident

    def member_room(request, ident):
        room = rooms.rooms.get(request.match_info['room'])
        if not room or not any(m['user'] == ident for m in room['members']):
            raise web.HTTPNotFound(text='找不到你的房间')
        return room

    async def index(request):
        return web.FileResponse(BASE / 'web' / 'lobby.html', headers={'Cache-Control': 'no-store'})

    async def login(request):
        data = await request.json()
        supplied = str(data.get('password', ''))
        if not hmac.compare_digest(supplied.encode(), password.encode()):
            raise web.HTTPUnauthorized(text='访问口令错误')
        ident = secrets.token_hex(16)
        response = web.json_response({'csrf': csrf(ident)})
        response.set_cookie('sts2_session', token(ident), httponly=True, samesite='Strict',
                            secure=os.getenv('SECURE_COOKIES') == '1', max_age=7 * 86400)
        return response

    async def session(request):
        ident = user(request)
        status = await background_call(runner.preflight)
        return web.json_response({'csrf': csrf(ident), 'runtime': status,
                                 'rooms': [rooms.public(r, ident) for r in rooms.mine(ident)]})

    async def create(request):
        ident = mutation(request)
        data = await request.json()
        return web.json_response(rooms.public(rooms.create(ident, data.get('name', ''), data.get('capacity', 2)), ident))

    async def join(request):
        ident = mutation(request)
        data = await request.json()
        code = str(data.get('code', '')).strip().upper()
        room = next((r for r in rooms.rooms.values() if r['code'] == code and r['state'] != 'closed'), None)
        if not room:
            raise web.HTTPNotFound(text='房间码不存在')
        return web.json_response(rooms.public(rooms.add(room, ident, data.get('name', '')), ident))

    async def get_room(request):
        ident = user(request)
        return web.json_response(rooms.public(member_room(request, ident), ident))

    async def wait_worker(room):
        try:
            for _ in range(300):
                if room['state'] != 'starting':
                    return
                if hasattr(runner, 'is_alive') and not runner.is_alive(room):
                    break
                ready = True
                for m in room['members']:
                    try:
                        async with app['http'].get(room['worker']['url'] + '/api/' + m['seat'] + '/status',
                                headers={'X-Access-Key': m['key']}, timeout=3) as response:
                            status = await response.json()
                            seat_ready = bool(status.get('connected')) and status.get('scene') in ('NMainMenu', 'NRun')
                            if getattr(runner, 'requires_character_select', False):
                                seat_ready = seat_ready and 'NCharacterSelectScreen' in status.get('screens', [])
                            ready = ready and seat_ready
                    except Exception:
                        ready = False
                if ready and room['state'] == 'starting':
                    room['state'] = 'playing'
                    rooms.save(room)
                    return
                await asyncio.sleep(1)
            if room['state'] == 'starting':
                if hasattr(runner, 'is_alive'):
                    await background_call(runner.stop, room)
                if room['state'] != 'starting':
                    return
                room['state'] = 'failed'
                rooms.save(room)
        except asyncio.CancelledError:
            raise

    async def start(request):
        ident = mutation(request)
        room = member_room(request, ident)
        if room['owner'] != ident:
            raise web.HTTPForbidden(text='只有房主可以开始')
        if room['state'] != 'waiting':
            raise web.HTTPConflict(text='房间已开始或正在准备')
        if len(room['members']) < 2:
            raise web.HTTPConflict(text='至少需要两名玩家')
        room['state'] = 'starting'
        rooms.save(room)
        try:
            room['worker'] = await background_call(runner.start, room)
        except Exception as ex:
            room['state'] = 'waiting'
            rooms.save(room)
            return web.json_response({'error': str(ex)}, status=503)
        rooms.save(room)
        task = asyncio.create_task(wait_worker(room))
        app['tasks'].add(task)
        task.add_done_callback(app['tasks'].discard)
        return web.json_response(rooms.public(room, ident))

    async def leave(request):
        ident = mutation(request)
        room = member_room(request, ident)
        if room['owner'] == ident:
            if room['state'] == 'starting' and not room.get('worker'):
                raise web.HTTPConflict(text='正在启动，请稍后关闭')
            await background_call(runner.stop, room)
            room['state'] = 'closed'
        elif room['state'] == 'waiting':
            room['members'] = [m for m in room['members'] if m['user'] != ident]
        else:
            raise web.HTTPConflict(text='游戏中可关闭页面，之后用相同浏览器重连')
        rooms.save(room)
        return web.json_response({'ok': True})

    async def play(request):
        ident = user(request)
        room = member_room(request, ident)
        seat = request.match_info['seat']
        me = next(m for m in room['members'] if m['user'] == ident)
        if me['seat'] != seat or room['state'] != 'playing':
            raise web.HTTPForbidden(text='无法访问这个席位')
        text = (BASE / 'web' / 'player.html').read_text('utf-8')
        text = text.replace('ROOM_ID_PLACEHOLDER', room['id'])
        return web.Response(text=text, content_type='text/html', headers={'Cache-Control': 'no-store'})

    async def stream(request):
        ident = user(request)
        room = member_room(request, ident)
        seat = request.match_info['seat']
        me = next(m for m in room['members'] if m['user'] == ident)
        if me['seat'] != seat or room['state'] != 'playing':
            raise web.HTTPForbidden()
        if request.headers.get('Origin', '').split('://')[-1] != request.host:
            raise web.HTTPForbidden()
        try:
            upstream = await app['http'].ws_connect(room['worker']['url'] + '/ws/' + seat,
                                                  params={'key': me['key']}, heartbeat=15, max_msg_size=5_000_000)
        except Exception:
            raise web.HTTPServiceUnavailable(text='席位暂时不可连接，请关闭其他页面后重试')
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4096)
        await ws.prepare(request)
        async def relay(source, target):
            async for msg in source:
                if msg.type == WSMsgType.TEXT:
                    await target.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await target.send_bytes(msg.data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    return
        tasks = [asyncio.create_task(relay(ws, upstream)), asyncio.create_task(relay(upstream, ws))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError, ConnectionError):
                    await task
            with suppress(asyncio.TimeoutError, ConnectionError):
                await asyncio.wait_for(upstream.close(), 3)
            with suppress(asyncio.TimeoutError, ConnectionError):
                await asyncio.wait_for(ws.close(), 3)
        return ws

    async def health(request):
        return web.json_response({'service': 'sts2-linux-lobby', 'version': '0.3'})

    async def startup(app):
        app['http'] = ClientSession()
        for room in rooms.rooms.values():
            if room['state'] == 'starting' or (
                    room['state'] == 'playing' and hasattr(runner, 'is_alive') and not runner.is_alive(room)):
                room['state'] = 'failed'
                rooms.save(room)

    async def cleanup(app):
        for task in list(app['tasks']):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await app['http'].close()
        if hasattr(runner, 'shutdown'):
            await background_call(runner.shutdown)
        rooms.db.close()

    app.add_routes([web.get('/', index), web.get('/api/health', health), web.post('/api/login', login),
        web.get('/api/session', session), web.post('/api/rooms', create), web.post('/api/join', join),
        web.get('/api/rooms/{room}', get_room), web.post('/api/rooms/{room}/start', start),
        web.post('/api/rooms/{room}/leave', leave), web.get('/play/{room}/{seat}', play),
        web.get('/stream/{room}/{seat}', stream)])
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == '__main__':
    web.run_app(create_app(), host=os.getenv('BIND_HOST', '0.0.0.0'),
                port=int(os.getenv('PORT', '8088')), access_log=None)
