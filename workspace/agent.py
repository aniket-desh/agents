#!/usr/bin/env python3
"""Permission-free defaults, only in the managed isolated workspace image."""

import os
from pathlib import Path
import stat
import sys


MARKER = Path("/etc/research-workspace")
VENDOR = Path("/opt/vendor/bin")
CODEX_MANAGEMENT = set("agents login logout mcp plugin app-server remote-control app completion update doctor sandbox debug apply a queue archive delete migrate-rollouts unarchive cloud exec-server features help".split())
CLAUDE_MANAGEMENT = set("agents auth auto-mode doctor gateway install mcp plugin plugins project setup-token ultrareview update upgrade help".split())
# Only needed to recognize a management command after leading global options.
VALUE_OPTIONS = {
    "codex": set("-c --config --enable --disable --remote --remote-auth-token-env -i --image -m --model --local-provider -p --profile -s --sandbox -C --cd --add-dir -a --ask-for-approval".split()),
    "claude": set("--agent --agents --append-system-prompt --debug-file --effort --fallback-model --input-format --json-schema --max-budget-usd --model -n --name --output-format --permission-mode --plugin-dir --plugin-url --session-id --setting-sources --settings --system-prompt".split()),
}


def require_workspace():
    """Prevent accidental use on a normal host; Docker supplies the actual boundary."""
    if sys.platform != "linux" or os.geteuid() == 0 or not Path("/.dockerenv").is_file():
        raise ValueError("These agent defaults require the non-root managed research container.")
    info = MARKER.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022 or MARKER.read_text() != "research-agents-isolated-v1\n"):
        raise ValueError("Missing trusted research-workspace image marker.")


def leading_command(agent, args):
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--":
            return None
        if not value.startswith("-"):
            return value
        index += 2 if value in VALUE_OPTIONS[agent] else 1
    return None


def default_arguments(agent, args):
    options = args[:args.index("--")] if "--" in args else args
    if any(arg in ("-h", "--help", "-V", "-v", "--version") for arg in options):
        return args
    management = CODEX_MANAGEMENT if agent == "codex" else CLAUDE_MANAGEMENT
    if leading_command(agent, args) in management:
        return args
    if agent == "claude":
        explicit = ("--permission-mode", "--dangerously-skip-permissions",
                    "--allow-dangerously-skip-permissions", "--settings", "--safe-mode")
        if any(arg.split("=", 1)[0] in explicit for arg in options):
            return args
        return ["--dangerously-skip-permissions", *args]

    explicit = ("--sandbox", "--ask-for-approval", "--approve-for-me", "--full-auto",
                "--dangerously-bypass-approvals-and-sandbox", "--profile", "--remote")
    if any(arg.split("=", 1)[0] in explicit or
           (arg.startswith(("-s", "-a", "-p")) and not arg.startswith("--")) for arg in options):
        return args
    for index, arg in enumerate(options):
        config = ""
        if arg in ("-c", "--config") and index + 1 < len(options):
            config = options[index + 1]
        elif arg.startswith("--config="):
            config = arg[len("--config="):]
        elif arg.startswith("-c") and arg != "-c":
            config = arg[2:].removeprefix("=")
        if config.split("=", 1)[0].strip() in ("approval_policy", "sandbox_mode"):
            return args
    # These global settings also work for `exec`, `resume`, and `review`, whose
    # accepted flags differ. This is the equivalent of Codex's full-bypass flag.
    return ["-c", 'approval_policy="never"', "-c", 'sandbox_mode="danger-full-access"', *args]


def main():
    try:
        require_workspace()
        if len(sys.argv) < 2 or sys.argv[1] not in ("codex", "claude"):
            raise ValueError("Expected codex or claude.")
        agent = sys.argv[1]
        binary = VENDOR / agent
        os.execv(str(binary), [str(binary), *default_arguments(agent, sys.argv[2:])])
    except (OSError, ValueError) as exc:
        print(f"research workspace: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
