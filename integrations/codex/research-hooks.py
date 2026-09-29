#!/usr/bin/env python3
"""Codex SessionStart/Stop hook. See https://learn.chatgpt.com/docs/hooks.

Review/trust through native /hooks; never bypass trust in the installer.
Prefer native Goals for durable research intent. This bounded Stop fallback
is disabled by the controller when continuation_driver is native. It does not
restart a closed CLI.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research_runtime.continuation import hook_main

if __name__ == "__main__":
    hook_main("codex")
