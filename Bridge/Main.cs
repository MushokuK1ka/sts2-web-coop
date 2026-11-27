using System.Buffers.Binary;
using System.Collections.Concurrent;
using System.Net.Sockets;
using System.Text.Json;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Runs;
using Environment = System.Environment;

namespace WebCoop;

[ModInitializer(nameof(Initialize))]
public static class Main
{
    private static readonly ConcurrentQueue<JsonElement> Events = new();
    private static readonly SemaphoreSlim WriteLock = new(1, 1);
    private static NetworkStream? _stream;
    private static SceneTree _tree = null!;
    private static string _seat = "A";
    private static byte[]? _latestFrame;
    private const double FrameIntervalMs = 1000.0 / 24;
    private static double _nextFrame;
    private static long _lastStatus;
    private static MouseButtonMask _buttons;
    private static Vector2 _lastMouse;
    private static long _inputs;
    private static bool _watching;
    private static readonly HashSet<Key> Keys = new();

    public static void Initialize()
    {
        _seat = CommandLineHelper.GetValue("web-seat") ?? "A";
        var harmony = new Harmony("local.webcoop.bridge");
        harmony.Patch(AccessTools.Method(typeof(NGame), nameof(NGame.IsGameFocusedWindow)),
            prefix: new HarmonyMethod(typeof(Main), nameof(Focused)));
        harmony.Patch(AccessTools.Method(typeof(Viewport), nameof(Viewport.GetMousePosition)),
            prefix: new HarmonyMethod(typeof(Main), nameof(ViewportMouse)));
        harmony.Patch(AccessTools.Method(typeof(CanvasItem), nameof(CanvasItem.GetGlobalMousePosition)),
            prefix: new HarmonyMethod(typeof(Main), nameof(GlobalMouse)));
        harmony.Patch(AccessTools.Method(typeof(CanvasItem), nameof(CanvasItem.GetLocalMousePosition)),
            prefix: new HarmonyMethod(typeof(Main), nameof(LocalMouse)));
        if (CommandLineHelper.HasArg("web-local-only"))
            harmony.Patch(AccessTools.Method(typeof(ENetConnection), nameof(ENetConnection.CreateHostBound)),
                prefix: new HarmonyMethod(typeof(Main), nameof(BindEnetLocally)));
        _tree = (SceneTree)Engine.GetMainLoop();
        Input.UseAccumulatedInput = false;
        _tree.ProcessFrame += Tick;
        RenderingServer.FramePostDraw += Capture;
        _ = Task.Run(Connect);
        GD.Print($"[WebCoop] bridge loaded seat={_seat} userDir={OS.GetUserDataDir()}");
    }

    private static bool Focused(ref bool __result) { __result = true; return false; }
    private static void BindEnetLocally(ref string __0)
    {
        if (__0 == "0.0.0.0") __0 = "127.0.0.1";
    }
    private static Vector2 LogicalMouse()
    {
        var win = DisplayServer.WindowGetSize();
        var visible = _tree.Root.GetVisibleRect().Size;
        return new Vector2(_lastMouse.X * visible.X / win.X, _lastMouse.Y * visible.Y / win.Y);
    }
    private static bool ViewportMouse(Viewport __instance, ref Vector2 __result)
    {
        if (_inputs == 0 || __instance != _tree.Root) return true;
        __result = LogicalMouse(); return false;
    }
    private static bool GlobalMouse(CanvasItem __instance, ref Vector2 __result)
    {
        if (_inputs == 0) return true;
        __result = __instance.GetCanvasTransform().AffineInverse() * LogicalMouse(); return false;
    }
    private static bool LocalMouse(CanvasItem __instance, ref Vector2 __result)
    {
        if (_inputs == 0) return true;
        __result = __instance.GetGlobalTransformWithCanvas().AffineInverse() * LogicalMouse(); return false;
    }

    private static async Task Connect()
    {
        while (true)
        {
            try
            {
                using var client = new TcpClient { NoDelay = true };
                await client.ConnectAsync("127.0.0.1", 7861);
                var stream = client.GetStream();
                _stream = stream;
                _ = Send(2, JsonSerializer.SerializeToUtf8Bytes(new { seat = _seat, pid = Environment.ProcessId }));
                var header = new byte[4];
                while (true)
                {
                    await stream.ReadExactlyAsync(header);
                    var size = BinaryPrimitives.ReadInt32BigEndian(header);
                    if (size < 2 || size > 65536) throw new IOException("Invalid bridge message size");
                    var body = new byte[size];
                    await stream.ReadExactlyAsync(body);
                    using var doc = JsonDocument.Parse(body.AsMemory(1));
                    if (Events.Count < 512) Events.Enqueue(doc.RootElement.Clone());
                }
            }
            catch { _stream = null; _watching = false; }
            await Task.Delay(1000);
        }
    }

    private static async Task Send(byte kind, byte[] data)
    {
        await WriteLock.WaitAsync();
        try
        {
            var stream = _stream;
            if (stream == null) return;
            var header = new byte[5];
            BinaryPrimitives.WriteInt32BigEndian(header, data.Length + 1);
            header[4] = kind;
            await stream.WriteAsync(header);
            await stream.WriteAsync(data);
        }
        catch { _stream = null; }
        finally { WriteLock.Release(); }
    }

    private static async Task SendLatestFrame()
    {
        var data = Interlocked.Exchange(ref _latestFrame, null);
        if (data != null) await Send(1, data);
    }

