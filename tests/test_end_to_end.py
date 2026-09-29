"""Real socket, detached worker, snapshot, collection, and retained fake pod."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EndToEndTests(unittest.TestCase):
    def test_cpu_bundle_and_agent_disconnect(self):
        with tempfile.TemporaryDirectory(prefix="research-e2e-", dir="/tmp") as tmp:
            root = Path(tmp)
            config = json.loads((ROOT / "config.example.json").read_text())
            config["provider"]["fake_state"] = str(root / "provider.json")
            config["safety"]["poll_seconds"] = 1
            config["limits"].update(max_pods=1, max_gpus=1)
            cfg = root / "config.json"
            cfg.write_text(json.dumps(config))
            sock = str(root / "control.sock")
            env = {**os.environ, "PYTHONPATH": str(ROOT), "RESEARCH_COMPUTE_SOCKET": sock,
                   "RESEARCH_COMPUTE_RUNS_DIR": str(root / "runs")}
            processes = []
            logs = (root / "service.log").open("w+")
            try:
                for name in ("daemon", "watchdog"):
                    command = [sys.executable, "-m", "research_runtime.cli", name,
                               "--config", str(cfg), "--state-dir", str(root / "state")]
                    if name == "daemon":
                        command += ["--socket", sock]
                    processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=logs, stderr=logs))
                def cli(*args):
                    run = subprocess.run([sys.executable, "-m", "research_runtime.cli", *args],
                                         env=env, cwd=ROOT, text=True, capture_output=True, timeout=20)
                    self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
                    return json.loads(run.stdout)
                end = time.monotonic() + 8
                while time.monotonic() < end:
                    if all(p.poll() is None for p in processes) and Path(sock).exists():
                        time.sleep(0.1)
                        break
                    time.sleep(0.05)
                else:
                    logs.seek(0)
                    self.fail("Services did not start: " + logs.read())
                doctor = cli("doctor")
                self.assertIsNotNone(doctor["last_watchdog_tick"])
                repo = root / "source"
                repo.mkdir()
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                (repo / "experiment.py").write_text(
                    "import json, pathlib, time\n"
                    "time.sleep(0.5)\n"
                    "x=[[0,0,0],[1,0,2]]\n"
                    "counts=[sum(v!=0 for v in row) for row in x]\n"
                    "pathlib.Path('results').mkdir()\n"
                    "pathlib.Path('results/metrics.json').write_text(json.dumps({'l0':sum(counts)/len(counts),'counts':counts}))\n")
                (repo / ".env").write_text("SECRET=do-not-copy")
                # Include a tracked secret: snapshot's exclusion must override Git.
                subprocess.run(["git", "-C", str(repo), "add", "experiment.py", ".env"], check=True)
                policy = root / "policy.json"
                policy.write_text(json.dumps({"objective": "verify sparse activity", "budget_usd": 2}))
                session = cli("session", "create", "--policy", str(policy))
                spec = root / "spec.json"
                spec.write_text(json.dumps({"repo": str(repo), "command": [sys.executable, "experiment.py"],
                    "outputs": ["results"], "timeout_seconds": 10,
                    "request": {"gpu_type": "NVIDIA A40", "image": "research/fake:local", "duration_seconds": 60}}))
                started = cli("run", "--session", session["id"], "--spec", str(spec), "--local")
                registered = cli("jobs", "--session", session["id"])
                self.assertEqual(len(registered["jobs"]), 1)
                # The second collector waits for capacity, then reuses the same
                # stopped volume. Its admission must not require another model turn.
                second = cli("run", "--session", session["id"], "--spec", str(spec), "--local")
                # run has exited: the separate collector/worker must still finish.
                job_dir = Path(started["job_dir"])
                end = time.monotonic() + 15
                while time.monotonic() < end:
                    record = json.loads((job_dir / "job.json").read_text())
                    other = json.loads((Path(second["job_dir"]) / "job.json").read_text())
                    if "stop_confirmed" in record and "stop_confirmed" in other:
                        break
                    time.sleep(0.1)
                self.assertEqual(record["status"], "SUCCEEDED", record)
                self.assertTrue(record["stop_confirmed"])
                self.assertEqual(other["status"], "SUCCEEDED", other)
                self.assertTrue(other["stop_confirmed"])
                self.assertIn(".env", record["manifest"]["excluded"])
                metric = json.loads((job_dir / "outputs/outputs/results/metrics.json").read_text())
                self.assertEqual(metric, {"l0": 1.0, "counts": [0, 2]})
                status = cli("session", "status", session["id"])
                self.assertEqual(status["allocations"][0]["state"], "STOPPED")
                self.assertEqual(len(status["allocations"]), 2)
                self.assertEqual(status["retained_volume_gb"], 20)
                self.assertGreater(status["estimated_spend_usd"], 0)
                pods = json.loads((root / "provider.json").read_text())["pods"]
                self.assertEqual(len(pods), 1)
                self.assertEqual(next(iter(pods.values()))["status"], "STOPPED")
            finally:
                for process in processes:
                    process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                logs.close()


if __name__ == "__main__":
    unittest.main()
