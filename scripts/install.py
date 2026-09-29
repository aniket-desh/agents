#!/usr/bin/env python3
"""Install the unprivileged research profile and tools; never deploy services."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


BEGIN = "<!-- research-agents:begin -->"
END = "<!-- research-agents:end -->"
OWNER = "aniket-desh/agents"
MARKER = ".research-agents-owned.json"
AGENTS = {
    "codex": (".codex/AGENTS.md", ".agents/skills"),
    "claude": (".claude/CLAUDE.md", ".claude/skills"),
    "pi": (".pi/agent/AGENTS.md", ".pi/agent/skills"),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def reject_symlinks(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Refusing symlinked install path: {part}")


def write_file(path, data, executable=False):
    """Back up changed files and replace atomically; leave identical files alone."""
    reject_symlinks(path)
    if path.exists() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.bak.{stamp}")
        shutil.copy2(path, backup)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    try:
        temporary.chmod(0o755 if executable else 0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def managed_document(path, profile):
    old = path.read_text() if path.exists() else ""
    block = f"{BEGIN}\n{profile.rstrip()}\n{END}"
    if BEGIN not in old and END not in old:
        return (old + ("\n\n" if old and not old.endswith("\n\n") else "") + block + "\n").encode()
    if old.count(BEGIN) != 1 or old.count(END) != 1:
        raise ValueError(f"Ambiguous managed instruction markers: {path}")
    start = old.index(BEGIN)
    finish = old.index(END)
    if finish < start:
        raise ValueError(f"Reversed managed instruction markers: {path}")
    return (old[:start] + block + old[finish + len(END):]).encode()


def check_owned(directory, planned_names):
    """Do not claim an existing user's skill or a manually modified owned tree."""
    reject_symlinks(directory)
    marker = directory / MARKER
    reject_symlinks(marker)
    if not directory.exists():
        return
    if not marker.is_file():
        raise ValueError(f"Existing directory is not owned by this installer: {directory}")
    record = json.loads(marker.read_text())
    if record.get("owner") != OWNER or not isinstance(record.get("files"), dict):
        raise ValueError(f"Invalid ownership record: {marker}")
    for name, expected in record["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Invalid owned path in {marker}")
        installed = directory / relative
        reject_symlinks(installed)
        if not installed.is_file() or digest(installed.read_bytes()) != expected:
            raise ValueError(f"Local edits in managed installation; preserve or move it before updating: {installed}")
    for name in planned_names:
        destination = directory / name
        reject_symlinks(destination)
        if destination.exists() and name not in record["files"]:
            raise ValueError(f"Unmanaged file conflicts with update: {destination}")


def owned_files(directory, files, external_hashes=None):
    marker = directory / MARKER
    previous = json.loads(marker.read_text())["files"] if marker.exists() else {}
    for name in previous.keys() - files.keys():
        retired = directory / name
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        retired.replace(retired.with_name(f"{retired.name}.bak.{stamp}"))
    record = {"owner": OWNER, "files": {name: digest(data) for name, (data, _) in files.items()}}
    if external_hashes:
        record["external_files"] = external_hashes
    for name, (data, executable) in files.items():
        write_file(directory / name, data, executable)
    write_file(directory / MARKER, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())


def hook_settings(path, command):
    """Append our two hooks without rewriting or disabling any existing hooks."""
    reject_symlinks(path)
    settings = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
        raise ValueError(f"Expected an object with a hooks object: {path}")
    events = settings.setdefault("hooks", {})
    hook = {"type": "command", "command": command, "timeout": 10}
    for event in ("SessionStart", "Stop"):
        entries = events.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"Expected a list for hooks.{event}: {path}")
        if any(not isinstance(entry, dict) or not isinstance(entry.get("hooks", []), list) for entry in entries):
            raise ValueError(f"Invalid hook entry for {event}: {path}")
        found = any(existing.get("command") == command for entry in entries
                    if isinstance(entry, dict) for existing in entry.get("hooks", [])
                    if isinstance(existing, dict))
        if not found:
            entries.append({"hooks": [hook]})
    return (json.dumps(settings, indent=2) + "\n").encode()


