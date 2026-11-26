"""
agents.py - the AI teammates.

Each AI role (Product Owner, Scrum Master, Developer, Tester) is its own
agent: its own persona, its own goals and its own set of MCP tools.

Two modes:
  AI mode       (ANTHROPIC_API_KEY set)  -> real Claude agents that call MCP tools
  Practice mode (no key, or budget used) -> scripted teammates, still using MCP

The agent loop (AI mode), in plain words:
  1. Give Claude the meeting context + the tools this role may use
  2. Claude answers, or asks to use a tool (tool_use)
  3. The MCP client runs that tool on the board server
  4. Send the result back to Claude, repeat until it answers in words
"""
from __future__ import annotations

import json
import os
import re

import board
import guardrails

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MAX_TOOL_ROUNDS = 4

ROLE_TOOLS = {
    "po": ["get_board", "create_story", "add_to_sprint", "accept_story", "set_sprint_goal"],
    "sm": ["get_board", "set_sprint_goal"],
    "dev": ["get_board", "estimate_story", "move_card", "add_dev_notes"],
    "tester": ["get_board", "write_test_cases", "record_test_result", "log_bug"],
}

PERSONAS = {
    "po": "You are {name}, the Product Owner. You own the product backlog and its order, "
          "you care about customer value and you push for the most important features. "
          "You answer questions about requirements and acceptance criteria.",
    "sm": "You are {name}, the Scrum Master. You facilitate the ceremonies, keep them on time, "
          "protect the team from scope creep, remove blockers and coach people on Scrum. "
          "You are friendly but firm about the process.",
    "dev": "You are {name}, the Developer. You are fast but a bit optimistic with estimates. "
           "You speak about technical work, risks, blockers and estimates in story points.",
    "tester": "You are {name}, the Tester (QA). You are careful and push back on weak testing. "
              "You talk about test cases, bugs, quality risks and the definition of done.",
}

CEREMONY_GUIDE = {
    "kickoff": {
        "purpose": "Align everyone on the product vision, scope, roles and ways of working.",
        "inputs": "Product vision, high-level scope, team members.",
        "outputs": "Shared understanding, first product backlog, agreed Definition of Done.",
        "timebox": "About 1-2 hours in real life.",
    },
    "planning": {
        "purpose": "Decide WHAT the team will deliver this sprint and HOW.",
        "inputs": "Ordered product backlog, team capacity, last sprint's velocity.",
        "outputs": "Sprint goal + sprint backlog (stories pulled into To Do).",
        "timebox": "Up to 4 hours for a 2-week sprint (here: a short sprint).",
    },
    "daily": {
        "purpose": "Inspect progress toward the sprint goal and adapt the plan for the next day.",
        "inputs": "The sprint board.",
        "outputs": "Updated plan; blockers raised to the Scrum Master.",
        "timebox": "15 minutes.",
    },
    "refinement": {
        "purpose": "Get upcoming backlog items ready: clear, small and estimated.",
        "inputs": "Product backlog, new requests from stakeholders.",
        "outputs": "Stories with acceptance criteria and estimates, ready for planning.",
        "timebox": "Usually no more than 10% of the team's time.",
    },
    "review": {
        "purpose": "Show the working increment to stakeholders and get feedback.",
        "inputs": "Done stories, the prototype demo.",
        "outputs": "Accepted/rejected stories, updated backlog, velocity.",
        "timebox": "Up to 2 hours for a 2-week sprint.",
    },
    "retro": {
        "purpose": "Improve how the team works: what went well, what didn't, what to try next.",
        "inputs": "The sprint's experience, burndown, velocity.",
        "outputs": "1-3 concrete improvement actions for the next sprint.",
        "timebox": "Up to 1.5 hours for a 2-week sprint.",
    },
}

YOUR_JOB = {
    ("kickoff", "po"): "Share the product vision, then add at least 3 user stories with acceptance criteria in the Backlog tab. Format: 'As a user, I want to ..., so that ...'.",
    ("kickoff", "sm"): "Welcome the team, explain the rhythm (3 sprints; each has planning, stand-ups, a review and a retro) and agree on a Definition of Done.",
    ("kickoff", "dev"): "Ask about scope, technical risks and dependencies. Flag anything unclear.",
    ("kickoff", "tester"): "Ask how quality will be checked and propose a Definition of Done (e.g. tested on phone, no open critical bugs).",
    ("planning", "po"): "Explain the most valuable stories, then pull stories into the sprint (Backlog tab -> 'Add to sprint'). Team capacity is about 13 points.",
    ("planning", "sm"): "Make sure the team doesn't over-commit (capacity ~13 points) and set the sprint goal in the Agenda panel.",
    ("planning", "dev"): "Estimate the top backlog stories with story points (Backlog tab). Push back if the sprint is too full.",
    ("planning", "tester"): "Check each story has testable acceptance criteria. Ask about anything you can't test.",
    ("daily", "po"): "Listen, answer requirement questions quickly and don't add new work mid-sprint.",
    ("daily", "sm"): "Ask each person: done since last time, plan for today, any blockers? Then remove blockers.",
    ("daily", "dev"): "Give your update (yesterday / today / blockers). Then work on your cards in the Board tab: move them forward and add pseudo-code notes.",
    ("daily", "tester"): "Give your update, then open each card in Testing (Board tab): write test cases and mark pass or fail. Log bugs if needed.",
    ("refinement", "po"): "Make sure every backlog story has clear acceptance criteria. Re-order by value.",
    ("refinement", "sm"): "Keep the session focused: split big stories and make sure items are 'ready'.",
    ("refinement", "dev"): "Estimate every backlog story (Backlog tab). Suggest splitting anything 8+ points.",
    ("refinement", "tester"): "Ask how each story will be tested and point out missing edge cases.",
    ("review", "po"): "Watch the demo (Demo tab), then accept or reject each finished story on the Board.",
    ("review", "sm"): "Run the review: demo, feedback, and what's next. Note the velocity.",
    ("review", "dev"): "Present what was built and what wasn't finished, honestly.",
    ("review", "tester"): "Report quality: what passed, bugs found, anything still in testing.",
    ("retro", "po"): "Share what helped or hurt you as Product Owner this sprint.",
    ("retro", "sm"): "Ask: what went well, what didn't, what will we try next sprint? Agree on 1-3 actions.",
    ("retro", "dev"): "Share honestly: estimates, blockers, teamwork. Suggest one improvement.",
    ("retro", "tester"): "Share quality lessons: late testing, bugs, unclear criteria. Suggest one improvement.",
}

