"""Run one native LAN room on Windows under a kill-on-close job object."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


BASE = Path(__file__).resolve().parent
EXE = 'SlayTheSpire2.exe'
RUNTIME = 'data_sts2_windows_x86_64'
VERSION = '0.107.1'
STREAM_PORT = 17860
GAME_PORT = 33771
BRIDGE_PORT = 7861
ROOT_FILES = (EXE, 'SlayTheSpire2.pck', 'release_info.json', 'fmod.dll', 'fmodstudio.dll',
              'libGodotFmod.windows.template_release.x86_64.dll',
              'libspine_godot.windows.template_release.x86_64.dll',
              'libsentry.windows.release.x86_64.dll', 'crashpad_handler.exe', 'crashpad_wer.dll')
# Steam transport is unused in this local room. Stage no Steam loader or proxy.
STEAM_NATIVE_FILES = {'steam_api64.dll', 'sovereign64.dll', 'steamfix64.dll', 'winmm.dll'}


class BasicLimit(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
                ('max_working_set', ctypes.c_size_t), ('active_limit', wintypes.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                ('scheduling', wintypes.DWORD)]


class JobLimits(ctypes.Structure):
    _fields_ = [('basic', BasicLimit), ('io_counters', ctypes.c_uint64 * 6),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]


class WindowsJob:
    """A job owns the worker and every game it starts, even if the lobby crashes."""
    def __init__(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': (wintypes.HANDLE, (ctypes.c_void_p, wintypes.LPCWSTR)),
            'SetInformationJobObject': (wintypes.BOOL, (wintypes.HANDLE, wintypes.INT, ctypes.c_void_p, wintypes.DWORD)),
            'OpenProcess': (wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)),
            'AssignProcessToJobObject': (wintypes.BOOL, (wintypes.HANDLE, wintypes.HANDLE)),
            'CloseHandle': (wintypes.BOOL, (wintypes.HANDLE,)),
        }
        for name, (restype, argtypes) in signatures.items():
            fn = getattr(kernel, name)
            fn.restype, fn.argtypes = restype, argtypes
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = JobLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def add(self, pid):
        process = self.kernel.OpenProcess(0x101, False, pid)  # TERMINATE | SET_QUOTA
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class WindowsWorkers:
    max_rooms = 1
    requires_character_select = True

    def __init__(self, state):
        self.state = state
        self.game = Path(os.getenv('GAME_ROOT', state / 'game')).resolve()
        self.bridge = Path(os.getenv('WEB_BRIDGE_DLL', BASE / 'worker/WebCoop.dll')).resolve()
        self.active = None
        self.lock = threading.Lock()
        self.closing = False

    def preflight(self):
        if not all((self.game / name).is_file() for name in (EXE, 'SlayTheSpire2.pck', RUNTIME + '/sts2.dll')):
            return {'ready': False, 'reason': '请配置完整的 Windows 游戏目录 GAME_ROOT'}
        try:
            version = json.loads((self.game / 'release_info.json').read_text('utf-8'))['version']
        except (OSError, ValueError, KeyError):
            return {'ready': False, 'reason': '无法确认游戏版本'}
        if version.lstrip('v') != VERSION:
            return {'ready': False, 'reason': f'当前桥接需要 Windows 游戏 {VERSION}'}
        if not self.bridge.is_file():
            return {'ready': False, 'reason': '请先构建 WebCoop.dll'}
        return {'ready': True, 'reason': ''}

    def start(self, room):
        with self.lock:
            if self.closing or self.active:
                raise RuntimeError('Windows 主机当前只能运行一个房间')
            check = self.preflight()
            if not check['ready']:
                raise RuntimeError(check['reason'])
            check_ports()
            room_dir = (self.state / 'rooms' / room['id']).resolve()
            room_dir.mkdir(parents=True, exist_ok=True)
            members = sorted(room['members'], key=lambda m: m['seat'])
            env = os.environ.copy()
            env.update(GAME_ROOT=str(self.game), ROOM_DIR=str(room_dir), WEB_BRIDGE_DLL=str(self.bridge),
                       WEB_BIND='127.0.0.1', WEB_STREAM_PORT=str(STREAM_PORT),
                       WEB_SEATS=','.join(m['seat'] for m in members),
                       WEB_ACCESS_KEYS=json.dumps({m['seat']: m['key'] for m in members}),
                       ROOM_MEMBERS=json.dumps([{'seat': m['seat'], 'name': m['name']} for m in members]))
            job = WindowsJob()
            child = None
            try:
                with (room_dir / 'worker.log').open('ab') as log:
                    child = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve())],
                                             cwd=room_dir, env=env, stdin=subprocess.PIPE,
                                             stdout=log, stderr=subprocess.STDOUT,
                                             creationflags=subprocess.CREATE_NO_WINDOW)
                job.add(child.pid)
                # The worker cannot spawn games until the job assignment succeeds.
                with child.stdin as ready:
                    ready.write(b'1')
                token = secrets.token_hex(16)
                self.active = (room['id'], token, child, job)
                return {'id': token, 'url': f'http://127.0.0.1:{STREAM_PORT}'}
            except BaseException:
                job.close()
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait()
                raise

    def _matches(self, room):
        return self.active and self.active[:2] == (room['id'], (room.get('worker') or {}).get('id'))

    def is_alive(self, room):
        with self.lock:
            return bool(self._matches(room) and self.active[2].poll() is None)

    def stop(self, room):
        with self.lock:
            if self._matches(room):
                self._close_active()

    def shutdown(self):
        with self.lock:
            self.closing = True
            if self.active:
                self._close_active()

    def _close_active(self):
        _, _, child, job = self.active
        job.close()
        child.wait(timeout=10)
        self.active = None


def check_ports():
    for kind, port in ((socket.SOCK_STREAM, STREAM_PORT), (socket.SOCK_STREAM, BRIDGE_PORT),
                       (socket.SOCK_DGRAM, GAME_PORT)):
        try:
            with socket.socket(socket.AF_INET, kind) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                listener.bind(('127.0.0.1', port))
        except OSError as error:
            raise RuntimeError(f'Windows 本地端口 {port} 正被使用') from error


def stage_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def stage_seat(game, bridge, room_dir, members, index):
    seat = members[index]['seat']
    root = room_dir / seat
    root.mkdir()
    for name in ROOT_FILES:
        source = game / name
        if source.is_file():
            stage_file(source, root / name)
    payload = game / RUNTIME
    for source in payload.iterdir():
        if source.is_file() and source.suffix.lower() in ('.dll', '.json') and source.name.lower() not in STEAM_NATIVE_FILES:
            stage_file(source, root / RUNTIME / source.name)
    profile = f'STS2WebCoop_{room_dir.name}_{seat}'
    (root / 'override.cfg').write_text(f'''[application]
config/name="STS2 WebCoop {seat}"
config/use_custom_user_dir=true
config/custom_user_dir_name="{profile}"
[display]
window/size/mode=0
window/size/window_width_override=960
window/size/window_height_override=540
[rendering]
renderer/rendering_method="gl_compatibility"
''', encoding='utf-8')
    identity = 1 if index == 0 else index * 1000
    settings = Path(os.environ['APPDATA']) / profile / 'default' / str(identity) / 'settings.save'
    settings.parent.mkdir(parents=True, exist_ok=True)
    if not settings.exists():
        settings.write_text(json.dumps({'schema_version': 5, 'language': 'zhs', 'seen_ea_disclaimer': True,
            'fullscreen': False, 'skip_intro_logo': True, 'limit_fps_in_background': False, 'fps_limit': 30,
            'window_size': {'X': 960, 'Y': 540}, 'mod_settings': {'mods_enabled': True, 'mod_list': []},
            'volume_master': 0}), encoding='utf-8')
    mod = root / 'mods/WebCoop'
    mod.mkdir(parents=True)
    shutil.copy2(bridge, mod / 'WebCoop.dll')
    (mod / 'WebCoop.json').write_text(json.dumps({'id': 'WebCoop', 'name': 'WebCoop',
        'version': '0.3.0', 'has_dll': True, 'has_pck': False, 'affects_gameplay': False,
        'dependencies': [], 'min_game_version': VERSION}), encoding='utf-8')
    names = [{'net_id': 1 if i == 0 else i * 1000, 'name': member['name']} for i, member in enumerate(members)]
    (root / 'mp_names.json').write_text(json.dumps(names, ensure_ascii=False), encoding='utf-8')
    role = 'host_standard' if index == 0 else 'join'
    return root, [str(root / EXE), '--windowed', '--resolution', '960x540', '--max-fps', '30',
                  '--rendering-method', 'gl_compatibility', '--rendering-driver', 'opengl3',
                  '--force-steam=off', '--web-local-only', f'--fastmp={role}', f'--clientId={identity}',
                  f'--web-seat={seat}']


def run_room():
    if sys.stdin.buffer.read(1) != b'1':
        return
    room_dir = Path(os.environ['ROOM_DIR'])
    game = Path(os.environ['GAME_ROOT'])
    bridge = Path(os.environ['WEB_BRIDGE_DLL'])
    members = json.loads(os.environ['ROOM_MEMBERS'])
    keys = json.loads(os.environ['WEB_ACCESS_KEYS'])
    children = []
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start(args, cwd, name):
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        with (room_dir / name).open('ab') as log:
            children.append(subprocess.Popen(args, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                             startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW))

    def wait_ready(seat, require_game):
        request = urllib.request.Request(f'http://127.0.0.1:{STREAM_PORT}/api/{seat}/status',
                                         headers={'X-Access-Key': keys[seat]})
        for _ in range(180):
            if any(child.poll() is not None for child in children):
                raise RuntimeError(f'Room process exited while waiting for {seat}')
            try:
                with http.open(request, timeout=1) as response:
                    status = json.load(response)
                if not require_game or (status.get('connected') and 'NCharacterSelectScreen' in status.get('screens', [])):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        raise RuntimeError(f'Timed out waiting for seat {seat}')

    try:
        start([sys.executable, '-u', str(BASE / 'worker/server.py')], room_dir, 'stream.log')
        wait_ready('A', False)
        for index, member in enumerate(members):
            root, args = stage_seat(game, bridge, room_dir, members, index)
            start(args, root, f'game-{member["seat"]}.log')
            wait_ready(member['seat'], True)
        while all(child.poll() is None for child in children):
            time.sleep(1)
        raise RuntimeError('A room process exited')
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()


if __name__ == '__main__':
    run_room()