def install(source, target_home, agents, agent_homes=None):
    source = source.resolve()
    home = target_home.expanduser().absolute()
    if home.is_symlink():
        raise ValueError(f"Refusing symlinked target home: {home}")
    home = home.parent.resolve() / home.name
    reject_symlinks(home)
    prefix = home / ".local/share/research-agents"
    profile = (source / "guidance/research.md").read_text()
    runtime = source / "research_runtime"
    binary = source / "bin/research-compute"
    if not runtime.is_dir() or not binary.is_file():
        raise ValueError("Runtime is missing from this checkout; install a complete revision.")

    package_files = {"guidance/research.md": (profile.encode(), False)}
    package_files["bin/research-compute"] = (binary.read_bytes(), True)
    workspace_launcher = source / "scripts/research-workspace.py"
    if not workspace_launcher.is_file():
        raise ValueError("Workspace launcher is missing; install a complete revision.")
    package_files["scripts/research-workspace.py"] = (workspace_launcher.read_bytes(), False)
    for path in sorted(runtime.rglob("*.py")):
        if "__pycache__" not in path.parts:
            package_files[str(path.relative_to(source))] = (path.read_bytes(), False)
    for directory in ("integrations", "docs"):
        for path in sorted((source / directory).rglob("*")):
            if path.is_file() and path.suffix in (".py", ".ts", ".md"):
                package_files[str(path.relative_to(source))] = (path.read_bytes(), False)
    skill_sets = {}
    documents = {}
    settings = {}
    external = {}
    for agent in agents:
        document_name, skills_name = AGENTS[agent]
        if agent_homes and agent in agent_homes:
            custom = agent_homes[agent].expanduser().absolute()
            document_name = custom / Path(document_name).name
            if agent in ("claude", "pi"):
                skills_name = custom / "skills"
        skill_root = home / skills_name
        for skill in ("research-code", "gpu-research"):
            destination = skill_root / skill
            skill_sets[destination] = {"SKILL.md": ((source / "skills" / skill / "SKILL.md").read_bytes(), False)}
        routing = (f"\nResearch tools: `{home / '.local/bin/research-compute'}`. "
                   f"For scientific code, read `{skill_root / 'research-code/SKILL.md'}` when useful. "
                   f"For GPU requests, read `{skill_root / 'gpu-research/SKILL.md'}` "
                   f"and the concrete tool examples at `{prefix / 'docs/compute-tools.md'}`.\n")
        document = home / document_name
        reject_symlinks(document)
        if agent == "codex":
            routing += "Codex requires one-time native `/hooks` trust before the installed continuation hooks can run; do not bypass that trust requirement.\n"
        documents[document] = managed_document(document, profile + routing)
        if agent in ("codex", "claude"):
            relative = "integrations/codex/research-hooks.py" if agent == "codex" else "integrations/claude-stop.py"
            if relative not in package_files:
                raise ValueError(f"Required agent integration is missing: {relative}")
            command = (f"env RESEARCH_COMPUTE_BIN={shlex.quote(str(home / '.local/bin/research-compute'))} "
                       f"python3 {shlex.quote(str(prefix / relative))}")
            config_path = document.parent / ("hooks.json" if agent == "codex" else "settings.json")
            settings[config_path] = hook_settings(config_path, command)
        elif agent == "pi":
            external[document.parent / "extensions/research-agents.ts"] = (source / "integrations/pi/research.ts").read_bytes()

    launchers = {}
    for name, script in (("research-compute", "bin/research-compute"),
                         ("research-workspace", "scripts/research-workspace.py")):
        launcher = home / ".local/bin" / name
        launcher_source = ("#!/usr/bin/env python3\n"
                           "# Managed by aniket-desh/agents; client/profile installation.\n"
                           "import runpy\n"
                           f"runpy.run_path({str(prefix / script)!r}, run_name='__main__')\n").encode()
        reject_symlinks(launcher)
        if launcher.exists() and launcher.read_bytes() != launcher_source:
            raise ValueError(f"Refusing to replace an existing command: {launcher}")
        launchers[launcher] = launcher_source

    # Check every conflict before changing any installed file.
    check_owned(prefix, package_files)
    for directory, files in skill_sets.items():
        check_owned(directory, files)
    previous = json.loads((prefix / MARKER).read_text()) if (prefix / MARKER).exists() else {}
    previous_external = previous.get("external_files", {})
    for path, data in external.items():
        reject_symlinks(path)
        if path.exists() and digest(path.read_bytes()) != previous_external.get(str(path)):
            raise ValueError(f"Existing extension is not owned or has local edits: {path}")

    owned_files(prefix, package_files, {**previous_external, **{str(path): digest(data) for path, data in external.items()}})
    for directory, files in skill_sets.items():
        owned_files(directory, files)
    for document, content in documents.items():
        write_file(document, content)
    for path, content in {**settings, **external}.items():
        write_file(path, content)
    for launcher, content in launchers.items():
        write_file(launcher, content, executable=True)
    return {"home": str(home), "agents": agents, "command": str(home / '.local/bin/research-compute'),
            "workspace_command": str(home / '.local/bin/research-workspace'),
            "service_installed": False, "message": "Ensure ~/.local/bin is on PATH. The controller and workspace image are deployed separately. Permission-free agent defaults apply only inside the managed workspace image."}


