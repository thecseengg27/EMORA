"""
EMORA backend — FastAPI app.

Run with:  python -m uvicorn main:app --reload --port 8000

Endpoints
    GET  /api/health
    POST /api/learn/start          topic -> curriculum (SerpApi + LLM), returns run_id
    POST /api/learn/next           teach the next turn in the style matching the learner state
    POST /api/learn/answer         grade an activity on the server, award XP, update the plan
    POST /api/learn/assessment     final "boss level" mixed assessment
    GET  /api/progress/{run_id}    stats, mastery, state timeline
    POST /api/search               raw SerpApi search (for testing)
"""
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()  # reads backend/.env

import agent
import db
import llm
import serp_client
import teacher

app = FastAPI(title="EMORA API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
db.init_db()


class StartReq(BaseModel):
    topic: str


class NextReq(BaseModel):
    run_id: str
    expression_signal: str = "neutral"


class AnswerReq(BaseModel):
    run_id: str
    activity_id: int
    response: Any = None
    hint_used: bool = False
    response_seconds: float = 0.0


class RunReq(BaseModel):
    run_id: str


class SearchReq(BaseModel):
    query: str


def _need_llm():
    if not llm.configured():
        raise HTTPException(503, f"No LLM key found. Add {llm.key_name()} (and LLM_PROVIDER) to backend/.env, then restart the server.")


def _search(query: str):
    """Live SerpApi search; never crashes a lesson if search is unavailable."""
    try:
        return serp_client.search(query), None
    except Exception as e:
        return [], str(e)[:160]


def _llm_call(fn, *args):
    try:
        return fn(*args)
    except llm.LLMError as e:
        raise HTTPException(502, str(e))


def _run_or_404(run_id: str) -> dict:
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Unknown run — start learning again.")
    return run


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "serpapi_key_set": bool(os.getenv("SERPAPI_KEY")),
        "llm_provider": llm.provider(),
        "llm_key_set": llm.configured(),
    }


@app.post("/api/search")
def search(req: SearchReq):
    try:
        return {"results": serp_client.search(req.query)}
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.post("/api/learn/start")
def learn_start(req: StartReq):
    _need_llm()
    topic = req.topic.strip()[:200]
    if not topic:
        raise HTTPException(400, "Please enter a topic.")
    query = f"{topic} tutorial for beginners"
    resources, err = _search(query)
    curriculum = _llm_call(teacher.build_curriculum, topic, resources)
    run_id = db.create_run(topic, curriculum)
    return {
        "run_id": run_id,
        "topic": topic,
        "steps": [s["title"] for s in curriculum],
        "search": {"query": query, "ok": err is None, "error": err, "results": len(resources)},
    }


@app.post("/api/learn/next")
def learn_next(req: NextReq):
    _need_llm()
    run = _run_or_404(req.run_id)
    steps, idx, retry = run["curriculum"], run["step_idx"], run["retries"]
    if idx >= len(steps):
        return {"done": True}

    stats = db.recent_stats(req.run_id)
    state = agent.compute_state(
        stats["accuracy"], stats["avg_seconds"], req.expression_signal, stats["consecutive_wrong"], stats["attempts"]
    )
    strat = agent.STRATEGIES[state]
    step = steps[idx]

    query = agent.plan_query(run["topic"], step["title"], state, retry)
    resources, err = _search(query)
    turn = _llm_call(teacher.teach_step, run["topic"], step, idx, len(steps), state, retry, run["last_feedback"], resources)

    activity_id = db.create_activity(req.run_id, idx, "practice", turn["activity"], state)
    db.log_turn(req.run_id, idx, state, strat["label"], req.expression_signal)
    if retry >= 3:  # full solution was shown; move on so the learner is never stuck
        db.advance_step(req.run_id)

    return {
        "done": False,
        "step_idx": idx,
        "total_steps": len(steps),
        "step_title": step["title"],
        "state": state,
        "approach": strat["label"],
        "note": strat["note"],
        "title": turn["title"],
        "blocks": turn["blocks"],
        "encouragement": turn["encouragement"],
        "activity": teacher.public_activity(turn["activity"], activity_id),
        "sources": [{"title": resources[i]["title"], "link": resources[i]["link"]} for i in turn["sources"]],
        "search": {"query": query, "ok": err is None, "error": err, "results": len(resources)},
    }


@app.post("/api/learn/answer")
def learn_answer(req: AnswerReq):
    act = db.get_activity(req.activity_id)
    if not act or act["run_id"] != req.run_id:
        raise HTTPException(404, "Unknown activity.")
    if act["answered"]:
        raise HTTPException(409, "This activity was already answered.")

    payload = act["payload"]
    ok, correct_text, learner_text = teacher.grade(payload, req.response)
    streak_before = db.current_streak(req.run_id)
    xp = agent.award_xp(act["kind"], ok, req.hint_used, streak_before)

    db.mark_answered(req.activity_id)
    db.log_attempt(req.run_id, req.activity_id, act["step_idx"], act["kind"], act["state"], ok,
                   req.hint_used, req.response_seconds, xp)

    run = db.get_run(req.run_id)
    if act["kind"] == "practice" and act["step_idx"] == run["step_idx"]:
        if ok:
            db.advance_step(req.run_id)
        else:
            db.bump_retries(req.run_id, {
                "question": payload["prompt"], "learner_answer": learner_text,
                "correct_answer": correct_text, "explanation": payload["explanation"],
            })
        run = db.get_run(req.run_id)

    return {
        "correct": ok,
        "correct_answer": correct_text,
        "explanation": payload["explanation"],
        "xp_gained": xp,
        "streak": streak_before + 1 if ok else 0,
        "finished_curriculum": run["step_idx"] >= len(run["curriculum"]),
    }


@app.post("/api/learn/assessment")
def learn_assessment(req: RunReq):
    _need_llm()
    run = _run_or_404(req.run_id)
    prog = db.progress(req.run_id)
    questions = _llm_call(teacher.build_assessment, run["topic"], run["curriculum"], prog["step_mastery"])
    out = []
    for q in questions:
        aid = db.create_activity(req.run_id, q.pop("step_index"), "assessment", q)
        out.append(teacher.public_activity(q, aid))
    return {"questions": out}


@app.get("/api/progress/{run_id}")
def get_progress(run_id: str):
    p = db.progress(run_id)
    if p is None:
        raise HTTPException(404, "Unknown run.")
    return p