"""One immutable experiment bundle per worker, over direct SSH or local demo.

This module is also the small stdlib-only worker copied to the retained volume.
Its runtime limit kills experiment processes; only the external controller can
release a billed GPU. No provider credentials are needed here.
"""

import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time


class RunnerError(RuntimeError):
    pass


_PRIVATE_DIRS = {".git", ".aws", ".ssh", ".codex", ".claude", ".config", ".venv", "venv",
                 "node_modules", "__pycache__", ".cache", ".pytest_cache", ".mypy_cache"}
_PRIVATE_FILES = {"auth.json", "credentials", "credentials.json", "secrets.json", "api_keys.json", "id_rsa", "id_ed25519",
                  "id_ecdsa", ".netrc", ".npmrc", ".pypirc", "token", "token.json"}
_TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "INTERRUPTED"}


def _experiment_env():
    # Trusted worker templates may set HF_TOKEN, WANDB_API_KEY, library paths,
    # and dataset credentials. Preserve them without forwarding pod-management
    # keys to either the detached supervisor or the experiment process.
    env = {key: value for key, value in os.environ.items()
           if not (key.upper().startswith("RUNPOD_") and
                   ("KEY" in key.upper() or "TOKEN" in key.upper()))}
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _relative(path):
    value = PurePosixPath(path)
    if not path or value.is_absolute() or any(part in ("", ".", "..") for part in path.split("/")):
        raise RunnerError("Paths must be explicit relative paths without . or .. components")
    if "\\" in path or "\x00" in path:
        raise RunnerError("Invalid relative path")
    return value


def _private(path):
    path = PurePosixPath(path)
    return (any(part in _PRIVATE_DIRS for part in path.parts)
            or path.name in _PRIVATE_FILES or path.name.startswith(".env")
            or path.suffix.lower() in (".env", ".pem", ".key", ".p12", ".pfx"))