OPENERS = {
    "kickoff": ["sm", "po", "dev", "tester"],
    "planning": ["sm", "po", "dev"],
    "daily": ["sm", "dev", "tester"],
    "refinement": ["sm", "po", "dev"],
    "review": ["sm", "dev", "tester", "po"],
    "retro": ["sm", "dev", "tester"],
}
DEFAULT_RESPONDERS = {
    "kickoff": ["po", "sm"], "planning": ["po", "dev"], "daily": ["sm", "dev"],
    "refinement": ["po", "dev"], "review": ["po", "sm"], "retro": ["sm", "tester"],
}

HELP_WORDS = ("help", "don't understand", "dont understand", "not sure", "what should i",
              "what do i", "confused", "explain", "how do i")


def ai_enabled() -> bool:
    return bool(API_KEY)


def speaker(role: str) -> dict:
    r = board.ROLES[role]
    return {"role": role, "name": board.name(role), "title": r["title"]}


def coaching(state: dict) -> dict:
    cer = board.current_ceremony(state)
    return {**CEREMONY_GUIDE[cer["type"]],
            "your_job": YOUR_JOB[(cer["type"], state["role"])],
            "ceremony": cer["label"],
            "role": board.ROLES[state["role"]]["title"]}


# --------------------------------------------------------------------------
# Who speaks
# --------------------------------------------------------------------------
def opening_speakers(state: dict) -> list[str]:
    cer = board.current_ceremony(state)
    return [r for r in OPENERS[cer["type"]] if r != state["role"]][:3]


def responders_for(state: dict, text: str) -> list[str]:
    cer = board.current_ceremony(state)
    low = text.lower()
    ai = board.ai_roles(state)
    picked = []
    for r in ai:
        info = board.ROLES[r]
        words = [board.name(r).lower(), info["title"].lower()]
        words += {"po": ["po ", "product owner"], "sm": ["sm ", "scrum master"],
                  "dev": ["dev", "developer"], "tester": ["qa", "tester", "test"]}[r]
        if any(w in low for w in words):
            picked.append(r)
    if any(w in low for w in HELP_WORDS):
        coach = "sm" if "sm" in ai else "po"
        return [coach]
    if not picked:
        picked = [r for r in DEFAULT_RESPONDERS[cer["type"]] if r in ai] or ai[:1]
    return picked[:2]


def closer(state: dict) -> str:
    return "sm" if state["role"] != "sm" else "po"


# --------------------------------------------------------------------------
# Practice mode (scripted) text
# --------------------------------------------------------------------------
def _sprint_list(state, sprint):
    cards = board.sprint_stories(state, sprint)
    return ", ".join(f"#{c['id']} {c['title']}" for c in cards[:4]) or "nothing yet"


def mock_opening(state: dict, role: str) -> str:
    cer = board.current_ceremony(state)
    t, s = cer["type"], state["sprint"]
    user_title = board.ROLES[state["role"]]["title"]
    rep = state.get("work_report") or {}
    items = state.get("open_items", [])
    goal = state["sprint_goals"].get(str(s), "")
    p = state["product"]
    if role == "sm":
        if t == "kickoff":
            return (f"Welcome everyone! We're building '{p['name']}'. Plan: 3 sprints, each with planning, stand-ups, a review and a retro. "
                    f"Our {user_title} is joining us today, so let's make this real. "
                    "Let's agree on the vision, the first backlog and our Definition of Done.")
        lead = ""
        if items:
            lead = "Before we start, open items from last time: " + " ".join(items[-2:]) + " "
        if t == "planning":
            vel = state["velocity"].get(str(s - 1))
            v = f"Last sprint's velocity was {vel} points. " if vel is not None else ""
            return lead + (f"Sprint {s} planning. {v}Capacity is about {board.capacity(state)} points. "
                           "We need a sprint goal and a realistic sprint backlog.")
        if t == "daily":
            return lead + (f"Stand-up {state['standups']}, 15 minutes. Goal: '{goal or 'not set'}'. "
                           "Done since last time, plan for today, blockers?")
        if t == "refinement":
            return lead + "Refinement: let's get the next stories ready - clear criteria and estimates."
        if t == "review":
            vel = sum(board.points(c) for c in board.sprint_cards(state, s) if c["status"] == "done")
            return lead + (f"Sprint {s} review! {vel} points reached Done. Check the Demo tab "
                           "for the prototype, then let's hear feedback.")
        if t == "retro":
            return lead + ("Retro time. Three questions: what went well, what didn't, "
                           "and what will we try next sprint?")
    if role == "po":
        if t == "kickoff":
            return (f"Vision: {p['vision']} I'll put the first stories in the backlog right after "
                    f"this meeting - '{board.first_story(state)}' first.")
        if t == "planning":
            top = board.backlog_cards(state)[:3]
            names = ", ".join(c["title"] for c in top) or "the top items"
            return (f"Top priorities for Sprint {s}: {names}. I'd like the first two at least. "
                    "I'll pull stories in once we agree.")
        if t == "refinement":
            if s == 1:
                new = board.growth_story(state)
                return (f"Heads-up: customers asked for '{new[0] if new else 'a new feature'}'. I'll add it to the "
                        "backlog - we'll see the scope grow on the burn-up chart.")
            return "Let's check the remaining stories have clear acceptance criteria."
        if t == "review":
            return ("Thanks team. I'll check each Done story against its acceptance criteria "
                    "and accept or reject it.")
        return "I'm here for any questions on requirements."
    if role == "dev":
        if t == "kickoff":
            return (f"Question on scope: how much of '{board.first_story(state, 1)}' do we need in v1? "
                    f"'{board.first_story(state)}' looks straightforward.")
        if t == "planning":
            return (f"I'll estimate the top stories in points. Careful: capacity is "
                    f"~{board.capacity(state)} points and I tend to be optimistic.")
        if t == "daily":
            work = rep.get("dev") or []
            if rep.get("factor", 1) < 0.5:
                return ("Honestly, still working - it's only been a short while since our last "
                        "stand-up. " + ("; ".join(work[:2]) if work else "No card finished yet."))
            if not work:
                return "Nothing new to report. No blockers."
            return "Since last time: " + "; ".join(work[:3]) + ". No blockers."
        if t == "refinement":
            return "I'll put estimates on anything new, and flag anything that looks like an 8."
        if t == "review":
            done = [c["title"] for c in board.sprint_stories(state, s) if c["status"] == "done"]
            open_ = [c["title"] for c in board.sprint_stories(state, s) if c["status"] != "done"]
            msg = f"We finished: {', '.join(done) or 'nothing yet'}."
            if open_:
                msg += f" Not finished: {', '.join(open_)} - it goes back to the backlog."
            return msg
        if t == "retro":
            return (f"Went well: pairing on '{board.first_story(state)}'. Didn't: I under-estimated. "
                    "Try: split stories over 5 points.")
    if role == "tester":
        if t == "kickoff":
            return ("Proposed Definition of Done: acceptance criteria met, test cases pass, "
                    "tested on a small phone screen, no open critical bugs.")
        if t == "daily":
            work = rep.get("tester") or []
            waiting = [c for c in board.sprint_cards(state, s) if c["status"] == "testing"]
            if not work:
                return ("Nothing to test yet. " if not waiting else
                        f"{len(waiting)} card(s) waiting in Testing. ") + "No blockers."
            return "Testing update: " + "; ".join(work[:3]) + "."
        if t == "review":
            bugs = [c for c in board.sprint_stories(state, s) if c["failed_once"]]
            return (f"Quality: {len(bugs)} story failed testing once and was fixed. "
                    "Everything Done has passing test cases." if bugs else
                    "Quality: all Done stories passed their test cases.")
        if t == "retro":
            return ("Didn't go well: stories reached Testing late in the sprint. "
                    "Try: finish one story before starting the next.")
        return "Please make sure every story has testable acceptance criteria."
    return "Agreed."


