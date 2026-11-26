"""
agendas.py - every meeting runs as a short agenda, one speaker at a time.

The Scrum Master leads. Every meeting ends the same way:
  1. "Any other questions or concerns before we close?"  (open floor)
  2. A short summary, then the meeting closes.

Each step: role (who speaks), kind (what the step is), instruction (for a Claude
teammate), script (practice-mode line) and prompt (what YOU are asked when it's your role).
"""
from __future__ import annotations

import re

import board
import kickoff


def who(state: dict, role: str) -> str:
    """How teammates address someone: by name, or by title if it's the human."""
    return board.player(state) if role == state["role"] else board.name(role)


def _stories(cards: list[dict], n: int = 3) -> str:
    names = [c["title"] for c in cards[:n]]
    return ", ".join(names) if names else "nothing yet"


def _numbered(state: dict, cards: list[dict], n: int = 4) -> str:
    """'story 1 Login (3 pts), story 2 ...' so people can pick stories by number."""
    out = [f"story {c['id']} {c['title']} ({board.points(c) or '?'} pts + test {board.pts_of(state, t) if (t := board.test_ticket(state, c['id'])) else 0})" for c in cards[:n]]
    return "; ".join(out) if out else "nothing yet"


def last_standup(state: dict, before: int | None = None) -> dict:
    """What each person said at the previous stand-up of this sprint: {role: text}."""
    meetings = state["meetings"][:before] if before is not None else state["meetings"]
    prev = next((m for m in reversed(meetings) if m["type"] == "daily" and m["sprint"] == state["sprint"]
                 and m.get("messages")), None)
    if not prev:
        return {}
    out = {}
    for m in prev["messages"]:
        role = state["role"] if m["who"] == "user" else m["who"]
        text = re.sub(r"^(Thanks|Thank you),?\s+\w+[.!]\s*", "", m["text"]).strip()
        if re.fullmatch(r"(?i)\W*(no|nope|none|nothing|all good|no questions|nothing from me)\b[^?]*", text):
            continue
        if role in board.ROLES and m.get("kind") in ("update", "interjection", "reply", "floor_answer") and len(text) > 8:
            out[role] = (out.get(role, "") + " " + text).strip()[-300:]
    return out


def _short(text: str, n: int = 110) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3] + "..."


def proposal(state: dict, transcript: list[dict]) -> tuple[list[dict], int]:
    """The stories proposed so far in this planning (named by the PO), and their total points."""
    in_sprint = board.sprint_stories(state, state["sprint"])
    if in_sprint:  # what's really on the board wins (the Board Keeper puts agreed stories there)
        return in_sprint, board.committed(state, state["sprint"])
    ids = state.get("planned_ids", {}).get(str(state["sprint"])) or []
    cards = [c for c in state["cards"] if c["id"] in ids]
    if not cards:  # nothing named: the top of the backlog up to capacity
        total, cards = 0, []
        for c in board.backlog_cards(state):
            p = board.with_test(state, c)
            if total + p > board.capacity(state) and total:
                break
            cards.append(c)
            total += p
    return cards, sum(board.with_test(state, c) for c in cards)


def closing_steps(state: dict) -> list[dict]:
    lead = "sm"
    return [
        {"role": lead, "kind": "open_floor",
         "instruction": "Ask if anyone has other questions or concerns before you close. One sentence.",
         "script": "Any other questions or concerns before we close? If not, I'll wrap up.",
         "prompt": "Ask for last questions."},
        {"role": "user_floor", "kind": "floor_answer", "instruction": "", "script": "",
         "prompt": "Any other questions or concerns? Say them now, or click 'Nothing from me' to close."},
        {"role": lead, "kind": "summary",
         "instruction": ("Summarise this meeting in 2-3 sentences: what was decided and what happens next. "
                         "Then say you're closing the meeting."),
         "script": None,
         "prompt": "Wrap up and close."},
    ]