def _atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temp.open("w") as handle:
        json.dump(value, handle, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def _regular(path, base):
    """Reject a symlink anywhere in the path, including an ancestor."""
    path, base = Path(path), Path(base)
    relative = path.relative_to(base)
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RunnerError(f"Symlinks are not transferred: {relative}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise RunnerError(f"Not a regular file: {relative}")
    return info


def snapshot(repo, dest, exclude=(), max_bytes=64 * 1024 * 1024):
    """Capture tracked + unignored files, including edits, without copying .git."""
    repo, dest = Path(repo).resolve(), Path(dest).resolve()
    exclusions = []
    for item in exclude:
        item = Path(item)
        if item.is_absolute():
            try:
                item = item.relative_to(repo)
            except ValueError:
                continue
        exclusions.append(str(_relative(str(item))))
    listing = subprocess.run(["git", "-C", str(repo), "ls-files", "--cached", "--others",
                              "--exclude-standard", "-z"], capture_output=True, check=True).stdout
    names = sorted(set(os.fsdecode(item) for item in listing.split(b"\0") if item))
    dest.mkdir(parents=True, exist_ok=False)
    files, skipped, total = [], [], 0
    try:
        with tarfile.open(dest / "source.tar.gz", "w:gz") as archive:
            for name in names:
                _relative(name)
                source = repo / name
                if (_private(name) or any(name == item or name.startswith(item + "/") for item in exclusions)
                        or source == dest or dest in source.parents):
                    skipped.append(name)
                    continue
                if not source.exists() and not source.is_symlink():
                    continue  # A tracked deletion is part of the current working tree.
                try:
                    info = _regular(source, repo)
                except RunnerError:
                    skipped.append(name)
                    continue
                total += info.st_size
                if total > max_bytes:
                    raise RunnerError("Source snapshot is too large; exclude datasets, outputs, or caches")
                data = source.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                mode = 0o755 if info.st_mode & 0o111 else 0o644
                entry = tarfile.TarInfo(name)
                entry.size, entry.mode, entry.mtime = len(data), mode, 0
                archive.addfile(entry, io.BytesIO(data))
                files.append({"path": name, "sha256": digest, "bytes": len(data), "mode": mode})
        source_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        manifest = {"source_sha256": source_hash, "git_commit": head.stdout.strip() if head.returncode == 0 else None,
                    "files": files, "excluded": skipped, "created_at": time.time(),
                    "archive_sha256": hashlib.sha256((dest / "source.tar.gz").read_bytes()).hexdigest()}
        _atomic_json(dest / "manifest.json", manifest)
        return manifest
    except Exception:
        # Leave failed snapshots for diagnosis; they cannot pass _unpack validation.
        raise


def _extract(archive, dest, max_bytes):
    """Never use extractall on data returned by a worker."""
    # The caller chooses this destination. Canonicalize OS aliases (/tmp on
    # macOS); reject archive-controlled symlinks below this trusted root.
    dest = Path(dest).resolve()
    total, seen = 0, set()
    for member in archive:
        name = str(_relative(member.name))
        if not member.isfile() or name in seen:
            raise RunnerError("Archive contains links, special files, or duplicate paths")
        seen.add(name)
        total += member.size
        if member.size < 0 or total > max_bytes:
            raise RunnerError("Archive exceeds the transfer limit")
        target = dest / name
        current = dest
        for part in PurePosixPath(name).parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise RunnerError("Archive destination contains a symlink")
            current.mkdir(exist_ok=True)
        if target.is_symlink():
            raise RunnerError("Archive destination is a symlink")
        staging = target.with_name(target.name + f".{os.getpid()}.partial")
        with archive.extractfile(member) as source, staging.open("xb") as output:
            while chunk := source.read(65536):
                output.write(chunk)
        staging.chmod(0o755 if member.mode & 0o111 else 0o644)
        staging.replace(target)


def _publish_collection(archive, dest, max_bytes):
    """Verify in isolation, then replace only a previously managed bundle."""
    dest = Path(dest)
    if dest.is_symlink() or dest.name in ("", ".", ".."):
        raise RunnerError("Collection destination must be a non-symlink directory")
    dest = dest.parent.resolve() / dest.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    marker = ".research-compute-collection"
    ownership = b"research-compute collection v1\n"

    def verify(folder, managed=False):
        manifest = folder / "collection.json"
        if _regular(manifest, folder).st_size > max_bytes:
            raise RunnerError("Collection manifest exceeds the transfer limit")
        try:
            entries = json.loads(manifest.read_text())["files"]
            if not isinstance(entries, list) or len(entries) > 2054:
                raise ValueError("invalid file list")
            expected, total = {"collection.json"}, manifest.stat().st_size
            for entry in entries:
                name = str(_relative(entry["path"]))
                if name in expected or name == marker:
                    raise ValueError("duplicate or reserved collection path")
                expected.add(name)
                path = folder / name
                size = _regular(path, folder).st_size
                total += size
                if total > max_bytes or size != entry["bytes"]:
                    raise ValueError("collection size mismatch")
                if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                    raise ValueError("collection checksum mismatch")
            if managed:
                expected.add(marker)
            actual = set()
            for path in folder.rglob("*"):
                if path.is_dir() and not path.is_symlink():
                    continue
                _regular(path, folder)
                actual.add(str(path.relative_to(folder)))
            if actual != expected:
                raise ValueError("collection contains unlisted files")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise RunnerError(f"Invalid collection: {exc}") from None

    with tempfile.TemporaryDirectory(prefix=f".{dest.name}.collect-", dir=dest.parent) as temporary:
        temporary = Path(temporary)
        fresh, previous = temporary / "new", temporary / "previous"
        fresh.mkdir()
        _extract(archive, fresh, max_bytes)
        verify(fresh)
        (fresh / marker).write_bytes(ownership)
        lock = dest.with_name(f".{dest.name}.collection.lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if dest.is_symlink():
                raise RunnerError("Collection destination must not be a symlink")
            if dest.exists():
                owner = dest / marker
                if (not dest.is_dir() or owner.is_symlink() or not owner.is_file()
                        or owner.stat().st_size != len(ownership) or owner.read_bytes() != ownership):
                    raise RunnerError("Refusing to replace an unowned collection directory")
                verify(dest, managed=True)
                dest.rename(previous)
            try:
                fresh.rename(dest)
            except BaseException:
                if previous.exists():
                    previous.rename(dest)
                raise
    return str(dest)


def _unpack(root):
    manifest = json.loads((root / "manifest.json").read_text())
    archive_path = root / "source.tar.gz"
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != manifest["archive_sha256"]:
        raise RunnerError("Source archive checksum mismatch")
    source = root / "source"
    source.mkdir()
    with tarfile.open(archive_path, "r:gz") as archive:
        _extract(archive, source, 64 * 1024 * 1024)
    actual = set()
    for entry in manifest["files"]:
        relative = str(_relative(entry["path"]))
        path = source / relative
        _regular(path, source)
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise RunnerError("Source file checksum mismatch")
        actual.add(relative)
    if actual != {str(path.relative_to(source)) for path in source.rglob("*") if path.is_file()}:
        raise RunnerError("Source archive does not match its manifest")


def _identity(pid):
    try:
        proc = Path(f"/proc/{pid}/stat")
        if proc.exists():
            fields = proc.read_text().rsplit(")", 1)[1].split()
            if fields[0] in ("Z", "X"):
                return None
            return {"pid": pid, "start": fields[19],
                    "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
        # The local demo also works on macOS. Linux workers use /proc above.
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
                                capture_output=True, text=True)
        output = result.stdout.strip()
        if result.returncode or not output or output.split()[-1].startswith("Z"):
            return None
        return {"pid": pid, "start": output.rsplit(None, 1)[0], "boot": "local"}
    except (OSError, IndexError):
        return None


def _status(root):
    status = json.loads((root / "status.json").read_text())
    if status["state"] not in _TERMINAL and status.get("supervisor"):
        if _identity(status["supervisor"]["pid"]) != status["supervisor"]:
            status = {**status, "state": "INTERRUPTED", "ended_at": time.time(), "exit_code": None}
            _atomic_json(root / "status.json", status)
    return {**status, "status": status["state"]}


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)  # Give a checkpoint-aware SIGTERM handler time to finish.
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _worker_run(root):
    spec = json.loads((root / "job.json").read_text())
    started = time.time()
    status = {"state": "RUNNING", "started_at": started, "exit_code": None,
              "supervisor": _identity(os.getpid()), "source_sha256": spec["source_sha256"]}
    _atomic_json(root / "status.json", status)
    child = None
    try:
        if status["supervisor"] is None:
            raise RunnerError("Process identity is unavailable; cannot supervise this worker reliably")
        _unpack(root)
        env = _experiment_env()
        with (root / "stdout.log").open("wb") as stdout, (root / "stderr.log").open("wb") as stderr:
            child = subprocess.Popen(spec["command"], cwd=root / "source", env=env,
                                     stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                     start_new_session=True)
            status["process"] = _identity(child.pid)
            _atomic_json(root / "status.json", status)
            deadline = time.monotonic() + spec["timeout_seconds"]
            state = None
            while child.poll() is None:
                if (root / "cancel.request").exists():
                    state = "CANCELLED"
                    break
                if time.monotonic() >= deadline:
                    state = "TIMED_OUT"
                    break
                time.sleep(0.1)
            _kill_group(child)  # Also clean children left behind by an exited launcher.
            code = child.wait()
            status.update(state=state or ("SUCCEEDED" if code == 0 else "FAILED"), exit_code=code)
    except Exception as exc:
        if child is not None:
            try:
                _kill_group(child)
            except OSError:
                status["cleanup_error"] = "Process-group cleanup failed; the controller lease must stop the pod"
        status.update(state="FAILED", exit_code=None, error=str(exc))
    status["ended_at"] = time.time()
    _atomic_json(root / "status.json", status)


def _worker_launch(root):
    _atomic_json(root / "status.json", {"state": "STARTING", "exit_code": None})
    with (root / "worker.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, str(root / "worker.py"), "--worker", "run", str(root)],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                                   env=_experiment_env())
    threading.Thread(target=process.wait, daemon=True).start()
    return {"state": "STARTING", "exit_code": None}


def _output_bundle(root, output):
    spec = json.loads((root / "job.json").read_text())
    source = root / "source"
    limit = spec["max_output_bytes"]
    status = _status(root)
    collection = {"captured_at": time.time(), "source_sha256": spec["source_sha256"],
                  "job_status": status["status"], "provisional": status["status"] not in _TERMINAL,
                  "log_tail_bytes": 128 * 1024, "files": []}
    paths = {}
    for relative in spec["outputs"]:
        relative = str(_relative(relative))
        path = source / relative
        if not path.exists() and not path.is_symlink():
            continue
        current = source
        for part in PurePosixPath(relative).parts:
            current = current / part
            if current.is_symlink():
                raise RunnerError("Symlinks are not transferred in output selections")
        candidates = [path] if not path.is_dir() else sorted(path.rglob("*"))
        for candidate in candidates:
            if candidate.is_dir() and not candidate.is_symlink():
                continue
            _regular(candidate, source)
            name = str(candidate.relative_to(source))
            if _private(name):
                raise RunnerError("Output selection contains a credential-like file")
            paths["outputs/" + name] = candidate
    if len(paths) > 2048:
        raise RunnerError("Too many output files; select compact results explicitly")
    total = 0
    with tarfile.open(fileobj=output, mode="w|") as archive:
        def add(name, data):
            nonlocal total
            total += len(data)
            if total > limit:
                raise RunnerError("Outputs exceed collection limit; large artifacts remain on the volume")
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
            collection["files"].append({"path": name, "bytes": len(data),
                                        "sha256": hashlib.sha256(data).hexdigest()})

        for name in ("manifest.json", "job.json", "status.json", "stdout.log", "stderr.log", "worker.log"):
            path = root / name
            if not path.exists():
                continue
            _regular(path, root)
            with path.open("rb") as stream:
                if name.endswith(".log"):
                    stream.seek(max(0, path.stat().st_size - 128 * 1024))
                data = stream.read(limit - total + 1)
            add(name, data)
        for name, path in sorted(paths.items()):
            info = _regular(path, source)
            if total + info.st_size > limit:
                raise RunnerError("Outputs exceed collection limit; large artifacts remain on the volume")
            with path.open("rb") as stream:
                data = stream.read(limit - total + 1)
                after = os.fstat(stream.fileno())
            if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RunnerError("Output changed during collection; retry after an atomic output write")
            add(name, data)
        add("collection.json", json.dumps(collection, sort_keys=True).encode() + b"\n")


def _spec(snapshot_dir, session_id, job_id, command, timeout_s, outputs, max_output_bytes):
    for value in (session_id, job_id):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", value):
            raise RunnerError("Session/job IDs must be simple path components")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and "\x00" not in x for x in command):
        raise RunnerError("command must be a nonempty argv list; use bash -lc explicitly for shell code")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not 0 < timeout_s <= 7 * 86400:
        raise RunnerError("timeout_s must be finite and between 0 and seven days")
    if not isinstance(max_output_bytes, int) or not 1024 <= max_output_bytes <= 256 * 1024 * 1024:
        raise RunnerError("max_output_bytes must be between 1 KiB and 256 MiB")
    outputs = [str(_relative(str(path))) for path in outputs]
    manifest = json.loads((Path(snapshot_dir) / "manifest.json").read_text())
    return {"session_id": session_id, "job_id": job_id, "command": command,
            "timeout_seconds": timeout_s, "outputs": outputs, "max_output_bytes": max_output_bytes,
            "source_sha256": manifest["source_sha256"]}


def _package(snapshot_dir, spec, stream):
    with tarfile.open(fileobj=stream, mode="w|") as archive:
        for name, path in (("source.tar.gz", Path(snapshot_dir) / "source.tar.gz"),
                           ("manifest.json", Path(snapshot_dir) / "manifest.json"),
                           ("worker.py", Path(__file__))):
            data = path.read_bytes()
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
        data = json.dumps(spec, allow_nan=False).encode()
        entry = tarfile.TarInfo("job.json")
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))