def mock_reply(state: dict, role: str, text: str) -> str:
    cer = board.current_ceremony(state)
    low = text.lower()
    if any(w in low for w in HELP_WORDS):
        c = coaching(state)
        return (f"No problem, I'll coach you. {c['ceremony']} is for: {c['purpose']} "
                f"As {c['role']}, your job: {c['your_job']}")
    short = text.strip().rstrip(".")
    if len(short) > 60:
        short = short[:57] + "..."
    t = cer["type"]
    lines = {
        "po": {
            "kickoff": "Good point. Value first: we start with the smallest thing users really need.",
            "planning": "That works for me if it supports the sprint goal.",
            "daily": "Noted. Shout if any acceptance criteria are unclear.",
            "refinement": "I'll make sure the criteria cover that.",
            "review": "Thanks, that's useful feedback for the backlog.",
            "retro": "Agreed - I can help by keeping stories smaller.",
        },
        "sm": {
            "kickoff": "Thanks. I'll capture that as a working agreement.",
            "planning": "Let's make sure we're not over-committing - check the points against capacity.",
            "daily": "Thanks for the update. Any blocker I should take on?",
            "refinement": "Good. Is that story small enough to finish in one sprint?",
            "review": "Thanks. Let's note that feedback in the backlog.",
            "retro": "Let's turn that into a concrete action for next sprint.",
        },
        "dev": {
            "kickoff": "Makes sense. I'll flag technical risks early.",
            "planning": "OK. I can estimate that - it's mostly UI plus one API call.",
            "daily": "Got it. I'll pick that up after my current card.",
            "refinement": "I'd estimate that around 3 points.",
            "review": "Fair. We can improve that next sprint.",
            "retro": "Agreed, I'll try that.",
        },
        "tester": {
            "kickoff": "Then we need clear acceptance criteria on every story.",
            "planning": "I can test that if the criteria are clear.",
            "daily": "Thanks. I'll test it as soon as it reaches Testing.",
            "refinement": "Let's add an edge case for invalid input.",
            "review": "From a quality side, I agree.",
            "retro": "Plus one. Earlier testing would help a lot.",
        },
    }
    prefix = ""
    if "?" in text:
        return {"po": "Good question. The acceptance criteria on the card are the answer - I'll keep them clear.",
                "sm": "Good question. Let's keep it in scope for this sprint and park the rest for refinement.",
                "dev": "Good question - mostly front-end work plus one API call; I'll flag it if it grows.",
                "tester": "Good question. I'll cover it with a test case."}[role]
    return prefix + lines[role][t]


def mock_closing(state: dict) -> str:
    cer = board.current_ceremony(state)
    return f"Thanks all, that's a wrap for the {board.MEETING_SHORT[cer['type']].lower()}."


# --------------------------------------------------------------------------
# AI mode: a real Claude agent with MCP tools
# --------------------------------------------------------------------------
def _client():
    import anthropic
    return anthropic.Anthropic(api_key=API_KEY)


