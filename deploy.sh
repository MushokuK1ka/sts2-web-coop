#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -f .env ]; then
    echo 'Copy .env.example to .env and configure the paths and site password first.' >&2
    exit 1
fi
python3 doctor.py
docker compose config --quiet
docker build -f Dockerfile.worker -t sts2-web-worker:0.3 .
if grep -q '^SOFTWARE_RENDER=0$' .env; then
    python3 prepare_gpu_driver.py
    docker build -f Dockerfile.gpu -t sts2-web-worker:0.3-gpu .
fi
docker compose up --build -d
echo 'Deployment started. Check docker compose logs -f for readiness.'
