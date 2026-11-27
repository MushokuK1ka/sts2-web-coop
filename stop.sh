#!/bin/sh
set -eu
cd "$(dirname "$0")"
echo 'Close game rooms in the lobby first; game save directories are retained.'
docker compose stop
