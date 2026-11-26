"""
kickoff.py - the Project Kickoff, run one turn at a time.

The Scrum Master leads. Each step has:
  role         who speaks
  instruction  what an AI teammate should say (AI mode)
  script       what a teammate says in practice mode
  prompt       what the human is asked to do when it's THEIR role's turn
The last step is the summary: the meeting is summarised and closed.
"""
from __future__ import annotations

import re

import board


def _who(state: dict, role: str) -> str:
    """How teammates address someone: by name, or by title if it's the human."""
    if role == state["role"]:
        return board.player(state)
    return board.name(role)


def agenda(state: dict) -> list[dict]:
    p = state["product"]
    po, dev, qa = _who(state, "po"), _who(state, "dev"), _who(state, "tester")
    you_po = state["role"] == "po"
    steps = [
        {"role": "sm", "kind": "open",
         "instruction": (f"Open the kickoff for '{p['name']}': welcome everyone, say in one sentence what the "
                         f"kickoff is for, then ask {po} (Product Owner) to share the product vision."),
         "script": f"Welcome to the kickoff for {p['name']}. {po}, could you start with the product vision?",
         "prompt": "Open the kickoff."},
        {"role": "po", "kind": "vision",
         "instruction": f"Share the product vision in one or two sentences. Vision: {p['vision']}",
         "script": f"Sure. {p['vision']} '{board.first_story(state)}' comes first: it's the smallest useful thing.",
         "prompt": "Your turn: share the product vision. What are we building, for whom, and what comes first?"},
        {"role": "sm", "kind": "invite",
         "instruction": f"Thank the Product Owner in a few words and ask {dev} for questions.",
         "script": f"Thanks, {po}. Questions on the vision? {dev}?",
         "prompt": "Ask for questions on the vision."},
        {"role": "dev", "kind": "question",
         "instruction": "Ask the Product Owner ONE question about scope or technical risk. Just the question.",
         "script": "What's the smallest first release we're aiming for? I'd like to know what's in and what's out.",
         "prompt": "Your turn: ask the Product Owner a question about scope or technical risk."},
        {"role": "po", "kind": "answer",
         "instruction": "Answer the question that was just asked, clearly and briefly.",
         "script": "The smallest useful version first. Anything extra goes in the backlog for later sprints.",
         "prompt": "Answer the question that was just asked."},
        {"role": "tester", "kind": "dod_answer",
         "instruction": "Propose a short Definition of Done in one sentence.",
         "script": ("One thing before we close: I propose a story is done when its criteria are met, its "
                    "tests pass, and there are no open critical bugs."),
         "prompt": "Propose a Definition of Done: when is a story really finished?"},
    ]
    if you_po:
        # You decide the product: name and vision are given here, in the meeting.
        steps[0]["instruction"] = (f"Open the project kickoff in one sentence, then ask {po} (Product Owner) "
                                   "what we are building: the product name and the vision.")
        steps[0]["script"] = f"Welcome to our project kickoff. {po}, what are we building? Product name and vision, please."
        steps[1]["prompt"] = "Your turn: tell the team the product name, who it's for, and what problem it solves."
    return steps + [
        {"role": "sm", "kind": "summary",
         "instruction": ("Summarise the kickoff in 2-3 sentences: the vision, the answers to the questions, "
                         f"the Definition of Done, and next steps ({po} adds the first stories to the "
                         "backlog; next meeting is Backlog Refinement). Then say you're closing the meeting."),
         "script": None,  # built from what was actually said
         "prompt": "Wrap up and close."},
    ]


def _said(transcript: list[dict], role: str, state: dict) -> list[str]:
    who = "user" if role == state["role"] else role
    return [m["text"] for m in transcript if m["who"] == who]


def _first_sentence(text: str, limit: int = 160, question: bool = False) -> str:
    """The sentence that carries the point: the question, or the first non-filler sentence."""
    import re
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    pick = None
    if question:
        pick = next((p for p in parts if p.endswith("?")), None)
    if not pick:
        pick = next((p for p in parts if len(p) >= 25), parts[0] if parts else text)
    return (pick[: limit - 3] + "...") if len(pick) > limit else pick


def summary_script(state: dict, transcript: list[dict]) -> tuple[str, list[str]]:
    """Practice mode: build the spoken wrap-up and the written minutes from what was said."""
    po = _who(state, "po")
    po_lines = _said(transcript, "po", state)
    dev_q = _said(transcript, "dev", state)
    qa_lines = _said(transcript, "tester", state)
    vision = state["product"]["vision"]
    if state["role"] == "po" and po_lines:
        vision = po_lines[0].strip()
    minutes = [f"Vision: {vision}"]
    # Each question, paired with the Product Owner's next line (the answer).
    po_who = "user" if state["role"] == "po" else "po"
    labels = {"dev": "Developer", "tester": "Tester"}
    for i, m in enumerate(transcript):
        role = "dev" if m["who"] == ("user" if state["role"] == "dev" else "dev") else \
               "tester" if m["who"] == ("user" if state["role"] == "tester" else "tester") else None
        if not role or "?" not in m["text"]:
            continue
        minutes.append(f"{labels[role]} asked: {_first_sentence(m['text'], question=True)}")
        answer = next((x for x in transcript[i + 1:] if x["who"] == po_who), None)
        if answer:
            minutes.append(f"Product Owner answered: {_first_sentence(answer['text'])}")
    me = board.player(state)
    for m in transcript:
        if m["who"] == "user" and m.get("kind") in ("open", "invite", "dod", "interjection") and len(m["text"]) > 12:
            t = m["text"].strip()
            minutes.append(f"{me} said: {t if len(t) <= 140 else t[:137] + '...'}")
    dod = next((m["text"] for m in transcript if m.get("kind") == "dod_answer"), None)
    if dod:
        minutes.append(f"Definition of Done: {dod.strip()}")
    for m in transcript:
        if m.get("kind") == "floor_answer" and not re.fullmatch(r"(?i)\W*(no|none|nothing|all good)[^?]*", m["text"]):
            minutes.append(f"Raised at the end: {_first_sentence(m['text'])}")
    minutes.append(f"Next steps: {po} adds the first user stories to the backlog. "
                   "Next meeting: Backlog Refinement.")
    spoken = (f"To sum up: vision agreed, scope question answered, Definition of Done agreed. Next, {po} "
              "adds the first stories, then backlog refinement. Closing the meeting now. Thanks, everyone!")
    return spoken, minutes
