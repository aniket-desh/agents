#!/usr/bin/env python3
"""Open a shell in an isolated, persistent research workspace on the local Linux host."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time


LABEL = "org.research-agents.workspace"
CONTROLLER = "/run/research-compute"
ENTRYPOINT = "/opt/research-agents/workspace/entrypoint.sh"
TMPFS = "rw,nosuid,nodev,size=256m,mode=1777"


class Docker:
    def run(self, *args, allow_missing=False):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
        if result.returncode:
            if allow_missing and "no such" in result.stderr.lower():
                return None
            raise ValueError(f"docker {' '.join(args[:2])} failed: {result.stderr.strip()}")
        return result.stdout

    def inspect(self, kind, name):
        raw = self.run(kind, "inspect", name, allow_missing=True)
        return json.loads(raw)[0] if raw is not None else None


def validate_host(docker, uid, env, platform, in_container=False):
    if in_container:
        raise ValueError("Run research-workspace on the host, not inside a container.")
    if platform != "linux" or uid == 0:
        raise ValueError("Use an ordinary nonroot user on the Linux Docker host.")
    if env.get("DOCKER_HOST") or env.get("DOCKER_CONTEXT"):
        raise ValueError("Unset DOCKER_HOST and DOCKER_CONTEXT; only the inspected local Docker context is supported.")
    context = docker.run("context", "show").strip()
    endpoint = json.loads(docker.run("context", "inspect", context))[0]["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith("unix://"):
        raise ValueError("Remote Docker contexts are unsupported; the controller socket must be on this host.")
    info = json.loads(docker.run("info", "--format", "{{json .}}"))
    options = " ".join(info.get("SecurityOptions", [])).lower()
    if info.get("OSType") != "linux" or "rootless" in options or "userns" in options:
        raise ValueError("A local rootful Linux daemon without user-namespace remapping is required to preserve the controller UID.")


def specification(name, image, uid, gid, socket_gid):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,47}", name):
        raise ValueError("Workspace names use 1-48 lowercase letters, digits, dots, underscores, or hyphens, starting with a letter or digit.")
    if image["Config"].get("User") != f"{uid}:{gid}" or image["Config"].get("Labels", {}).get(LABEL) != "v1":
        raise ValueError("Build the workspace image with USER_UID/USER_GID matching this host user.")
    if image["Config"].get("Entrypoint") != [ENTRYPOINT] or image["Config"].get("Cmd") != ["sleep", "infinity"]:
        raise ValueError("Image does not have the expected workspace entrypoint.")
    spec = {"name": f"research-{uid}-{name}", "image": image["Id"], "user": f"{uid}:{gid}", "socket_gid": str(socket_gid)}
    spec["labels"] = {LABEL: "v1", LABEL + ".uid": str(uid), LABEL + ".name": name}
    spec["fingerprint"] = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    return spec


def create_command(spec):
    args = ["container", "create", "--name", spec["name"], "--init", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
            "--cpus", "4", "--memory", "8g", "--memory-swap", "8g", "--pids-limit", "1024",
            "--network", "bridge", "--ipc", "private", "--cgroupns", "private",
            "--user", spec["user"], "--group-add", spec["socket_gid"],
            "--workdir", "/workspace", "--tmpfs", f"/tmp:{TMPFS}"]
    for key, value in {**spec["labels"], LABEL + ".spec": spec["fingerprint"]}.items():
        args.extend(["--label", f"{key}={value}"])
    for suffix, destination in (("home", "/home/research"), ("work", "/workspace")):
        args.extend(["--mount", f"type=volume,src={spec['name']}-{suffix},dst={destination}"])
    args.extend(["--mount", f"type=bind,src={CONTROLLER},dst={CONTROLLER},readonly", spec["image"]])
    return args


def validate_container(container, spec, image):
    config, host = container["Config"], container["HostConfig"]
    labels = config.get("Labels") or {}
    expected_labels = {**spec["labels"], LABEL + ".spec": spec["fingerprint"]}
    secure = (
        all(labels.get(k) == v for k, v in expected_labels.items())
        and container.get("Image") == spec["image"] and config.get("User") == spec["user"]
        and config.get("Entrypoint") == [ENTRYPOINT] and config.get("Cmd") == ["sleep", "infinity"]
        and config.get("WorkingDir") == "/workspace"
        and sorted(config.get("Env") or []) == sorted(image["Config"].get("Env") or [])
        and host.get("ReadonlyRootfs") is True and host.get("Privileged") is False
        and host.get("Init") is True and set(host.get("CapDrop") or []) == {"ALL"}
        and host.get("NanoCpus") == 4_000_000_000 and host.get("Memory") == 8 * 1024**3
        and host.get("MemorySwap") == 8 * 1024**3 and host.get("PidsLimit") == 1024
        and not host.get("CapAdd") and host.get("NetworkMode") == "bridge"
        and host.get("PidMode", "") == "" and host.get("IpcMode") == "private"
        and host.get("CgroupnsMode") == "private" and host.get("UsernsMode", "") == ""
        and host.get("UTSMode", "") == "" and host.get("GroupAdd") == [spec["socket_gid"]]
        and not any(host.get(key) for key in ("Binds", "VolumesFrom", "Devices", "DeviceRequests", "PortBindings", "Links"))
        and host.get("Tmpfs") == {"/tmp": TMPFS}
        and set(container.get("NetworkSettings", {}).get("Networks", {})) <= {"bridge"}
    )
    security = host.get("SecurityOpt") or []
    secure = secure and len(security) == 1 and security[0] in ("no-new-privileges", "no-new-privileges=true", "no-new-privileges:true")
    mounts = [mount for mount in container.get("Mounts", []) if mount.get("Type") != "tmpfs"]
    expected = {"/home/research": ("volume", spec["name"] + "-home", True),
                "/workspace": ("volume", spec["name"] + "-work", True),
                CONTROLLER: ("bind", CONTROLLER, False)}
    actual = {m["Destination"]: (m["Type"], m.get("Name") if m["Type"] == "volume" else m.get("Source"), m["RW"]) for m in mounts}
    tmpfs_ok = all(m.get("Destination") == "/tmp" and m.get("RW") is True for m in container.get("Mounts", []) if m.get("Type") == "tmpfs")
    if not secure or len(mounts) != 3 or actual != expected or not tmpfs_ok:
        raise ValueError("Existing container does not match the isolated workspace specification; use a new name. Nothing was removed or replaced.")


def prepare(docker, name, image_name, uid, gid, socket_gid):
    image = docker.inspect("image", image_name)
    if image is None:
        raise ValueError("Workspace image is missing. Build workspace/Dockerfile explicitly; the launcher never pulls images.")
    spec = specification(name, image, uid, gid, socket_gid)
    container = docker.inspect("container", spec["name"])
    if container is not None:
        validate_container(container, spec, image)
    for role in ("home", "work"):
        volume_name = spec["name"] + "-" + role
        volume = docker.inspect("volume", volume_name)
        labels = {**spec["labels"], LABEL + ".role": role}
        if volume is not None:
            if volume.get("Driver") != "local" or volume.get("Options") or not all((volume.get("Labels") or {}).get(k) == v for k, v in labels.items()):
                raise ValueError(f"Existing volume is not a private workspace volume: {volume_name}")
        elif container is not None:
            raise ValueError("An existing workspace volume is missing; refusing to recreate it.")
        else:
            args = ["volume", "create", "--driver", "local"]
            for key, value in labels.items():
                args.extend(["--label", f"{key}={value}"])
            docker.run(*args, volume_name)
    if container is None:
        docker.run(*create_command(spec))
        container = docker.inspect("container", spec["name"])
        validate_container(container, spec, image)
    if not container["State"]["Running"]:
        docker.run("container", "start", spec["name"])
    return spec["name"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--image", default="research-agents-workspace:local")
    args = parser.parse_args()
    docker = Docker()
    try:
        uid, gid = os.getuid(), os.getgid()
        validate_host(docker, os.geteuid(), os.environ, sys.platform, Path("/.dockerenv").exists())
        directory = Path(CONTROLLER).lstat()
        control = Path(CONTROLLER, "control.sock").lstat()
        if not stat.S_ISDIR(directory.st_mode) or not stat.S_ISSOCK(control.st_mode) or directory.st_gid != control.st_gid:
            raise ValueError("The local controller directory and socket must share their configured client group.")
        if control.st_gid not in {gid, *os.getgroups()}:
            raise ValueError("Reconnect your host login after joining research-compute-clients.")
        name = prepare(docker, args.name, args.image, uid, gid, control.st_gid)
        for _ in range(30):
            ready = subprocess.run(["docker", "exec", name, "test", "-f", "/tmp/research-ready"], capture_output=True, timeout=5)
            if ready.returncode == 0:
                break
            if not docker.inspect("container", name)["State"]["Running"]:
                raise ValueError(f"Workspace initialization failed; inspect docker logs {name}.")
            time.sleep(1)
        else:
            raise ValueError(f"Workspace has not initialized; inspect docker logs {name}.")
        # Inherit the validated container user and supplemental controller group.
        print(f"Entering isolated workspace {args.name}; its home and /workspace are retained.", flush=True)
        os.execvp("docker", ["docker", "exec", "-it", "--workdir", "/workspace", name, "bash", "-l"])
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"Workspace: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