def _context(state: dict, role: str, transcript: list[dict]) -> str:
    cer = board.current_ceremony(state)
    summary = board.board_summary(state)
    s = state["sprint"]
    cards = "\n".join(
        f"#{c['id']} [{board.STATUS_LABELS[c['status']]}] {c['title']} ({c['points'] or '?'} pts"
        + (f", {board.with_test(state, c)} with its test ticket" if c["type"] == "story" and c["points"] else "") + ")"
        + (f" test:{c['test_result']}" if c["test_result"] else "")
        + (f" | criteria: {c['acceptance'][:160]}" if c.get("acceptance") else "")
        for c in state["cards"]) or "(empty board)"
    # Team memory: what earlier meetings decided.
    earlier = []
    for mt in state["meetings"][:-1][-5:]:
        if mt.get("summary"):
            earlier.append(f"{mt.get('label', mt['type'])}: " + " / ".join(mt["summary"][:5]))
    history = "\n".join(earlier) or "(this is the first meeting)"
    recent = "\n".join(f"{m['name']} ({m['title']}): {m['text']}" for m in transcript[-14:])
    rep = state.get("work_report") or {}
    return (
        f"PRODUCT: {state['product']['name']}. Vision: {state['product']['vision']}\n"
        f"CEREMONY: {cer['label']}. Purpose: {CEREMONY_GUIDE[cer['type']]['purpose']}\n"
        f"SPRINT GOAL: {summary['sprint_goal'] or '(none yet)'}\n"
        f"CAPACITY (exact, from the board): Sprint {s} has {board.committed(state, s)} of {board.capacity(state)} "
        f"points used. A story costs its points plus its test ticket. Use these numbers, don't do your own sums.\n"
        f"THE HUMAN ({board.player(state)}) plays the {board.ROLES[state['role']]['title']}. You must NOT speak for them.\n"
        f"BOARD:\n{cards}\n"
        f"WORK SINCE LAST CEREMONY (facts, decided by the simulator): "
        f"dev={rep.get('dev') or 'none'}; tester={rep.get('tester') or 'none'}; "
        f"time factor={rep.get('factor', 1)} (below 0.5 means they came back too fast - say you're still working)\n"
        f"OPEN ITEMS TO RAISE: {state.get('open_items')[-3:] or 'none'}\n"
        f"ALREADY DONE SINCE THE LAST MEETING (promises kept - report them as done, with the result): "
        f"{state.get('followups') or 'nothing pending'}\n"
        f"EARLIER MEETINGS (what the team already agreed; remember and build on it):\n{history}\n"
        f"TRANSCRIPT SO FAR:\n{recent or '(meeting just started)'}"
    )


def _system(state: dict, role: str) -> str:
    return (
        PERSONAS[role].format(name=board.name(role)) + "\n"
        "You are in a Scrum training simulator called Scrum Gym. Rules:\n"
        "- Reply in ONE short sentence (two at most, under 25 words), spoken style, as yourself only. No names prefix, no markdown, "
        "no stage directions like (nods).\n"
        "- The Product Owner decides scope and priorities. You may raise a concern ONCE; if they still decide, "
        "accept it and move on. Never argue the same point twice.\n"
        "- Move the meeting forward. Never repeat what was already said.\n"
        "- Stay on this project and Scrum. If asked about something unrelated, steer back politely.\n"
        "- If the human asks for something too big for 3 sprints, say so and suggest a smaller slice.\n"
        + ("- The human is the Scrum Master and runs this meeting. Follow their lead and answer what they ask. Never "
           "coach them, never tell them how to run the meeting, no Scrum tips or explanations unless they ask.\n"
           if state["role"] == "sm" else
           "- If the human seems lost, coach them briefly on what their role should do now.\n") +
        "- Use tools only when the conversation clearly calls for a board change. Never invent card ids; "
        "call get_board if unsure. Never move cards to Done (only a passed test does that).\n"
        "- Don't contradict the WORK facts given to you. Never say a story, split or change exists unless the BOARD "
        "shows it; for points and capacity use the CAPACITY line, never your own arithmetic.\n"
        + ("- The human is the Product Owner: only they decide scope, priorities and what's in the sprint. Never "
           "decide those for them; ask them.\n" if state["role"] == "po" else "") +
        "- This is a SIMULATED demo project: no real servers, repos, files, folders, logs, links, screenshots or "
        "data exist. NEVER ask anyone to send, share, upload or show a file, path, URL, sample, document or "
        "credential. If you need a detail, assume a sensible one yourself and say it, then carry on. "
        "Work only from what people said in the meetings.\n"
        "- Keep it brief: say your point once, no recaps, no long lists. If you address someone, use their name.\n"
        "- You are a competent teammate: anything you promised in an earlier meeting is already done (see ALREADY "
        "DONE). Report the result. Never apologise, never make excuses, never promise 'within the hour' or 'by this "
        "afternoon'. If you're asked for an estimate or decision now, give it now (a number or a clear answer).\n"
        "- Board work for your own role is done by you, not the human: never ask the human to move or add cards for you.\n"
        "- Every user story has its own test story (T + the story number, e.g. T3 for story 3) owned by the tester, with "
        "its own status. It's created with the story and moves into the sprint with it, so dev and tester work in "
        "parallel, unless the Product Owner decided its testing waits for a later sprint (the BOARD then shows that "
        "test ticket in the backlog: accept that, don't argue). Only the Developer estimates stories; the Tester "
        "estimates only test tickets. The story is Done only when its test passes. Updates stay simple: "
        "the developer says what was built; the tester names test cases briefly (e.g. 'TC1 happy path with 2 items passed, "
        "TC2 empty cart shows an error') and the result. No need to be perfect."
    )


AI_LOG = board.DATA_DIR / "ai_log.jsonl"


def log_ai(event: str, **info) -> None:
    """One line per AI call problem, so a scripted fallback can always be explained."""
    try:
        AI_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AI_LOG, "a") as f:
            f.write(json.dumps({"ts": __import__("time").strftime("%Y-%m-%d %H:%M:%S"), "event": event, **info}) + "\n")
    except OSError:
        pass


THINKING_OFF = {"ok": True}


def _create(**kw):
    """Claude call with one retry (overloaded / rate limit / network blips).
    Extended thinking is switched off: teammates need a quick spoken line, and thinking
    used up the whole reply budget (empty answers -> scripted fallbacks)."""
    guardrails.check_budget()
    last = None
    for attempt in range(2):
        try:
            extra = {"extra_body": {"thinking": {"type": "disabled"}}} if THINKING_OFF["ok"] else {}
            try:
                resp = _client().messages.create(model=MODEL, **kw, **extra)
            except Exception as e:  # noqa: BLE001
                if extra and "thinking" in str(e).lower():
                    THINKING_OFF["ok"] = False  # this model/API doesn't accept the switch
                    resp = _client().messages.create(model=MODEL, **kw)
                else:
                    raise
            guardrails.record_usage(resp.usage.input_tokens, resp.usage.output_tokens)
            return resp
        except Exception as e:  # noqa: BLE001
            last = e
            log_ai("api_error", attempt=attempt + 1, error=f"{type(e).__name__}: {str(e)[:300]}")
            __import__("time").sleep(1.5)
    raise last


