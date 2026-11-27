"""Copy matching host NVIDIA userspace libraries into our private deployment directory."""
from pathlib import Path
import json
import shutil
import subprocess

base = Path(__file__).resolve().parent
target = base / 'gpu-driver'
target.mkdir(exist_ok=True, mode=0o755)
version = subprocess.check_output(['nvidia-smi', '--query-gpu=driver_version', '--format=csv,noheader'], text=True).splitlines()[0].strip()
source = Path('/lib/x86_64-linux-gnu')
libraries = ['libEGL_nvidia.so.0', 'libGLX_nvidia.so.0']
libraries += [prefix + '.so.' + version for prefix in ('libnvidia-eglcore', 'libnvidia-glcore', 'libnvidia-glsi', 'libnvidia-tls')]
for name in libraries:
    src = source / name
    if not src.is_file():
        raise SystemExit('Missing host graphics library: ' + str(src))
    shutil.copy2(src, target / name)
(target / '10_nvidia.json').write_text(json.dumps({'file_format_version': '1.0.0', 'ICD': {'library_path': '/opt/nvidia/libEGL_nvidia.so.0'}}))
print('Private NVIDIA driver libraries prepared:', version)
