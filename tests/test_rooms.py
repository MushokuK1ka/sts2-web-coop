import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest

from aiohttp import web, WSServerHandshakeError, WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from app import create_app, DockerWorkers


class ControlledWorkers:
    def __init__(self, url=None):
        self.url = url
        self.started = []
        self.stopped = []

    def preflight(self):
        return {'ready': bool(self.url), 'reason': '' if self.url else '等待配置游戏运行环境'}

    def start(self, room):
        if not self.url:
            raise RuntimeError('等待配置游戏运行环境')
        self.started.append(room['id'])
        return {'id': room['id'], 'url': self.url}

    def stop(self, room):
        self.stopped.append(room['id'])


class RoomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workers = ControlledWorkers()
        self.app = create_app(Path(self.temp.name), workers=self.workers, password='test-password')
        if os.getenv('DEBUG_ROUTES'):
            @web.middleware
            async def trace(request, handler):
                print('ENTER', request.path, flush=True)
                response = await handler(request)
                print('EXIT', request.path, response.status, flush=True)
                return response
            self.app.middlewares.append(trace)
        self.server = TestServer(self.app)
        await self.server.start_server()
        self.clients = []
        self.upstream = None

    async def asyncTearDown(self):
        for client, _ in self.clients:
            await client.close()
        await self.server.close()
        if self.upstream:
            await self.upstream.close()
        self.temp.cleanup()

    async def player(self):
        # Separate cookie jars represent separate browser users.
        client = TestClient(self.server)
        await client.start_server()
        response = await client.post('/api/login', json={'password': 'test-password'})
        self.assertEqual(response.status, 200)
        headers = {'X-CSRF-Token': (await response.json())['csrf']}
        self.clients.append((client, headers))
        return client, headers

    async def room(self, player, capacity=2):
        client, headers = player
        response = await client.post('/api/rooms', json={'name': '房主', 'capacity': capacity}, headers=headers)
        self.assertEqual(response.status, 200)
        return await response.json()

    async def join(self, player, room):
        client, headers = player
        return await client.post('/api/join', json={'code': room['code'], 'name': '朋友'}, headers=headers)

    async def enable_worker(self, frame=b'encoded-game-frame'):
        app = web.Application()
        async def status(request):
            return web.json_response({'connected': True, 'scene': 'NMainMenu'})
        async def echo(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_bytes(frame)
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await ws.send_str(message.data)
            return ws
        app.add_routes([web.get('/api/{seat}/status', status), web.get('/ws/{seat}', echo)])
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        self.workers.url = str(self.upstream.make_url('')).rstrip('/')

    async def start(self, player, room):
        client, headers = player
        response = await client.post('/api/rooms/' + room['id'] + '/start', json={}, headers=headers)
        return response

    async def wait_playing(self, player, room):
        for _ in range(50):
            response = await player[0].get('/api/rooms/' + room['id'])
            data = await response.json()
            if data['state'] == 'playing':
                return data
            await asyncio.sleep(0.02)
        self.fail('worker readiness was not observed')

    async def test_login_and_csrf_are_required(self):
        client = TestClient(self.server)
        await client.start_server()
        self.clients.append((client, {}))
        self.assertEqual((await client.get('/api/session')).status, 401)
        self.assertEqual((await client.post('/api/login', json={'password': 'wrong'})).status, 401)
        player = await self.player()
        self.assertEqual((await player[0].post('/api/rooms', json={'capacity': 2})).status, 403)

    async def test_join_capacity_and_room_secrets(self):
        owner, guest, extra = await self.player(), await self.player(), await self.player()
        room = await self.room(owner)
        joined = await self.join(guest, room)
        self.assertEqual(joined.status, 200)
        view = await joined.json()
        self.assertEqual(view['my_seat'], 'B')
        self.assertEqual(len(view['members']), 2)
        self.assertNotIn('key', json.dumps(view))
        self.assertEqual((await self.join(extra, room)).status, 409)
        self.assertEqual((await extra[0].get('/api/rooms/' + room['id'])).status, 404)
        self.assertEqual((await self.room_extra_attempt(owner)).status, 409)

    async def room_extra_attempt(self, player):
        return await player[0].post('/api/rooms', json={'name': '第二房', 'capacity': 2}, headers=player[1])

    async def test_simultaneous_join_does_not_overfill(self):
        owner, guest, extra = await self.player(), await self.player(), await self.player()
        room = await self.room(owner)
        responses = await asyncio.gather(self.join(guest, room), self.join(extra, room))
        self.assertEqual(sorted(r.status for r in responses), [200, 409])
        self.assertEqual(len(self.app['rooms'].rooms[room['id']]['members']), 2)

    async def test_owner_only_start_and_graceful_unavailable_runtime(self):
        owner, guest = await self.player(), await self.player()
        room = await self.room(owner)
        self.assertEqual((await self.start(owner, room)).status, 409)
        await self.join(guest, room)
        self.assertEqual((await self.start(guest, room)).status, 403)
        self.assertEqual((await self.start(owner, room)).status, 503)
        view = await (await owner[0].get('/api/rooms/' + room['id'])).json()
        self.assertEqual(view['state'], 'waiting')
        self.assertIsNone(view['play_url'])

    async def test_start_readiness_seat_authorization_and_stream_proxy(self):
        task = asyncio.current_task()
        deadline = asyncio.get_running_loop().call_later(12, task.cancel)
        self.addCleanup(deadline.cancel)
        await self.enable_worker()
        owner, guest = await self.player(), await self.player()
        room = await self.room(owner)
        await self.join(guest, room)
        self.assertEqual((await self.start(owner, room)).status, 200)
        view = await self.wait_playing(owner, room)
        self.assertEqual(self.workers.started, [room['id']])
        self.assertEqual((await owner[0].head(view['play_url'])).status, 200)
        self.assertEqual((await guest[0].head(view['play_url'])).status, 403)
        path = '/stream/' + room['id'] + '/A'
        with self.assertRaises(WSServerHandshakeError) as bad_origin:
            await owner[0].ws_connect(path, origin='https://unrelated.invalid')
        self.assertEqual(bad_origin.exception.status, 403)
        origin = str(self.server.make_url('')).rstrip('/')
        async with owner[0].ws_connect(path, origin=origin) as ws:
            self.assertEqual((await asyncio.wait_for(ws.receive(), 3)).data, b'encoded-game-frame')
            await ws.send_json({'type': 'move', 'x': 0.2, 'y': 0.3})
            self.assertEqual(json.loads((await asyncio.wait_for(ws.receive(), 3)).data)['type'], 'move')

    async def test_leave_and_close_preserve_other_room(self):
        owner, guest, other = await self.player(), await self.player(), await self.player()
        room = await self.room(owner)
        second = await self.room(other)
        await self.join(guest, room)
        path = '/api/rooms/' + room['id'] + '/leave'
        self.assertEqual((await guest[0].post(path, json={}, headers=guest[1])).status, 200)
        self.assertEqual((await owner[0].post(path, json={}, headers=owner[1])).status, 200)
        self.assertEqual(self.workers.stopped, [room['id']])
        self.assertEqual(self.app['rooms'].rooms[second['id']]['state'], 'waiting')

    @unittest.skipIf(os.getenv('WSL_LOOPBACK_LIMITED') == '1', 'Local WSL raw TCP transfers over 1 KB time out; run this case on real Linux before deployment')
    async def test_full_player_page_and_large_video_payload(self):
        frame = b'frame-bytes:' + b'x' * 65536
        await self.enable_worker(frame)
        owner, guest = await self.player(), await self.player()
        room = await self.room(owner)
        await self.join(guest, room)
        self.assertEqual((await self.start(owner, room)).status, 200)
        view = await self.wait_playing(owner, room)
        response = await asyncio.wait_for(owner[0].get(view['play_url']), 4)
        self.assertEqual(response.status, 200)
        body = await asyncio.wait_for(response.text(), 4)
        self.assertIn(room['id'], body)
        self.assertNotIn('ROOM_ID_PLACEHOLDER', body)
        origin = str(self.server.make_url('')).rstrip('/')
        async with owner[0].ws_connect('/stream/' + room['id'] + '/A', origin=origin) as ws:
            self.assertEqual((await asyncio.wait_for(ws.receive(), 4)).data, frame)

    async def test_close_during_startup_cannot_become_playing_later(self):
        entered, release = asyncio.Event(), asyncio.Event()
        upstream = web.Application()
        async def status(request):
            entered.set()
            await release.wait()
            return web.json_response({'connected': True, 'scene': 'NMainMenu'})
        upstream.router.add_get('/api/{seat}/status', status)
        self.upstream = TestServer(upstream)
        await self.upstream.start_server()
        self.workers.url = str(self.upstream.make_url('')).rstrip('/')
        owner, guest = await self.player(), await self.player()
        room = await self.room(owner)
        await self.join(guest, room)
        self.assertEqual((await self.start(owner, room)).status, 200)
        await asyncio.wait_for(entered.wait(), 2)
        response = await owner[0].post('/api/rooms/' + room['id'] + '/leave', json={}, headers=owner[1])
        release.set()
        self.assertEqual(response.status, 200)
        await asyncio.sleep(0.1)
        self.assertEqual(self.app['rooms'].rooms[room['id']]['state'], 'closed')
        self.assertEqual(self.workers.stopped, [room['id']])

    async def test_server_restart_keeps_room_membership(self):
        owner = await self.player()
        room = await self.room(owner)
        from app import Rooms
        reopened = Rooms(Path(self.temp.name) / 'rooms.sqlite')
        self.assertEqual(reopened.rooms[room['id']]['code'], room['code'])
        self.assertEqual(len(reopened.rooms[room['id']]['members']), 1)
        reopened.db.close()


class WorkerConfigurationTests(unittest.TestCase):
    def test_room_uses_its_own_network_namespace_and_host_save_path(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as state:
            runner = DockerWorkers(Path(state))
            runner.root = '/host/sts2/state'
            runner.game = '/host/sts2/game-linux'
            runner.preflight = lambda: {'ready': True}
            client = MagicMock()
            container = client.containers.run.return_value
            container.id = 'container-123'
            container.attrs = {'NetworkSettings': {'Networks': {runner.network: {'IPAddress': '172.30.1.2'}}}}
            runner.client = lambda: client
            room = {'id': '0123456789abcdef', 'members': [{'seat': 'A', 'key': 'a', 'name': '甲'}, {'seat': 'B', 'key': 'b', 'name': '乙'}]}
            result = runner.start(room)
            kwargs = client.containers.run.call_args.kwargs
            self.assertNotIn('ports', kwargs)
            self.assertNotEqual(kwargs.get('network_mode'), 'host')
            self.assertIn('/host/sts2/state/rooms/0123456789abcdef', kwargs['volumes'])
            self.assertTrue((Path(state) / 'rooms/0123456789abcdef').is_dir())
            self.assertEqual(result['url'], 'http://172.30.1.2:7860')


if __name__ == '__main__':
    unittest.main(verbosity=2)
