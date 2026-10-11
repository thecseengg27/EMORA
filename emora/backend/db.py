"""
SQLite persistence for EMORA (standard library only).
A "run" is one learning journey on one topic. Activities keep their answer keys
here on the server; the browser only sees the public version.
"""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emora_v2.db")


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    with _db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, topic TEXT NOT NULL, curriculum TEXT NOT NULL,
                step_idx INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0,
                last_feedback TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, step_idx INTEGER NOT NULL,
                state TEXT NOT NULL, approach TEXT NOT NULL, expression TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, step_idx INTEGER NOT NULL,
                kind TEXT NOT NULL, state TEXT, payload TEXT NOT NULL,
                answered INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, activity_id INTEGER NOT NULL,
                step_idx INTEGER NOT NULL, kind TEXT NOT NULL, state TEXT, is_correct INTEGER NOT NULL,
                hint_used INTEGER NOT NULL DEFAULT 0, response_seconds REAL, xp INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL);
            """
        )


# ---- runs ----
def create_run(topic: str, curriculum: list) -> str:
    run_id = "r-" + uuid.uuid4().hex[:12]
    with _db() as c:
        c.execute(
            "INSERT INTO runs (run_id, topic, curriculum, created_at) VALUES (?,?,?,?)",
            (run_id, topic, json.dumps(curriculum), _now()),
        )
    return run_id


def get_run(run_id: str):
    with _db() as c:
        row = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["curriculum"] = json.loads(d["curriculum"])
    d["last_feedback"] = json.loads(d["last_feedback"]) if d["last_feedback"] else None
    return d


def advance_step(run_id: str) -> None:
    with _db() as c:
        c.execute("UPDATE runs SET step_idx=step_idx+1, retries=0, last_feedback=NULL WHERE run_id=?", (run_id,))


def bump_retries(run_id: str, feedback: dict) -> None:
    with _db() as c:
        c.execute(
            "UPDATE runs SET retries=retries+1, last_feedback=? WHERE run_id=?",
            (json.dumps(feedback), run_id),
        )


# ---- turns / activities / attempts ----
def log_turn(run_id, step_idx, state, approach, expression) -> None:
    with _db() as c:
        c.execute(
            "INSERT INTO turns (run_id, step_idx, state, approach, expression, created_at) VALUES (?,?,?,?,?,?)",
            (run_id, step_idx, state, approach, expression, _now()),
        )


def create_activity(run_id, step_idx, kind, payload, state=None) -> int:
    with _db() as c:
        cur = c.execute(
            "INSERT INTO activities (run_id, step_idx, kind, state, payload, created_at) VALUES (?,?,?,?,?,?)",
            (run_id, step_idx, kind, state, json.dumps(payload), _now()),
        )
        return cur.lastrowid


def get_activity(activity_id: int):
    with _db() as c:
        row = c.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


def mark_answered(activity_id: int) -> None:
    with _db() as c:
        c.execute("UPDATE activities SET answered=1 WHERE id=?", (activity_id,))


def log_attempt(run_id, activity_id, step_idx, kind, state, is_correct, hint_used, seconds, xp) -> None:
    with _db() as c:
        c.execute(
            "INSERT INTO attempts (run_id, activity_id, step_idx, kind, state, is_correct, hint_used, response_seconds, xp, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, activity_id, step_idx, kind, state, int(is_correct), int(hint_used), seconds, xp, _now()),
        )


def current_streak(run_id: str) -> int:
    with _db() as c:
        rows = c.execute("SELECT is_correct FROM attempts WHERE run_id=? ORDER BY id DESC", (run_id,)).fetchall()
    n = 0
    for r in rows:
        if not r["is_correct"]:
            break
        n += 1
    return n


def recent_stats(run_id: str, n: int = 4) -> dict:
    """Behaviour signals from the last few PRACTICE answers."""
    with _db() as c:
        rows = c.execute(
            "SELECT is_correct, response_seconds FROM attempts WHERE run_id=? AND kind='practice' ORDER BY id DESC",
            (run_id,),
        ).fetchall()
    last = rows[:n]
    consecutive_wrong = 0
    for r in rows:
        if r["is_correct"]:
            break
        consecutive_wrong += 1
    times = [r["response_seconds"] for r in last if r["response_seconds"]]
    return {
        "attempts": len(last),
        "accuracy": (sum(r["is_correct"] for r in last) / len(last)) if last else 0.6,
        "avg_seconds": (sum(times) / len(times)) if times else 50.0,
        "consecutive_wrong": consecutive_wrong,
    }


def progress(run_id: str):
    run = get_run(run_id)
    if not run:
        return None
    with _db() as c:
        attempts = [dict(r) for r in c.execute("SELECT * FROM attempts WHERE run_id=? ORDER BY id", (run_id,))]
        turns = [dict(r) for r in c.execute("SELECT * FROM turns WHERE run_id=? ORDER BY id", (run_id,))]

    steps = run["curriculum"]
    total = len(attempts)
    correct = sum(a["is_correct"] for a in attempts)
    times = [a["response_seconds"] for a in attempts if a["response_seconds"]]
    mastery = [{"title": s["title"], "attempts": 0, "correct": 0} for s in steps]
    for a in attempts:
        if 0 <= a["step_idx"] < len(mastery):
            mastery[a["step_idx"]]["attempts"] += 1
            mastery[a["step_idx"]]["correct"] += a["is_correct"]
    assess = [a for a in attempts if a["kind"] == "assessment"]

    streak = 0
    for a in reversed(attempts):
        if not a["is_correct"]:
            break
        streak += 1

    return {
        "topic": run["topic"],
        "steps": [s["title"] for s in steps],
        "step_idx": run["step_idx"],
        "attempts": total,
        "correct": correct,
        "accuracy": (correct / total) if total else None,
        "avg_seconds": (sum(times) / len(times)) if times else None,
        "xp": sum(a["xp"] for a in attempts),
        "streak": streak,
        "step_mastery": mastery,
        "assessment": {"attempts": len(assess), "correct": sum(a["is_correct"] for a in assess)},
        "timeline": [
            {
                "step": steps[t["step_idx"]]["title"] if t["step_idx"] < len(steps) else "",
                "state": t["state"], "approach": t["approach"], "expression": t["expression"], "at": t["created_at"],
            }
            for t in turns
        ],
        "history": [{"correct": bool(a["is_correct"]), "kind": a["kind"], "state": a["state"]} for a in attempts],
    }