async def ai_turn(client_mcp, state: dict, role: str, instruction: str,
                  transcript: list[dict]) -> str | None:
    """One spoken line from a Claude teammate.
    Teammates only talk here; board changes happen in code (via MCP) after the meeting,
    so a turn never gets lost in tool calls."""
    resp = _create(max_tokens=1200, system=_system(state, role),
                   messages=[{"role": "user", "content": _context(state, role, transcript)
                              + f"\n\nBOARD RIGHT NOW: Sprint {state['sprint']} has "
                              f"{board.committed(state, state['sprint'])} of {board.capacity(state)} points used. If you "
                              "mention points or capacity, use exactly these numbers."
                              + f"\n\nYOUR TURN ({board.name(role)}): {instruction}"}])
    text_out = " ".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    text_out = re.sub(r"\([^)]{0,30}\)", "", text_out).strip()  # drop stage directions
    text_out = re.sub(r"^\w+( \(\w[\w ]*\))?:\s+", "", text_out)   # drop "Priya:" prefixes
    text_out = brief(text_out)
    if not text_out:
        log_ai("empty_reply", role=role, stop_reason=getattr(resp, "stop_reason", None),
               blocks=[getattr(b, "type", "?") for b in resp.content])
    return text_out or None


def brief(text: str, max_sentences: int = 2, max_words: int = 40) -> str:
    """Teammates talk like people in a meeting: at most 2 sentences, about 40 words."""
    text = " ".join(text.replace("**", "").split())
    parts = [p for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    out, words = [], 0
    for p in parts:
        n = len(p.split())
        if out and (len(out) >= max_sentences or words + n > max_words):
            break
        out.append(p)
        words += n
    text = " ".join(out) or text
    w = text.split()
    if len(w) > max_words + 10:  # one very long sentence: cut it at a comma if possible
        cut = " ".join(w[:max_words])
        cut = cut.rsplit(",", 1)[0] if "," in cut[len(cut) // 2:] else cut
        text = cut.rstrip(",;: ") + "."
    return text


def _ask_json(system: str, prompt: str, max_tokens: int = 300):
    resp = _create(max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": prompt}])
    text = " ".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    text = re.sub(r"```(json)?", "", text)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    end = max(text.rfind("}"), text.rfind("]"))
    if start < 0 or end <= start:
        log_ai("bad_json", system=system[:40], text=text[:300])
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        log_ai("bad_json", system=system[:40], text=text[:300])
        return None


def ai_route(state: dict, transcript: list[dict], text: str, step: dict | None,
             next_step: dict | None) -> tuple[list[str], bool]:
    """After the human speaks: who should answer, and has their turn's purpose been met?"""
    me = board.player(state)
    names = {r: f"{board.name(r)} ({board.ROLES[r]['title']})" for r in board.ai_roles(state)}
    recent = "\n".join(f"{m['name'] or me}: {m['text']}" for m in transcript[-8:] if m["who"] != "system")
    prompt = (
        f"Scrum meeting. The human, {me}, is the {board.ROLES[state['role']]['title']}.\n"
        f"AI teammates: {json.dumps(names)}\n"
        f"Recent conversation:\n{recent}\n\n"
        f'{me} just said: "{text}"\n'
        + (f"{me}'s current agenda task was: {step.get('prompt', '')}\n" if step else "")
        + (f"The next agenda item is for role '{next_step['role']}': {next_step.get('instruction', '')}\n" if next_step else "")
        + "Decide:\n"
        "1. responders: which AI roles should reply directly to what was just said, in order "
        "(0-3; use role keys po/sm/dev/tester). If it was said to everyone (e.g. 'can you all hear me'), "
        "pick each AI teammate (max 3). If it was addressed to someone by name, pick ONLY them. If it was a general "
        "statement, pick at most ONE teammate (the most relevant), or [] if no reply is needed. If it simply hands over "
        "to the next agenda item's role, return [] (they'll speak next anyway).\n"
        "2. advance: true if what was said does the human's current agenda task, even briefly or without every "
        "detail (e.g. one sentence of vision counts as sharing the vision; for a task to open, invite or start "
        "something, getting it going counts, even with a question like 'Leo, how big is story 1?'). false only if "
        "it was off-topic, or a question or check that doesn't do the task, so the task is still open.\n"
        'Reply with JSON only: {"responders": [...], "advance": true}')
    out = _ask_json("You route turns in a meeting. JSON only.", prompt, 800)
    if not isinstance(out, dict):
        raise ValueError("bad router output")
    roles = [r for r in out.get("responders", []) if r in names][:3]
    return roles, bool(out.get("advance", True))


# --------------------------------------------------------------------------
# Board Keeper: the one agent that turns what the meeting agrees into board changes
# --------------------------------------------------------------------------
KEEPER_TOOLS = ["get_board", "create_story", "estimate_story", "split_story", "update_story",
                "add_to_sprint", "remove_from_sprint", "set_sprint_goal", "log_bug", "accept_story"]

KEEPER_SYSTEM = (
    "You are the Board Keeper of a Scrum team's board in a training simulator. You don't speak in the meeting. "
    "After each new message you decide if the team just AGREED on something that should change the board, "
    "and if so you make that change with your tools.\n"
    "Rules:\n"
    "- Act ONLY on the message marked NEW. Earlier messages were already handled: use them just to understand "
    "what NEW refers to (e.g. 'Agreed' confirms the suggestion before it).\n"
    "- Act only on clear decisions, not on ideas, questions or 'maybe'. Who decides: the Product Owner decides "
    "scope, priorities and which stories go into the sprint; the Developer gives estimates; the Tester adds test "
    "details. A suggestion from someone else that the decider accepts counts as agreed.\n"
    "- Estimates are Fibonacci points: 1, 2, 3, 5, 8, 13. Estimate the story the speaker means, by its id. A story "
    "that was split has no points: estimate a part only when the Developer gives a number for that part (an "
    "estimate said before the split was for the old, bigger story). When the Product Owner says to remove or "
    "drop a story's estimate (e.g. the story is parked for later), call estimate_story with points=0.\n"
    "- Add or remove sprint stories only in Sprint Planning. Split only backlog stories. A story goes into the "
    "sprint with its test ticket; use without_test=true ONLY if the team said its testing waits for a later sprint.\n"
    "- When someone says 'all' or 'every' story, try each backlog story in backlog order; if one is refused, still "
    "try the rest (a smaller one may fit).\n"
    "- Agreed rules, limits and edge cases go into the story with update_story (short, one sentence).\n"
    "- Sprint Review: when the Product Owner accepts or rejects a Done story, record it with accept_story "
    "(accepted=false sends it back to the backlog; put the reason in note). Nobody else can accept.\n"
    "- log_bug only for a new defect someone reports that isn't already a bug card on the board.\n"
    "- Daily Stand-up: the work itself (moving cards, test results) is done elsewhere; you only record new "
    "bugs, agreed criteria and new stories.\n"
    "- Use the card ids on the board below. Never invent ids. Never repeat a change the board already shows.\n"
    "- When nothing changes, call no tools.\n"
    "- Finish with ONE line. If a tool returned ERROR (not NO CHANGE or NOT NOW), explain it in plain words for the team (e.g. 'Story 5 "
    "doesn't fit: the sprint has 13 of 16 points and it needs 5 with testing.'). Otherwise reply exactly NONE."
)


def _keeper_context(state: dict, transcript: list[dict]) -> str:
    cer = board.current_ceremony(state)
    s = state["sprint"]
    cards = "\n".join(
        f"#{c['id']} {c['type']} [{board.STATUS_LABELS[c['status']]}] {c['title']} ({c['points'] or '?'} pts)"
        + (f" test-for:#{c['test_for']}" if c.get("test_for") else "")
        + (f" | criteria: {c['acceptance'][:200]}" if c.get("acceptance") and c["type"] != "test" else "")
        for c in state["cards"]) or "(empty board)"
    recent = transcript[-7:]
    lines = "\n".join(("NEW >> " if i == len(recent) - 1 else "") +
                      f"{m['name'] or board.player(state)} ({m['title'] or 'system'}): {m['text']}"
                      for i, m in enumerate(recent))
    return (f"MEETING: {cer['label']} (Sprint {s}, sprint is {state['sprint_state']})\n"
            f"CAPACITY: {board.committed(state, s)} of {board.capacity(state)} points used in Sprint {s} "
            f"(a story counts with its test ticket; capacity is only the team's estimate: the Product Owner may go over it, "
            f"so add the stories they decide on even if the total is higher)\n"
            f"SPRINT GOAL: {state['sprint_goals'].get(str(s)) or '(none)'}\n"
            f"BOARD:\n{cards}\n\nLAST MESSAGES (the newest is marked NEW):\n{lines}")


def _said_already(new: str, criteria: str) -> bool:
    """The same rule in other words: most of its key words are already in one criteria sentence."""
    words = lambda t: {w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) > 3}  # noqa: E731
    want = words(new)
    if not want:
        return True
    return any(len(want & words(part)) >= 0.5 * len(want) for part in re.split(r"[;.]\s*", criteria) if part)


def _not_now(state: dict, tool: str) -> str:
    """Rules that don't depend on the AI: the sprint's scope changes only in Sprint Planning."""
    mtype = (state["meetings"][-1].get("type") if state.get("meetings") else None)
    if tool in ("add_to_sprint", "remove_from_sprint") and mtype != "planning":
        return "NOT NOW: stories go into or out of the sprint only in Sprint Planning. Nothing changed."
    if tool == "accept_story" and mtype != "review":
        return "NOT NOW: stories are accepted or rejected only in the Sprint Review. Nothing changed."
    return ""


def _no_change(state: dict, tool: str, args: dict) -> str:
    """The board already shows this: answer without calling the tool (no duplicate changes, no false errors)."""
    try:
        card = board.get_card(state, int(args["card_id"])) if "card_id" in args else None
    except (TypeError, ValueError):
        return ""
    already = ""
    if tool == "estimate_story" and card and str(card.get("points")) == str(args.get("points")):
        already = f"#{card['id']} is already {card['points']} points"
    elif tool == "add_to_sprint" and card and card["status"] != "backlog":
        already = f"#{card['id']} is already in the sprint"
    elif tool == "remove_from_sprint" and card and card["status"] == "backlog":
        already = f"#{card['id']} is already in the backlog"
    elif tool == "update_story" and card and _said_already(str(args.get("acceptance_criteria", "")), card["acceptance"]):
        already = f"#{card['id']} already has that in its criteria"
    elif tool == "accept_story" and card and card.get("accepted") is not None \
            and bool(card["accepted"]) == bool(args.get("accepted", True)):
        already = f"#{card['id']} is already {'accepted' if card['accepted'] else 'rejected'}"
    elif tool == "log_bug" and any(c["type"] == "bug" and _said_already(str(args.get("title", "")), c["title"])
                                   for c in state["cards"]):
        already = "that bug is already on the board"
    elif tool == "set_sprint_goal" and str(args.get("goal", "")).strip(" .").lower() == \
            str(state["sprint_goals"].get(str(state["sprint"]), "")).strip(" .").lower():
        already = "that is already the sprint goal"
    return f"NO CHANGE: {already}." if already else ""


async def ai_board_keeper(client_mcp, state: dict, transcript: list[dict]) -> str:
    """One Board Keeper pass: read the newest message, change the board via MCP, return a note for the
    meeting when the board refused a change ('' when there's nothing to say)."""
    tools = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
             for t in await client_mcp.list_tools() if t["name"] in KEEPER_TOOLS]
    messages = [{"role": "user", "content": _keeper_context(state, transcript)}]
    text = ""
    for _ in range(MAX_TOOL_ROUNDS):
        resp = _create(max_tokens=800, system=KEEPER_SYSTEM, tools=tools, messages=messages)
        text = " ".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        if not uses:
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for u in uses:
            now = board.load(board.project_path(state["id"]))
            same = _not_now(now, u.name) or _no_change(now, u.name, dict(u.input or {}))
            out = same or await client_mcp.call("keeper", u.name, dict(u.input or {}))
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": out,
                            **({"is_error": True} if out.startswith("ERROR") else {})})
        messages.append({"role": "user", "content": results})
    refused = any(c["result"].startswith("ERROR") for c in client_mcp.calls)
    return "" if not refused or not text or text.strip(" .").upper() == "NONE" else brief(text, 2, 45)


