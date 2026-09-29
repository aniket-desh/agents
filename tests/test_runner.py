import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from research_runtime.runner import LocalRunner, RunnerError, SSHRunner, _extract, snapshot


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.runner = LocalRunner(self.root / "workers")

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, program):
        (self.repo / "run.py").write_text(program)
        snapshot(self.repo, self.root / "snapshot")

    def wait(self, job):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.runner.poll(job)
            if result["status"] not in ("RUNNING", "STARTING"):
                return result
            time.sleep(0.05)
        self.runner.cancel(job)
        self.fail("worker did not finish")

    def test_snapshot_keeps_working_edits_and_excludes_credentials(self):
        (self.repo / "model.py").write_text("old = 1\n")
        (self.repo / ".env.secret").write_text("pretend-secret\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "model.py", ".env.secret"], check=True)
        (self.repo / "model.py").write_text("new = 2\n")
        (self.repo / "untracked.py").write_text("untracked = True\n")
        (self.repo / "alias.py").symlink_to(self.repo / ".env.secret")
        (self.repo / "out").mkdir()
        (self.repo / "out" / "data.txt").write_text("large-result")
        manifest = snapshot(self.repo, self.root / "snapshot", exclude=[str(self.repo / "out")])
        self.assertIsNone(manifest["git_commit"])
        self.assertEqual({entry["path"] for entry in manifest["files"]}, {"model.py", "untracked.py"})
        with tarfile.open(self.root / "snapshot" / "source.tar.gz") as archive:
            self.assertEqual(archive.extractfile("model.py").read(), b"new = 2\n")

    def test_detached_run_and_compact_results_reproduce_numbers(self):
        self.prepare("import json,pathlib,os\nassert os.environ.get('HF_TOKEN')=='fake-model-token'\nassert 'RUNPOD_API_KEY' not in os.environ\np=pathlib.Path('results');p.mkdir()\n(p/'metrics.json').write_text(json.dumps({'l0':1.0,'per_token':[0,2]}))\n(p/'obsolete.txt').write_text('old result')\nprint('done')\n")
        with patch.dict(os.environ, {"HF_TOKEN": "fake-model-token", "RUNPOD_API_KEY": "fake-provider-key"}):
            job = self.runner.launch(self.root / "snapshot", "session", "job", [sys.executable, "run.py"], 10, ["results"])
        self.assertEqual(self.wait(job)["exit_code"], 0)
        dest = self.root / "collected"
        report = self.runner.collect(job, dest)
        self.assertEqual(report["status"]["status"], "SUCCEEDED")
        data = json.loads((dest / "outputs/results/metrics.json").read_text())
        self.assertEqual(sum(data["per_token"]) / len(data["per_token"]), data["l0"])
        self.assertIn("done", (dest / "stdout.log").read_text())
        collection = json.loads((dest / "collection.json").read_text())
        self.assertFalse(collection["provisional"])
        for entry in collection["files"]:
            self.assertEqual(hashlib.sha256((dest / entry["path"]).read_bytes()).hexdigest(), entry["sha256"])
        worker_results = Path(job["path"]) / "source/results"
        (worker_results / "obsolete.txt").unlink()
        updated = json.dumps({"l0": 2.0, "per_token": [2, 2]}).encode()
        (worker_results / "metrics.json").write_bytes(updated)
        self.runner.collect(job, dest)
        self.assertFalse((dest / "outputs/results/obsolete.txt").exists())
        self.assertEqual((dest / "outputs/results/metrics.json").read_bytes(), updated)
        previous = {str(path.relative_to(dest)): path.read_bytes() for path in dest.rglob("*") if path.is_file()}

        def corrupt_bundle(root, stream):
            manifest = {"files": [{"path": "outputs/results/metrics.json", "bytes": 6, "sha256": "wrong"}]}
            with tarfile.open(fileobj=stream, mode="w|") as archive:
                for name, data in (("outputs/results/metrics.json", b"broken"),
                                   ("collection.json", json.dumps(manifest).encode())):
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    archive.addfile(entry, io.BytesIO(data))

        with patch("research_runtime.runner._output_bundle", corrupt_bundle):
            with self.assertRaisesRegex(RunnerError, "checksum mismatch"):
                self.runner.collect(job, dest)
        self.assertEqual(previous, {str(path.relative_to(dest)): path.read_bytes() for path in dest.rglob("*") if path.is_file()})
        unowned = self.root / "personal"
        unowned.mkdir()
        (unowned / "notes.txt").write_text("keep me")
        with self.assertRaisesRegex(RunnerError, "unowned"):
            self.runner.collect(job, unowned)
        self.assertEqual((unowned / "notes.txt").read_text(), "keep me")
        with self.assertRaises(FileExistsError):
            self.runner.launch(self.root / "snapshot", "session", "job", [sys.executable, "run.py"], 10, [])

    def test_worker_deadline_interrupts_process(self):
        self.prepare("import time\ntime.sleep(30)\n")
        job = self.runner.launch(self.root / "snapshot", "session", "timeout", [sys.executable, "run.py"], 0.2, [])
        result = self.wait(job)
        self.assertEqual(result["status"], "TIMED_OUT")
        self.assertNotEqual(result["exit_code"], 0)

    def test_output_links_and_traversal_are_rejected(self):
        self.prepare("from pathlib import Path\nPath('leak').symlink_to('/etc/passwd')\n")
        job = self.runner.launch(self.root / "snapshot", "session", "links", [sys.executable, "run.py"], 10, ["leak"])
        self.wait(job)
        with self.assertRaisesRegex(RunnerError, "Symlinks"):
            self.runner.collect(job, self.root / "collected")
        with self.assertRaises(RunnerError):
            self.runner.launch(self.root / "snapshot", "session", "other", ["true"], 10, ["../secret"])

    def test_remote_archive_cannot_escape_destination(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            entry = tarfile.TarInfo("../escaped")
            entry.size = 4
            archive.addfile(entry, io.BytesIO(b"evil"))
        stream.seek(0)
        dest = self.root / "output"
        dest.mkdir()
        with tarfile.open(fileobj=stream) as archive, self.assertRaises(RunnerError):
            _extract(archive, dest, 1024)
        self.assertFalse((self.root / "escaped").exists())

    def test_ssh_host_identity_is_kept_and_arguments_are_not_a_shell(self):
        runner = SSHRunner({"host": "127.0.0.1", "port": 2222, "user": "research"})
        self.assertIn("StrictHostKeyChecking=accept-new", runner.argv)
        with self.assertRaises(RunnerError):
            SSHRunner({"host": "-oProxyCommand=touch-pwned", "port": 22, "user": "research"})


if __name__ == "__main__":
    unittest.main()