def story_steps(state: dict, c: dict) -> list[dict]:
    """Refinement for one story: the PO presents it, the developer estimates, the tester adds an edge case."""
    po, sm, dev, qa = (who(state, r) for r in ("po", "sm", "dev", "tester"))
    t = c["title"]
    return [
        {"role": "po", "kind": "present_story", "card_id": c["id"],
         "instruction": f"Present the story '{t}' in one sentence and why it matters. Acceptance criteria: {c['acceptance']}",
         "script": f"Story: {t}. {c['acceptance']}",
         "prompt": f"Present the story '{t}': what it is and why it matters."},
        {"role": "dev", "kind": "estimate", "card_id": c["id"],
         "instruction": (f"Estimate '{t}' in story points. Points are RELATIVE effort compared with the "
                         "other stories (1 tiny, 2 small, 3 medium, 5 big, 8 very big): think of complexity, "
                         "unknowns and testing. Say the number and the one reason."),
         "script": (f"I'd say {board.estimate_for(t)} points for {t}: "
                    + {1: "it's tiny.", 2: "small and well understood.", 3: "medium, a few screens plus error handling.",
                       5: "big, it touches several parts and has unknowns.", 8: "very big, we may want to split it."}
                    .get(board.estimate_for(t), "medium effort.")),
         "prompt": (f"Estimate '{t}' in story points: 1, 2, 3, 5 or 8. Points = relative effort vs the other "
                    "stories (complexity, unknowns, testing). Say the number and why.")},
        {"role": "tester", "kind": "edge_case", "card_id": c["id"],
         "instruction": (f"Estimate the test story T{c['id']} (writing and running the tests for '{t}') in points "
                         "(1, 2, 3 or 5), and add ONE edge case. One or two short sentences, e.g. "
                         "'Testing is about 2 points. Add: an empty list shows a friendly message.'"),
         "script": f"Testing is about {board.test_estimate(board.points(c) or board.estimate_for(t))} points. " + ["Add: empty or invalid input shows a clear error message, not a crash.",
                "Add: it still works on a slow connection and shows a loading state.",
                "Add: an empty list shows a friendly message instead of a blank screen."][c["id"] % 3],
         "prompt": (f"Estimate your test story T{c['id']} in points (1, 2, 3 or 5: effort to write and run the "
                    f"tests for '{t}'), and add one edge case.")},
    ]


