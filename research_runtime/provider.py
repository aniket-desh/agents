"""RunPod lifecycle through the pinned official MCP server; no deletion surface.

The controller supplies a protected configuration, never experiment code. A
timed-out mutation is ambiguous: reconcile its intent instead of retrying it.
"""

import fcntl
import json
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import threading
import time


class ProviderError(RuntimeError):
    pass


class PodNotFound(ProviderError):
    """A structured provider 404, not a failed connection or guessed absence."""
    def __init__(self, pod_id):
        super().__init__(f"Provider confirms pod {pod_id} does not exist")
        self.pod_id = pod_id


class CreateRejected(ProviderError):
    """An explicit provider validation/auth rejection with no created pod."""
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class AmbiguousMutation(ProviderError):
    def __init__(self, message, pod_id=None):
        super().__init__(message)
        self.pod_id = pod_id


def _number(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderError(f"Missing or invalid {name}")
    if not math.isfinite(value) or value < minimum:
        raise ProviderError(f"Missing or invalid {name}")
    return float(value)


def _request(request):
    result = dict(request)
    for key, default, minimum in (("gpu_count", 1, 1), ("volume_gb", 20, 10),
                                  ("container_disk_gb", 20, 1)):
        value = result.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ProviderError(f"{key} must be an integer >= {minimum}")
        result[key] = value
    if not isinstance(result.get("gpu_type"), str) or not result["gpu_type"].strip():
        raise ProviderError("gpu_type is required")
    cloud = result.get("cloud", "SECURE").upper()
    if cloud not in ("SECURE", "COMMUNITY"):
        raise ProviderError("cloud must be SECURE or COMMUNITY")
    result["cloud"] = cloud
    return result


def _storage_rate(config, volume, disk):
    # A 28-day divisor intentionally overestimates the monthly-to-hourly rate.
    monthly = _number(config.get("active_storage_usd_gb_month", 0.10), "storage rate")
    return (volume + disk) * monthly / (28 * 24)


def _api_key(config):
    filename = config.get("env_file")
    if not filename:
        key = os.environ.get("RUNPOD_API_KEY", "")
    else:
        fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ProviderError("Provider env_file must be a private regular file (mode 600)")
            if info.st_uid not in (0, os.geteuid()):
                raise ProviderError("Provider env_file has an unexpected owner")
            key = ""
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                name, sep, value = line.partition("=")
                if not sep or name.strip() != "RUNPOD_API_KEY":
                    raise ProviderError("Provider env_file may contain only RUNPOD_API_KEY")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                key = value
    if not key or any(char.isspace() for char in key):
        raise ProviderError("RUNPOD_API_KEY is missing or invalid")
    return key


class _StdioMCP:
    """One serialized JSON-RPC stream, with bounded reads and no shell invocation."""

    def __init__(self, config):
        self.timeout = _number(config.get("timeout_seconds", 45), "MCP timeout", 0.01)
        self.key = _api_key(config)
        command = config.get("command", ["npx", "-y", "@runpod/mcp-server@4.0.0"])
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise ProviderError("MCP command must be an argv list")
        # Do not forward API-host overrides or unrelated service credentials.
        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "SSL_CERT_FILE",
                                        "NODE_EXTRA_CA_CERTS") if k in os.environ}
        env["RUNPOD_API_KEY"] = self.key
        self.lock = threading.RLock()
        self.buffer = b""
        self.sequence = 0
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=env, start_new_session=True)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                        "clientInfo": {"name": "research-controller", "version": "0.1"}})
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.tools = {}
            cursor = None
            while True:
                response = self.request("tools/list", {"cursor": cursor} if cursor else {})
                self.tools.update({tool["name"]: tool["inputSchema"] for tool in response["tools"]})
                next_cursor = response.get("nextCursor")
                if not next_cursor:
                    break
                if next_cursor == cursor:
                    raise ProviderError("MCP tools/list cursor did not advance")
                cursor = next_cursor
        except Exception:
            self.close()
            raise

    def _send(self, payload):
        self.process.stdin.write(json.dumps(payload, allow_nan=False).encode() + b"\n")
        self.process.stdin.flush()

    def request(self, method, params):
        with self.lock:
            self.sequence += 1
            request_id = self.sequence
            deadline = time.monotonic() + self.timeout
            try:
                self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                while True:
                    if time.monotonic() >= deadline:
                        raise ProviderError("MCP request timed out; provider outcome may be unknown")
                    if b"\n" not in self.buffer:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not self.selector.select(remaining):
                            raise ProviderError("MCP request timed out; provider outcome may be unknown")
                        chunk = os.read(self.process.stdout.fileno(), 65536)
                        if not chunk:
                            raise ProviderError("MCP server disconnected")
                        self.buffer += chunk
                        if len(self.buffer) > 16 * 1024 * 1024:
                            raise ProviderError("MCP response exceeds 16 MiB")
                        continue
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    message = json.loads(line)
                    if "method" in message:
                        if "id" in message:
                            self._send({"jsonrpc": "2.0", "id": message["id"],
                                        "error": {"code": -32601, "message": "Client method not supported"}})
                        continue
                    if message.get("id") != request_id:
                        raise ProviderError("MCP response ID mismatch")
                    if "error" in message:
                        raise ProviderError("MCP protocol error: " + json.dumps(message["error"]).replace(self.key, "[redacted]"))
                    return message["result"]
            except (OSError, ValueError, KeyError, ProviderError) as exc:
                self.close()
                raise ProviderError(str(exc).replace(self.key, "[redacted]")) from None

    def call(self, name, arguments):
        schema = self.tools.get(name)
        if schema is None:
            raise ProviderError(f"Pinned MCP server does not expose {name}")
        required = set(schema.get("required", []))
        allowed = set(schema.get("properties", {}))
        if not required <= arguments.keys() or not arguments.keys() <= allowed:
            raise ProviderError(f"MCP schema changed for {name}; refusing to guess arguments")
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        payload = response.get("structuredContent")
        if payload is None:
            texts = [entry["text"] for entry in response.get("content", []) if entry.get("type") == "text"]
            try:
                payload = json.loads("\n".join(texts))
            except ValueError:
                raise ProviderError(f"MCP {name} returned non-JSON content") from None
        if response.get("isError"):
            # v2 ErrorResponse.status is numeric and preserved by the official
            # MCP server. Empty-body errors or prose alone do not prove absence.
            if name == "get-pod" and isinstance(payload, dict) and payload.get("status") == 404:
                raise PodNotFound(arguments["id"])
            detail = json.dumps(payload).replace(self.key, "[redacted]")
            status = payload.get("status") if isinstance(payload, dict) else None
            if name == "create-pod" and type(status) is int and status in {400, 401, 403, 404, 422}:
                raise CreateRejected(f"MCP create-pod rejected: {detail[:1200]}", status)
            raise ProviderError(f"MCP {name} failed: {detail[:1200]}")
        return payload

    def close(self):
        with self.lock:
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=2)
                except ProcessLookupError:
                    pass
            self.selector.close()
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()


