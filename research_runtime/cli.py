"""Thin internal CLI for agents and the service installer."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .control import PolicyError, CapacityError


def read_json(path):
    return json.loads(Path(path).read_text()) if path != "-" else json.load(sys.stdin)


def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("daemon", "watchdog"):
        s = sub.add_parser(name)
        s.add_argument("--config", required=True)
        s.add_argument("--state-dir", required=True)
        if name == "daemon":
            s.add_argument("--socket", default="/run/research-compute/control.sock")
        else:
            s.add_argument("--once", action="store_true")
    sub.add_parser("doctor")
    s = sub.add_parser("session").add_subparsers(dest="action", required=True)
    c = s.add_parser("create")
    c.add_argument("--policy", required=True, help="JSON file or - for stdin")
    s.add_parser("list")
    for name in ("status", "pause", "resume", "finish", "block", "review", "mode", "driver", "journal", "bind"):
        c = s.add_parser(name)
        c.add_argument("session_id")
        if name in ("pause", "finish", "block", "review"):
            c.add_argument("--reason", default=name)
        elif name == "resume":
            c.add_argument("--user-confirmed", action="store_true", help="record an actual user continuation instruction")
        elif name == "mode":
            c.add_argument("mode", choices=("interactive", "auto"))
        elif name == "driver":
            c.add_argument("driver", choices=("hook", "native"))
        elif name == "journal":
            c.add_argument("--text", required=True)
        elif name == "bind":
            binding_args(c)
            c.add_argument("--take-over", action="store_true", help="explicitly transfer a paused investigation from its previous conversation")
    c = s.add_parser("continuation")
    binding_args(c)
    for name in ("quote", "allocate"):
        c = sub.add_parser(name)
        c.add_argument("--session", dest="session_id", required=True)
        c.add_argument("--request", required=True, help="JSON allocation request")
    for name in ("get", "stop"):
        c = sub.add_parser("allocation-" + name)
        c.add_argument("--session", dest="session_id", required=True)
        c.add_argument("allocation_id")
    for name in ("jobs", "wait"):
        c = sub.add_parser(name)
        c.add_argument("--session", dest="session_id", required=True)
        if name == "wait":
            c.add_argument("--timeout-seconds", type=int, default=60)
    c = sub.add_parser("run")
    c.add_argument("--session", dest="session_id", required=True)
    c.add_argument("--spec", required=True, help="JSON {repo,command:[...],request:{...},outputs:[...],timeout_seconds}")
    c.add_argument("--foreground", action="store_true")
    c.add_argument("--local", action="store_true", help="CPU-only runner; requires a fake provider")
    c = sub.add_parser("_run-job", help=argparse.SUPPRESS)
    c.add_argument("job_dir")
    c = sub.add_parser("collect")
    c.add_argument("job_dir")
    c = sub.add_parser("owner-amend", help="local service-owner operation; unavailable through the agent socket")
    c.add_argument("--config", required=True)
    c.add_argument("--state-dir", required=True)
    c.add_argument("--session", dest="session_id", required=True)
    c.add_argument("--patch", required=True)
    c = sub.add_parser("owner-resolve-no-pod", help="owner attestation after checking an ambiguous creation in RunPod")
    c.add_argument("--config", required=True)
    c.add_argument("--state-dir", required=True)
    c.add_argument("--allocation", required=True)
    c.add_argument("--reason", required=True)
    c.add_argument("--confirmed-no-pod", action="store_true", required=True)
    return p


def binding_args(p):
    p.add_argument("--agent", choices=("codex", "claude", "pi"), required=True)
    p.add_argument("--conversation-id", required=True)
    p.add_argument("--workspace", default=os.getcwd())


def request_call(method, args=None):
    # Lazy import so --help and instruction installation do not start MCP.
    from .service import call
    return call(method, args)


def start_job(args):
    from .runner import snapshot
    spec = read_json(args.spec)
    if not isinstance(spec.get("command"), list) or not spec["command"] or not all(isinstance(x, str) for x in spec["command"]):
        raise PolicyError("command must be a nonempty argv array")
    if args.local and request_call("doctor")["provider"] != "fake":
        raise PolicyError("--local is only available with a fake provider")
    sid = args.session_id
    status = request_call("session.status", {"session_id": sid})
    if status["status"] != "ACTIVE":
        raise PolicyError("investigation is not active")
    jid = uuid.uuid4().hex
    root = Path(os.environ.get("RESEARCH_COMPUTE_RUNS_DIR", Path.home() / ".local/state/research-agents/runs"))
    folder = root / jid
    folder.mkdir(parents=True, mode=0o700)
    manifest = snapshot(Path(spec["repo"]).resolve(), folder / "snapshot", exclude=spec.get("exclude", []))
    record = {"id": jid, "session_id": sid, "spec": spec, "local": args.local,
              "manifest": manifest, "status": "PREPARING", "created": time.time(),
              "local_root": str(folder / "worker")}
    write_json(folder / "job.json", record)
    request_call("job.record", {"session_id": sid, "job_id": jid, "allocation_id": None,
                                "status": "QUEUED", "detail": {"job_dir": str(folder)}})
    if args.foreground:
        return run_job(folder)
    try:
        with (folder / "supervisor.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, "-m", "research_runtime.cli", "_run-job", str(folder)],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                                       env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    except OSError:
        request_call("job.record", {"session_id": sid, "job_id": jid, "allocation_id": None,
                                    "status": "FAILED", "detail": {"job_dir": str(folder), "error": "could not start collector"}})
        raise
    return {"job_id": jid, "job_dir": str(folder), "supervisor_pid": process.pid, "status": "PREPARING"}


def get_runner(record):
    from .runner import LocalRunner, SSHRunner
    if record["local"]:
        return LocalRunner(record["local_root"])
    return SSHRunner(record["pod"]["ssh"], ssh_key=record["spec"].get("ssh_key"))


def run_job(folder):
    folder = Path(folder)
    record = read_json(folder / "job.json")
    sid, jid, spec = record["session_id"], record["id"], record["spec"]
    aid = None
    runner = None
    remote = None
    try:
        queue_until = time.monotonic() + max(0, min(86400, int(spec.get("queue_timeout_seconds", 3600))))
        while True:
            status = request_call("session.status", {"session_id": sid})
            if status["status"] != "ACTIVE":
                raise PolicyError("investigation stopped while job was queued")
            try:
                allocated = request_call("allocate", {"session_id": sid, "request": spec["request"]})
                break
            except CapacityError as exc:
                if time.monotonic() >= queue_until:
                    raise PolicyError("queue wait expired: " + str(exc)) from None
                record.update(status="QUEUED", queue_reason=str(exc))
                write_json(folder / "job.json", record)
                request_call("job.record", {"session_id": sid, "job_id": jid, "allocation_id": None,
                    "status": "QUEUED", "detail": {"job_dir": str(folder), "reason": str(exc)}})
                time.sleep(5)
        aid = allocated["allocation_id"]
        record["allocation_id"] = aid
        write_json(folder / "job.json", record)
        if "pod" not in allocated:
            raise RuntimeError(allocated.get("error", "allocation failed"))
        expiry = allocated["expires_at"]
        record["pod"] = allocated["pod"]
        def update(state, **extra):
            record.update(status=state, **extra)
            write_json(folder / "job.json", record)
            request_call("job.record", {"session_id": sid, "job_id": jid, "allocation_id": aid,
                                        "status": state, "detail": {"job_dir": str(folder),
                                        "source_sha256": record["manifest"]["archive_sha256"],
                                        "git_commit": record["manifest"].get("git_commit"), **extra}})
        update("QUEUED")
        while not record["local"] and not record["pod"].get("ssh"):
            if time.time() >= expiry - 30:
                raise RuntimeError("pod never became SSH-ready within admitted lease")
            time.sleep(2)
            view = request_call("allocation.get", {"session_id": sid, "allocation_id": aid})
            if view["state"] != "RUNNING":
                raise RuntimeError("allocation was stopped during provisioning")
            record["pod"] = view["pod"]
        write_json(folder / "job.json", record)
        runner = get_runner(record)
        timeout = min(int(spec.get("timeout_seconds", 300)), int(expiry - time.time() - 30))
        if timeout < 1:
            raise RuntimeError("no execution time remains after provisioning")
        remote = runner.launch(folder / "snapshot", sid, jid, spec["command"], timeout,
                               spec.get("outputs", ["results"]), max_output_bytes=int(spec.get("max_output_bytes", 64 * 1024 * 1024)))
        record["remote"] = remote
        update("RUNNING")
        last_collect = 0
        while True:
            state = runner.poll(remote)
            if time.time() - last_collect >= spec.get("collect_interval_seconds", 30):
                try:
                    runner.collect(remote, folder / "outputs")
                    record["collection_error"] = None
                except Exception as exc:
                    record["collection_error"] = type(exc).__name__
                last_collect = time.time()
            state_name = state.get("status", "UNKNOWN").upper()
            if state_name in ("SUCCEEDED", "FAILED", "INTERRUPTED", "TIMED_OUT", "CANCELLED"):
                result = "SUCCEEDED" if state_name == "SUCCEEDED" else "FAILED"
                try:
                    runner.collect(remote, folder / "outputs")
                    update(result, result=state, collection_error=None, collection_complete=True)
                except Exception as exc:
                    update("FAILED", result=state, collection_error=type(exc).__name__, collection_complete=False)
                break
            current = request_call("allocation.get", {"session_id": sid, "allocation_id": aid})
            if current["state"] != "RUNNING" or time.time() >= expiry - 15:
                runner.cancel(remote)
                grace_end = min(current.get("stop_after") or expiry, time.time() + 15)
                while time.time() < grace_end:
                    cancelled = runner.poll(remote).get("status", "UNKNOWN").upper()
                    if cancelled in ("SUCCEEDED", "FAILED", "INTERRUPTED", "TIMED_OUT", "CANCELLED"):
                        break
                    time.sleep(0.25)
                try:
                    runner.collect(remote, folder / "outputs")
                finally:
                    update("INTERRUPTED", reason="allocation ended")
                break
            time.sleep(min(2, max(0.1, expiry - time.time())))
    except Exception as exc:
        record.update(status="FAILED", error=str(exc))
        write_json(folder / "job.json", record)
        try:
            request_call("job.record", {"session_id": sid, "job_id": jid, "allocation_id": aid,
                                        "status": "FAILED", "detail": {"job_dir": str(folder), "error": str(exc)}})
        except Exception:
            pass  # Independent lease expiry still protects paid resources.
    finally:
        if aid:
            try:
                stopped = request_call("allocation.stop", {"session_id": sid, "allocation_id": aid})
                record["stop_confirmed"] = next((a["state"] in ("STOPPED", "TERMINATED") for a in stopped["allocations"] if a["id"] == aid), False)
            except Exception:
                record["stop_confirmed"] = False
            write_json(folder / "job.json", record)
    return record


def main():
    args = parser().parse_args()
    try:
        if args.command in ("daemon", "watchdog"):
            from .service import serve, watchdog
            if args.command == "daemon":
                serve(args.config, args.state_dir, args.socket)
            else:
                watchdog(args.config, args.state_dir, args.once)
            return
        if args.command in ("owner-amend", "owner-resolve-no-pod"):
            from .control import Controller
            from .service import load_config
            controller = Controller(load_config(args.config), args.state_dir, None)
            result = (controller.amend(args.session_id, read_json(args.patch)) if args.command == "owner-amend"
                      else controller.resolve_no_pod(args.allocation, args.reason))
        elif args.command == "run":
            result = start_job(args)
        elif args.command == "_run-job":
            result = run_job(args.job_dir)
        elif args.command == "collect":
            record = read_json(Path(args.job_dir) / "job.json")
            result = get_runner(record).collect(record["remote"], Path(args.job_dir) / "outputs")
        elif args.command == "session":
            payload = vars(args).copy()
            payload.pop("command")
            action = payload.pop("action")
            if action == "create":
                payload["policy"] = read_json(payload["policy"])
            result = request_call("session." + action, payload)
        elif args.command in ("quote", "allocate"):
            result = request_call(args.command, {"session_id": args.session_id, "request": read_json(args.request)})
        elif args.command.startswith("allocation-"):
            result = request_call(args.command.replace("-", "."), {"session_id": args.session_id, "allocation_id": args.allocation_id})
        elif args.command in ("jobs", "wait"):
            until = time.monotonic() + (max(0, min(3600, args.timeout_seconds)) if args.command == "wait" else 0)
            while True:
                result = request_call("session.status", {"session_id": args.session_id})
                running = any(j["status"] in ("QUEUED", "RUNNING") for j in result["jobs"])
                if not running or result["status"] != "ACTIVE" or time.monotonic() >= until:
                    break
                time.sleep(2)
        else:
            result = request_call(args.command)
        print(json.dumps(result, indent=2))
        if args.command in ("run", "_run-job") and result.get("status") in ("FAILED", "INTERRUPTED"):
            raise SystemExit(1)
    except (OSError, PolicyError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