class LocalRunner:
    """Offline demo using the same worker and collection checks as SSH execution."""
    def __init__(self, root=None):
        default = Path(tempfile.gettempdir()) / f"research-agents-{os.getuid()}" / "workers"
        self.root = Path(root or os.environ.get("RESEARCH_COMPUTE_LOCAL_ROOT", default)).resolve()

    def launch(self, snapshot_dir, session_id, job_id, command, timeout_s, outputs,
               max_output_bytes=16 * 1024 * 1024):
        spec = _spec(snapshot_dir, session_id, job_id, command, timeout_s, outputs, max_output_bytes)
        root = self.root / session_id / job_id
        root.mkdir(parents=True, exist_ok=False)
        with tempfile.TemporaryFile() as stream:
            _package(snapshot_dir, spec, stream)
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                _extract(archive, root, 72 * 1024 * 1024)
        _worker_launch(root)
        return {"session_id": session_id, "job_id": job_id, "path": str(root), "transport": "local"}

    def _root(self, job):
        root = Path(job["path"]).resolve()
        if self.root not in root.parents:
            raise RunnerError("Job is outside the runner root")
        return root

    def poll(self, job):
        return _status(self._root(job))

    def cancel(self, job):
        (self._root(job) / "cancel.request").touch()
        return self.poll(job)

    def collect(self, job, dest):
        root, dest = self._root(job), Path(dest)
        limit = json.loads((root / "job.json").read_text())["max_output_bytes"]
        with tempfile.TemporaryFile() as stream:
            _output_bundle(root, stream)
            stream.seek(0)
            status = self.poll(job)
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                path = _publish_collection(archive, dest, limit)
        return {"path": path, "status": status}