def agenda(state: dict, mtype: str) -> list[dict]:
    s = state["sprint"]
    po, sm, dev, qa = (who(state, r) for r in ("po", "sm", "dev", "tester"))
    backlog = board.backlog_cards(state)
    sprint = board.sprint_stories(state, s)
    rep = state.get("work_report") or {}

    if mtype == "kickoff":
        steps = [st for st in kickoff.agenda(state) if st["kind"] != "summary"]
    elif mtype == "refinement":
        # Story by story: PO presents, developer estimates, tester adds an edge case.
        steps = [
            {"role": "sm", "kind": "open",
             "instruction": f"Open backlog refinement in one sentence: we go story by story. Hand over to {po}.",
             "script": f"Welcome to backlog refinement. We'll go story by story: {po} presents, {dev} estimates, {qa} adds edge cases. {po}, first story please.",
             "prompt": "Open refinement."},
        ]
        for c in backlog[:3]:
            steps += story_steps(state, c)
        new = board.growth_story(state)
        po_player = state["role"] == "po"
        steps.append(
            {"role": "po", "kind": "confirm",
             "instruction": "Confirm the stories are ready for planning in one sentence."
                            + (f" Mention a new request, '{new[0]}', goes to the backlog." if new else ""),
             "script": "These are ready for planning." + (f" New request: {new[0]}; I'll add it to the backlog." if new else ""),
             "prompt": "Confirm the stories are ready for planning."
                       + ((f" (A stakeholder asked for '{new[0]}'. You decide: add it in the Backlog tab if you want it.)"
                           if po_player else f" (A new request, '{new[0]}', will be added to the backlog.)") if new else "")})
    elif mtype == "planning":
        vel = state["velocity"].get(str(s - 1))
        steps = [
            {"role": "sm", "kind": "open",
             "instruction": f"Open Sprint {s} planning. {board.capacity_note(state)} Mention the capacity in a few words"
                            + (f", last velocity was {vel}" if vel is not None else "") + f". Ask {po} what matters most.",
             "script": f"Sprint {s} planning. Our capacity is about {board.capacity(state)} points"
                       + (" (a starting guess for our 2-person team)" if vel is None else " (our average velocity)")
                       + (f", and last sprint we delivered {vel}" if vel is not None else "")
                       + f". {po}, what matters most this sprint?",
             "prompt": f"Open Sprint {s} planning."},
            {"role": "po", "kind": "priorities",
             "instruction": (f"Propose which stories go in this sprint, by number, most important first. "
                             f"Backlog: {_numbered(state, backlog, 5)}. Stay within about {board.capacity(state)} points."),
             "script": "I'd like " + " and ".join(f"story {c['id']}, {c['title']}" for c in backlog[:2]) + ".",
             "prompt": (f"Say which stories you want in this sprint, by number (e.g. 'story {backlog[0]['id'] if backlog else 1} "
                        f"and story {backlog[1]['id'] if len(backlog) > 1 else 2}'). Backlog: {_numbered(state, backlog, 5)}.")},
            {"role": "dev", "kind": "capacity_check",
             "instruction": "Check the proposal against capacity and push back if it's too much. One or two sentences.",
             "script": f"That's close to our {board.capacity(state)} points. I'm fine with it, but nothing more.",
             "prompt": "Check the proposal against capacity. Push back if it's too much."},
            {"role": "sm", "kind": "goal",
             "instruction": "Propose a one-sentence sprint goal. Start with 'Sprint goal:'.",
             "script": f"Sprint goal: a user can use '{_stories(backlog, 1)}' end to end. Agreed?",
             "prompt": "Set the sprint goal."},
            {"role": "po", "kind": "commit",
             "instruction": "Agree to the goal and confirm the sprint backlog in one sentence.",
             "script": "Agreed. That's our sprint backlog.",
             "prompt": "Confirm the sprint goal and the stories in the sprint."},
        ]
    elif mtype == "daily":
        dev_work = "; ".join(rep.get("dev") or []) or "no change since last time"
        qa_work = "; ".join(rep.get("tester") or []) or "nothing to test yet"
        slow = rep.get("factor", 1) < 0.5
        note = ""
        me = who(state, state["role"])
        if state["role"] == "tester":
            waiting = [c for c in board.sprint_stories(state, state["sprint"]) if c["status"] == "testing"]
            if waiting:
                note = f" {me}, {len(waiting)} card{'s are' if len(waiting) > 1 else ' is'} waiting for you in Testing; we can't finish without your testing."
        if state["role"] == "dev":
            open_ = [c for c in board.sprint_stories(state, state["sprint"]) if c["status"] in ("todo", "inprogress")]
            if open_:
                note = f" {me}, {len(open_)} card{'s' if len(open_) > 1 else ''} still with you in To Do or In Progress; please move them on the Board."
        # The team remembers the last stand-up: what each person said they'd do, and any blocker.
        prev = last_standup(state)
        mine = prev.get(state["role"], "")
        if mine and re.search(r"(?i)block|stuck|wait|problem|issue|help", mine):
            note += f" {me}, last time you said: '{_short(mine, 90)}'. Is that sorted?"
        said = lambda r: (f" At the last stand-up you said: '{_short(prev[r])}'. Say briefly whether you did it, then today's plan."  # noqa: E731
                          if prev.get(r) else "")
        steps = [
            {"role": "sm", "kind": "open",
             "instruction": "Open the stand-up in one sentence: done since last time, plan, blockers." + (f" Also say:{note}" if note else ""),
             "script": f"Stand-up {state['standups']}. Quick round: done since last time, plan for today, any blockers?{note} {dev}, you first.",
             "prompt": "Open the stand-up."},
            {"role": "dev", "kind": "update",
             "instruction": f"Give your stand-up update. Facts: {dev_work}." + said("dev") + (" You were given little time, say you're still working." if slow else ""),
             "script": ("Still working, it's only been a short while. " if slow else "")
                       + ("As I said last time, I kept going. " if prev.get("dev") else "") + f"Since last time: {dev_work}. No blockers.",
             "prompt": "Give your update: done since last time, plan for today, blockers. Then move your cards on the Board."
                       + (f" (Last time you said: '{_short(prev['dev'])}')" if prev.get("dev") and state["role"] == "dev" else "")},
            {"role": "tester", "kind": "update",
             "instruction": f"Give your stand-up update. Facts: {qa_work}." + said("tester"),
             "script": f"Testing update: {qa_work}. No blockers.",
             "prompt": "Give your update: what you tested, what's next, blockers. Then test cards on the Board."
                       + (f" (Last time you said: '{_short(prev['tester'])}')" if prev.get("tester") and state["role"] == "tester" else "")},
            {"role": "po", "kind": "update",
             "instruction": "One sentence: available for questions, no new work mid-sprint.",
             "script": "I'm around for questions on the stories. No new work mid-sprint.",
             "prompt": "Anything for the team? Keep it short; no new work mid-sprint."},
        ]
    elif mtype == "review":
        done = [c for c in sprint if c["status"] == "done"]
        open_ = [c for c in sprint if c["status"] != "done"]
        steps = [
            {"role": "sm", "kind": "open",
             "instruction": f"Open the Sprint {s} review. Ask {dev} to demo what was built (Demo tab).",
             "script": f"Welcome to the Sprint {s} review. {dev}, can you show us what we built? It's in the Demo tab.",
             "prompt": "Open the review."},
            {"role": "dev", "kind": "demo",
             "instruction": f"Demo briefly. Done: {_stories(done, 5)}. Not finished: {_stories(open_, 5)}.",
             "script": f"We finished: {_stories(done, 5)}." + (f" Not finished: {_stories(open_, 5)}; it goes back to the backlog." if open_ else ""),
             "prompt": "Present what was built (Demo tab) and what wasn't finished."},
            {"role": "tester", "kind": "quality",
             "instruction": "Report quality in one or two sentences: what passed, bugs found.",
             "script": "Quality: everything in Done passed its test cases. One story failed once and was fixed." if any(c["failed_once"] for c in sprint) else "Quality: everything in Done passed its test cases.",
             "prompt": "Report on quality: what passed, what failed, bugs found."},
            {"role": "po", "kind": "accept",
             "instruction": "Accept the finished stories that meet their acceptance criteria and give one piece of feedback.",
             "script": f"I'm accepting {_stories(done, 5)}. Nice work. Next sprint, let's look at what's left in the backlog.",
             "prompt": "Accept the finished stories (or say what you reject) and give feedback."},
        ]
    else:  # retro
        steps = [
            {"role": "sm", "kind": "open",
             "instruction": "Open the retrospective: what went well, what didn't, what we'll try next sprint.",
             "script": "Retro time. Three questions: what went well, what didn't, and what will we try next sprint?",
             "prompt": "Open the retro."},
            {"role": "dev", "kind": "retro_point",
             "instruction": "Share one thing that went well and one that didn't.",
             "script": f"Went well: pairing on {_stories(sprint, 1)}. Didn't: I under-estimated one story.",
             "prompt": "Share what went well and what didn't for you."},
            {"role": "tester", "kind": "retro_point",
             "instruction": "Share one thing that went well and one that didn't, from a quality view.",
             "script": "Didn't go well: stories reached testing late. Went well: clear acceptance criteria.",
             "prompt": "Share what went well and what didn't, from a quality view."},
            {"role": "po", "kind": "retro_point",
             "instruction": "Share one point from the Product Owner's view.",
             "script": "Splitting stories earlier would help me order the backlog.",
             "prompt": "Share one point from the Product Owner's view."},
            {"role": "sm", "kind": "actions",
             "instruction": "Agree on two concrete improvement actions for next sprint.",
             "script": "Actions for next sprint: finish one story before starting the next, and split anything over 5 points.",
             "prompt": "Agree on improvement actions."},
        ]
    return steps + closing_steps(state)


