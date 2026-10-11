"""
EMORA agent core: learner-state estimation, teaching strategy per state,
search planning and XP rules. Deliberately rule-based and explainable.
"""
from typing import Literal

State = Literal["confused", "frustrated", "neutral", "engaged"]

# One teaching strategy per learner state. `instructions` go straight into the
# teaching prompt, `allowed` limits which game/quiz types the tutor may use.
STRATEGIES = {
    "confused": {
        "label": "Analogy first, tiny steps",
        "note": "Signals suggest this is tricky, so EMORA slows down: an everyday analogy, a line-by-line worked example, then an easy check.",
        "allowed": ["mcq", "order"],
        "instructions": (
            "- Open with ONE everyday analogy (no code) that captures the core idea.\n"
            "- Split the idea into at most 3 tiny parts of 1-2 sentences each.\n"
            "- Include a worked example traced slowly, line by line (use a 'code' block if relevant and narrate each step in text blocks).\n"
            "- Activity must be EASY (mcq or order). Wrong options should be plausible but clearly distinguishable. Give a genuinely helpful hint."
        ),
    },
    "frustrated": {
        "label": "One tiny win",
        "note": "Repeated misses and a tense signal, so EMORA shrinks the goal to one small idea and a very small win.",
        "allowed": ["mcq", "fill_blank"],
        "instructions": (
            "- Start with ONE short, warm sentence saying this idea trips up many learners (never claim to know how they feel).\n"
            "- Shrink scope to a SINGLE idea: at most 4 sentences of explanation plus one tiny example.\n"
            "- If a previous mistake is given, directly fix that specific misunderstanding.\n"
            "- Activity must be a VERY small win: mcq with 2-3 options, or fill_blank with a one-word answer. The hint should nearly give the answer away."
        ),
    },
    "neutral": {
        "label": "Explain, example, practice",
        "note": "Steady progress, so EMORA uses the standard path: clear explanation, concrete example, practice.",
        "allowed": ["mcq", "order", "fill_blank"],
        "instructions": (
            "- Clear explanation in 2-3 short paragraphs, then one concrete example (code block if relevant), then one practical tip.\n"
            "- Activity at standard difficulty (mcq, order or fill_blank)."
        ),
    },
    "engaged": {
        "label": "Challenge mode",
        "note": "You're doing well, so EMORA skips the basics and raises the difficulty with a deeper challenge.",
        "allowed": ["find_bug", "fill_blank", "order"],
        "instructions": (
            "- Skip the basics. State the core idea in 2-3 sentences, then go deeper: an edge case, a common pitfall, or how it shows up in real problems or interviews.\n"
            "- Frame the activity as a CHALLENGE.\n"
            "- Activity should be harder (find_bug, fill_blank or a multi-step order). The hint should be subtle."
        ),
    },
}


def compute_state(
    quiz_accuracy: float,
    avg_response_seconds: float,
    expression_signal: str = "neutral",
    repeated_mistakes: int = 0,
    attempts: int = 0,
) -> State:
    """
    Combine behaviour + the webcam signal into one learner state.
    The expression is ONE signal, never ground truth. Response times include
    reading time, so 'slow' and 'fast' are generous.
    """
    slow = avg_response_seconds > 75
    fast = avg_response_seconds < 40
    negative = expression_signal == "frustrated"

    if repeated_mistakes >= 3 or (repeated_mistakes >= 2 and (slow or negative)):
        return "frustrated"
    if negative and attempts >= 1 and quiz_accuracy < 0.7:
        return "frustrated"
    if repeated_mistakes >= 1 or expression_signal == "confused" or (attempts >= 2 and quiz_accuracy < 0.5):
        return "confused"
    if attempts >= 1 and quiz_accuracy >= 0.8 and (fast or expression_signal == "positive"):
        return "engaged"
    return "neutral"


def plan_query(topic: str, step_title: str, state: State, retry: int = 0) -> str:
    """Decide what to search for, based on the step and the learner's state."""
    base = f"{topic} {step_title}"
    query = {
        "confused": f"{base} explained simply with analogy",
        "frustrated": f"{base} very simple example for beginners",
        "engaged": f"{base} advanced examples common pitfalls",
    }.get(state, f"{base} tutorial with examples")
    return query + (" step by step" if retry >= 2 else "")


def award_xp(kind: str, correct: bool, hint_used: bool, streak_before: int) -> int:
    if not correct:
        return 0
    base = 15 if kind == "assessment" else 10
    if hint_used:
        base //= 2
    return base + min(streak_before, 5) * 2