def stage_controller(source, stage_root, config, provider_env=None, replace_config=False):
    """Copy a standalone controller tree. Identity/permissions belong to the root wrapper."""
    source = source.resolve()
    root = stage_root.resolve()
    if not (source / "research_runtime").is_dir() or not (source / "bin/research-compute").is_file():
        raise ValueError("Runtime is missing from this checkout; install a complete revision.")
    configuration = config.read_bytes()
    json.loads(configuration)
    destinations = {
        root / "etc/research-compute/config.json": configuration,
        root / "etc/systemd/system/research-compute.service": (source / "deploy/research-compute.service").read_bytes(),
        root / "etc/systemd/system/research-compute-watchdog.service": (source / "deploy/research-compute-watchdog.service").read_bytes(),
    }
    if provider_env is not None:
        destinations[root / "etc/research-compute/runpod.env"] = provider_env.read_bytes()
    prefix = root / "opt/research-agents"
    files = {"bin/research-compute": ((source / "bin/research-compute").read_bytes(), True)}
    for path in sorted((source / "research_runtime").rglob("*.py")):
        if "__pycache__" not in path.parts:
            if path.is_symlink():
                raise ValueError(f"Refusing linked controller source: {path}")
            files[str(path.relative_to(source))] = (path.read_bytes(), False)
    check_owned(prefix, files)
    for destination, content in destinations.items():
        reject_symlinks(destination)
        if destination.exists() and destination.read_bytes() != content:
            if not replace_config:
                raise ValueError(f"Existing configuration differs; use --replace-config deliberately: {destination}")
    owned_files(prefix, files)
    for destination, content in destinations.items():
        write_file(destination, content)
        if "research-compute" in destination.parts:
            destination.chmod(0o600 if destination.name == "runpod.env" else 0o640)
    return {"staged_root": str(root), "services_started": False}


def check_controller_runtime(config):
    """Check locally installed runtime dependencies without downloading anything."""
    if sys.version_info < (3, 11):
        raise ValueError("The controller requires Python 3.11 or newer on the system PATH.")
    provider = json.loads(config.read_text())["provider"]
    if provider["kind"] == "mcp":
        for command in ("node", "npm", "npx"):
            if shutil.which(command) is None:
                raise ValueError(f"Live MCP deployment requires {command} on the system PATH; install Node.js 20+ and npm before --enable.")
        version = subprocess.run(["node", "--version"], check=True, capture_output=True, text=True).stdout.strip()
        if int(version.removeprefix("v").split(".")[0]) < 20:
            raise ValueError(f"The RunPod MCP server requires Node.js 20 or newer; found {version}.")
        command = provider.get("command", ["npx", "-y", "@runpod/mcp-server@4.0.0"])
        if not isinstance(command, list) or not command or not isinstance(command[0], str) or shutil.which(command[0]) is None:
            raise ValueError("The configured MCP command is not available on the system PATH.")
    return {"runtime_dependencies": "available", "provider": provider["kind"], "downloads": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-home", type=Path, help="Home to install into; isolates validation from inherited agent home settings.")
    parser.add_argument("--codex-home", type=Path, help="Override Codex's instruction directory (otherwise CODEX_HOME for a real-home install).")
    parser.add_argument("--claude-config-dir", type=Path, help="Override Claude's config directory (otherwise CLAUDE_CONFIG_DIR for a real-home install).")
    parser.add_argument("--pi-agent-dir", type=Path, help="Override Pi's agent directory.")
    parser.add_argument("--agents", nargs="+", choices=tuple(AGENTS), default=list(AGENTS))
    parser.add_argument("--controller-stage", type=Path, help="Stage controller files only; normally called by install-controller.sh.")
    parser.add_argument("--check-controller-runtime", action="store_true", help="Check installed service dependencies without network or system changes.")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--provider-env", type=Path)
    parser.add_argument("--replace-config", action="store_true")
    args = parser.parse_args()
    try:
        source = Path(__file__).resolve().parents[1]
        if args.check_controller_runtime:
            if args.config is None:
                parser.error("--check-controller-runtime requires --config")
            result = check_controller_runtime(args.config)
        elif args.controller_stage is not None:
            if args.config is None:
                parser.error("--controller-stage requires --config")
            result = stage_controller(source, args.controller_stage, args.config, args.provider_env, args.replace_config)
        else:
            if args.config or args.provider_env or args.replace_config:
                parser.error("controller options require --controller-stage")
            homes = {}
            for agent, explicit, variable in (("codex", args.codex_home, "CODEX_HOME"), ("claude", args.claude_config_dir, "CLAUDE_CONFIG_DIR"), ("pi", args.pi_agent_dir, "PI_CODING_AGENT_DIR")):
                chosen = explicit or (os.environ.get(variable) if args.target_home is None else None)
                if chosen:
                    homes[agent] = Path(chosen)
            result = install(source, args.target_home or Path.home(), list(dict.fromkeys(args.agents)), homes)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Install failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
