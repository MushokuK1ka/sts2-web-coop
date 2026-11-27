"""One network namespace per room; one X display and user-data root per seat."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

GAME = Path('/opt/game')
RUNTIME = Path('/run/sts2')
DATA = Path('/data')
children = []
logs = []

def launch(args, env=None, cwd=None, log=None):
    handle = open(log, 'ab') if log else None
    if handle:
        logs.append(handle)
    child = subprocess.Popen(args, env=env, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT if handle else None)
    children.append(child)
    return child

def prepare(member, index):
    seat = member['seat']
    root = RUNTIME / seat
    root.mkdir(parents=True, exist_ok=True)
    for item in GAME.iterdir():
        dest = root / item.name
        if item.name in ('override.cfg', 'mods') or dest.exists():
            continue
        if item.name == EXECUTABLE:
            # Real path must stay in this seat directory, so mod discovery is isolated.
            shutil.copy2(item, dest)
            dest.chmod(0o755)
        else:
            dest.symlink_to(item, target_is_directory=item.is_dir())
    (root / 'override.cfg').write_text('''[application]
config/use_custom_user_dir=true
config/custom_user_dir_name="STS2WebCoop"
[display]
window/size/mode=0
window/size/window_width_override=960
window/size/window_height_override=540
[rendering]
renderer/rendering_method="gl_compatibility"
renderer/rendering_method.mobile="gl_compatibility"
''', encoding='utf-8')
    mod = root / 'mods/WebCoop'
    mod.mkdir(parents=True, exist_ok=True)
    shutil.copy2('/opt/worker/WebCoop.dll', mod / 'WebCoop.dll')
    (mod / 'WebCoop.json').write_text(json.dumps({'id': 'WebCoop', 'name': 'WebCoop', 'author': 'local',
        'version': '0.2.0', 'has_dll': True, 'has_pck': False, 'affects_gameplay': False,
        'dependencies': [], 'min_game_version': '0.107.1'}), encoding='utf-8')
    identity = 1 if index == 0 else index * 1000
    data = DATA / seat
    data.mkdir(parents=True, exist_ok=True)
    names = [{'net_id': 1 if i == 0 else i * 1000, 'name': m['name']} for i, m in enumerate(members)]
    (root / 'mp_names.json').write_text(json.dumps(names, ensure_ascii=False), encoding='utf-8')
    display = ':{}'.format(90 + index)
    launch(['Xvfb', display, '-screen', '0', '1280x720x24', '-nolisten', 'tcp', '-ac'], log=DATA / ('display-' + seat + '.log'))
    display_socket = Path('/tmp/.X11-unix/X' + str(90 + index))
    for _ in range(100):
        if display_socket.exists():
            break
        time.sleep(0.1)
    if not display_socket.exists():
        raise RuntimeError('virtual display failed: ' + seat)
    env = os.environ.copy()
    env.update(DISPLAY=display, XDG_DATA_HOME=str(data), HOME=str(data),
               XDG_CONFIG_HOME=str(data / 'config'), XDG_CACHE_HOME=str(data / 'cache'))
    software = os.getenv('SOFTWARE_RENDER', '1') == '1'
    if software:
        env['LIBGL_ALWAYS_SOFTWARE'] = '1'
        env['LP_NUM_THREADS'] = '2'
    else:
        env.pop('LIBGL_ALWAYS_SOFTWARE', None)
        env['LD_LIBRARY_PATH'] = '/opt/nvidia'
        env['__EGL_VENDOR_LIBRARY_FILENAMES'] = '/opt/nvidia/10_nvidia.json'
    if EXECUTABLE.endswith('.exe'):
        prefix = data / 'wine'
        # CoreCLR still loads IL-only DLLs through Wine's mscoree PE entry stub.
        env.update(WINEPREFIX=str(prefix), WINEARCH='win64', WINEDEBUG='-all', WINEDLLOVERRIDES='mscoree=b;mshtml=')
        with (DATA / ('wineboot-' + seat + '.log')).open('ab') as out:
            subprocess.run(['/usr/lib/wine/wine64', 'wineboot.exe', '-u'], env=env, stdout=out, stderr=subprocess.STDOUT, timeout=90, check=True)
        users = prefix / 'drive_c/users'
        profiles = [p for p in users.iterdir() if p.is_dir() and p.name.lower() not in ('public', 'all users')]
        if not profiles:
            raise RuntimeError('Wine user profile was not created')
        account = profiles[0] / 'AppData/Roaming/STS2WebCoop/default' / str(identity)
    else:
        account = data / 'STS2WebCoop/default' / str(identity)
    account.mkdir(parents=True, exist_ok=True)
    settings = account / 'settings.save'
    if not settings.exists():
        settings.write_text(json.dumps({'schema_version': 8, 'language': 'zhs', 'seen_ea_disclaimer': True,
            'fullscreen': False, 'skip_intro_logo': True, 'limit_fps_in_background': False, 'fps_limit': 30,
            'window_size': {'X': 960, 'Y': 540}, 'window_position': {'X': 0, 'Y': 0},
            'mod_settings': {'mods_enabled': True, 'mod_list': []}, 'volume_master': 0}), encoding='utf-8')
    save = account / 'modded/profile1/saves/current_run_mp.save'
    role = ('load' if save.exists() else 'host_standard') if index == 0 else 'join'
    command = (['/usr/lib/wine/wine64'] if EXECUTABLE.endswith('.exe') else []) + [str(root / EXECUTABLE),
        '--windowed', '--resolution', '960x540', '--max-fps', '30', '--rendering-method', 'gl_compatibility',
        '--rendering-driver', 'opengl3', '--force-steam=off', '--fastmp=' + role, '--clientId=' + str(identity), '--web-seat=' + seat]
    if not software:
        command = ['/opt/VirtualGL/bin/vglrun', '-d', 'egl0', '-c', 'proxy'] + command
    launch(command,
        env=env, cwd=root, log=DATA / ('game-' + seat + '.log'))

def shutdown(*_):
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 10
    for child in reversed(children):
        try:
            child.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
    for handle in logs:
        handle.close()

if __name__ == '__main__':
    members = json.loads(os.environ['ROOM_MEMBERS'])
    if not 2 <= len(members) <= 4 or members[0]['seat'] != 'A':
        raise SystemExit('Expected 2-4 seats with host seat A')
    EXECUTABLE = next((name for name in ('SlayTheSpire2', 'SlayTheSpire2.x86_64', 'SlayTheSpire2.exe') if (GAME / name).is_file()), None)
    if EXECUTABLE is None:
        raise SystemExit('Matching game files must be mounted at /opt/game')
    DATA.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    try:
        # FMOD still needs an output device when the game's master volume is zero.
        # A private null sink supplies that device; it does not stream audio.
        pulse_dir = RUNTIME / 'pulse'
        pulse_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        pulse_state = DATA / 'pulse'
        pulse_state.mkdir(parents=True, exist_ok=True)
        os.environ['PULSE_RUNTIME_PATH'] = str(pulse_dir)
        os.environ['PULSE_STATE_PATH'] = str(pulse_state)
        os.environ['PULSE_SERVER'] = 'unix:' + str(pulse_dir / 'native')
        launch(['pulseaudio', '--daemonize=no', '--exit-idle-time=-1', '--disable-shm=true', '-n',
            '-L', 'module-null-sink sink_name=game',
            '-L', 'module-native-protocol-unix auth-anonymous=1 socket=' + str(pulse_dir / 'native')],
            log=DATA / 'audio-device.log')
        for _ in range(100):
            if (pulse_dir / 'native').exists():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError('virtual audio device failed; inspect /data/audio-device.log')
        launch([sys.executable, '-u', '/opt/worker/server.py'], cwd='/opt/worker', log=DATA / 'stream.log')
        for index, member in enumerate(members):
            prepare(member, index)
            if index == 0:
                # Readiness comes from the actual game bridge, not just the process existing.
                import urllib.request
                keys = json.loads(os.environ['WEB_ACCESS_KEYS'])
                for _ in range(120):
                    try:
                        request = urllib.request.Request('http://127.0.0.1:7860/api/A/status', headers={'X-Access-Key': keys['A']})
                        with urllib.request.urlopen(request, timeout=1) as response:
                            status = json.load(response)
                        if status.get('scene') == 'NMainMenu' and any(s in status.get('screens', []) for s in ('NCharacterSelectScreen', 'NMultiplayerLoadGameScreen')):
                            break
                    except Exception:
                        pass
                    time.sleep(1)
                else:
                    raise RuntimeError('host failed to open lobby; inspect /data/game-A.log')
        while all(child.poll() is None for child in children):
            time.sleep(1)
        raise RuntimeError('a game or display process exited; inspect persistent room logs')
    finally:
        shutdown()
