"""
EMORA teaching engine: builds a curriculum, teaches ONE step at a time in the
style that matches the learner's state, generates games/quizzes, and grades
answers on the server (answers never reach the browser before the learner replies).
"""
import random

import agent
import llm

TYPES = ("mcq", "order", "find_bug", "fill_blank")
BLOCK_KINDS = ("text", "analogy", "code", "tip")

SYSTEM = (
    "You are EMORA, a warm, precise tutor who TEACHES concepts directly instead of pointing to links. "
    "Web search snippets are provided only so your facts stay accurate and current; teach in your own words. "
    "You adapt to observable signals (quiz results, timing, a facial-expression estimate) and never claim to know "
    "what the learner truly feels. Use plain text only (no markdown symbols like ** or #). "
    "Reply with ONE valid JSON object and nothing else."
)

ACTIVITY_SPEC = """Activity types (choose ONE from the allowed list). Exact shapes:
{"type":"mcq","prompt":"...","code":"optional code shown above the options, else empty string","options":["A","B","C"],"answer_index":0,"explanation":"why the answer is right","hint":"a nudge"}
{"type":"order","prompt":"Put these steps in the correct order","items":["first","second","third","fourth"],"explanation":"...","hint":"..."}   (items listed in the CORRECT order, 3-6 short items)
{"type":"find_bug","prompt":"One line is wrong. Click it.","lines":["line 1","line 2","line 3","line 4"],"bug_line_index":2,"explanation":"...","hint":"..."}   (3-10 lines; for non-programming topics the lines are statements and exactly one is false)
{"type":"fill_blank","prompt":"...","template":"text or code containing ____ exactly once","answer":"expected text","accepted":["other accepted answers"],"explanation":"...","hint":"..."}
Never reveal the activity's answer inside the teaching blocks."""


# ---------- validation ----------
def _s(v, name):
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{name} is missing")
    return v.strip()


def _idx(v, n, name):
    try:
        i = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if isinstance(v, bool) or not 0 <= i < n:
        raise ValueError(f"{name} out of range")
    return i


def _str_list(v, lo, hi, name):
    if not isinstance(v, list) or not lo <= len(v) <= hi or not all(isinstance(x, str) and x.strip() for x in v):
        raise ValueError(f"{name} must be a list of {lo}-{hi} non-empty strings")
    return [x.strip() for x in v]


def normalize_activity(a, allowed=None) -> dict:
    if not isinstance(a, dict):
        raise ValueError("activity is missing")
    t = a.get("type")
    if t not in TYPES:
        raise ValueError(f"unknown activity type {t!r}")
    if allowed and t not in allowed:
        raise ValueError(f"activity type {t!r} not allowed here; use one of {allowed}")
    out = {
        "type": t,
        "prompt": _s(a.get("prompt"), "prompt"),
        "explanation": _s(a.get("explanation"), "explanation"),
        "hint": str(a.get("hint") or "").strip(),
    }
    if t == "mcq":
        opts = _str_list(a.get("options"), 2, 5, "options")
        out.update(options=opts, answer_index=_idx(a.get("answer_index"), len(opts), "answer_index"),
                   code=str(a.get("code") or ""))
    elif t == "order":
        items = _str_list(a.get("items"), 3, 6, "items")
        if len(set(items)) != len(items):
            raise ValueError("order items must be unique")
        out["items"] = items
    elif t == "find_bug":
        lines = _str_list(a.get("lines"), 3, 10, "lines")
        out.update(lines=lines, bug_line_index=_idx(a.get("bug_line_index"), len(lines), "bug_line_index"))
    else:  # fill_blank
        tpl = _s(a.get("template"), "template")
        if "____" not in tpl:
            raise ValueError("template must contain ____")
        acc = a.get("accepted") if isinstance(a.get("accepted"), list) else []
        out.update(template=tpl, answer=_s(a.get("answer"), "answer"), accepted=[str(x) for x in acc])
    return out


def public_activity(p: dict, activity_id: int) -> dict:
    """What the browser is allowed to see (no answers)."""
    pub = {"id": activity_id, "type": p["type"], "prompt": p["prompt"], "hint": p["hint"]}
    if p["type"] == "mcq":
        pub.update(options=p["options"], code=p.get("code", ""))
    elif p["type"] == "order":
        items = p["items"][:]
        random.Random(activity_id).shuffle(items)
        if items == p["items"]:
            items = items[1:] + items[:1]
        pub["items"] = items
    elif p["type"] == "find_bug":
        pub["lines"] = p["lines"]
    else:
        pub["template"] = p["template"]
    return pub


def _norm(s) -> str:
    return " ".join(str(s).lower().split())


def grade(p: dict, response):
    """Return (is_correct, correct_answer_text, learner_answer_text)."""
    t = p["type"]
    if t in ("mcq", "find_bug"):
        try:
            i = int(response)
        except (TypeError, ValueError):
            i = -1
        if t == "mcq":
            right, pool = p["answer_index"], p["options"]
            correct = pool[right]
        else:
            right, pool = p["bug_line_index"], p["lines"]
            correct = f"line {right + 1}: {pool[right]}"
        mine = pool[i] if 0 <= i < len(pool) else ""
        if t == "find_bug" and mine:
            mine = f"line {i + 1}: {mine}"
        return i == right, correct, mine
    if t == "order":
        resp = [str(x) for x in response] if isinstance(response, list) else []
        return resp == p["items"], " → ".join(p["items"]), " → ".join(resp)
    accepted = {_norm(p["answer"])} | {_norm(x) for x in p.get("accepted", [])}
    return _norm(response or "") in accepted, p["answer"], str(response or "")


