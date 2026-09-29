"""Unix-socket API; provider credentials and ledger stay in this process."""

import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import struct
import threading
import time

from .control import Controller, PolicyError, CapacityError, money, positive_int
from .provider import make_provider


def load_config(path):
    config = json.loads(Path(path).read_text())
    if config.get("provider", {}).get("kind") not in ("fake", "mcp"):
        raise PolicyError("provider.kind must be fake or mcp")
    limits = config["limits"]
    for key in ("budget_usd", "max_hourly_usd"):
        if money(limits[key]) <= 0:
            raise PolicyError(f"{key} must be positive")
    for key in ("max_gpus", "max_pods", "max_retained_gb", "max_volume_gb"):
        positive_int(limits[key], key)
    if not limits.get("allowed_gpu_types"):
        raise PolicyError("configure allowed GPU types explicitly")
    if not limits.get("allowed_images") and not limits.get("allowed_templates"):
        raise PolicyError("configure an image or template allowlist explicitly")
    for key, value in config.get("safety", {}).items():
        if key in ("poll_seconds", "shutdown_margin_seconds", "max_lease_seconds", "idle_grace_seconds", "checkpoint_grace_seconds"):
            positive_int(value, key)
    return config


def peer_uid(sock):
    if hasattr(socket, "SO_PEERCRED"):
        return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if hasattr(sock, "getpeereid"):
        return sock.getpeereid()[0]
    # Development on macOS: only the socket owner's identity is supported.
    # Protected multi-user deployment is Linux and uses SO_PEERCRED above.
    return os.getuid()


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(120)
        try:
            raw = self.rfile.readline(1_048_577)
            if len(raw) > 1_048_576:
                raise PolicyError("request too large")
            request = json.loads(raw)
            result = self.server.controller.dispatch(request["method"], request.get("args", {}), peer_uid(self.connection))
            reply = {"ok": True, "result": result}
        except CapacityError as exc:
            reply = {"ok": False, "error": str(exc), "retryable": True}
        except (PolicyError, ValueError, KeyError, TypeError) as exc:
            reply = {"ok": False, "error": str(exc)}
        except Exception as exc:
            # Never return provider stderr, configuration, or credentials to clients.
            reply = {"ok": False, "error": f"service operation failed ({type(exc).__name__}); inspect service logs"}
        try:
            self.wfile.write(json.dumps(reply).encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve(config_path, state_dir, socket_path):
    config = load_config(config_path)
    provider = make_provider(config["provider"])
    controller = Controller(config, state_dir, provider)
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if path.exists():
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            path.unlink()  # Stale socket from a crashed instance.
        else:
            raise RuntimeError("another controller is already listening")
        finally:
            probe.close()
    with Server(str(path), Handler) as server:
        server.controller = controller
        os.chmod(path, 0o660)
        def stop(_signum, _frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            path.unlink(missing_ok=True)
            provider.close()


def watchdog(config_path, state_dir, once=False):
    config = load_config(config_path)
    provider = make_provider(config["provider"])
    controller = Controller(config, state_dir, provider)
    try:
        while True:
            controller.watchdog_tick()
            if once:
                break
            time.sleep(config.get("safety", {}).get("poll_seconds", 15))
    finally:
        provider.close()


def call(method, args=None, socket_path=None):
    path = socket_path or os.environ.get("RESEARCH_COMPUTE_SOCKET", "/run/research-compute/control.sock")
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(120)
        sock.connect(path)
        sock.sendall(json.dumps({"method": method, "args": args or {}}).encode() + b"\n")
        with sock.makefile("rb") as stream:
            raw = stream.readline(16_777_217)
        if len(raw) > 16_777_216:
            raise RuntimeError("controller response too large")
    result = json.loads(raw)
    if not result["ok"]:
        if result.get("retryable"):
            raise CapacityError(result["error"])
        raise PolicyError(result["error"])
    return result["result"]
