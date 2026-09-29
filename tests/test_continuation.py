"""Native hooks must never select another conversation or launch an agent."""
import json
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from research_runtime.continuation import hook_response
from research_runtime.control import Controller


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.event = {"hook_event_name": "Stop", "session_id": "native-id", "cwd": "/tmp/project with spaces"}

    def test_exact_binding_and_controller_decision(self):
        for agent in ("codex", "claude"):
            run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"continue": True, "prompt": "Read the journal and test the next hypothesis."})))
            with patch.dict("os.environ", {"RESEARCH_COMPUTE_BIN": "/installed/research-compute"}):
                response = hook_response(agent, self.event, run)
            self.assertEqual(response["decision"], "block")
            self.assertEqual(run.call_args.args[0], ["/installed/research-compute", "session", "continuation",
                "--agent", agent, "--conversation-id", "native-id", "--workspace", "/tmp/project with spaces"])
            self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_pause_wait_budget_and_invalid_decisions_do_not_continue(self):
        for decision in ({"continue": False}, {"continue": False, "waiting": True},
                         {"continue": "true", "prompt": "wrong"}, {"continue": True, "prompt": ""}, []):
            run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(decision)))
            self.assertEqual(hook_response("codex", self.event, run), {})

    def test_start_discloses_identity_without_creating_grant(self):
        run = Mock()
        response = hook_response("claude", {**self.event, "hook_event_name": "SessionStart"}, run)
        context = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn('"conversation_id": "native-id"', context)
        run.assert_not_called()

    def test_subagents_interrupts_and_missing_identity_are_ignored(self):
        for event in ({}, {**self.event, "hook_event_name": "SubagentStop"},
                      {**self.event, "hook_event_name": "Interrupt"}, {**self.event, "session_id": ""}):
            run = Mock()
            self.assertEqual(hook_response("codex", event, run), {})
            run.assert_not_called()

    def test_native_driver_suppresses_hooks_without_consuming_hook_allowance(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / "config.example.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(config, directory, Mock())
            session = controller.dispatch("session.create", {"policy": {
                "objective": "inspect evidence", "budget_usd": 1, "mode": "auto",
                "continuation_driver": "native", "max_iterations": 1,
            }}, 1000)
            binding = {"agent": "codex", "conversation_id": "native-id", "workspace": directory}
            controller.dispatch("session.bind", {"session_id": session["id"], **binding}, 1000)
            for _ in range(2):
                result = controller.dispatch("session.continuation", binding, 1000)
                run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(result)))
                self.assertEqual(hook_response("codex", {**self.event, "cwd": directory}, run), {})
            controller.dispatch("session.driver", {"session_id": session["id"], "driver": "hook"}, 1000)
            self.assertTrue(controller.dispatch("session.continuation", binding, 1000)["continue"])
            controller.dispatch("session.driver", {"session_id": session["id"], "driver": "native"}, 1000)
            self.assertFalse(controller.dispatch("session.continuation", binding, 1000)["continue"])
            self.assertEqual(controller.dispatch("session.status", {"session_id": session["id"]}, 1000)["status"], "ACTIVE")

    def test_real_entrypoints_allow_exit_on_missing_service_and_bad_input(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ("integrations/claude-stop.py", "integrations/codex/research-hooks.py"):
            for data in (json.dumps(self.event), "bad json"):
                with patch.dict("os.environ", {"RESEARCH_COMPUTE_BIN": "/missing/research-compute"}):
                    result = subprocess.run([sys.executable, str(root / relative)], input=data, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stdout), {})

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the Pi extension")
    def test_pi_same_session_wait_pause_and_interruption(self):
        # The extension uses plain JS plus JSDoc, so test its real source with a
        # mock of Pi's documented API without installing Pi or calling a model.
        script = r'''
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const timers = [];
globalThis.setTimeout = (fn, delay) => { const t = { fn, delay, active: true }; timers.push(t); return t; };
globalThis.clearTimeout = t => { t.active = false; };
async function tick() { const t = timers.shift(); if (t?.active) await t.fn(); }
const code = readFileSync(process.argv[1], "utf8");
const { default: install } = await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));
const handlers = {}, sent = [], commands = [];
let response = { continue: true, prompt: "Analyze the completed result." }, pending = false;
const pi = {
  on: (name, callback) => { handlers[name] = callback; },
  exec: async (command, args) => { commands.push(args); return { code: 0, stdout: JSON.stringify(response) }; },
  sendUserMessage: (message, options) => sent.push({ message, options }),
};
const ctx = { cwd: "/tmp/own-worktree", sessionManager: { getSessionId: () => "native-pi" },
  isIdle: () => true, hasPendingMessages: () => pending };
const event = { outcome: "completed", continue: false, context: { canContinue: true, pendingMessages: [] } };
install(pi);
assert.equal(timers.length, 0, "loading the factory must not start timers");
const direct = await handlers.agent_before_settle(event, ctx);
assert.equal(direct.continue, true);
assert.equal(direct.entries[0].content, response.prompt);
assert.deepEqual(commands[0], ["session", "continuation", "--agent", "pi", "--conversation-id", "native-pi", "--workspace", ctx.cwd]);
const before = commands.length;
await handlers.agent_before_settle({ ...event, outcome: "aborted" }, ctx);
assert.equal(commands.length, before, "aborting must not immediately restart autonomy");
response = { continue: false, waiting: true };
await handlers.agent_before_settle(event, ctx);
assert.equal(timers[0].delay, 15000);
await tick();
assert.equal(sent.length, 0, "running jobs must not burn model turns");
response = { continue: true, prompt: "Now collect the finished job." };
await tick();
assert.equal(sent.length, 1);
assert.equal(sent[0].options.deliverAs, "followUp");
response = { continue: false, waiting: true };
await handlers.agent_before_settle(event, ctx);
await handlers.session_before_switch({}, ctx);
await tick();
assert.equal(sent.length, 1, "an old workspace timer must not write into a switched conversation");
response = { continue: false };
assert.equal(await handlers.agent_before_settle(event, ctx), undefined);
assert.equal(timers.length, 0, "paused work must not schedule another poll");
pending = true;
const count = commands.length;
await handlers.agent_before_settle(event, ctx);
assert.equal(commands.length, count, "human steering takes priority");
'''
        source = Path(__file__).resolve().parents[1] / "integrations/pi/research.ts"
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
