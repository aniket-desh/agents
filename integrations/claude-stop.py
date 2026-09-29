#!/usr/bin/env python3
"""Claude SessionStart/Stop hook. See https://code.claude.com/docs/en/hooks."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_runtime.continuation import hook_main

if __name__ == "__main__":
    hook_main("claude")
