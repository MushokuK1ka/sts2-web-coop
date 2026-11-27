# STS2 Web Co-op

An unofficial, self-hosted browser lobby and input/video bridge for Slay the Spire 2. The host runs a separate game instance per seat. The browser displays the host-rendered game and sends input. This project is independent of Mega Crit, Valve, and Sts2MobileLauncher.

The repository contains the lobby, browser UI, game bridge source, tests, and deployment scripts. The host supplies its own game installation and builds the bridge locally. The repository ships no game binaries, assets, archives, third-party binaries, or server data.

## Windows host

Install Python 3.12 and the .NET 9 SDK. Supply a Windows game installation at **0.107.1**. From PowerShell in the repository, run:

```powershell
.\start_windows.ps1 -GameRoot 'D:\Games\Slay the Spire 2' -BindAddress '192.168.1.20'
```

Replace the bind address with the host's LAN or private VPN IPv4 address. Omit it for a test limited to the host. The script builds the bridge locally and prints the lobby URL and the path to its generated access password. Each player opens that URL, enters the password, and joins using the room code. After the owner clicks **进入选角**, each browser uses its own **进入游戏** link.

Allow inbound TCP 8088 from the trusted network if Windows Firewall blocks it. Friends outside the LAN need a route into the private network. The worker stream, game bridge and ENet transport listen on loopback (TCP 17860, TCP 7861 and UDP 33771). Windows supports one room at a time because the game's local session uses a fixed port.

Close the room in the lobby to free its game processes. Ctrl+C in the host terminal also stops the lobby and its rooms. Room logs and staged game files are under `-StateRoot` (default `%LOCALAPPDATA%\STS2WebCoop`). Each seat saves into its own `%APPDATA%\STS2WebCoop_<room>_<seat>` directory. Game mods already installed in the supplied game directory are kept out of the staged room.

## Linux host

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

The bridge targets 24 FPS. Actual frame rate depends on host load, client decoding, and network latency. Browser audio is unimplemented. Two-player combat and reconnect were exercised on Linux/Wine and native Windows.

## Lobby tests

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```
