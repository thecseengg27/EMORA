# EMORA — Emotion-Aware Learning Research Agent

Prototype for SerpApi India Hackathon 2026 (Track 5: Knowledge & Public Interest — Education).

EMORA **teaches** a topic step by step. It estimates the learner's observable state from a webcam
facial-expression signal plus quiz behaviour, searches the live web with **SerpApi** to keep its
teaching accurate and current, and changes *how* it teaches — analogy-first for confusion, one tiny win
for frustration, standard explanation when steady, challenge mode when doing well.

> The webcam is one signal, not a mind-reader. EMORA never claims to know what a learner feels.

## How it works

1. Learner enters a goal (e.g. "Java Recursion"). EMORA searches SerpApi and builds a 5-step path.
2. For each step the agent: estimates state → plans a state-specific search (SerpApi) → the tutor LLM
   teaches the step in the matching style → the learner plays a game/quiz.
3. Games: Quick Quiz, Sequence Builder, Bug Hunt, Fill the Gap. Graded on the server; XP and streaks.
4. A wrong answer triggers a re-teach from a different angle (and a fresh search). After 3 misses EMORA
   walks through the full solution and moves on.
5. After the last step: a mixed "boss level" assessment, then a results page with mastery per step.

## Structure

```
backend/
  main.py          FastAPI endpoints
  agent.py         learner-state rules, teaching strategy per state, search planning, XP
  teacher.py       curriculum / lesson / assessment prompts, validation, server-side grading
  llm.py           tiny LLM client (Anthropic or Gemini)
  serp_client.py   SerpApi wrapper (cached, never leaks the key in errors)
  db.py            SQLite storage (runs, turns, activities, attempts)
frontend/index.html   single-page UI: webcam (face-api.js), lessons, games, progress
```

## Setup

```bash
cd backend
python -m pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
```

Edit `backend/.env`: add your `SERPAPI_KEY` and ONE LLM key (`ANTHROPIC_API_KEY`, or set
`LLM_PROVIDER=gemini` and `GEMINI_API_KEY`). Then:

```bash
python -m uvicorn main:app --reload --port 8000
```

Check http://localhost:8000/api/health — `serpapi_key_set` and `llm_key_set` should both be `true`.

In a second terminal:

```bash
cd frontend
python -m http.server 5500
```

Open http://localhost:5500 in Chrome/Edge and allow the camera.
If the webcam is unavailable, the manual state buttons still drive the demo.

## Privacy & security

- Webcam frames are analysed in the browser (face-api.js) and never uploaded or stored.
- Only a text label ("confused", …) is sent to the backend.
- The topic text is sent to the LLM provider and to SerpApi.
- API keys live in `backend/.env`, which is git-ignored. Error messages never include the key.
- Quiz answers are stored and graded on the server; they are not sent to the browser before you answer.

## Future work

Knowledge tracing across topics, teacher dashboard, multilingual and voice learning, stronger multimodal
state estimation, evaluation of adaptive vs non-adaptive learning, formal security hardening (auth, rate limits).