class SSHRunner:
    def __init__(self, ssh, ssh_key=None, known_hosts=None, timeout_seconds=30,
                 root="/workspace/research"):
        self.ssh = dict(ssh)
        self.timeout = timeout_seconds
        self.root = root.rstrip("/")
        if not self.root.startswith("/workspace/") or ".." in PurePosixPath(self.root).parts:
            raise RunnerError("Worker root must be under the retained /workspace volume")
        host, user = ssh.get("host", ""), ssh.get("user", "")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", host) or host.startswith("-"):
            raise RunnerError("Invalid SSH host")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", user) or user.startswith("-"):
            raise RunnerError("Invalid SSH user")
        port = int(ssh["port"])
        if not 1 <= port <= 65535:
            raise RunnerError("Invalid SSH port")
        self.argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                     "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
                     "-o", "ServerAliveCountMax=2", "-p", str(port)]
        if ssh_key:
            self.argv += ["-i", str(ssh_key), "-o", "IdentitiesOnly=yes"]
        if known_hosts:
            self.argv += ["-o", f"UserKnownHostsFile={known_hosts}"]
        self.argv.append(f"{user}@{host}")

    def _run(self, command, stdin=None, stdout=subprocess.PIPE):
        result = subprocess.run(self.argv + [shlex.join(command)], stdin=stdin, stdout=stdout,
                                stderr=subprocess.PIPE, timeout=self.timeout)
        if result.returncode:
            raise RunnerError(f"SSH runner failed (exit {result.returncode}): " +
                              result.stderr.decode(errors="replace")[-2000:])
        return result.stdout

    def _root(self, job):
        path = PurePosixPath(job["path"])
        if PurePosixPath(self.root) not in path.parents or ".." in path.parts:
            raise RunnerError("Job is outside the worker root")
        return str(path)

    def launch(self, snapshot_dir, session_id, job_id, command, timeout_s, outputs,
               max_output_bytes=16 * 1024 * 1024):
        spec = _spec(snapshot_dir, session_id, job_id, command, timeout_s, outputs, max_output_bytes)
        root = f"{self.root}/{session_id}/{job_id}"
        # Inputs have four fixed regular-file names. The destination is created
        # exclusively so a repeated launch cannot overwrite or rerun a bundle.
        prepare = "\n".join([
            "import pathlib,sys,tarfile",
            "mounts={line.split()[4] for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines()}",
            "if '/workspace' not in mounts: raise ValueError('/workspace is not a mounted persistent volume')",
            "root=pathlib.Path(sys.argv[1])",
            "if any(p.is_symlink() for p in (root,*root.parents)): raise ValueError('Worker path contains a symlink')",
            "root.mkdir(parents=True,exist_ok=False)",
            "allowed={'source.tar.gz','manifest.json','worker.py','job.json'}",
            "with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as archive:",
            " for member in archive:",
            "  if member.name not in allowed or not member.isfile() or member.size>72*1024*1024: raise ValueError('Invalid input bundle')",
            "  allowed.remove(member.name)",
            "  (root/member.name).write_bytes(archive.extractfile(member).read())",
            "if allowed: raise ValueError('Incomplete input bundle')",
        ])
        with tempfile.TemporaryFile() as stream:
            _package(snapshot_dir, spec, stream)
            stream.seek(0)
            self._run(["python3", "-c", prepare, root], stdin=stream)
        self._run(["python3", f"{root}/worker.py", "--worker", "launch", root])
        return {"session_id": session_id, "job_id": job_id, "path": root,
                "transport": "ssh", "max_output_bytes": max_output_bytes}

    def _command(self, job, action):
        root = self._root(job)
        return ["python3", f"{root}/worker.py", "--worker", action, root]

    def poll(self, job):
        return json.loads(self._run(self._command(job, "status")))

    def cancel(self, job):
        return json.loads(self._run(self._command(job, "cancel")))

    def collect(self, job, dest):
        with tempfile.TemporaryFile() as stream:
            self._run(self._command(job, "bundle"), stdout=stream)
            limit = job.get("max_output_bytes", 16 * 1024 * 1024)
            if stream.tell() > limit + 2 * 1024 * 1024:
                raise RunnerError("Returned output bundle exceeds the collection limit")
            stream.seek(0)
            status = self.poll(job)
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                path = _publish_collection(archive, dest, limit)
        return {"path": path, "status": status}


def _main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", choices=["run", "launch", "status", "cancel", "bundle"], required=True)
    parser.add_argument("path")
    args = parser.parse_args()
    root = Path(args.path).resolve()
    if args.worker == "run":
        _worker_run(root)
    elif args.worker == "bundle":
        _output_bundle(root, sys.stdout.buffer)
    else:
        if args.worker == "launch":
            result = _worker_launch(root)
        else:
            if args.worker == "cancel":
                (root / "cancel.request").touch()
            result = _status(root)
        print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    _main()
