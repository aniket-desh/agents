import importlib.util
import os
from pathlib import Path
import stat
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("workspace_agent", ROOT / "workspace/agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class AgentDefaultsTests(unittest.TestCase):
    def test_normal_and_resumed_research_defaults(self):
        defaults = ["-c", 'approval_policy="never"', "-c", 'sandbox_mode="danger-full-access"']
        for args in ([], ["Check the SAE metric"], ["resume", "--last"], ["fork", "--last"],
                     ["exec", "resume", "--last"], ["review", "--uncommitted"]):
            self.assertEqual(agent.default_arguments("codex", args), defaults + args)
        for args in ([], ["--continue"], ["--resume", "session"], ["-p", "Check the metric"]):
            self.assertEqual(agent.default_arguments("claude", args), ["--dangerously-skip-permissions", *args])
        # Tokens following -- are prompt text, not wrapper options.
        self.assertEqual(agent.default_arguments("codex", ["--", "--sandbox"]), defaults + ["--", "--sandbox"])

    def test_explicit_permissions_remote_and_management_are_preserved(self):
        cases = {
            "codex": [["--sandbox", "read-only"], ["exec", "-sworkspace-write", "check"],
                      ["--ask-for-approval=on-request"], ["--approve-for-me"], ["-p", "careful"],
                      ["--config", 'approval_policy="on-request"'], ["-csandbox_mode=read-only"],
                      ["-c=sandbox_mode=read-only"],
                      ["--remote", "wss://example.invalid"], ["--model", "example", "login"],
                      ["mcp", "list"], ["--version"], ["exec", "--help"]],
            "claude": [["--permission-mode", "plan"], ["--permission-mode=manual"],
                       ["--settings", "my-settings.json"], ["--dangerously-skip-permissions"],
                       ["auth", "login"], ["--model", "example", "mcp", "list"], ["--help"]],
        }
        for name, invocations in cases.items():
            for args in invocations:
                with self.subTest(agent=name, args=args):
                    self.assertEqual(agent.default_arguments(name, args), args)

    def test_guard_refuses_host_root_and_writable_marker(self):
        with mock.patch.object(agent.sys, "platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "managed research container"):
                agent.require_workspace()
        with mock.patch.object(agent.sys, "platform", "linux"), mock.patch.object(os, "geteuid", return_value=0):
            with self.assertRaises(ValueError):
                agent.require_workspace()
        marker = mock.Mock()
        marker.lstat.return_value = mock.Mock(st_mode=stat.S_IFREG | 0o666, st_uid=0)
        marker.read_text.return_value = "research-agents-isolated-v1\n"
        with mock.patch.object(agent.sys, "platform", "linux"), mock.patch.object(os, "geteuid", return_value=1000), \
                mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(agent, "MARKER", marker):
            with self.assertRaisesRegex(ValueError, "trusted"):
                agent.require_workspace()
            marker.lstat.return_value.st_mode = stat.S_IFREG | 0o444
            agent.require_workspace()

    def test_exec_uses_absolute_vendor_binary_and_preserves_arguments(self):
        with mock.patch.object(agent, "require_workspace"), mock.patch.object(agent.sys, "argv", ["agent.py", "claude", "a prompt with spaces"]), \
                mock.patch.object(os, "execv") as execute:
            agent.main()
        execute.assert_called_once_with("/opt/vendor/bin/claude", ["/opt/vendor/bin/claude", "--dangerously-skip-permissions", "a prompt with spaces"])


if __name__ == "__main__":
    unittest.main()
