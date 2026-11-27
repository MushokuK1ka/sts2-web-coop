"""Read-only deployment checks; no sudo, installation or permission changes."""
import json
from pathlib import Path
import platform
import subprocess
import sys

BASE = Path(__file__).resolve().parent

def main():
    values = {}
    env = BASE / '.env'
    if env.exists():
        for raw in env.read_text('utf-8-sig').splitlines():
            line = raw.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('"\'')
    checks = {}
    checks['linux_x86_64'] = platform.system() == 'Linux' and platform.machine() == 'x86_64'
    checks['env_file'] = env.exists()
    checks['site_password_configured'] = len(values.get('SITE_PASSWORD', '')) >= 12 and not values.get('SITE_PASSWORD', '').startswith('replace-')
    game = Path(values.get('GAME_ROOT', 'game-linux'))
    exe = next((game / name for name in ('SlayTheSpire2', 'SlayTheSpire2.x86_64', 'SlayTheSpire2.exe') if (game / name).is_file()), None)
    magic = None
    if exe:
        with exe.open('rb') as handle:
            magic = handle.read(4)
    checks['supported_game_payload'] = bool(exe and (magic == b'\x7fELF' or exe.suffix == '.exe' and magic[:2] == b'MZ'))
    try:
        version = json.loads((game / 'release_info.json').read_text('utf-8'))['version']
    except (OSError, ValueError, KeyError):
        version = None
    checks['matching_game_version'] = bool(version and version.lstrip('v') == '0.107.1')
    checks['game_assets'] = (game / 'SlayTheSpire2.pck').is_file()
    checks['bridge_dll'] = (BASE / 'worker/WebCoop.dll').is_file()
    for name, command in [('docker_access', ['docker', 'info', '--format', '{{.ServerVersion}}']),
                          ('compose_v2', ['docker', 'compose', 'version'])]:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            checks[name] = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            checks[name] = False
    report = {'ready_for_deployment': all(checks.values()), 'checks': checks, 'game_version': version,
              'game_runtime': 'wine' if exe and exe.suffix == '.exe' else 'native',
              'rendering': 'NVIDIA via VirtualGL EGL' if values.get('SOFTWARE_RENDER') == '0' else 'software Xvfb/Mesa',
              'browser_audio': False}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ready_for_deployment'] else 1

if __name__ == '__main__':
    sys.exit(main())