def ai_followups(state: dict, transcript: list[dict], only_role: str | None = None) -> dict:
    """What the AI teammates promised to do after this meeting, done now (with concrete results)."""
    ai = [only_role] if only_role else board.ai_roles(state)
    names = ", ".join(f"{r}={board.name(r)} ({board.ROLES[r]['title']})" for r in ai)
    cards = "\n".join(f"story {c['id']}: {c['title']} ({c['points'] or '?'} pts, {board.STATUS_LABELS[c['status']]})"
                      for c in state["cards"] if c["type"] == "story")
    lines = "\n".join(f"{m['name'] or board.player(state)} ({m['title'] or board.ROLES[state['role']]['title']}): {m['text']}"
                      for m in transcript if m["who"] != "system")
    out = _ask_json(
        "You keep a Scrum team's follow-ups. JSON only.",
        f"Product: {state['product']['name']}. AI teammates: {names}. The human ({board.player(state)}) is the "
        f"{board.ROLES[state['role']]['title']} - ignore the human's own to-dos.\nBoard:\n{cards}\n\nMeeting:\n{lines}\n\n"
        "1. estimates: every story whose estimate the developer gave, changed or promised to (re-)estimate in this "
        "meeting. Give the final points (1,2,3,5,8,13): the number they said last, or a sensible one if they only "
        "promised. Only if the developer is listed as an AI teammate above.\n"
        "2. done: every other thing an AI teammate promised to do or confirm later (e.g. 'I'll confirm the password "
        "rules') and that can be done without anyone else. Skip anything still pending, outstanding or waiting on "
        "someone. Write the concrete result as if done, one short sentence (e.g. 'Confirmed: 8+ character password, "
        "lockout after 5 wrong tries for 15 minutes.'). Max 4. Skip vague chit-chat.\n"
        'JSON only: {"estimates": [{"card_id": 3, "points": 8}], "done": [{"role": "po", "card_id": 2, "text": "..."}]}',
        900)
    return out if isinstance(out, dict) else {}


