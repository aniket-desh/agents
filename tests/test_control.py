"""Checks for financially meaningful state transitions, without paid resources."""

import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest

from research_runtime.control import Controller, PolicyError
from research_runtime.service import load_config


class Provider:
    def __init__(self):
        self.pods = {}
        self.creates = 0
        self.ambiguous = False
        self.stop_fails = False

    def quote(self, request):
        return {"hourly_usd": request["gpu_count"]}

    def create(self, name, request):
        self.creates += 1
        pod = {"id": str(self.creates), "name": name, "status": "RUNNING", "hourly_usd": request["gpu_count"]}
        self.pods[pod["id"]] = pod
        if self.ambiguous:
            raise TimeoutError("request timed out after creation")
        return pod.copy()

    def get(self, pod_id):
        return self.pods[pod_id].copy()

    def list(self):
        return [p.copy() for p in self.pods.values()]

    def stop(self, pod_id):
        if self.stop_fails:
            raise TimeoutError("provider unavailable")
        self.pods[pod_id]["status"] = "STOPPED"

    def start(self, pod_id):
        self.pods[pod_id]["status"] = "RUNNING"
        return self.get(pod_id)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1_800_000_000.0
        self.provider = Provider()
        self.config = {"provider": {"kind": "fake"}, "limits": {
            "budget_usd": 5, "max_gpus": 2, "max_pods": 2, "max_volume_gb": 20,
            "max_retained_gb": 40, "max_hourly_usd": 2,
            "allowed_gpu_types": ["A40"], "allowed_images": ["pinned"], "allowed_templates": []},
            "safety": {"shutdown_margin_seconds": 120, "max_lease_seconds": 3600, "poll_seconds": 15}}
        self.ctrl = Controller(self.config, self.tmp.name, self.provider, clock=lambda: self.now)
        self.ctrl.watchdog_tick()
        self.policy = {"objective": "test hypothesis", "budget_usd": 5}
        self.request = {"gpu_type": "A40", "gpu_count": 1, "volume_gb": 20,
                        "image": "pinned", "duration_seconds": 1800}

    def session(self, **extra):
        return self.ctrl.dispatch("session.create", {"policy": {**self.policy, **extra}}, 1000)["id"]

    def allocate(self, sid, **extra):
        return self.ctrl.dispatch("allocate", {"session_id": sid, "request": {**self.request, **extra}}, 1000)

    def status(self, sid):
        return self.ctrl.dispatch("session.status", {"session_id": sid}, 1000)

    def test_parallel_sessions_share_transactional_capacity(self):
        ids = [self.session() for _ in range(3)]
        def submit(sid):
            try:
                return self.allocate(sid)
            except PolicyError:
                return None
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            results = list(pool.map(submit, ids))
        self.assertEqual(sum(r is not None for r in results), 2)
        self.assertEqual(self.provider.creates, 2)

    def test_optional_account_cap_preserves_independent_session_limits(self):
        self.config["limits"]["budget_usd"] = None
        small = self.session(budget_usd=0.8)
        large = self.session(budget_usd=50)
        self.allocate(small)
        self.now += 2900
        self.ctrl.watchdog_tick()
        self.assertEqual(self.status(small)["status"], "BUDGET_REACHED")
        self.assertEqual(self.status(large)["status"], "ACTIVE")
        self.assertIn("pod", self.allocate(large))
        with self.assertRaises(PolicyError):
            self.session(budget_usd=0)
        with self.assertRaises(PolicyError):
            self.session(budget_usd=-1)
        self.assertIsNone(self.ctrl.dispatch("doctor", {}, 1000)["account_budget_usd"])

    def test_no_account_cap_still_reserves_against_session_budget(self):
        self.config["limits"]["budget_usd"] = None
        sid = self.session(budget_usd=0.8)
        self.allocate(sid)
        with self.assertRaises(PolicyError):
            self.allocate(sid)
        self.ctrl.dispatch("session.pause", {"session_id": sid}, 1000)
        self.now += 120
        self.ctrl.watchdog_tick()
        spent = self.status(sid)["estimated_spend_usd"]
        self.ctrl.amend(sid, {"budget_usd": 50})
        self.assertEqual(self.status(sid)["estimated_spend_usd"], spent)

    def allow_unbounded(self):
        self.config["limits"].update(budget_usd=None, allow_unbounded_sessions=True)

    def test_unbounded_budget_requires_explicit_owner_opt_in_and_explicit_null(self):
        with self.assertRaisesRegex(PolicyError, "owner permission"):
            self.session(budget_usd=None)
        self.config["limits"]["allow_unbounded_sessions"] = True
        with self.assertRaisesRegex(PolicyError, "null account"):
            self.session(budget_usd=None)
        self.config["limits"].update(budget_usd=None, allow_unbounded_sessions=False)
        with self.assertRaisesRegex(PolicyError, "owner permission"):
            self.session(budget_usd=None)
        self.allow_unbounded()
        with self.assertRaisesRegex(PolicyError, "explicit session budget"):
            self.ctrl.dispatch("session.create", {"policy": {"objective": "no missing-budget loophole"}}, 1000)
        status = self.status(self.session(budget_usd=None))
        self.assertIsNone(status["remaining_usd"])
        self.assertIsNone(status["available_usd"])
        self.assertNotIn("Infinity", json.dumps(status, allow_nan=False))

    def test_unbounded_config_requires_boolean_opt_in_and_null_global_cap(self):
        config_path = Path(self.tmp.name) / "config.json"
        for opt_in, budget in (("true", None), (1, None), (True, 5)):
            with self.subTest(opt_in=opt_in, budget=budget):
                self.config["limits"].update(allow_unbounded_sessions=opt_in, budget_usd=budget)
                config_path.write_text(json.dumps(self.config))
                with self.assertRaises(PolicyError):
                    load_config(config_path)
        self.allow_unbounded()
        config_path.write_text(json.dumps(self.config))
        self.assertTrue(load_config(config_path)["limits"]["allow_unbounded_sessions"])

    def test_unbounded_sessions_keep_reservations_and_hourly_concurrency_limits(self):
        self.allow_unbounded()
        sid = self.session(budget_usd=None)
        self.allocate(sid)
        self.assertGreater(self.status(sid)["reserved_usd"], 0)
        self.allocate(sid)
        with self.assertRaisesRegex(PolicyError, "capacity"):
            self.allocate(sid)
        other = self.session(budget_usd=None, max_hourly_usd=0.5)
        self.ctrl.dispatch("session.pause", {"session_id": sid}, 1000)
        self.config["limits"]["max_retained_gb"] = 60
        with self.assertRaisesRegex(PolicyError, "hourly"):
            self.allocate(other)
        self.assertEqual(self.provider.creates, 2)

    def test_unbounded_session_still_stops_expired_lease_after_restart(self):
        self.allow_unbounded()
        sid = self.session(budget_usd=None)
        allocation = self.allocate(sid, duration_seconds=300)
        self.ctrl.dispatch("job.record", {"session_id": sid, "allocation_id": allocation["allocation_id"],
                           "job_id": "still-running", "status": "RUNNING"}, 1000)
        self.now += 100_000
        Controller(self.config, self.tmp.name, self.provider, clock=lambda: self.now).watchdog_tick()
        status = self.status(sid)
        self.assertEqual(status["status"], "ACTIVE")
        self.assertEqual(status["allocations"][0]["state"], "STOPPED")
        self.assertGreater(status["estimated_spend_usd"], 5)
        self.assertIsNone(status["remaining_usd"])

    def test_unbounded_session_keeps_lease_storage_and_deadline_admission_checks(self):
        from datetime import datetime, timezone
        self.allow_unbounded()
        sid = self.session(budget_usd=None)
        with self.assertRaisesRegex(PolicyError, "lease"):
            self.allocate(sid, duration_seconds=3601)
        self.config["limits"]["max_retained_gb"] = 20
        self.allocate(sid)
        self.ctrl.dispatch("session.pause", {"session_id": sid}, 1000)
        self.ctrl.dispatch("session.resume", {"session_id": sid}, 1000)
        with self.assertRaisesRegex(PolicyError, "retained storage"):
            self.allocate(sid, container_disk_gb=10)
        end = datetime.fromtimestamp(self.now + 200, timezone.utc).isoformat()
        deadline_sid = self.session(budget_usd=None, deadline=end)
        with self.assertRaisesRegex(PolicyError, "deadline"):
            self.allocate(deadline_sid, duration_seconds=100)
        self.now += 81
        self.ctrl.watchdog_tick()
        self.assertEqual(self.status(deadline_sid)["status"], "DEADLINE_REACHED")

    def test_unbounded_review_reserves_stops_and_resumes_without_adding_total_cap(self):
        self.allow_unbounded()
        sid = self.session(budget_usd=None, review_at_usd=0.2)
        self.allocate(sid, duration_seconds=590)
        with self.assertRaisesRegex(PolicyError, "reservations"):
            self.allocate(sid, duration_seconds=590)
        self.now += 610
        self.ctrl.watchdog_tick()
        status = self.status(sid)
        self.assertEqual(status["status"], "REVIEW_REQUIRED")
        self.assertIsNone(status["remaining_usd"])
        self.assertAlmostEqual(status["available_usd"], 0.2 - status["estimated_spend_usd"])
        with self.assertRaisesRegex(PolicyError, "continuation instruction"):
            self.ctrl.dispatch("session.resume", {"session_id": sid}, 1000)
        self.ctrl.dispatch("session.resume", {"session_id": sid, "user_confirmed": True}, 1000)
        self.assertIsNone(self.status(sid)["available_usd"])
        self.assertIn("pod", self.allocate(sid))

    def test_unbounded_owner_amendments_remain_local_and_preserve_spend(self):
        sid = self.session(budget_usd=1)
        with self.assertRaisesRegex(PolicyError, "owner permission"):
            self.ctrl.amend(sid, {"budget_usd": None})
        self.allow_unbounded()
        self.allocate(sid)
        with self.assertRaisesRegex(PolicyError, "confirm stopped"):
            self.ctrl.amend(sid, {"budget_usd": None})
        self.now += 100
        self.ctrl.dispatch("session.pause", {"session_id": sid}, 1000)
        spent = self.status(sid)["estimated_spend_usd"]
        status = self.ctrl.amend(sid, {"budget_usd": None, "review_at_usd": 0.5})
        self.assertEqual(status["estimated_spend_usd"], spent)
        self.assertEqual(status["status"], "PAUSED")
        self.assertIsNone(status["remaining_usd"])
        self.assertAlmostEqual(status["available_usd"], 0.5 - spent)
        with self.assertRaisesRegex(PolicyError, "owner allowance"):
            self.ctrl.amend(sid, {"budget_usd": spent})
        with self.assertRaisesRegex(PolicyError, "review threshold"):
            self.ctrl.amend(sid, {"budget_usd": 0.1})
        self.assertEqual(self.ctrl.amend(sid, {"budget_usd": 2})["policy"]["budget_usd"], 2)
        with self.assertRaisesRegex(PolicyError, "unsupported"):
            self.ctrl.dispatch("owner.amend", {"session_id": sid, "budget_usd": None}, 1000)

    def test_unbounded_hook_continuation_handles_null_remaining_budget(self):
        self.allow_unbounded()
        sid = self.session(budget_usd=None, mode="auto", max_iterations=1)
        binding = {"agent": "claude", "conversation_id": "uncapped-thread", "workspace": self.tmp.name}
        self.ctrl.dispatch("session.bind", {"session_id": sid, **binding}, 1000)
        self.assertTrue(self.ctrl.dispatch("session.continuation", binding, 1000)["continue"])
        self.assertFalse(self.ctrl.dispatch("session.continuation", binding, 1000)["continue"])

    def test_ambiguous_create_reconciles_without_duplicate(self):
        sid = self.session()
        self.provider.ambiguous = True
        self.assertEqual(self.allocate(sid)["state"], "UNKNOWN")
        with self.assertRaises(PolicyError):
            self.allocate(sid)
        self.ctrl.watchdog_tick()
        self.assertEqual(self.provider.creates, 1)
        self.assertEqual(self.status(sid)["allocations"][0]["state"], "STOPPED")

    def test_renamed_pod_stop_refusal_preserves_reserved_exposure(self):
        from research_runtime.provider import FakeProvider
        provider = FakeProvider({"fake_state": str(Path(self.tmp.name) / "pods.json")})
        self.ctrl.provider = provider
        sid = self.session()
        allocation = self.allocate(sid)
        self.assertTrue(allocation["pod"]["name"].startswith("aniket-research-"))
        pod_id = allocation["pod"]["id"]
        state = json.loads(provider.path.read_text())
        state["pods"][pod_id]["name"] = "someone-else"
        provider.path.write_text(json.dumps(state))
        self.ctrl.dispatch("session.pause", {"session_id": sid}, 1000)
        self.now += 120
        self.ctrl.watchdog_tick()
        status = self.status(sid)
        self.assertEqual(status["allocations"][0]["state"], "STOPPING")
        self.assertEqual(json.loads(provider.path.read_text())["pods"][pod_id]["status"], "RUNNING")
        with self.assertRaises(PolicyError):
            self.ctrl.dispatch("session.resume", {"session_id": sid}, 1000)
        with self.ctrl.locked() as db:
            record = db.execute("SELECT rate,stopped FROM allocations WHERE id=?",
                                (allocation["allocation_id"],)).fetchone()
            self.assertGreater(record["rate"], 0)
            self.assertIsNone(record["stopped"])

    def test_stop_failure_retains_liability_and_blocks_new_resources(self):
        sid = self.session()
        self.allocate(sid)
        self.provider.stop_fails = True
        self.ctrl.dispatch("session.pause", {"session_id": sid}, 1000)
        self.now += 120
        first = self.status(sid)
        self.assertEqual(first["allocations"][0]["state"], "STOPPING")
        self.assertGreater(first["estimated_spend_usd"], 0)
        with self.assertRaises(PolicyError):
            self.allocate(self.session())
        self.provider.stop_fails = False
        self.ctrl.watchdog_tick()
        self.assertEqual(self.status(sid)["allocations"][0]["state"], "STOPPED")

    def test_lease_survives_controller_restart(self):
        sid = self.session()
        self.allocate(sid, duration_seconds=300)
        self.now += 301
        restarted = Controller(self.config, self.tmp.name, self.provider, clock=lambda: self.now)
        restarted.watchdog_tick()
        self.assertEqual(self.status(sid)["allocations"][0]["state"], "STOPPED")
        self.assertGreater(self.status(sid)["estimated_spend_usd"], 0.08)

    def test_review_resume_preserves_spend_and_stopped_volume(self):
        sid = self.session(review_at_usd=0.2)
        self.allocate(sid, duration_seconds=590)
        self.now += 610
        self.ctrl.watchdog_tick()
        before = self.status(sid)
        self.assertEqual(before["status"], "REVIEW_REQUIRED")
        with self.assertRaises(PolicyError):
            self.ctrl.dispatch("session.resume", {"session_id": sid}, 1000)
        self.ctrl.dispatch("session.resume", {"session_id": sid, "user_confirmed": True}, 1000)
        self.assertEqual(self.status(sid)["estimated_spend_usd"], before["estimated_spend_usd"])
        pod_id = before["allocations"][0]["pod_id"]
        resumed = self.allocate(sid, container_disk_gb=20)
        self.assertEqual(resumed["pod"]["id"], pod_id)
        self.assertEqual(self.provider.creates, 1)

    def test_pause_has_bounded_checkpoint_grace_and_lost_stop_reply_recovers(self):
        sid = self.session()
        aid = self.allocate(sid)["allocation_id"]
        self.ctrl.dispatch("job.record", {"session_id": sid, "allocation_id": aid,
                           "job_id": "job1", "status": "RUNNING"}, 1000)
        self.ctrl.dispatch("session.pause", {"session_id": sid}, 1000)
        self.assertEqual(self.status(sid)["allocations"][0]["state"], "STOPPING")
        self.assertEqual(self.provider.pods["1"]["status"], "RUNNING")
        self.now += 31
        self.provider.pods["1"]["status"] = "STOPPED"
        self.provider.stop_fails = True
        self.ctrl.watchdog_tick()
        self.assertEqual(self.status(sid)["allocations"][0]["state"], "STOPPED")

    def test_owner_amendment_keeps_spend_and_requires_stopped_resources(self):
        sid = self.session(budget_usd=1)
        self.allocate(sid)
        with self.assertRaises(PolicyError):
            self.ctrl.amend(sid, {"budget_usd": 2})
        self.now += 100
        self.ctrl.dispatch("session.pause", {"session_id": sid}, 1000)
        spent = self.status(sid)["estimated_spend_usd"]
        amended = self.ctrl.amend(sid, {"budget_usd": 2})
        self.assertEqual(amended["estimated_spend_usd"], spent)
        self.assertEqual(amended["status"], "PAUSED")
        with self.assertRaises(PolicyError):
            self.ctrl.dispatch("owner.amend", {"session_id": sid, "budget_usd": 4}, 1000)

    def test_native_pending_jobs_do_not_burn_hook_iterations_and_handoff_requires_pause(self):
        sid = self.session(mode="auto")
        binding = {"agent": "claude", "conversation_id": "native1", "workspace": self.tmp.name}
        self.ctrl.dispatch("session.bind", {"session_id": sid, **binding}, 1000)
        self.ctrl.dispatch("job.record", {"session_id": sid, "job_id": "pending1",
                           "allocation_id": None, "status": "QUEUED"}, 1000)
        for _ in range(4):
            self.assertTrue(self.ctrl.dispatch("session.continuation", binding, 1000)["waiting"])
        self.assertEqual(self.status(sid)["status"], "ACTIVE")
        switched = {"session_id": sid, **binding, "agent": "pi", "take_over": True}
        with self.assertRaises(PolicyError):
            self.ctrl.dispatch("session.bind", switched, 1000)
        self.ctrl.dispatch("session.pause", {"session_id": sid}, 1000)
        self.ctrl.dispatch("session.bind", switched, 1000)
        self.assertFalse(self.ctrl.dispatch("session.continuation", binding, 1000)["continue"])

    def test_rejected_or_owner_resolved_creation_releases_only_uncreated_exposure(self):
        from research_runtime.provider import CreateRejected
        sid = self.session()
        def rejected(*_args):
            raise CreateRejected("template rejected", 422)
        self.provider.create = rejected
        self.assertEqual(self.allocate(sid)["state"], "NOT_CREATED")
        self.assertEqual(self.status(sid)["retained_volume_gb"], 0)
        def uncertain(*_args):
            raise TimeoutError("no reply")
        self.provider.create = uncertain
        unknown = self.allocate(sid)
        self.now += 30
        self.assertGreater(self.status(sid)["estimated_spend_usd"], 0)
        self.ctrl.resolve_no_pod(unknown["allocation_id"], "Owner checked settled provider history: no creation")
        status = self.status(sid)
        self.assertEqual(status["estimated_spend_usd"], 0)
        self.assertEqual(status["retained_volume_gb"], 0)

    def test_account_allowance_cannot_be_reset_by_new_session(self):
        self.config["limits"]["budget_usd"] = 0.8
        self.policy["budget_usd"] = 0.8
        sid = self.session()
        self.allocate(sid, duration_seconds=1800)
        self.now += 1800
        self.ctrl.watchdog_tick()
        with self.assertRaises(PolicyError):
            self.allocate(self.session(), duration_seconds=1800)

    def test_watchdog_required_and_request_cannot_override_provider(self):
        sid = self.session()
        self.now += 61
        with self.assertRaisesRegex(PolicyError, "watchdog"):
            self.allocate(sid)
        self.ctrl.watchdog_tick()
        with self.assertRaisesRegex(PolicyError, "unknown allocation"):
            self.allocate(sid, env={"RUNPOD_API_KEY": "unsafe"})
        with self.assertRaisesRegex(PolicyError, "allowlist"):
            self.allocate(sid, image="unapproved")

    def test_session_isolation_and_native_binding(self):
        sid = self.session(mode="auto", max_iterations=1)
        with self.assertRaises(PolicyError):
            self.ctrl.dispatch("session.status", {"session_id": sid}, 1001)
        binding = {"agent": "claude", "conversation_id": "thread-1", "workspace": self.tmp.name}
        self.ctrl.dispatch("session.bind", {"session_id": sid, **binding}, 1000)
        self.assertTrue(self.ctrl.dispatch("session.continuation", binding, 1000)["continue"])
        self.assertFalse(self.ctrl.dispatch("session.continuation", binding, 1000)["continue"])
        self.assertEqual(self.status(sid)["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