class MCPProvider:
    def __init__(self, config):
        self.config = dict(config)
        self.client = None

    def _call(self, name, arguments):
        if name not in {"list-gpu-types", "create-pod", "get-pod", "list-pods", "pod-action"}:
            raise ProviderError("This adapter exposes only quoting and stop/retain pod lifecycle")
        if name == "pod-action" and arguments.get("body", {}).get("action") not in ("start", "stop"):
            raise ProviderError("Only start and stop actions are permitted")
        if self.client is None:
            self.client = _StdioMCP(self.config)
        try:
            return self.client.call(name, arguments)
        except ProviderError:
            self.close()
            raise

    def quote(self, request):
        request = _request(request)
        args = {"include": ["AVAILABILITY"], "product": ["POD"],
                "cloud": request["cloud"], "count": request["gpu_count"]}
        payload = self._call("list-gpu-types", args)
        wanted = request["gpu_type"].casefold()
        matches = [gpu for gpu in payload["gpus"] if wanted in
                   (gpu.get("id", "").casefold(), gpu.get("name", "").casefold())]
        if len(matches) != 1:
            raise ProviderError("gpu_type must match one exact catalog ID or name")
        gpu = matches[0]
        tier = request["cloud"].lower()
        if gpu.get("availability") in (None, "NONE"):
            raise ProviderError("Requested GPU configuration is unavailable")
        if gpu.get("maxCount", {}).get(tier, 0) < request["gpu_count"]:
            raise ProviderError("Requested GPU count exceeds the catalog limit")
        compute = _number(gpu.get("price", {}).get(tier), "GPU quote", 0.000001) * request["gpu_count"]
        storage = _storage_rate(self.config, request["volume_gb"], request["container_disk_gb"])
        return {**request, "gpu_type": gpu["id"], "hourly_usd": compute + storage,
                "compute_hourly_usd": compute, "storage_hourly_usd": storage,
                "quoted_at": time.time(), "rate_is_estimate": True}

    def _normalize(self, pod):
        status = pod.get("status", "UNKNOWN").upper()
        status = "STOPPED" if status == "EXITED" else status
        gpu = pod.get("gpu") or {}
        persistent = (pod.get("mounts") or {}).get("persistent") or {}
        volume = _number(persistent.get("size", 0), "pod persistent volume")
        disk = _number(pod.get("disk"), "pod container disk")
        stopped = status in ("STOPPED", "TERMINATED")
        provider_rate = _number(pod.get("cost"), "pod hourly cost", 0 if stopped else 0.000001)
        # Preserve a conservative storage allowance even if provider `cost`
        # already includes some storage. An uncertain bill must not become zero.
        hourly = 0.0 if stopped else provider_rate + _storage_rate(self.config, volume, disk)
        direct = (pod.get("ssh") or {}).get("direct")
        ssh = ({"host": direct["host"], "port": direct["port"], "user": direct["username"]}
               if direct else None)
        return {"id": pod["id"], "name": pod.get("name", ""), "status": status,
                "hourly_usd": hourly, "provider_hourly_usd": provider_rate,
                "rate_is_estimate": True, "gpu_type": gpu.get("id"),
                "gpu_count": gpu.get("count", 0), "volume_gb": volume,
                "container_disk_gb": disk, "ssh": ssh, "created_at": pod.get("createdAt"),
                "started_at": pod.get("startedAt"), "actions": pod.get("actions", [])}

    def create(self, name, request):
        request = _request(request)
        if not request.get("image") and not request.get("template_id"):
            raise ProviderError("An approved image or template_id is required")
        body = {"name": name, "gpu": {"id": request["gpu_type"], "count": request["gpu_count"]},
                "cloud": request["cloud"], "disk": request["container_disk_gb"],
                "mounts": {"persistent": {"path": "/workspace", "size": request["volume_gb"]}},
                "ports": ["22/tcp"], "startSsh": True}
        for source, target in (("image", "image"), ("template_id", "templateId")):
            if request.get(source):
                body[target] = request[source]
        pod = None
        try:
            pod = self._call("create-pod", {"body": body})
            return self._normalize(pod)
        except CreateRejected:
            raise
        except (ProviderError, KeyError, TypeError, ValueError) as exc:
            pod_id = pod.get("id") if isinstance(pod, dict) else None
            raise AmbiguousMutation(f"Create requires reconciliation: {exc}", pod_id) from None

    def get(self, pod_id):
        return self._normalize(self._call("get-pod", {"id": pod_id}))

    def list(self):
        pods, seen, seen_ids = [], set(), set()
        args = {"limit": 1000}
        while True:
            page = self._call("list-pods", args)
            if not isinstance(page, dict) or not isinstance(page.get("pods"), list):
                raise ProviderError("Pod list response is malformed")
            pagination = page.get("pagination")
            if not isinstance(pagination, dict) or not isinstance(pagination.get("hasNextPage"), bool):
                raise ProviderError("Pod list lacks complete pagination metadata")
            for pod in page["pods"]:
                if not isinstance(pod, dict) or not pod.get("id") or pod["id"] in seen_ids:
                    raise ProviderError("Pod list has missing or duplicate IDs")
                seen_ids.add(pod["id"])
                try:
                    pods.append(self._normalize(pod))
                except ProviderError as exc:
                    # Reconciliation needs the identity of an unpriced failed
                    # create. Never erase it, or reinterpret an unknown rate as 0.
                    status = pod.get("status", "UNKNOWN")
                    pods.append({"id": pod["id"], "name": pod.get("name", ""),
                                 "status": "STOPPED" if status == "EXITED" else status,
                                 "hourly_usd": None, "rate_error": str(exc), "ssh": None})
            if not pagination["hasNextPage"]:
                if pagination.get("nextCursor") is not None:
                    raise ProviderError("Pod list pagination is inconsistent")
                return pods
            cursor = pagination.get("nextCursor")
            if not cursor or cursor in seen:
                raise ProviderError("Pod pagination did not advance")
            seen.add(cursor)
            args["cursor"] = cursor

    def _action(self, pod_id, action):
        try:
            self._call("pod-action", {"id": pod_id, "body": {"action": action}})
            return self.get(pod_id)
        except ProviderError as exc:
            raise AmbiguousMutation(f"{action} requires reconciliation: {exc}", pod_id) from None

    def start(self, pod_id):
        return self._action(pod_id, "start")

    def stop(self, pod_id):
        return self._action(pod_id, "stop")

    def close(self):
        if self.client:
            self.client.close()
            self.client = None