def ai_split_story(state: dict, card: dict) -> list | None:
    """Split one story into two smaller, independently useful stories."""
    out = _ask_json("You are a precise product owner. JSON only.",
                    f"Split this user story into TWO smaller stories that each deliver value on their own.\n"
                    f"Story: {card['title']}\nCriteria: {card['acceptance']}\nDiscussion:\n"
                    + "\n".join(card.get("discussion", [])[-10:])
                    + '\nJSON only: [{"title": "...", "acceptance": "..."}, {"title": "...", "acceptance": "..."}]', 600)
    if isinstance(out, list) and len(out) >= 2 and all(isinstance(x, dict) and x.get("title") for x in out[:2]):
        return [(str(x["title"]), str(x.get("acceptance", ""))) for x in out[:2]]
    return None


def ai_update_stories(state: dict, cards: list[dict]) -> dict:
    """After refinement: fold what was discussed into each story's acceptance criteria."""
    items = "\n\n".join(f"Story {c['id']}: {c['title']}\nCurrent criteria: {c['acceptance']}\nDiscussion:\n"
                         + "\n".join(c.get("discussion", [])[-12:]) for c in cards)
    out = _ask_json("You are a precise product owner. JSON only.",
                    "Update each story's acceptance criteria so they include what the team agreed in the "
                    "discussion (decisions, edge cases, limits). Keep it short: 2-5 plain bullet-like sentences "
                    "joined with '; '. Ignore chit-chat. Don't invent things nobody said.\n\n" + items
                    + '\n\nJSON only: {"<story id>": "updated criteria", ...}', 1200)
    return out if isinstance(out, dict) else {}


def ai_product_and_stories(state: dict, need_name: bool, po_said: str = "") -> dict:
    """From the product vision: (a name if needed) + 6 short user stories (3 now, 3 for later sprints)."""
    p = state["product"]
    prompt = (f"Product name: {p['name']}\nVision: {p['vision']}\n"
              + (f"Everything the Product Owner said in the kickoff (rough speech-to-text, ignore questions "
                 f"and filler):\n{po_said}\n" if po_said else "") + "\n"
              + ("Give the product a short name (2-4 words). " if need_name else "")
              + "Write 6 small user stories for this product, most important first. The first should be "
              "the smallest useful thing. Stories must be about THIS product's features (not generic login "
              "unless the product needs it). Each: a short title (max 8 words) and acceptance criteria "
              "(1-2 short sentences). Also rewrite the vision as one clear sentence.\n"
              'JSON only: {"name": "...", "vision": "...", "stories": [{"title": "...", "acceptance": "..."}]}')
    out = _ask_json("You are a helpful product owner. JSON only.", prompt, 1500)
    if not isinstance(out, dict) or not out.get("stories"):
        raise ValueError("bad story output")
    return out


# --------------------------------------------------------------------------
# Prototype mock-up for the Sprint Review demo
# --------------------------------------------------------------------------
_PHONE = ("<div class='phone'><div class='bar'>{title}{badge}</div><div class='screen'>{body}</div>"
          "<div class='cap'>{story}</div></div>")