    private static void Tick()
    {
        for (int n = 0; n < 128 && Events.TryDequeue(out var e); n++)
        {
            try { Handle(e); } catch (Exception ex) { GD.PrintErr("[WebCoop] input: " + ex.Message); }
        }
        var now = Environment.TickCount64;
        if (now - _lastStatus < 1000 || _stream == null) return;
        _lastStatus = now;
        try
        {
            var game = NGame.Instance;
            var run = RunManager.Instance;
            var viewport = _tree.Root.GetViewport().GetVisibleRect().Size;
            var scenes = new List<string>();
            Walk(_tree.Root, scenes);
            _ = Send(2, JsonSerializer.SerializeToUtf8Bytes(new {
                seat = _seat, pid = Environment.ProcessId, userDir = OS.GetUserDataDir(),
                width = viewport.X, height = viewport.Y, inputs = _inputs,
                window = DisplayServer.WindowGetSize().ToString(), mouse = _tree.Root.GetMousePosition().ToString(),
                focused = NGame.IsGameFocusedWindow(),
                running = run?.IsInProgress ?? false,
                scene = game?.RootSceneContainer?.CurrentScene?.GetType().Name,
                screens = scenes,
                players = run?.DebugOnlyGetState()?.Players.Select(p => new {
                    id = p.NetId, character = p.Character.Id.ToString(), hp = p.Creature.CurrentHp
                }).ToArray()
            }));
        }
        catch (Exception ex) { GD.PrintErr("[WebCoop] status: " + ex.Message); }
    }

    private static void Walk(Node node, List<string> found)
    {
        if (node is Control c && c.IsVisibleInTree() && (node.GetType().Name.Contains("Screen") || node.GetType().Name.Contains("Popup")))
            found.Add(node.GetType().Name);
        foreach (var child in node.GetChildren()) Walk(child, found);
    }

    private static void Capture()
    {
        var now = Environment.TickCount64;
        if (_stream == null || !_watching || now < _nextFrame || _latestFrame != null || WriteLock.CurrentCount == 0) return;
        // Keep the target cadence when the game and capture clocks do not align.
        _nextFrame = Math.Max(_nextFrame, now - FrameIntervalMs) + FrameIntervalMs;
        try
        {
            using var image = _tree.Root.GetTexture().GetImage();
            if (image == null || image.IsEmpty()) return;
            if (image.GetWidth() > 960) image.Resize(960, image.GetHeight() * 960 / image.GetWidth());
            _latestFrame = image.SaveJpgToBuffer(0.64f);
            _ = SendLatestFrame();
        }
        catch (Exception ex) { GD.PrintErr("[WebCoop] capture: " + ex.Message); }
    }

    private static void Handle(JsonElement e)
    {
        var type = e.GetProperty("type").GetString();
        if (type == "watch") { _watching = e.GetProperty("enabled").GetBoolean(); return; }
        if (type == "release")
        {
            foreach (var button in new[] { MouseButton.Left, MouseButton.Right })
                Input.ParseInputEvent(new InputEventMouseButton { ButtonIndex = button, Pressed = false, Position = _lastMouse, GlobalPosition = _lastMouse });
            foreach (var key in Keys)
                NInputManager.Instance?._UnhandledKeyInput(new InputEventKey { Keycode = key, PhysicalKeycode = key, Pressed = false });
            Keys.Clear();
            _buttons = 0; return;
        }
        // ParseInputEvent expects window pixels; Godot then applies the root stretch transform.
        var size = DisplayServer.WindowGetSize();
        var pos = e.TryGetProperty("x", out var x) ? new Vector2(Math.Clamp(x.GetSingle(), 0, 1) * size.X,
            Math.Clamp(e.GetProperty("y").GetSingle(), 0, 1) * size.Y) : _lastMouse;
        var previousMouse = _lastMouse;
        _lastMouse = pos;
        _inputs++;
        if (type == "move")
            Input.ParseInputEvent(new InputEventMouseMotion { Position = pos, GlobalPosition = pos, Relative = pos - previousMouse, ButtonMask = _buttons });
        else if (type is "down" or "up")
        {
            var button = e.TryGetProperty("button", out var b) && b.GetInt32() == 2 ? MouseButton.Right : MouseButton.Left;
            var mask = button == MouseButton.Right ? MouseButtonMask.Right : MouseButtonMask.Left;
            if (type == "down") _buttons |= mask; else _buttons &= ~mask;
            Input.ParseInputEvent(new InputEventMouseButton { Position = pos, GlobalPosition = pos, ButtonIndex = button,
                ButtonMask = _buttons, Pressed = type == "down" });
        }
        else if (type == "wheel")
        {
            var button = e.GetProperty("delta").GetDouble() > 0 ? MouseButton.WheelDown : MouseButton.WheelUp;
            Input.ParseInputEvent(new InputEventMouseButton { Position = pos, GlobalPosition = pos, ButtonIndex = button, Pressed = true, Factor = 1 });
        }
        else if (type == "key")
        {
            var key = e.GetProperty("key").GetString();
            Key code = key switch { "Escape" => Key.Escape, "Enter" => Key.Enter, " " => Key.Space,
                _ when key?.Length == 1 && char.IsAsciiLetterOrDigit(key[0]) => (Key)char.ToUpperInvariant(key[0]), _ => Key.None };
            if (code != Key.None)
            {
                var pressed = e.GetProperty("pressed").GetBoolean();
                if (pressed) Keys.Add(code); else Keys.Remove(code);
                // Hidden windows do not receive Godot's native unhandled-key stage.
                // Use the game's normal key-to-action mapper on its main thread.
                NInputManager.Instance?._UnhandledKeyInput(new InputEventKey { Keycode = code, PhysicalKeycode = code, Pressed = pressed });
            }
        }
    }
}
