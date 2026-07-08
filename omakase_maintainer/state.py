"""Durable maintainer state: miners, submissions, the FIFO queue, and the
enforcement of the limits that are config-only elsewhere.

This is where rate-limits, the open-PR cap, credibility decay, and the banlist
actually *happen* — not in a doc. Every decision is derived from committed rows,
so it survives a restart and is auditable. Publishes JSON snapshots the
dashboard projects (queue / miners / metrics).
"""
from __future__ import annotations

import json
import os
import sqlite3

CREDIBILITY_START = 1.0
CREDIBILITY_FLOOR = 0.1
MECH_FAIL_DECAY = 0.34  # identity/manifest/static failures — three strikes to the floor
RERUN_FAIL_DECAY = 0.03  # losing a fair Gate-4 race barely stings
RERUN_COOLDOWN_S = 24 * 3600


class State:
    def __init__(self, db_path: str, now: float):
        self.now = now
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS miners(
                hotkey TEXT PRIMARY KEY, github_login TEXT, credibility REAL,
                banned INTEGER DEFAULT 0, first_seen REAL, last_seen REAL);
            CREATE TABLE IF NOT EXISTS submissions(
                id TEXT PRIMARY KEY, competition TEXT, pr INTEGER, hotkey TEXT,
                github_login TEXT, artifact_sha TEXT, status TEXT,
                enqueued_ts REAL, decided_ts REAL, reason TEXT, tier TEXT);
            """
        )
        self.db.commit()

    # -- miners --------------------------------------------------------------
    def touch_miner(self, hotkey: str, github_login: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM miners WHERE hotkey=?", (hotkey,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO miners VALUES(?,?,?,?,?,?)",
                (hotkey, github_login, CREDIBILITY_START, 0, self.now, self.now))
        else:
            self.db.execute("UPDATE miners SET last_seen=?, github_login=? WHERE hotkey=?",
                            (self.now, github_login, hotkey))
        self.db.commit()
        return self.db.execute("SELECT * FROM miners WHERE hotkey=?", (hotkey,)).fetchone()

    def adjust_credibility(self, hotkey: str, delta: float) -> None:
        cred = max(0.0, self.db.execute("SELECT credibility FROM miners WHERE hotkey=?", (hotkey,)).fetchone()[0] + delta)
        banned = 1 if cred < CREDIBILITY_FLOOR else 0
        self.db.execute("UPDATE miners SET credibility=?, banned=? WHERE hotkey=?", (cred, banned, hotkey))
        self.db.commit()

    def is_banned(self, hotkey: str) -> bool:
        row = self.db.execute("SELECT banned FROM miners WHERE hotkey=?", (hotkey,)).fetchone()
        return bool(row and row["banned"])

    # -- admission control ---------------------------------------------------
    def admit(self, hotkey: str, competition: str) -> tuple[bool, str]:
        """Enforce banlist, open-PR cap (1), and the 24h rerun cooldown."""
        if self.is_banned(hotkey):
            return False, "banlisted (credibility below floor)"
        open_pr = self.db.execute(
            "SELECT COUNT(*) FROM submissions WHERE hotkey=? AND competition=? AND status IN('queued','running')",
            (hotkey, competition)).fetchone()[0]
        if open_pr:
            return False, "open submission already in flight (1 per hotkey per competition)"
        last = self.db.execute(
            "SELECT MAX(decided_ts) FROM submissions WHERE hotkey=? AND decided_ts IS NOT NULL", (hotkey,)).fetchone()[0]
        if last and self.now - last < RERUN_COOLDOWN_S:
            return False, f"rerun cooldown ({int((RERUN_COOLDOWN_S-(self.now-last))/3600)}h remaining)"
        return True, "ok"

    # -- submissions / queue -------------------------------------------------
    def enqueue(self, sub_id: str, competition: str, pr: int, hotkey: str, github_login: str, artifact_sha: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO submissions(id,competition,pr,hotkey,github_login,artifact_sha,status,enqueued_ts)"
            " VALUES(?,?,?,?,?,?, 'queued', ?)",
            (sub_id, competition, pr, hotkey, github_login, artifact_sha, self.now))
        self.db.commit()

    def set_status(self, sub_id: str, status: str, reason: str = "", tier: str | None = None) -> None:
        decided = self.now if status in ("merged", "closed") else None
        self.db.execute("UPDATE submissions SET status=?, reason=?, tier=?, decided_ts=COALESCE(?,decided_ts) WHERE id=?",
                        (status, reason, tier, decided, sub_id))
        self.db.commit()

    def queue(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM submissions WHERE status IN('queued','running') ORDER BY enqueued_ts").fetchall()
        return [{"competition": r["competition"], "pr": r["pr"], "hotkey": r["hotkey"],
                 "github_login": r["github_login"], "status": r["status"], "position": i + 1,
                 "enqueued_ts": r["enqueued_ts"]} for i, r in enumerate(rows)]

    # -- dashboard snapshots -------------------------------------------------
    def metrics(self) -> dict:
        day = self.now - 86400
        closes = self.db.execute("SELECT COUNT(*) FROM submissions WHERE status='closed' AND decided_ts>?", (day,)).fetchone()[0]
        evals = self.db.execute("SELECT COUNT(*) FROM submissions WHERE decided_ts>?", (day,)).fetchone()[0]
        banned = self.db.execute("SELECT COUNT(*) FROM miners WHERE banned=1").fetchone()[0]
        return {"queue_depth": len(self.queue()), "auto_closes_24h": closes,
                "banlist_size": banned, "evals_24h": evals, "updated_ts": round(self.now, 3)}

    def miners(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM miners ORDER BY credibility DESC").fetchall()
        out = []
        for r in rows:
            last = self.db.execute(
                "SELECT MAX(decided_ts) FROM submissions WHERE hotkey=? AND decided_ts IS NOT NULL",
                (r["hotkey"],)).fetchone()[0]
            in_flight = self.db.execute(
                "SELECT COUNT(*) FROM submissions WHERE hotkey=? AND status IN('queued','running')",
                (r["hotkey"],)).fetchone()[0]
            out.append({
                "hotkey": r["hotkey"], "github_login": r["github_login"],
                "credibility": round(r["credibility"], 3), "banned": bool(r["banned"]),
                "submissions": self.db.execute("SELECT COUNT(*) FROM submissions WHERE hotkey=?", (r["hotkey"],)).fetchone()[0],
                # next moment a new submission is admissible (cooldown from the last decision)
                "next_eligible_ts": round(last + RERUN_COOLDOWN_S, 3) if last else None,
                "in_flight": bool(in_flight),
            })
        return out

    def publish_snapshots(self, state_dir: str) -> None:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "queue.json"), "w") as f:
            json.dump({"items": self.queue()}, f, indent=1)
        with open(os.path.join(state_dir, "metrics.json"), "w") as f:
            json.dump(self.metrics(), f, indent=1)
        with open(os.path.join(state_dir, "miners.json"), "w") as f:
            json.dump({"miners": self.miners()}, f, indent=1)