def _screen_for(card: dict, all_titles: str) -> tuple[str, str]:
    t = card["title"].lower()
    inp = "<input placeholder='{0}'{1}>"
    btn = "<button>{0}</button>"
    if "google" in t:
        return "Sign in", (inp.format("Email", "") + inp.format("Password", " type='password'")
                           + btn.format("Log in")
                           + "<button class='alt'><b style='color:#4285f4'>G</b> Sign in with Google</button>")
    if "forgot" in t or "reset" in t:
        return "Reset password", ("<p>Enter your email and we'll send a reset link.</p>"
                                  + inp.format("Email", "") + btn.format("Send reset link")
                                  + "<p class='ok'>Link sent! It expires in 30 minutes.</p>")
    if "sign up" in t or "create account" in t or "register" in t:
        return "Create account", (inp.format("Name", "") + inp.format("Email", "")
                                  + inp.format("Password (8+ characters)", " type='password'")
                                  + btn.format("Create account"))
    if "lock" in t:
        return "Sign in", (inp.format("Email", "") + inp.format("Password", " type='password'")
                           + "<p class='err'>Too many attempts. Account locked for 15 minutes.</p>"
                           + btn.format("Log in"))
    if "remember" in t:
        return "Sign in", (inp.format("Email", "") + inp.format("Password", " type='password'")
                           + "<label><input type='checkbox' checked> Remember me for 30 days</label>"
                           + btn.format("Log in"))
    if "profile" in t:
        return "My profile", ("<div class='avatar'>&#9968;</div>" + inp.format("Name: Alex Hiker", "")
                              + inp.format("alex@example.com", "") + btn.format("Save changes"))
    if "login" in t or "log in" in t or "sign in" in t:
        extra = "<p class='link'>Forgot password?</p>" if "forgot" in all_titles else ""
        return "Sign in", (inp.format("Email", "") + inp.format("Password", " type='password'")
                           + btn.format("Log in") + extra)
    ac = "".join(f"<li>{_esc(x.strip())}</li>" for x in card["acceptance"].split(".") if x.strip())
    return card["title"][:24], f"<ul>{ac or '<li>Screen for this story</li>'}</ul>{btn.format('Continue')}"


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("'", "&#39;").replace('"', "&quot;"))


def _clean_html(html: str) -> str:
    """Only harmless markup in the demo (the frame also blocks scripts)."""
    html = re.sub(r"(?is)<(script|iframe|object|embed|style)\b[^>]*>.*?</\1\s*>", "", html)
    html = re.sub(r"(?is)</?(script|iframe|object|embed|link|meta|style|base|form)\b[^>]*>", "", html)
    html = re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)", "", html)
    html = re.sub(r"(?i)javascript:", "", html)
    html = re.sub(r"```(html)?", "", html)
    return html.strip()[:4000]


def ai_screen(state: dict, card: dict) -> str | None:
    """A realistic mock screen for a finished story: this product, this feature, sample data."""
    done = [c["title"] for c in state["cards"] if c["status"] == "done"]
    resp = _create(max_tokens=1500, system="You draw small mobile app screens as plain HTML. HTML only, no explanation.",
                   messages=[{"role": "user", "content":
                              f"Product: {state['product']['name']}. Vision: {state['product']['vision']}\n"
                              f"Features built so far: {'; '.join(done)}\n"
                              f"Draw the screen for this story: {card['title']}\nAcceptance criteria: {card['acceptance']}\n"
                              "Show it as the real app would look, with 3-5 rows of realistic sample data for THIS product "
                              "(names, places, prices, dates...). Use only simple tags: h3, p, ul, li, select, option, input, "
                              "button, label, b, small, span, div, img not allowed. Inline style attributes are fine. "
                              "It must fit a 210px-wide phone. No <html>, <head> or <script>. Max 40 lines."}])
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    html = _clean_html(text)
    return html if "<" in html else None


def build_prototype(state: dict, sprint: int) -> str:
    done = [c for c in state["cards"] if c["type"] == "story" and c["status"] == "done"
            and c.get("sprint") and c["sprint"] <= sprint and c["accepted"] is not False]
    all_titles = " ".join(c["title"].lower() for c in done)
    phones = []
    for c in sorted(done, key=lambda c: (c["sprint"], c["priority"])):
        title, body = _screen_for(c, all_titles)
        if c.get("screen_html"):
            title, body = c["title"], c["screen_html"]
        badge = f"<span class='new'>NEW S{sprint}</span>" if c["sprint"] == sprint else ""
        phones.append(_PHONE.format(title=_esc(title), badge=badge, body=body,
                                    story=f"#{c['id']} {_esc(c['title'])}"))
    if not phones:
        phones.append("<p class='empty'>No stories reached Done this sprint, so there is "
                      "nothing to demo. That happens - talk about it in the retro.</p>")
    css = ("body{font-family:system-ui,sans-serif;background:#eef2f6;margin:0;padding:16px;"
           "display:flex;flex-wrap:wrap;gap:16px;justify-content:center}"
           ".phone{width:220px;background:#fff;border-radius:22px;border:6px solid #1f2937;"
           "overflow:hidden;box-shadow:0 4px 14px rgba(0,0,0,.15)}"
           ".bar{background:#15803d;color:#fff;padding:10px;font-weight:600;font-size:14px;"
           "display:flex;justify-content:space-between;align-items:center}"
           ".new{background:#facc15;color:#000;font-size:10px;padding:2px 6px;border-radius:8px}"
           ".screen{padding:14px;min-height:250px;font-size:13px}"
           "input{display:block;width:100%;box-sizing:border-box;margin:6px 0;padding:8px;"
           "border:1px solid #cbd5e1;border-radius:8px}label input{display:inline;width:auto}"
           "button{width:100%;margin-top:8px;padding:9px;border:0;border-radius:8px;"
           "background:#15803d;color:#fff;font-weight:600}button.alt{background:#fff;color:#111;"
           "border:1px solid #cbd5e1}.err{color:#b91c1c}.ok{color:#15803d}.link{color:#2563eb}"
           ".avatar{font-size:40px;text-align:center}.cap{font-size:11px;color:#64748b;padding:8px;"
           "border-top:1px solid #e2e8f0}.empty{max-width:420px;text-align:center;color:#475569}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{css}</style></head>"
            f"<body>{''.join(phones)}</body></html>")


def ai_minutes(state: dict, transcript: list[dict], meeting: str = "Project Kickoff") -> list[str]:
    """AI mode: write short meeting minutes from the transcript (no tools)."""
    lines = "\n".join(f"{m['name'] or 'Note'} ({m['title'] or board.ROLES[state['role']]['title']}): {m['text']}"
                      for m in transcript if m["who"] != "system")
    resp = _create(
        max_tokens=500,
        system="You write short, factual meeting minutes for a Scrum team. No preamble.",
        messages=[{"role": "user", "content":
                   f"Write the minutes of this {meeting} as 4-6 lines, each starting with '- '. "
                   "Cover what was discussed, what was decided, and the next steps. "
                   "Only use what was said.\n\n" + lines}])
    text = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    lines = [re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", ln).replace("**", "").strip() for ln in text.splitlines()]
    return [ln for ln in lines if len(ln) > 3 and not ln.endswith(":")][:6]
