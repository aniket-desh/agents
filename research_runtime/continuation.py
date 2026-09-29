"""Native lifecycle glue; the controller alone decides whether work may continue.

No model is launched here. Stop feedback goes back to the conversation that
invoked the hook. Native goals/scheduled wakeups handle long waits where a
vendor's Stop hook cannot wake an idle conversation.
"""

import json
import os
import subprocess
import sys


def hook_response(agent, event, run=subprocess.run):
    if not isinstance(event, dict):
        return {}
    native_id, workspace = event.get("session_id"), event.get("cwd")
    if not isinstance(native_id, str) or not native_id or not isinstance(workspace, str) or not workspace:
        return {}
    kind = event.get("hook_event_name")
    if kind == "SessionStart":
        context = (
            "Research compute binding context (this does not authorize compute): "
            + json.dumps({"agent": agent, "conversation_id": native_id, "workspace": workspace})
            + ". If the user requests a managed investigation, bind its ID to this exact "
            "conversation and workspace with research-compute session bind. "
            "Ordinary edits do not need a compute session."
        )
        return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext": context}}
    # Never intercept subagent completion, errors, or user interruption.
    if kind != "Stop":
        return {}
    command = os.environ.get("RESEARCH_COMPUTE_BIN", "research-compute")
    result = run(
        [command, "session", "continuation", "--agent", agent,
         "--conversation-id", native_id, "--workspace", workspace],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if result.returncode != 0:
        return {}
    decision = json.loads(result.stdout)
    if not isinstance(decision, dict) or decision.get("continue") is not True:
        return {}
    prompt = decision.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
        return {}
    # stop_hook_active is deliberately not an unconditional exit: the trusted
    # controller enforces finite iterations, failed-job and no-progress limits.
    # Claude also applies its own consecutive-Stop limit (currently eight).
    return {"decision": "block", "reason": prompt}


def hook_main(agent):
    try:
        response = hook_response(agent, json.load(sys.stdin))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Missing service, malformed output, or slow IPC never forces another
        # model turn. Independent GPU leases remain authoritative.
        response = {}
    print(json.dumps(response))
