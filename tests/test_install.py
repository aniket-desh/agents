import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("research_installer", ROOT / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        self.home = self.base / "home"
        self.source.mkdir()
        for name in ("guidance", "skills", "deploy"):
            shutil.copytree(ROOT / name, self.source / name)
        (self.source / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/research-workspace.py", self.source / "scripts/research-workspace.py")
        (self.source / "research_runtime").mkdir()
        (self.source / "research_runtime/__init__.py").write_text("VALUE = 'installed runtime'\n")
        (self.source / "bin").mkdir()
        (self.source / "bin/research-compute").write_text(
            "#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
            "from research_runtime import VALUE\nprint(VALUE)\n")
        for name, content in (("codex/research-hooks.py", "# Codex hook fixture\n"),
                              ("claude-stop.py", "# Claude hook fixture\n"),
                              ("pi/research.ts", "export default function() {}\n")):
            path = self.source / "integrations" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (self.source / "docs").mkdir()
        (self.source / "docs/compute-tools.md").write_text("# Tool examples\n")

    def install(self):
        return installer.install(self.source, self.home, ["codex", "claude", "pi"])

    def test_update_preserves_personal_instructions_and_command_works(self):
        instructions = self.home / ".codex/AGENTS.md"
        instructions.parent.mkdir(parents=True)
        personal = "# Personal conventions\nPreserve my exact words.\n"
        instructions.write_text(personal)
        self.install()
        first = instructions.read_text()
        self.assertTrue(first.startswith(personal))
        self.assertEqual([path.read_text() for path in instructions.parent.glob("*.bak.*")], [personal])
        result = subprocess.run([str(self.home / ".local/bin/research-compute")], check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "installed runtime")
        before = sorted(str(path) for path in self.home.rglob("*"))
        self.install()
        self.assertEqual(instructions.read_text(), first)
        self.assertEqual(sorted(str(path) for path in self.home.rglob("*")), before)
        instructions.write_text(first + "\nMore personal instructions.\n")
        (self.source / "guidance/research.md").write_text("Updated research policy.\n")
        self.install()
        self.assertTrue(instructions.read_text().startswith(personal))
        self.assertTrue(instructions.read_text().endswith("\nMore personal instructions.\n"))
        self.assertIn("Updated research policy.", instructions.read_text())
        self.assertEqual(instructions.read_text().count(installer.BEGIN), 1)

    def test_existing_skill_or_locally_edited_skill_is_preserved(self):
        skill = self.home / ".claude/skills/research-code/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("My independently installed skill.\n")
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.install()
        self.assertEqual(skill.read_text(), "My independently installed skill.\n")
        self.assertFalse((self.home / ".codex/AGENTS.md").exists())
        shutil.rmtree(skill.parent)
        self.install()
        skill.write_text("My locally revised skill.\n")
        with self.assertRaisesRegex(ValueError, "Local edits"):
            self.install()
        self.assertEqual(skill.read_text(), "My locally revised skill.\n")

    def test_conflicting_launcher_or_symlink_does_not_write_elsewhere(self):
        launcher = self.home / ".local/bin/research-compute"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("Existing command.\n")
        with self.assertRaisesRegex(ValueError, "existing command"):
            self.install()
        self.assertFalse((self.home / ".codex").exists())
        launcher.unlink()
        outside = self.base / "elsewhere"
        outside.mkdir()
        (self.home / ".codex").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinked"):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_controller_staging_copies_runtime_and_preserves_existing_limits(self):
        stage = self.base / "stage"
        config = self.base / "config.json"
        config.write_text('{"limits": {"budget_usd": 20}}\n')
        credentials = self.base / "provider.env"
        credentials.write_text("RUNPOD_API_KEY=installer-test-placeholder\n")
        result = installer.stage_controller(self.source, stage, config, credentials)
        self.assertFalse(result["services_started"])
        private_env = stage / "etc/research-compute/runpod.env"
        self.assertEqual(private_env.stat().st_mode & 0o777, 0o600)
        from research_runtime.provider import _api_key
        self.assertEqual(_api_key({"env_file": str(private_env)}), "installer-test-placeholder")
        installed = stage / "opt/research-agents/research_runtime/__init__.py"
        self.assertTrue(installed.is_file())
        self.assertFalse(installed.is_symlink())
        (self.source / "research_runtime/__init__.py").write_text("SOURCE_CHANGED = True\n")
        self.assertEqual(installed.read_text(), "VALUE = 'installed runtime'\n")
        config.write_text('{"limits": {"budget_usd": 100}}\n')
        with self.assertRaisesRegex(ValueError, "Existing configuration differs"):
            installer.stage_controller(self.source, stage, config)
        self.assertEqual((stage / "etc/research-compute/config.json").read_text(), '{"limits": {"budget_usd": 20}}\n')
        self.assertEqual(installed.read_text(), "VALUE = 'installed runtime'\n")

    def test_live_dependency_preflight_does_not_download_missing_dependencies(self):
        config = self.base / "live-config.json"
        config.write_text('{"provider": {"kind": "mcp"}}\n')
        with mock.patch.object(installer.shutil, "which", return_value=None), mock.patch.object(installer.subprocess, "run") as process:
            with self.assertRaisesRegex(ValueError, "requires node"):
                installer.check_controller_runtime(config)
            process.assert_not_called()

    def test_existing_hooks_survive_and_modified_pi_extension_is_not_replaced(self):
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        existing_hook = {"hooks": [{"type": "command", "command": "my-existing-hook", "timeout": 9}]}
        original = {"model": "my-preference", "hooks": {"Stop": [existing_hook]}}
        settings.write_text(json.dumps(original))
        self.install()
        actual = json.loads(settings.read_text())
        self.assertEqual(actual["model"], "my-preference")
        self.assertEqual(actual["hooks"]["Stop"][0], existing_hook)
        self.assertEqual(len(actual["hooks"]["Stop"]), 2)
        self.install()
        self.assertEqual(json.loads(settings.read_text()), actual)
        pi = self.home / ".pi/agent/extensions/research-agents.ts"
        self.assertEqual(pi.read_text(), "export default function() {}\n")
        pi.write_text("// My custom extension\n")
        with self.assertRaisesRegex(ValueError, "extension.*local edits"):
            self.install()
        self.assertEqual(pi.read_text(), "// My custom extension\n")
        self.assertEqual(json.loads(settings.read_text()), actual)

    def test_host_install_preserves_native_permissions_and_commands(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir(parents=True)
        original = 'approval_policy = "on-request"\nsandbox_mode = "workspace-write"\n'
        config.write_text(original)
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"permissions": {"defaultMode": "plan"}}')
        binary = self.home / ".local/bin/codex"
        binary.parent.mkdir(parents=True)
        binary.write_text("my native command\n")
        self.install()
        self.assertEqual(config.read_text(), original)
        self.assertEqual(json.loads(settings.read_text())["permissions"], {"defaultMode": "plan"})
        self.assertEqual(binary.read_text(), "my native command\n")
        self.assertFalse((binary.parent / "claude").exists())
        result = subprocess.run([str(binary.parent / "research-workspace"), "--help"],
                                check=True, capture_output=True, text=True)
        self.assertIn("workspace", result.stdout)


if __name__ == "__main__":
    unittest.main()
