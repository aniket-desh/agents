"""Durable, conservative spending ledger. Only the service writes this state.

All money is integer micro-dollars. Provider operations have durable intents;
an uncertain create is reconciled, never automatically repeated.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid


class PolicyError(ValueError):
    pass


class CapacityError(PolicyError):
    """Existing admitted work may release this capacity; wait without a new grant."""


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or isinstance(value, bool):
            raise ValueError()
        return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, TypeError):
        raise PolicyError("money must be a finite, nonnegative dollar amount") from None


def positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PolicyError(f"{name} must be a positive integer")
    return value


def dollars(value):
    return round(value / 1_000_000, 6)


def cost(rate, seconds):
    return math.ceil(rate * max(0, seconds) / 3600)


def deadline(value):
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.timestamp()
    except (ValueError, TypeError, AttributeError):
        raise PolicyError("deadline must be an ISO timestamp including timezone") from None


LIVE = ("CREATING", "RUNNING", "STOPPING", "UNKNOWN")
TERMINAL = ("COMPLETE", "BUDGET_REACHED", "DEADLINE_REACHED")


class Controller:
    def __init__(self, config, state_dir, provider, clock=time.time):
        self.config, self.provider, self.clock = config, provider, clock
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.db_path = self.root / "ledger.sqlite3"
        with self.locked() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL,
                    policy TEXT NOT NULL, status TEXT NOT NULL,
                    mode TEXT NOT NULL, created REAL NOT NULL,
                    review_cleared INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '', journal TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS allocations (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    name TEXT NOT NULL UNIQUE, pod_id TEXT, request TEXT NOT NULL,
                    state TEXT NOT NULL, rate INTEGER NOT NULL,
                    started REAL NOT NULL, stopped REAL, expires REAL NOT NULL,
                    spent INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', stop_after REAL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    allocation_id TEXT UNIQUE, status TEXT NOT NULL,
                    detail TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS bindings (
                    agent TEXT NOT NULL, conversation TEXT NOT NULL,
                    owner INTEGER NOT NULL, workspace TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    PRIMARY KEY(agent, conversation, owner));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, session_id TEXT,
                    kind TEXT NOT NULL, detail TEXT NOT NULL);
            """)
            if "stop_after" not in {r[1] for r in db.execute("PRAGMA table_info(allocations)")}:
                db.execute("ALTER TABLE allocations ADD COLUMN stop_after REAL")
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def locked(self):
        # Also serializes the independent watchdog and daemon processes.
        with (self.root / "controller.lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            db = sqlite3.connect(self.db_path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            finally:
                db.close()

    def event(self, db, sid, kind, detail):
        db.execute("INSERT INTO events(at,session_id,kind,detail) VALUES(?,?,?,?)",
                   (self.clock(), sid, kind, json.dumps(detail)))

    def session(self, db, sid, owner):
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None or row["owner"] != owner:
            raise PolicyError("unknown session for this user")
        return row

    def accrue(self, db):
        now = self.clock()
        for row in db.execute("SELECT * FROM allocations"):
            end = row["stopped"] if row["stopped"] is not None else now
            spent = cost(row["rate"], end - row["started"])
            db.execute("UPDATE allocations SET spent=? WHERE id=?", (spent, row["id"]))

    def totals(self, db, sid=None):
        rows = db.execute("SELECT * FROM allocations" + (" WHERE session_id=?" if sid else ""),
                          (sid,) if sid else ()).fetchall()
        now = self.clock()
        margin = self.config.get("safety", {}).get("shutdown_margin_seconds", 120)
        spent = sum(r["spent"] for r in rows)
        reserved = sum(cost(r["rate"], r["expires"] + margin - now)
                       for r in rows if r["state"] in LIVE)
        return spent, reserved

    def cutoff(self, row):
        p = json.loads(row["policy"])
        ceiling = money(p["budget_usd"])
        if p.get("review_at_usd") is not None and not row["review_cleared"]:
            ceiling = min(ceiling, money(p["review_at_usd"]))
        return ceiling

    def create_session(self, db, args, owner):
        p = dict(args["policy"])
        lim = self.config["limits"]
        if not str(p.get("objective", "")).strip():
            raise PolicyError("objective is required")
        if money(p.get("budget_usd", 0)) <= 0 or money(p["budget_usd"]) > money(lim["budget_usd"]):
            raise PolicyError("session budget must be positive and within the account allowance")
        for key in ("max_gpus", "max_pods", "max_volume_gb"):
            p.setdefault(key, lim[key])
            if positive_int(p[key], key) > lim[key]:
                raise PolicyError(f"{key} exceeds account limit")
        p.setdefault("max_hourly_usd", lim["max_hourly_usd"])
        if not 0 < money(p["max_hourly_usd"]) <= money(lim["max_hourly_usd"]):
            raise PolicyError("invalid session hourly limit")
        p.setdefault("allowed_gpu_types", lim["allowed_gpu_types"])
        if not p["allowed_gpu_types"] or not set(p["allowed_gpu_types"]).issubset(lim["allowed_gpu_types"]):
            raise PolicyError("GPU types must be included in the owner-configured allowlist")
        end = deadline(p.get("deadline"))
        if end is not None and end <= self.clock():
            raise PolicyError("deadline is already past")
        if p.get("review_at_usd") is not None and not 0 < money(p["review_at_usd"]) <= money(p["budget_usd"]):
            raise PolicyError("review threshold must be positive and within budget")
        mode = p.pop("mode", "interactive")
        if mode not in ("auto", "interactive"):
            raise PolicyError("mode must be auto or interactive")
        p.setdefault("max_iterations", 50)
        positive_int(p["max_iterations"], "max_iterations")
        p.setdefault("continuation_driver", "hook")
        if p["continuation_driver"] not in ("hook", "native"):
            raise PolicyError("continuation_driver must be hook or native")
        sid = uuid.uuid4().hex
        db.execute("INSERT INTO sessions(id,owner,policy,status,mode,created) VALUES(?,?,?,?,?,?)",
                   (sid, owner, json.dumps(p), "ACTIVE", mode, self.clock()))
        self.event(db, sid, "session_created", p)
        return self.status(db, sid, owner)

    def status(self, db, sid, owner):
        row = self.session(db, sid, owner)
        p = json.loads(row["policy"])
        spent, reserved = self.totals(db, sid)
        allocations = [dict(r) for r in db.execute("SELECT * FROM allocations WHERE session_id=? ORDER BY started", (sid,))]
        for a in allocations:
            a["request"] = json.loads(a["request"])
            a["estimated_spend_usd"] = dollars(a.pop("spent"))
            a["hourly_usd"] = dollars(a.pop("rate"))
        jobs = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE session_id=? ORDER BY updated", (sid,))]
        for j in jobs:
            j["detail"] = json.loads(j["detail"])
        retained = self.retained_gb(db, sid)
        return {"id": sid, "status": row["status"], "mode": row["mode"], "reason": row["reason"],
                "objective": p["objective"], "policy": p, "estimated_spend_usd": dollars(spent),
                "reserved_usd": dollars(reserved), "remaining_usd": dollars(max(0, money(p["budget_usd"]) - spent)),
                "available_usd": dollars(max(0, self.cutoff(row) - spent - reserved)),
                "accounting": "conservative estimate, not a provider invoice",
                "retained_volume_gb": retained,
                "estimated_stopped_storage_usd_month": retained * self.config.get("retention_usd_gb_month", 0.20),
                "journal": json.loads(row["journal"]), "allocations": allocations, "jobs": jobs}

    def retained_gb(self, db, sid=None):
        rows = db.execute("SELECT * FROM allocations" + (" WHERE session_id=?" if sid else "") + " ORDER BY started DESC",
                          (sid,) if sid else ()).fetchall()
        seen, retained = set(), 0
        for a in rows:
            key = a["pod_id"] or a["id"]
            if key in seen:
                continue
            seen.add(key)
            if a["state"] not in ("TERMINATED", "NOT_CREATED"):
                retained += json.loads(a["request"])["volume_gb"]
        return retained

    def validate_request(self, db, row, request):
        allowed = {"gpu_type", "gpu_count", "volume_gb", "container_disk_gb", "image", "template_id", "duration_seconds", "pod_id"}
        if set(request) - allowed:
            raise PolicyError(f"unknown allocation fields: {sorted(set(request) - allowed)}")
        r, p, lim = dict(request), json.loads(row["policy"]), self.config["limits"]
        r.setdefault("gpu_count", 1)
        r.setdefault("volume_gb", min(20, p["max_volume_gb"]))
        r.setdefault("container_disk_gb", 20)
        r.setdefault("duration_seconds", 600)
        for key in ("gpu_count", "volume_gb", "container_disk_gb", "duration_seconds"):
            positive_int(r[key], key)
        if r["gpu_type"] not in p["allowed_gpu_types"]:
            raise PolicyError("GPU type not authorized")
        if not 10 <= r["volume_gb"] <= p["max_volume_gb"]:
            raise PolicyError("persistent volume must be at least 10 GB and within the limit")
        if r["container_disk_gb"] > lim.get("max_container_disk_gb", 50):
            raise PolicyError("container disk exceeds limit")
        if r["duration_seconds"] > self.config.get("safety", {}).get("max_lease_seconds", 3600):
            raise PolicyError("requested lease exceeds maximum; use bounded experiments")
        if bool(r.get("image")) == bool(r.get("template_id")):
            raise PolicyError("specify exactly one image or template_id")
        if r.get("image") and r["image"] not in lim.get("allowed_images", []):
            raise PolicyError("image not in owner-configured allowlist")
        if r.get("template_id") and r["template_id"] not in lim.get("allowed_templates", []):
            raise PolicyError("template not in owner-configured allowlist")
        for scope, limits in ((None, lim), (row["id"], p)):
            active = db.execute("SELECT * FROM allocations WHERE state IN ('CREATING','RUNNING','STOPPING','UNKNOWN')" +
                                (" AND session_id=?" if scope else ""), (scope,) if scope else ()).fetchall()
            if len(active) >= limits["max_pods"] or sum(json.loads(a["request"])["gpu_count"] for a in active) + r["gpu_count"] > limits["max_gpus"]:
                if r["gpu_count"] > limits["max_gpus"]:
                    raise PolicyError("requested GPU count exceeds capacity limit")
                raise CapacityError("capacity limit reached; wait for existing allocations")
        return r

    def allocate(self, db, args, owner):
        row = self.session(db, args["session_id"], owner)
        if row["status"] != "ACTIVE":
            raise PolicyError(f"session is {row['status']}")
        r = self.validate_request(db, row, args["request"])
        p, lim, now = json.loads(row["policy"]), self.config["limits"], self.clock()
        expiry = now + r["duration_seconds"]
        margin = self.config.get("safety", {}).get("shutdown_margin_seconds", 120)
        end = deadline(p.get("deadline"))
        if end is not None and expiry + margin > end:
            raise PolicyError("experiment and shutdown margin do not fit before deadline")
        if not r.get("pod_id"):
            seen = set()
            for old in db.execute("SELECT * FROM allocations WHERE session_id=? ORDER BY started DESC", (row["id"],)).fetchall():
                if not old["pod_id"] or old["pod_id"] in seen:
                    continue
                seen.add(old["pod_id"])
                old_request = json.loads(old["request"])
                if old["state"] == "STOPPED" and all(old_request.get(k) == r.get(k) for k in
                        ("gpu_type", "gpu_count", "volume_gb", "container_disk_gb", "image", "template_id")):
                    r["pod_id"] = old["pod_id"]
                    break
        if r.get("pod_id"):
            previous = db.execute("SELECT * FROM allocations WHERE pod_id=? ORDER BY started DESC LIMIT 1", (r["pod_id"],)).fetchone()
            if previous is None or previous["session_id"] != row["id"] or previous["state"] != "STOPPED":
                raise PolicyError("only this session's confirmed stopped pods can be resumed")
            old = json.loads(previous["request"])
            if any(old.get(k) != r.get(k) for k in ("gpu_type", "gpu_count", "volume_gb", "container_disk_gb", "image", "template_id")):
                raise PolicyError("restart request must match retained pod configuration")
        else:
            retained = self.retained_gb(db)
            if retained + r["volume_gb"] > lim["max_retained_gb"]:
                raise PolicyError("retained storage limit reached; owner cleanup required")
        quoted = self.provider.quote(r)
        rate = money(quoted["hourly_usd"])
        if rate <= 0:
            raise PolicyError("provider did not supply a positive running rate")
        for scope, ceiling, hourly in ((None, money(lim["budget_usd"]), money(lim["max_hourly_usd"])),
                                       (row["id"], self.cutoff(row), money(p["max_hourly_usd"]))):
            spent, reserved = self.totals(db, scope)
            if spent + reserved + cost(rate, r["duration_seconds"] + margin) > ceiling:
                if reserved:
                    raise CapacityError("existing reservations occupy the remaining budget; wait for settled work")
                raise PolicyError("experiment plus shutdown reserve exceeds remaining budget/review allowance")
            existing_rate = db.execute("SELECT COALESCE(SUM(rate),0) FROM allocations WHERE state IN ('CREATING','RUNNING','STOPPING','UNKNOWN')" +
                                       (" AND session_id=?" if scope else ""), (scope,) if scope else ()).fetchone()[0]
            if rate + existing_rate > hourly:
                if rate <= hourly and existing_rate:
                    raise CapacityError("existing allocations occupy the hourly allowance")
                raise PolicyError("combined hourly rate exceeds limit")
        aid = uuid.uuid4().hex
        name = "research-" + aid
        db.execute("INSERT INTO allocations(id,session_id,name,pod_id,request,state,rate,started,expires) VALUES(?,?,?,?,?,?,?,?,?)",
                   (aid, row["id"], name, r.get("pod_id"), json.dumps(r), "CREATING", rate, now, expiry))
        self.event(db, row["id"], "allocation_intent", {"allocation_id": aid, "name": name, "request": r})
        db.commit()  # Record exposure before the provider can charge us.
        try:
            provider_request = {**r, "gpu_type": quoted.get("gpu_type", r["gpu_type"])}
            pod = self.provider.start(r["pod_id"]) if r.get("pod_id") else self.provider.create(name, provider_request)
            if not isinstance(pod, dict) or not pod.get("id"):
                raise RuntimeError("provider returned no pod ID")
            actual = money(pod.get("hourly_usd", quoted["hourly_usd"]))
            if actual > rate:
                db.execute("UPDATE allocations SET rate=? WHERE id=?", (actual, aid))
            db.execute("UPDATE allocations SET pod_id=?,state='RUNNING' WHERE id=?", (pod["id"], aid))
            db.commit()
            if actual > rate:  # A changed offer must not silently spend a larger reservation.
                self.stop_allocation(db, aid, "actual rate exceeded admitted quote")
                return {"allocation_id": aid, "state": "STOPPING", "error": "rate changed; stopping pod"}
            if pod.get("volume_gb", r["volume_gb"]) < r["volume_gb"]:
                self.stop_allocation(db, aid, "provider persistent volume did not match request")
                return {"allocation_id": aid, "state": "STOPPING", "error": "persistent storage mismatch; stopping"}
            return {"allocation_id": aid, "pod": pod, "expires_at": expiry,
                    "reserved_usd": dollars(cost(rate, r["duration_seconds"] + margin))}
        except Exception as exc:
            from .provider import CreateRejected
            if isinstance(exc, CreateRejected):
                db.execute("UPDATE allocations SET state='NOT_CREATED',stopped=started,error=? WHERE id=?", (str(exc), aid))
                self.event(db, row["id"], "creation_rejected", {"allocation_id": aid, "error": str(exc)})
                return {"allocation_id": aid, "state": "NOT_CREATED", "error": str(exc)}
            pod_id = getattr(exc, "pod_id", None)
            db.execute("UPDATE allocations SET state='UNKNOWN',pod_id=COALESCE(?,pod_id),error=? WHERE id=?",
                       (pod_id, type(exc).__name__ + ": provider outcome uncertain; reconcile before any retry", aid))
            self.event(db, row["id"], "allocation_uncertain", {"allocation_id": aid, "exception": type(exc).__name__})
            return {"allocation_id": aid, "state": "UNKNOWN", "error": "provider outcome uncertain; reserved exposure retained; do not retry creation"}

    def stop_allocation(self, db, aid, reason):
        a = db.execute("SELECT * FROM allocations WHERE id=?", (aid,)).fetchone()
        if a["state"] not in LIVE:
            return
        db.execute("UPDATE allocations SET state='STOPPING',error=? WHERE id=?", (reason, aid))
        db.commit()

        if not a["pod_id"]:
            return
        try:
            try:
                self.provider.stop(a["pod_id"])
            except Exception:
                pass  # A lost response or already-stopped error still needs a state read.
            try:
                pod = self.provider.get(a["pod_id"])
            except Exception as exc:
                from .provider import PodNotFound
                if not isinstance(exc, PodNotFound):
                    raise
                pod = {"status": "TERMINATED"}  # Definitive provider 404; never a failed/empty read.
            if pod["status"] not in ("STOPPED", "EXITED", "TERMINATED"):
                raise RuntimeError("stop not yet confirmed")
            state = "TERMINATED" if pod["status"] == "TERMINATED" else "STOPPED"
            db.execute("UPDATE allocations SET state=?,stopped=?,error=? WHERE id=?", (state, self.clock(), reason, aid))
            db.execute("UPDATE jobs SET status='INTERRUPTED',updated=? WHERE allocation_id=? AND status IN ('QUEUED','RUNNING')", (self.clock(), aid))
            self.event(db, a["session_id"], "stop_confirmed", {"allocation_id": aid, "pod_id": a["pod_id"], "reason": reason})
        except Exception as exc:
            db.execute("UPDATE allocations SET error=? WHERE id=?", (f"{reason}; stop unconfirmed ({type(exc).__name__})", aid))
        db.commit()

    def request_stop(self, db, aid, reason):
        a = db.execute("SELECT * FROM allocations WHERE id=?", (aid,)).fetchone()
        running_job = db.execute("SELECT 1 FROM jobs WHERE allocation_id=? AND status='RUNNING'", (aid,)).fetchone()
        if a["state"] == "RUNNING" and running_job:
            grace = min(self.config.get("safety", {}).get("checkpoint_grace_seconds", 30),
                        self.config.get("safety", {}).get("shutdown_margin_seconds", 120))
            stop_after = min(self.clock() + grace, a["expires"])
            db.execute("UPDATE allocations SET state='STOPPING',stop_after=?,error=? WHERE id=?", (stop_after, reason, aid))
            self.event(db, a["session_id"], "checkpoint_requested", {"allocation_id": aid, "stop_after": stop_after})
            db.commit()
        elif a["state"] == "STOPPING" and a["stop_after"] is not None and self.clock() < a["stop_after"]:
            return
        else:
            self.stop_allocation(db, aid, reason)

    def tick(self):
        with self.locked() as db:
            self.accrue(db)
            uncertain = db.execute("SELECT * FROM allocations WHERE state IN ('CREATING','UNKNOWN','STOPPING') AND pod_id IS NULL").fetchall()
            if uncertain:
                try:
                    pods = self.provider.list()
                    for a in uncertain:
                        matches = [p for p in pods if p.get("name") == a["name"]]
                        if len(matches) == 1:
                            db.execute("UPDATE allocations SET pod_id=?,state='STOPPING' WHERE id=?", (matches[0]["id"], a["id"]))
                except Exception:
                    pass  # Keep all uncertain exposure; never infer deletion from a failed read.
            now = self.clock()
            db.execute("UPDATE jobs SET status='INTERRUPTED',updated=? WHERE allocation_id IS NULL AND status='QUEUED' AND updated<?",
                       (now, now - max(120, self.config.get("safety", {}).get("poll_seconds", 15) * 4)))
            margin = self.config.get("safety", {}).get("shutdown_margin_seconds", 120)
            account_spent, _ = self.totals(db)
            live = db.execute("SELECT * FROM allocations WHERE state IN ('CREATING','RUNNING','STOPPING','UNKNOWN')").fetchall()
            total_rate = sum(a["rate"] for a in live)
            global_cutoff = account_spent + cost(total_rate, margin) >= money(self.config["limits"]["budget_usd"])
            for row in db.execute("SELECT * FROM sessions").fetchall():
                p = json.loads(row["policy"])
                owned = [a for a in live if a["session_id"] == row["id"]]
                spent, _ = self.totals(db, row["id"])
                session_rate = sum(a["rate"] for a in owned)
                end = deadline(p.get("deadline"))
                state = row["status"]
                reason = row["reason"]
                if state == "ACTIVE":
                    if global_cutoff or spent + cost(session_rate, margin) >= money(p["budget_usd"]):
                        state, reason = "BUDGET_REACHED", "spending cutoff, including shutdown allowance"
                    elif end is not None and now + margin >= end:
                        state, reason = "DEADLINE_REACHED", "deadline shutdown margin reached"
                    elif p.get("review_at_usd") is not None and not row["review_cleared"] and spent + cost(session_rate, margin) >= money(p["review_at_usd"]):
                        state, reason = "REVIEW_REQUIRED", "cumulative review threshold reached"
                    if state != row["status"]:
                        db.execute("UPDATE sessions SET status=?,reason=? WHERE id=?", (state, reason, row["id"]))
                for a in owned:
                    job = db.execute("SELECT status,updated FROM jobs WHERE allocation_id=?", (a["id"],)).fetchone()
                    idle_since = a["started"] if job is None else job["updated"] if job["status"] not in ("QUEUED", "RUNNING") else None
                    idle = idle_since is not None and now - idle_since >= self.config.get("safety", {}).get("idle_grace_seconds", 120)
                    if a["state"] in ("UNKNOWN", "CREATING") or now >= a["expires"] or idle:
                        self.stop_allocation(db, a["id"], reason or "allocation lease expired or uncertain")
                    elif a["state"] == "STOPPING" or state != "ACTIVE":
                        self.request_stop(db, a["id"], reason or "stop requested")
            self.accrue(db)

    def dispatch(self, method, args, owner):
        # Every admission/status sees current estimates; network reconciliation is watchdog work.
        with self.locked() as db:
            self.accrue(db)
            if method == "session.create":
                return self.create_session(db, args, owner)
            if method == "session.list":
                return [self.status(db, r["id"], owner) for r in db.execute("SELECT id FROM sessions WHERE owner=? ORDER BY created", (owner,)).fetchall()]
            if method == "doctor":
                spent, reserved = self.totals(db)
                return {"ok": True, "provider": self.config["provider"]["kind"], "estimated_spend_usd": dollars(spent),
                        "reserved_usd": dollars(reserved), "account_budget_usd": self.config["limits"]["budget_usd"],
                        "retained_volume_gb": self.retained_gb(db), "limits": self.config["limits"],
                        "protected_service": os.getuid() != owner, "last_watchdog_tick": self._last_tick(db)}
            if method == "quote":
                row = self.session(db, args["session_id"], owner)
                return self.provider.quote(self.validate_request(db, row, args["request"]))
            if method == "allocate":
                last = self._last_tick(db)
                if last is None or self.clock() - last > max(60, self.config.get("safety", {}).get("poll_seconds", 15) * 4):
                    raise PolicyError("watchdog is not healthy; no paid allocation admitted")
                if db.execute("SELECT 1 FROM allocations WHERE state IN ('UNKNOWN','STOPPING','CREATING') LIMIT 1").fetchone():
                    raise CapacityError("unresolved provider operation; wait for reconciliation")
                return self.allocate(db, args, owner)
            if method == "watchdog.heartbeat":
                raise PolicyError("watchdog heartbeat is service-only")
            if method == "session.continuation":
                b = db.execute("SELECT * FROM bindings WHERE agent=? AND conversation=? AND owner=?", (args["agent"], args["conversation_id"], owner)).fetchone()
                if b is None or (args.get("workspace") and os.path.realpath(args["workspace"]) != b["workspace"]):
                    return {"continue": False, "reason": "no matching bound investigation"}
                status = self.status(db, b["session_id"], owner)
                return self.continuation(db, status)
            sid = args.get("session_id")
            row = self.session(db, sid, owner)
            if method == "session.status":
                return self.status(db, sid, owner)
            if method in ("session.pause", "session.finish", "session.block", "session.review"):
                state = {"session.pause": "PAUSED", "session.finish": "COMPLETE", "session.block": "BLOCKED", "session.review": "REVIEW_REQUIRED"}[method]
                reason = str(args.get("reason", state))
                db.execute("UPDATE sessions SET status=?,reason=? WHERE id=?", (state, reason, sid))
                db.commit()
                for a in db.execute("SELECT id FROM allocations WHERE session_id=? AND state IN ('CREATING','RUNNING','STOPPING','UNKNOWN')", (sid,)).fetchall():
                    self.request_stop(db, a["id"], reason)
            elif method == "session.resume":
                if row["status"] in TERMINAL:
                    raise PolicyError("completed/exhausted sessions require an owner policy amendment")
                if db.execute("SELECT 1 FROM allocations WHERE session_id=? AND state IN ('CREATING','RUNNING','STOPPING','UNKNOWN')", (sid,)).fetchone():
                    raise PolicyError("resume requires confirmed stopped allocations")
                if row["status"] == "REVIEW_REQUIRED" and not args.get("user_confirmed"):
                    raise PolicyError("review resume requires the user's continuation instruction")
                db.execute("UPDATE sessions SET status='ACTIVE',reason='',review_cleared=MAX(review_cleared,?) WHERE id=?",
                           (int(row["status"] == "REVIEW_REQUIRED"), sid))
                self.event(db, sid, "session_resumed", {"user_confirmed": bool(args.get("user_confirmed"))})
            elif method == "session.mode":
                if args["mode"] not in ("auto", "interactive"):
                    raise PolicyError("invalid mode")
                db.execute("UPDATE sessions SET mode=? WHERE id=?", (args["mode"], sid))
            elif method == "session.driver":
                if args["driver"] not in ("hook", "native"):
                    raise PolicyError("invalid continuation driver")
                policy = json.loads(row["policy"])
                policy["continuation_driver"] = args["driver"]
                db.execute("UPDATE sessions SET policy=? WHERE id=?", (json.dumps(policy), sid))
            elif method == "session.journal":
                entries = json.loads(row["journal"])
                entries.append({"at": self.clock(), "text": str(args["text"])[:16000]})
                db.execute("UPDATE sessions SET journal=? WHERE id=?", (json.dumps(entries), sid))
            elif method == "session.bind":
                if args["agent"] not in ("codex", "claude", "pi") or not args["conversation_id"]:
                    raise PolicyError("invalid agent/conversation binding")
                old = db.execute("SELECT session_id FROM bindings WHERE agent=? AND conversation=? AND owner=?", (args["agent"], args["conversation_id"], owner)).fetchone()
                if old and old[0] != sid:
                    raise PolicyError("conversation already belongs to another investigation")
                other = db.execute("SELECT * FROM bindings WHERE session_id=? AND NOT(agent=? AND conversation=? AND owner=?)",
                                   (sid, args["agent"], args["conversation_id"], owner)).fetchall()
                if other:
                    if not args.get("take_over") or row["status"] not in ("PAUSED", "BLOCKED", "REVIEW_REQUIRED"):
                        raise PolicyError("investigation already has a driver; pause it and explicitly take over to switch conversations")
                    if db.execute("SELECT 1 FROM allocations WHERE session_id=? AND state IN ('CREATING','RUNNING','STOPPING','UNKNOWN')", (sid,)).fetchone():
                        raise PolicyError("wait for confirmed stopped allocations before switching drivers")
                    db.execute("DELETE FROM bindings WHERE session_id=?", (sid,))
                db.execute("INSERT OR REPLACE INTO bindings VALUES(?,?,?,?,?)", (args["agent"], args["conversation_id"], owner, os.path.realpath(args["workspace"]), sid))
            elif method == "allocation.get":
                a = db.execute("SELECT * FROM allocations WHERE id=? AND session_id=?", (args["allocation_id"], sid)).fetchone()
                if a is None or not a["pod_id"]:
                    raise PolicyError("allocation has no confirmed pod")
                return {"allocation_id": a["id"], "state": a["state"], "expires_at": a["expires"],
                        "stop_after": a["stop_after"], "pod": self.provider.get(a["pod_id"])}
            elif method == "allocation.stop":
                a = db.execute("SELECT id FROM allocations WHERE id=? AND session_id=?", (args["allocation_id"], sid)).fetchone()
                if a is None:
                    raise PolicyError("unknown allocation")
                self.stop_allocation(db, a["id"], "agent requested stop and retain")
            elif method == "job.record":
                if args.get("allocation_id") is None:
                    old = db.execute("SELECT * FROM jobs WHERE id=?", (args["job_id"],)).fetchone()
                    if old and (old["session_id"] != sid or old["allocation_id"] is not None):
                        raise PolicyError("job identity already used")
                    if args["status"] not in ("QUEUED", "FAILED", "INTERRUPTED"):
                        raise PolicyError("unallocated job must be queued or finished")
                    db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,NULL,?,?,?)", (args["job_id"], sid, args["status"], json.dumps(args.get("detail", {})), self.clock()))
                    return self.status(db, sid, owner)
                a = db.execute("SELECT * FROM allocations WHERE id=? AND session_id=?", (args["allocation_id"], sid)).fetchone()
                if a is None:
                    raise PolicyError("unknown allocation")
                state = args["status"]
                if state not in ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED"):
                    raise PolicyError("invalid job state")
                old = db.execute("SELECT * FROM jobs WHERE id=? OR allocation_id=?", (args["job_id"], args["allocation_id"])).fetchone()
                if old and (old["session_id"] != sid or old["allocation_id"] not in (None, args["allocation_id"]) or old["id"] != args["job_id"]):
                    raise PolicyError("job identity already used")
                if state in ("QUEUED", "RUNNING") and a["state"] != "RUNNING":
                    raise PolicyError("allocation is no longer active")
                db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?)", (args["job_id"], sid, a["id"], state, json.dumps(args.get("detail", {})), self.clock()))
            else:
                raise PolicyError("unsupported operation")
            self.accrue(db)
            return self.status(db, sid, owner)

    def _last_tick(self, db):
        r = db.execute("SELECT at FROM events WHERE kind='watchdog_tick' ORDER BY id DESC LIMIT 1").fetchone()
        return r[0] if r else None

    def amend(self, sid, patch):
        """Owner-only local operation: deliberately not exposed on the socket."""
        if set(patch) - {"budget_usd", "deadline", "review_at_usd", "max_iterations"}:
            raise PolicyError("amendment accepts budget_usd, deadline, review_at_usd, max_iterations")
        with self.locked() as db:
            self.accrue(db)
            row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise PolicyError("unknown session")
            if db.execute("SELECT 1 FROM allocations WHERE session_id=? AND state IN ('CREATING','RUNNING','STOPPING','UNKNOWN')", (sid,)).fetchone():
                raise PolicyError("pause and confirm stopped pods before an owner amendment")
            policy = {**json.loads(row["policy"]), **patch}
            spent, _ = self.totals(db, sid)
            if not spent < money(policy["budget_usd"]) <= money(self.config["limits"]["budget_usd"]):
                raise PolicyError("amended total budget must exceed spent and fit the owner allowance")
            end = deadline(policy.get("deadline"))
            if end is not None and end <= self.clock():
                raise PolicyError("amended deadline must be in the future")
            if policy.get("review_at_usd") is not None and not spent < money(policy["review_at_usd"]) <= money(policy["budget_usd"]):
                raise PolicyError("new review threshold must exceed spent and fit the total budget; use null to clear")
            positive_int(policy["max_iterations"], "max_iterations")
            db.execute("UPDATE sessions SET policy=?,status='PAUSED',reason='owner amended policy',review_cleared=0 WHERE id=?", (json.dumps(policy), sid))
            self.event(db, sid, "owner_amendment", patch)
            return self.status(db, sid, row["owner"])

    def resolve_no_pod(self, aid, reason):
        """Local owner attestation after checking an unresolved creation in RunPod."""
        if not str(reason).strip():
            raise PolicyError("record the evidence for the no-pod decision")
        with self.locked() as db:
            row = db.execute("SELECT * FROM allocations WHERE id=?", (aid,)).fetchone()
            if row is None or row["pod_id"] is not None or row["state"] not in ("CREATING", "UNKNOWN", "STOPPING"):
                raise PolicyError("only an unresolved creation with no known pod ID can be cleared")
            db.execute("UPDATE allocations SET state='NOT_CREATED',stopped=started,spent=0,error=? WHERE id=?", (reason, aid))
            self.event(db, row["session_id"], "owner_confirmed_no_pod", {"allocation_id": aid, "reason": reason})
            return {"allocation_id": aid, "state": "NOT_CREATED", "reason": reason}

    def watchdog_tick(self):
        self.tick()
        with self.locked() as db:
            # Keep one durable heartbeat, avoiding a growing log of routine polls.
            db.execute("DELETE FROM events WHERE kind='watchdog_tick'")
            self.event(db, None, "watchdog_tick", {})
        self.reconcile_retention()

    def reconcile_retention(self):
        # Low-priority provider reads never hold the lease/admission lock. Check
        # only one retained pod per minute so old volumes cannot delay shutdown.
        if self.clock() - getattr(self, "_retention_checked", 0) < 60:
            return
        self._retention_checked = self.clock()
        with self.locked() as db:
            rows = db.execute("SELECT a.* FROM allocations a WHERE a.state='STOPPED' AND a.pod_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM allocations b WHERE b.pod_id=a.pod_id AND b.started>a.started) ORDER BY a.pod_id").fetchall()
            if not rows:
                return
            index = getattr(self, "_retention_cursor", 0) % len(rows)
            self._retention_cursor = index + 1
            candidate = dict(rows[index])
        try:
            deleted = self.provider.get(candidate["pod_id"])["status"] == "TERMINATED"
        except Exception as exc:
            from .provider import PodNotFound
            deleted = isinstance(exc, PodNotFound)
        if deleted:
            with self.locked() as db:
                if not db.execute("SELECT 1 FROM allocations WHERE pod_id=? AND state IN ('CREATING','RUNNING','STOPPING','UNKNOWN')", (candidate["pod_id"],)).fetchone():
                    db.execute("UPDATE allocations SET state='TERMINATED' WHERE pod_id=? AND state='STOPPED'", (candidate["pod_id"],))

    def continuation(self, db, status):
        sid = status["id"]
        history = db.execute("SELECT * FROM events WHERE session_id=? AND kind='continuation' AND id>COALESCE((SELECT MAX(id) FROM events WHERE session_id=? AND kind IN ('session_resumed','owner_amendment')),0) ORDER BY id", (sid, sid)).fetchall()
        count = len(history)
        evidence = {"journal_entries": len(status["journal"]), "jobs": [(j["id"], j["status"]) for j in status["jobs"]]}
        reason = None
        if status["mode"] != "auto" or status["status"] != "ACTIVE":
            reason = "investigation is not active autonomous work"
        elif status["policy"].get("continuation_driver", "hook") == "native":
            return {"continue": False, "reason": "native goal or schedule owns continuation", "session_id": sid}
        elif status["remaining_usd"] <= 0:
            reason = "budget exhausted"
        elif deadline(status["policy"].get("deadline")) is not None and self.clock() >= deadline(status["policy"]["deadline"]):
            reason = "research deadline reached"
        elif any(j["status"] in ("QUEUED", "RUNNING") for j in status["jobs"]) and not any(
                j["status"] not in ("QUEUED", "RUNNING") and (not history or j["updated"] > history[-1]["at"]) for j in status["jobs"]):
            return {"continue": False, "waiting": True, "reason": "waiting for admitted jobs", "session_id": sid}
        elif count >= status["policy"].get("max_iterations", 50):
            reason = "bounded continuation limit reached; ask user before continuing"
        elif len(status["jobs"]) >= 3 and all(j["status"] in ("FAILED", "INTERRUPTED") for j in status["jobs"][-3:]):
            reason = "three consecutive experiment failures; investigate before resuming"
        elif len(history) >= 3 and all(json.loads(h["detail"]) == evidence for h in history[-3:]):
            reason = "three continuations without recorded progress; ask user before continuing"
        if reason:
            if status["status"] == "ACTIVE" and status["mode"] == "auto":
                db.execute("UPDATE sessions SET status='BLOCKED',reason=? WHERE id=?", (reason, sid))
            return {"continue": False, "reason": reason, "session_id": sid}
        self.event(db, sid, "continuation", evidence)
        return {"continue": True, "session_id": sid, "prompt":
                f"Continue the authorized research investigation {sid}: {status['objective']}. "
                "Read its status and journal. Wait for admitted jobs rather than duplicate them. "
                "Choose useful bounded follow-ups within its remaining budget. Record evidence; "
                "finish, pause, or block the investigation when appropriate. Do not chase a positive result."}