# ---------- LLM calls ----------
def _ask(user: str, validator, max_tokens: int = 3000):
    note, last = "", None
    for _ in range(2):
        try:
            return validator(llm.generate_json(SYSTEM, user + note, max_tokens))
        except (ValueError, KeyError, TypeError, IndexError) as e:
            last, note = e, f"\n\nYour previous reply was rejected ({e}). Return corrected JSON only."
    raise llm.LLMError(f"The model kept returning invalid JSON ({last}). Please try again.")


def _snippets(resources) -> str:
    if not resources:
        return "(no web results available — rely on your own accurate knowledge)"
    return "\n".join(f"[{i}] {r['title']} — {r['snippet']}" for i, r in enumerate(resources[:5]))


def build_curriculum(topic: str, resources) -> list[dict]:
    user = (
        f"Learner goal: {topic}\n\nWeb search snippets (for accuracy only):\n{_snippets(resources)}\n\n"
        "Design a learning path of exactly 5 steps, from first principles to application, for a learner new to this. "
        "Each step teaches ONE idea that builds on the previous one.\n"
        'Return JSON: {"steps":[{"title":"short title","goal":"one sentence: what the learner can do after this step"}]}'
    )

    def check(d):
        steps = d["steps"]
        if not isinstance(steps, list) or not 3 <= len(steps) <= 7:
            raise ValueError("steps must be a list of 3-7 items")
        return [{"title": _s(s.get("title"), "title"), "goal": _s(s.get("goal"), "goal")} for s in steps]

    return _ask(user, check, 1500)


def teach_step(topic, step, idx, total, state, retry, last_feedback, resources) -> dict:
    strat = agent.STRATEGIES[state]
    allowed = ["mcq", "fill_blank"] if retry >= 3 else strat["allowed"]

    extra = ""
    if last_feedback:
        extra += (
            "\nThe learner just got the previous activity wrong:\n"
            f"  question: {last_feedback.get('question')}\n"
            f"  their answer: {last_feedback.get('learner_answer') or '(none)'}\n"
            f"  correct answer: {last_feedback.get('correct_answer')}\n"
            "Explain the underlying misunderstanding in a NEW way (different from before), then give a NEW activity.\n"
        )
    if retry >= 3:
        extra += (
            "The learner has missed this several times. Walk through the full solution to the previous question "
            "step by step in the blocks, then give a very easy activity so they leave with a win.\n"
        )

    user = (
        f"Topic: {topic}\nStep {idx + 1} of {total}: {step['title']} — goal: {step['goal']}\n"
        f"Learner state (estimated from quiz behaviour + facial-expression signal): {state}\n"
        f"Teaching approach: {strat['label']}\n{strat['instructions']}\n"
        f"Allowed activity types: {allowed}\nAttempt number on this step: {retry + 1}\n{extra}\n"
        f"Web search snippets (for accuracy; cite by index in \"sources\"):\n{_snippets(resources)}\n\n{ACTIVITY_SPEC}\n\n"
        'Return JSON: {"title":"lesson title","blocks":[{"kind":"text|analogy|code|tip","text":"...","caption":"optional"}],'
        '"encouragement":"one short sentence","sources":[snippet indices you relied on],"activity":{...}}\n'
        "Use 2-6 blocks. Put code only in blocks with kind \"code\" (no markdown fences)."
    )

    def check(d):
        blocks = d["blocks"]
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= 8:
            raise ValueError("blocks must be a list of 1-8 items")
        clean = []
        for b in blocks:
            kind = b.get("kind")
            if kind not in BLOCK_KINDS:
                raise ValueError(f"unknown block kind {kind!r}")
            clean.append({"kind": kind, "text": _s(b.get("text"), "block text"), "caption": str(b.get("caption") or "")})
        src = [i for i in (d.get("sources") or []) if isinstance(i, int) and 0 <= i < min(len(resources), 5)]
        return {
            "title": _s(d.get("title"), "title"),
            "blocks": clean,
            "encouragement": str(d.get("encouragement") or "").strip(),
            "sources": src,
            "activity": normalize_activity(d.get("activity"), allowed),
        }

    return _ask(user, check, 3500)


def build_assessment(topic, steps, mastery) -> list[dict]:
    weak = [m["title"] for m in mastery if m["attempts"] and m["correct"] / m["attempts"] < 0.6]
    user = (
        f"Topic: {topic}\nLearning steps: " + "; ".join(f"{i}: {s['title']}" for i, s in enumerate(steps)) + "\n"
        + (f"The learner struggled with: {', '.join(weak)}. Weight questions toward these.\n" if weak else "")
        + "Create exactly 5 final-assessment questions covering the steps, mixing activity types "
        "(use at least 3 different types). Each question needs a 0-based \"step_index\".\n\n"
        + ACTIVITY_SPEC
        + '\n\nReturn JSON: {"questions":[{"step_index":0,"type":"...", ...activity fields...}]}'
    )

    def check(d):
        qs = d["questions"]
        if not isinstance(qs, list) or not 3 <= len(qs) <= 7:
            raise ValueError("questions must be a list of 3-7 items")
        out = []
        for q in qs:
            a = normalize_activity(q)
            a["step_index"] = max(0, min(len(steps) - 1, int(q.get("step_index", 0) or 0)))
            out.append(a)
        return out

    return _ask(user, check, 4000)