# --------------------------------------------------------------------------
# Practice-mode summary (AI mode writes its own)
# --------------------------------------------------------------------------
LABELS = {
    "vision": "Vision", "question": "Question", "answer": "Answer", "dod_answer": "Definition of Done",
    "present": "Top stories", "present_story": "Story", "estimate": "Estimate", "edge_case": "Edge case",
    "edge_cases": "Edge cases", "confirm": "Backlog changes",
    "priorities": "Proposed scope", "capacity_check": "Capacity", "testability": "Testing", "goal": "Sprint goal",
    "commit": "Decision", "update": "Update", "demo": "Demo", "quality": "Quality", "accept": "Acceptance",
    "retro_point": "Retro", "actions": "Improvement actions", "floor_answer": "Raised at the end",
}

NEXT = {
    "kickoff": "Product Owner adds the first stories to the backlog. Next: Backlog Refinement.",
    "refinement": "Stories are ready. Next: Sprint Planning.",
    "planning": "The sprint has started. Next: daily stand-ups.",
    "daily": "Everyone updates their cards. Next: another stand-up, or the Sprint Review.",
    "review": "Velocity recorded. Next: the Retrospective.",
    "retro": "Sprint closed. Next: Sprint Planning for the next sprint.",
}


def first_sentence(text: str, limit: int = 170, question: bool = False) -> str:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    pick = next((p for p in parts if p.endswith("?")), None) if question else None
    if not pick:
        pick = next((p for p in parts if len(p) >= 25), parts[0] if parts else text)
    return (pick[: limit - 3] + "...") if len(pick) > limit else pick


