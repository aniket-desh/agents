import copy
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
module = importlib.util.spec_from_file_location("workspace_launcher", ROOT / "scripts/research-workspace.py")
workspace = importlib.util.module_from_spec(module)
module.loader.exec_module(workspace)


class FakeDocker:
    """In-memory Docker objects; no subprocesses, volumes, or containers exist."""
    def __init__(self):
        self.image = {"Id": "sha256:workspace-test", "Config": {
            "User": "1001:1001", "Labels": {workspace.LABEL: "v1"},
            "Env": ["HOME=/home/research", "PATH=/opt/vendor/bin:/usr/bin:/bin"],
            "Entrypoint": [workspace.ENTRYPOINT], "Cmd": ["sleep", "infinity"]}}
        self.container = None
        self.volumes = {}
        self.commands = []
        self.endpoint = "unix:///var/run/docker.sock"
        self.info = {"OSType": "linux", "SecurityOptions": ["name=seccomp,profile=builtin"]}

    def inspect(self, kind, name):
        return {"image": self.image, "container": self.container, "volume": self.volumes.get(name)}[kind]

    def run(self, *args):
        self.commands.append(args)
        if args[:2] == ("context", "show"):
            return "default\n"
        if args[:2] == ("context", "inspect"):
            return json.dumps([{"Endpoints": {"docker": {"Host": self.endpoint}}}])
        if args[0] == "info":
            return json.dumps(self.info)
        labels = dict(args[index + 1].split("=", 1) for index, item in enumerate(args) if item == "--label")
        if args[:2] == ("volume", "create"):
            self.volumes[args[-1]] = {"Driver": "local", "Options": None, "Labels": labels}
        elif args[:2] == ("container", "create"):
            value = lambda flag: args[args.index(flag) + 1]
            mounts = []
            for index, item in enumerate(args):
                if item == "--mount":
                    parts = dict(piece.split("=", 1) if "=" in piece else (piece, True) for piece in args[index + 1].split(","))
                    mount = {"Type": parts["type"], "Destination": parts["dst"], "RW": "readonly" not in parts}
                    mount["Name" if parts["type"] == "volume" else "Source"] = parts["src"]
                    mounts.append(mount)
            config = copy.deepcopy(self.image["Config"])
            config.update(User=value("--user"), WorkingDir=value("--workdir"), Labels=labels)
            self.container = {"Image": args[-1], "Config": config, "State": {"Running": False}, "Mounts": mounts,
                              "NetworkSettings": {"Networks": {}}, "HostConfig": {
                                  "ReadonlyRootfs": "--read-only" in args, "Privileged": False,
                                  "Init": "--init" in args, "CapDrop": [value("--cap-drop")],
                                  "NanoCpus": int(value("--cpus")) * 1_000_000_000,
                                  "Memory": int(value("--memory")[:-1]) * 1024**3,
                                  "MemorySwap": int(value("--memory-swap")[:-1]) * 1024**3,
                                  "PidsLimit": int(value("--pids-limit")),
                                  "SecurityOpt": [value("--security-opt")], "NetworkMode": value("--network"),
                                  "IpcMode": value("--ipc"), "CgroupnsMode": value("--cgroupns"),
                                  "GroupAdd": [value("--group-add")], "Tmpfs": {"/tmp": value("--tmpfs").split(":", 1)[1]}}}
        elif args[:2] == ("container", "start"):
            self.container["State"]["Running"] = True
            self.container["NetworkSettings"]["Networks"] = {"bridge": {}}
        else:
            raise AssertionError(f"Unexpected Docker operation: {args}")
        return "fake-id\n"


class WorkspaceTests(unittest.TestCase):
    def prepare(self, docker):
        return workspace.prepare(docker, "theory", "research-agents-workspace:local", 1001, 1001, 900)

    def test_workspace_reuses_private_state_without_deletion_or_image_pull(self):
        docker = FakeDocker()
        name = self.prepare(docker)
        self.assertEqual(name, "research-1001-theory")
        self.assertEqual(set(docker.volumes), {name + "-home", name + "-work"})
        original = copy.deepcopy(docker.volumes)
        count = len(docker.commands)
        self.prepare(docker)
        self.assertEqual(len(docker.commands), count)
        docker.container["State"]["Running"] = False
        self.prepare(docker)
        self.assertEqual(docker.commands[-1], ("container", "start", name))
        self.assertEqual(docker.volumes, original)
        self.assertFalse(any(item in {"rm", "remove", "prune", "pull"} for command in docker.commands for item in command))

    def test_creation_exposes_only_controller_socket_directory_and_private_volumes(self):
        docker = FakeDocker()
        self.prepare(docker)
        container = docker.container
        binds = [mount for mount in container["Mounts"] if mount["Type"] == "bind"]
        self.assertEqual(binds, [{"Type": "bind", "Source": "/run/research-compute", "Destination": "/run/research-compute", "RW": False}])
        create = next(command for command in docker.commands if command[:2] == ("container", "create"))
        self.assertFalse({"--env", "--env-file", "--privileged", "--pid", "--gpus", "--publish"}.intersection(create))
        self.assertTrue(container["HostConfig"]["ReadonlyRootfs"])
        self.assertEqual(container["HostConfig"]["CapDrop"], ["ALL"])
        self.assertEqual(container["HostConfig"]["SecurityOpt"], ["no-new-privileges=true"])
        self.assertEqual(container["HostConfig"]["GroupAdd"], ["900"])
        self.assertEqual(container["Config"]["User"], "1001:1001")
        self.assertEqual(container["HostConfig"]["Memory"], 8 * 1024**3)
        self.assertEqual(container["HostConfig"]["MemorySwap"], 8 * 1024**3)
        self.assertEqual(container["HostConfig"]["PidsLimit"], 1024)

    def test_modified_container_and_foreign_volume_are_refused_without_replacement(self):
        docker = FakeDocker()
        self.prepare(docker)
        docker.container["HostConfig"]["NetworkMode"] = "host"
        before = list(docker.commands)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.prepare(docker)
        self.assertEqual(docker.commands, before)
        docker = FakeDocker()
        docker.volumes["research-1001-theory-home"] = {"Driver": "local", "Options": {"device": "/home/host"}, "Labels": {}}
        with self.assertRaisesRegex(ValueError, "not a private"):
            self.prepare(docker)
        self.assertIsNone(docker.container)
        self.assertEqual(docker.commands, [])

    def test_identity_and_daemon_boundaries_fail_before_allocation(self):
        docker = FakeDocker()
        for uid, env, inside in ((0, {}, False), (1001, {"DOCKER_HOST": "ssh://other-host"}, False), (1001, {}, True)):
            with self.subTest(uid=uid, env=env, inside=inside), self.assertRaises(ValueError):
                workspace.validate_host(docker, uid, env, "linux", inside)
        self.assertEqual(docker.commands, [])
        docker.endpoint = "ssh://other-host"
        with self.assertRaisesRegex(ValueError, "Remote Docker"):
            workspace.validate_host(docker, 1001, {}, "linux")
        docker.endpoint = "unix:///run/user/1001/docker.sock"
        docker.info["SecurityOptions"] = ["name=rootless"]
        with self.assertRaisesRegex(ValueError, "rootful"):
            workspace.validate_host(docker, 1001, {}, "linux")
        docker.image["Config"]["User"] = "1002:1002"
        with self.assertRaisesRegex(ValueError, "USER_UID"):
            self.prepare(docker)
        self.assertEqual(docker.volumes, {})


if __name__ == "__main__":
    unittest.main()