class FakeProvider:
    """Persistent, process-safe lifecycle simulation; it never contacts RunPod."""

    def __init__(self, config):
        self.config = dict(config)
        self.path = Path(config["fake_state"])
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _state(self, operation):
        with open(str(self.path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(self.path.read_text()) if self.path.exists() else {"next_id": 1, "pods": {}}
            result = operation(state)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(json.dumps(state, indent=2, allow_nan=False) + "\n")
            os.chmod(temp, 0o600)
            temp.replace(self.path)
            return result

    def quote(self, request):
        request = _request(request)
        price = _number(self.config.get("hourly_per_gpu", 1.0), "fake hourly_per_gpu", 0.000001)
        storage = _storage_rate(self.config, request["volume_gb"], request["container_disk_gb"])
        compute = price * request["gpu_count"]
        return {**request, "hourly_usd": compute + storage, "compute_hourly_usd": compute,
                "storage_hourly_usd": storage, "quoted_at": time.time(), "rate_is_estimate": True}

    def create(self, name, request):
        quote = self.quote(request)
        def create(state):
            pod_id = f"fake-{state['next_id']:06d}"
            state["next_id"] += 1
            pod = {**quote, "id": pod_id, "name": name, "status": "RUNNING", "ssh": None,
                   "created_at": time.time(), "started_at": time.time(),
                   "active_hourly_usd": quote["hourly_usd"], "actions": ["stop"]}
            state["pods"][pod_id] = pod
            return dict(pod)
        return self._state(create)

    def get(self, pod_id):
        def get(state):
            if pod_id not in state["pods"]:
                raise ProviderError(f"Unknown fake pod {pod_id}")
            return dict(state["pods"][pod_id])
        return self._state(get)

    def list(self):
        return self._state(lambda state: [dict(pod) for pod in state["pods"].values()])

    def _action(self, pod_id, active):
        def change(state):
            if pod_id not in state["pods"]:
                raise ProviderError(f"Unknown fake pod {pod_id}")
            pod = state["pods"][pod_id]
            pod["status"] = "RUNNING" if active else "STOPPED"
            pod["hourly_usd"] = pod["active_hourly_usd"] if active else 0.0
            pod["actions"] = ["stop"] if active else ["start"]
            if active:
                pod["started_at"] = time.time()
            return dict(pod)
        return self._state(change)

    def start(self, pod_id):
        return self._action(pod_id, True)

    def stop(self, pod_id):
        return self._action(pod_id, False)

    def close(self):
        pass


def make_provider(config):
    kind = config.get("kind", "fake")
    if kind == "fake":
        return FakeProvider(config)
    if kind == "mcp":
        return MCPProvider(config)
    raise ProviderError("Provider kind must be fake or mcp")
