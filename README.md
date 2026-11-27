# STS2 Web Co-op

An unofficial, self-hosted browser lobby and input/video bridge for Slay the Spire 2. The host runs a separate game instance per seat. The browser displays the host-rendered game and sends input. This project is independent of Mega Crit, Valve, and Sts2MobileLauncher.

The repository contains the lobby, browser UI, game bridge source, tests, and deployment scripts. The host supplies its own game installation and builds the bridge locally. The repository ships no game binaries, assets, archives, third-party binaries, or server data.

## Set up

1. Prepare a Linux x86-64 host with Docker and Compose v2 and a .NET 9 SDK for building the bridge.
2. Provide the matching game files separately from the repository. The bridge and deployment preflight target game version **0.107.1**. The prototype was tested with a Windows payload under Wine. Native Linux support in the launcher is experimental.
3. Build the bridge using the path to the matching data directory:

   ```sh
   sh build_bridge.sh /path/to/game/data_sts2_windows_x86_64
   ```

4. Copy `.env.example` to `.env` and set a private `SITE_PASSWORD` and absolute `GAME_ROOT` and `STATE_ROOT` paths. The default listener is `127.0.0.1:8088`. Set `BIND_ADDRESS` to the desired LAN address for trusted clients.
5. Run `sh deploy.sh`. Open the lobby, create a room, and share its room code. Each participant opens their own seat after the host starts the room.

The gateway uses the host Docker socket to start room containers. Run it on a trusted self-hosted machine and restrict access to trusted participants. Stop the service with `sh stop.sh`. Room save files are stored under `STATE_ROOT/rooms/`.

### Optional NVIDIA rendering

The default worker uses software rendering. For the tested NVIDIA EGL configuration, obtain [VirtualGL 3.1.5](https://github.com/VirtualGL/virtualgl/releases/tag/3.1.5) from its distributor and place `virtualgl_3.1.5_amd64.deb` in `vendor/`. Docker verifies its SHA-256. Set `SOFTWARE_RENDER=0`, `WORKER_IMAGE=sts2-web-worker:0.3-gpu`, and `GPU_DRIVER_ROOT` to the absolute path of this project's `gpu-driver/` directory. The deploy script prepares local driver libraries for the installed NVIDIA driver. The worker maps `/dev/nvidia0` and `/dev/nvidiactl`.

The bridge targets 24 FPS. Actual frame rate depends on host load, client decoding, and network latency. Browser audio is unimplemented. Two-player combat and reconnect were exercised on the Linux/Wine prototype.

## Lobby tests

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```