def summary_script(state: dict, mtype: str, transcript: list[dict]) -> tuple[str, list[str]]:
    if mtype == "kickoff":
        return kickoff.summary_script(state, transcript)
    if mtype == "refinement":
        # One line per story: estimate + the edge case that was added.
        minutes = []
        for cid in dict.fromkeys(m.get("card_id") for m in transcript if m.get("card_id")):
            try:
                c = board.get_card(state, cid)
            except ValueError:
                continue
            edge = next((m["text"] for m in transcript if m.get("card_id") == cid and m.get("kind") == "edge_case"), "")
            edge = re.sub(r"(?is)^.*?\badd:\s*", "", edge).strip()
            minutes.append(f"{c['title']}: {c['points'] or '?'} points" + (f"; edge case: {first_sentence(edge, 110)}" if edge else ""))
        new = board.growth_story(state)
        if state["role"] != "po" and new:
            minutes.append(f"New request: {new[0]} added to the backlog.")
        minutes.append(NEXT[mtype])
        return f"Thanks, everyone, that covers it. {NEXT[mtype]} I'm closing the meeting now.", minutes
    minutes = []
    for m in transcript:
        k = m.get("kind")
        if m["who"] == "user" and k not in LABELS and k != "summary" and len(m["text"]) > 12:
            t = m["text"].strip()
            minutes.append(f"{board.player(state)} said: {t if len(t) <= 140 else t[:137] + '...'}")
            continue
        if k not in LABELS or k == "floor_answer" and re.fullmatch(r"(?i)\W*(no|none|nothing|all good)[^?]*", m["text"]):
            continue
        name = board.player(state) if m["who"] == "user" else m["name"]
        minutes.append(f"{LABELS[k]} ({name}): {first_sentence(m['text'], question=(k == 'question'))}")
    minutes = minutes[:7]
    minutes.append(NEXT[mtype])
    spoken = f"Thanks, everyone, that covers it. {NEXT[mtype]} I'm closing the meeting now."
    return spoken, minutes
