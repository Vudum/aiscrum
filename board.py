"""
board.py - the project "database" and the Scrum rules.

Everything about the simulated project lives in one JSON file per project
(data/projects/<id>.json). Both the Flask app and the MCP board server use
the functions in this file, so the rules are written once.

Key idea for learners: the RULES (what moves where, who must do what, how
much work gets done each day) are plain code. The AI agents only talk and
ask for actions through MCP tools.
"""
from __future__ import annotations

import contextvars
import copy
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SCRUMGYM_DATA_DIR", BASE_DIR / "data"))
PROJECTS_DIR = DATA_DIR / "projects"

MAX_SPRINTS = 3
DEV_POINTS_PER_DAY = float(os.environ.get("DEV_POINTS_PER_DAY", "3"))
TEST_POINTS_PER_DAY = float(os.environ.get("TEST_POINTS_PER_DAY", "5"))
SPRINT_CAPACITY = int(os.environ.get("SPRINT_CAPACITY", "16"))   # starting guess for a 2-person team
# Real seconds that count as "one working day". Come back sooner and the AI
# team has done less ("still working..."). Set to 0 for instant progress.
REAL_SECONDS_PER_DAY = float(os.environ.get("REAL_SECONDS_PER_DAY", "90"))

ROLES = {
    "po": {"name": "Priya", "title": "Product Owner", "color": "#7c3aed"},
    "sm": {"name": "Sam", "title": "Scrum Master", "color": "#0ea5e9"},
    "dev": {"name": "Marcus", "title": "Developer", "color": "#16a34a"},
    "tester": {"name": "Ana", "title": "Tester", "color": "#ea580c"},
}
STATUSES = ["backlog", "todo", "inprogress", "testing", "done"]
STATUS_LABELS = {
    "backlog": "Product Backlog",
    "todo": "To Do",
    "inprogress": "In Progress",
    "testing": "Testing",
    "done": "Done",
}

CEREMONY_INFO = {
    "kickoff": "Project Kickoff",
    "planning": "Sprint Planning",
    "daily": "Daily Scrum",
    "refinement": "Backlog Refinement",
    "review": "Sprint Review + Demo",
    "retro": "Sprint Retrospective",
}

NEW_PRODUCT = "New product"  # until the Product Owner names it in the kickoff
# When the AI plays the Product Owner, it picks one of these products (never the last few used).
# Each has its own starter stories and the new requests that arrive in later sprints.
def _p(name, vision, stories, growth):
    return {"name": name, "vision": vision, "stories": stories, "growth": growth}


PRODUCTS = [
    _p("TrailMate - hiking trails", "Hikers can find nearby trails that match their level and save them for the weekend.",
       [("Search trails near me", "Shows trails within 30 km, sorted by distance. Each shows length and difficulty."),
        ("Filter by difficulty", "Easy, moderate and hard filters. The list updates right away."),
        ("Save a trail", "Saved trails appear in 'My trails' and stay after closing the app.")],
       [("Trail weather forecast", "Shows the 3-day forecast at the trailhead."),
        ("Share a trail with a friend", "Sends a link that opens the trail page."),
        ("Offline trail map", "The saved trail's map works without signal.")]),
    _p("PawPal - pet sitter booking", "Pet owners can find trusted pet sitters nearby and book them in a few taps.",
       [("Browse sitters nearby", "Shows sitters within 10 km with price per day and rating."),
        ("Sitter profile", "Shows photos, pets accepted, reviews and available dates."),
        ("Request a booking", "Owner picks dates and pet; the sitter gets the request and can accept or decline.")],
       [("Chat with the sitter", "Owner and sitter can message about a booking."),
        ("Daily photo updates", "Sitter can post a photo each day of the stay."),
        ("Leave a review", "Owner rates 1-5 stars after the booking ends.")]),
    _p("FreshCart - grocery delivery", "Busy families can order groceries and get them delivered the same day.",
       [("Browse products by category", "Categories like fruit, dairy and bakery; each item shows price and photo."),
        ("Add to cart", "Cart shows items, quantities and the total; quantities can be changed."),
        ("Choose a delivery slot", "Pick a 2-hour slot today or tomorrow; full slots are greyed out.")],
       [("Reorder last basket", "One tap adds everything from the last order to the cart."),
        ("Substitution preferences", "Customer chooses 'replace' or 'refund' if an item is out of stock."),
        ("Track the driver", "Shows the driver's position and arrival time.")]),
    _p("StudyBuddy - flashcards", "Students can study with flashcards that repeat what they keep forgetting.",
       [("Create a deck", "A deck has a name and cards with a front and back."),
        ("Study mode", "Shows the front; tap to flip; mark 'knew it' or 'forgot'."),
        ("Smart repeat", "Cards marked 'forgot' come back sooner than cards marked 'knew it'.")],
       [("Study streak", "Shows how many days in a row the student studied."),
        ("Share a deck", "A link lets classmates copy the deck."),
        ("Import from a spreadsheet", "Upload two columns to create cards.")]),
    _p("FitTrack - workout log", "Gym-goers can log a workout in seconds and see their progress over time.",
       [("Log a workout", "Pick an exercise, add sets with reps and weight, save."),
        ("Workout history", "List of past workouts by date; tap one to see details."),
        ("Progress chart", "Line chart of the heaviest weight per exercise over time.")],
       [("Rest timer", "Starts after a set; vibrates when rest is over."),
        ("Workout templates", "Save a workout as a template and start it again."),
        ("Personal record badge", "Shows a badge when the user beats their best.")]),
    _p("ParkEasy - city parking", "Drivers can find a free parking spot near their destination and pay from the phone.",
       [("Map of free spots", "Shows nearby spots with price per hour, updated every minute."),
        ("Pay for parking", "Choose duration, pay, and get a receipt on screen."),
        ("Expiry reminder", "Notification 10 minutes before the parking time ends.")],
       [("Extend parking remotely", "Add time from the phone without going back to the car."),
        ("Save favourite places", "Home and work show at the top."),
        ("Monthly parking report", "Total spent and time parked per month.")]),
    _p("MediRemind - medicine reminders", "Busy caregivers can track medicine schedules for the family and never miss a dose.",
       [("Add a medicine schedule", "Name, dose and times per day for a family member."),
        ("Dose reminder", "Notification at each dose time with 'Taken' and 'Snooze'."),
        ("Today's checklist", "Shows all doses for today and which are taken.")],
       [("Refill warning", "Warns when only 5 days of medicine are left."),
        ("Share with another caregiver", "A second person sees the same schedule."),
        ("Weekly adherence report", "Shows the percentage of doses taken on time.")]),
    _p("TableTime - restaurant booking", "Diners can find a restaurant with a free table tonight and book it instantly.",
       [("Search restaurants", "Search by area and cuisine; shows price level and next free time."),
        ("Book a table", "Pick party size and time; booking is confirmed on screen."),
        ("My bookings", "Upcoming bookings with cancel option until 2 hours before.")],
       [("Waitlist", "Join a waitlist when the restaurant is full and get notified."),
        ("Menu preview", "Shows the menu with prices before booking."),
        ("Special requests", "Add notes like 'window seat' or 'birthday'.")]),
    _p("HomeFix - handyman jobs", "Homeowners can describe a small repair and get quotes from local handymen the same day.",
       [("Post a repair job", "Title, description, photo and preferred day."),
        ("Receive quotes", "Handymen send a price and time; the owner sees them side by side."),
        ("Accept a quote", "Owner accepts one quote; others are told it's taken.")],
       [("Job status updates", "Shows 'on the way', 'working' and 'done'."),
        ("Pay in the app", "Owner pays after the job is marked done."),
        ("Rate the handyman", "1-5 stars and a short comment.")]),
    _p("EventHub - local events", "People can discover events in their city this weekend and get tickets fast.",
       [("Browse this weekend's events", "List by day with time, place and price."),
        ("Event details", "Description, map and ticket types."),
        ("Buy a ticket", "Choose ticket type and quantity; ticket shows a QR code.")],
       [("Save to calendar", "Adds the event to the phone's calendar."),
        ("Invite friends", "Share a link to the event."),
        ("Event reminders", "Notification the day before the event.")]),
]
DEFAULT_PRODUCT = PRODUCTS[0]

DEFAULT_BACKLOG = [
    ("Home screen with the main feature",
     "User sees the main feature on the first screen and can use it in 2 taps."),
    ("Search and filter",
     "User can search by name and filter the list; results update right away."),
    ("Save favourites",
     "User can save an item and find it later under 'Favourites'."),
]

# The backlog grows: at each refinement the Product Owner brings one new request.
SCOPE_GROWTH = [
    ("Share with a friend", "User can share an item with a link that opens it in the app."),
    ("Notifications", "User gets a reminder notification and can turn it off in settings."),
    ("Dark mode", "User can switch to a dark theme; the choice is remembered."),
]

SCOPE_CREEP_STORY = (
    "Sign in with Google",
    "User can sign in with a Google account. New users get an account created.",
)

ESTIMATE_HINTS = [
    ("google", 8), ("social", 8), ("sign up", 5), ("create account", 5),
    ("login", 5), ("log in", 5), ("forgot", 3), ("reset", 3),
    ("profile", 3), ("lockout", 3), ("lock", 3), ("remember", 2),
    ("error", 2), ("logout", 1), ("log out", 1),
]


# --------------------------------------------------------------------------
# Meetings: kickoff first, then the user chooses. The team checks if the
# chosen meeting makes sense right now; if not, they say why and close it.
# --------------------------------------------------------------------------
MEETING_TYPES = ["refinement", "planning", "daily", "review", "retro"]
MEETING_SHORT = {
    "kickoff": "Kickoff", "refinement": "Refinement", "planning": "Sprint Planning",
    "daily": "Stand-up", "review": "Sprint Review", "retro": "Retrospective",
}


def current_ceremony(state: dict) -> dict:
    """The latest meeting (live or ended). Before any meeting: the kickoff."""
    if not state["meetings"]:
        return {"type": "kickoff", "sprint": 0, "index": -1, "label": CEREMONY_INFO["kickoff"],
                "day": 0}
    i = len(state["meetings"]) - 1
    m = state["meetings"][i]
    return {"type": m["type"], "sprint": m["sprint"], "index": i, "label": m["label"],
            "day": m.get("standup", 0)}


def meeting_label(state: dict, mtype: str) -> str:
    if mtype == "kickoff":
        return CEREMONY_INFO["kickoff"]
    s = state["sprint"]
    if mtype == "daily":
        return f"Daily Stand-up {state['standups'] + 1} (Sprint {s})"
    return f"{CEREMONY_INFO[mtype]} (Sprint {s})"


def check_meeting(state: dict, mtype: str):
    """Return (ok, speaker_role, reason). Reason is said out loud by a teammate."""
    st, s = state["sprint_state"], state["sprint"]
    lead = "sm" if state["role"] != "sm" else "po"
    backlog = backlog_cards(state)
    if mtype == "refinement":
        if not backlog:
            return False, "po" if state["role"] != "po" else "sm", (
                "The backlog is empty, so there's nothing to refine. "
                + (f"{name('po')}, can you add stories first?" if state["role"] != "po"
                   else "Product Owner, please add some stories in the Backlog tab first."))
    if mtype == "planning":
        if st == "active":
            return False, lead, (f"We're in the middle of Sprint {s}. Planning for the next sprint "
                                 "happens after the review and retro. Let's close this one.")
        if st == "reviewed":
            return False, lead, "Sprint review is done. We need the retrospective before we plan again."
        if not backlog:
            return False, lead, ("There's nothing in the backlog to plan with. "
                                 "Let's get stories in first, then plan.")
    if mtype == "daily":
        if st == "unplanned":
            who = "dev" if state["role"] != "dev" else lead
            return False, who, ("We don't have a sprint plan yet, so what would I be reporting on? "
                                "Let's do Sprint Planning first, and check the backlog is ready.")
        if st == "reviewed":
            return False, lead, "The sprint is already reviewed. Next is the retrospective."
    if mtype == "review":
        if st == "unplanned":
            return False, lead, "There's no sprint to review yet. Plan a sprint first."
        if st == "reviewed":
            return False, lead, "We already reviewed this sprint. Time for the retrospective."
        if state["standups"] == 0:
            return False, "dev" if state["role"] != "dev" else lead, (
                "We haven't started the work yet, so there's nothing to demo. "
                "Let's have a stand-up first.")
    if mtype == "retro":
        if st != "reviewed":
            return False, lead, ("A retro closes the sprint, but we haven't reviewed the work "
                                 "with the Product Owner yet. Sprint Review first, please.")
    return True, None, ""


def suggested_meeting(state: dict) -> str:
    if not state["kickoff_done"]:
        return "kickoff"
    st = state["sprint_state"]
    if st == "unplanned":
        b = backlog_cards(state)
        if b and any(not points(c) or not c["acceptance"].strip() for c in b[:3]):
            return "refinement"
        return "planning"
    if st == "active":
        return "daily" if state["standups"] < 3 else "review"
    return "retro"


# --------------------------------------------------------------------------
# Load / save
# --------------------------------------------------------------------------
def project_path(project_id: str) -> Path:
    safe = "".join(ch for ch in project_id if ch.isalnum() or ch in "-_")
    return PROJECTS_DIR / f"{safe}.json"


def load(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    _NAMES.set(state.get("names") or {})   # this project's teammate names, for this request
    return state


def save(path: str | Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def growth_story(state: dict, sprint: int | None = None):
    """The next new request for the backlog this sprint (one per sprint), or None."""
    titles = {c["title"] for c in state["cards"]}
    growth = [tuple(g) for g in state.get("growth") or []]
    growth += [g for g in SCOPE_GROWTH if g[0] not in {x[0] for x in growth}]  # never run dry
    i = (sprint or state.get("sprint", 1)) - 1
    if i < len(growth) and growth[i][0] not in titles:
        return growth[i]
    return None


def first_story(state: dict, i: int = 0) -> str:
    """Title of a starter story ('the smallest useful thing' first), for scripted lines."""
    titles = [c["title"] for c in backlog_cards(state)] or [t for t, _ in (state.get("drafted") or DEFAULT_BACKLOG)]
    return titles[i] if i < len(titles) else (titles[-1] if titles else "the first feature")


def pick_product() -> dict:
    """A different product each time: never one of the last few used."""
    import random
    recent_file = DATA_DIR / "recent_products.json"
    try:
        recent = json.loads(recent_file.read_text())
    except (OSError, ValueError):
        recent = []
    fresh = [p for p in PRODUCTS if p["name"] not in recent] or PRODUCTS
    pick = random.choice(fresh)
    try:
        recent_file.parent.mkdir(parents=True, exist_ok=True)
        recent_file.write_text(json.dumps((recent + [pick["name"]])[-6:]))
    except OSError:
        pass
    return pick


# A new team for every project. Female/male pools match the voices (PO + tester female, SM + dev male).
NAME_POOL = {
    "po": ["Priya", "Sofia", "Maya", "Elena", "Aisha", "Hannah", "Mei", "Laura", "Nadia", "Grace"],
    "sm": ["Sam", "Daniel", "Omar", "Lucas", "Ravi", "Tom", "Jonas", "Kenji", "Mateo", "Ben"],
    "dev": ["Marcus", "Arjun", "Leo", "Ethan", "Kwame", "Diego", "Noah", "Felix", "Hiro", "Adam"],
    "tester": ["Ana", "Chloe", "Zara", "Ingrid", "Fatima", "Julia", "Amara", "Lena", "Rosa", "Yuki"],
}
_NAMES = contextvars.ContextVar("scrumgym_names", default={})


def name(role: str) -> str:
    """A teammate's name in the current project."""
    return _NAMES.get().get(role) or ROLES[role]["name"]


def pick_names(player_name: str = "") -> dict:
    """Fresh names every project: never your own name, never the names from the last two projects."""
    import random
    recent_file = DATA_DIR / "recent_names.json"
    try:
        recent = set(json.loads(recent_file.read_text()))
    except (OSError, ValueError):
        recent = set()
    taken = {player_name.strip().lower()}
    out = {}
    for r, pool in NAME_POOL.items():
        options = [n for n in pool if n.lower() not in taken and n not in recent] or \
                  [n for n in pool if n.lower() not in taken]
        out[r] = random.choice(options)
        taken.add(out[r].lower())
    try:
        recent_file.parent.mkdir(parents=True, exist_ok=True)
        old = json.loads(recent_file.read_text()) if recent_file.exists() else []
        recent_file.write_text(json.dumps((old + list(out.values()))[-8:]))
    except (OSError, ValueError):
        pass
    return out


def new_project(role: str, product_name: str = "", vision: str = "", player_name: str = "") -> dict:
    if role not in ROLES:
        raise ValueError("Unknown role")
    now = time.time()
    if role == "po" and not product_name.strip():
        product_name, vision = NEW_PRODUCT, vision or ""   # you name it in the kickoff
    pick = None
    if role != "po" and not product_name.strip():
        pick = pick_product()
        product_name, vision = pick["name"], vision.strip() or pick["vision"]
    names = pick_names(player_name)
    _NAMES.set(names)
    return {
        "names": names,
        "drafted": [tuple(x) for x in pick["stories"]] if pick else None,
        "growth": [tuple(x) for x in pick["growth"]] if pick else None,
        "id": uuid.uuid4().hex[:12],
        "created": now,
        "role": role,
        "player_name": "".join(ch for ch in (player_name or "").strip() if ch.isalnum() or ch in " -'.")[:30],
        "product": {
            "name": (product_name or DEFAULT_PRODUCT["name"])[:80],
            "vision": (vision if role == "po" else (vision or DEFAULT_PRODUCT["vision"]))[:400],
        },
        "ceremony_index": -1,
        "meetings": [],
        "kickoff_done": False,
        "sprint": 1,
        "sprint_state": "unplanned",   # unplanned -> active -> reviewed -> (retro) next sprint
        "standups": 0,
        "last_work_ts": now,
        "cards": [],
        "next_id": 1,
        "sprint_goals": {},
        "burndown": {},
        "burnup": [],
        "velocity": {},
        "open_items": [],
        "work_report": None,
        "mcp_log": [],
        "prototypes": {},
        "complete": False,
    }


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def estimate_for(title: str) -> int:
    t = title.lower()
    for word, pts in ESTIMATE_HINTS:
        if word in t:
            return pts
    return 3


def get_card(state: dict, card_id: int) -> dict:
    for c in state["cards"]:
        if c["id"] == int(card_id):
            return c
    raise ValueError(f"No card with id {card_id}")


def sprint_stories(state: dict, sprint: int) -> list[dict]:
    """Stories and bugs in a sprint (without the tester's test tickets)."""
    return [c for c in state["cards"] if c.get("sprint") == sprint and c["type"] != "test"]


def sprint_cards(state: dict, sprint: int) -> list[dict]:
    return [c for c in state["cards"] if c.get("sprint") == sprint]


def capacity(state: dict) -> int:
    """How many points the team can take this sprint. Sprint 1: a hypothetical 16 points (2-person team:
    1 developer + 1 tester). Later sprints: the average velocity of the last 3 sprints."""
    done = [v for k, v in sorted(state.get("velocity", {}).items(), key=lambda kv: int(kv[0]))
            if int(k) < state["sprint"] and v][-3:]
    return max(5, round(sum(done) / len(done))) if done else SPRINT_CAPACITY


def capacity_note(state: dict) -> str:
    """Short note on where the capacity number comes from."""
    n = len([v for k, v in state.get("velocity", {}).items() if int(k) < state["sprint"] and v])
    if not n:
        return (f"Capacity: {capacity(state)} points per sprint (build + testing), a hypothetical starting number for "
                "our 2-person team (1 developer + 1 tester). It's updated to the average velocity of the last 3 sprints.")
    return f"Capacity: {capacity(state)} points, the average velocity of the last {min(n, 3)} sprint{'s' if n > 1 else ''}."


def backlog_cards(state: dict) -> list[dict]:
    cards = [c for c in state["cards"] if c["status"] == "backlog" and c["type"] != "test"]
    return sorted(cards, key=lambda c: c["priority"])


def points(card: dict) -> int:
    return int(card.get("points") or 0)


def user_role(state: dict) -> str:
    return state["role"]


def player(state: dict) -> str:
    """What teammates call the human: their name if given, else their role title."""
    return state.get("player_name") or ROLES[state["role"]]["title"]


def ai_roles(state: dict) -> list[str]:
    return [r for r in ROLES if r != state["role"]]


def mark_touched(state: dict, card: dict) -> None:
    card["touched"] = state["ceremony_index"]


def log_mcp(state: dict, agent: str, tool: str, args: dict, result: str) -> None:
    state["mcp_log"].append({
        "ts": time.time(), "agent": agent, "tool": tool,
        "args": args, "result": result[:200],
        "ceremony": state["ceremony_index"],
    })
    state["mcp_log"] = state["mcp_log"][-150:]


# --------------------------------------------------------------------------
# Board actions (used by MCP tools AND by the user's clicks)
# --------------------------------------------------------------------------
def create_story(state, title, acceptance="", points_=0, kind="story", related=None):
    title = (title or "").strip()[:120]
    if not title:
        raise ValueError("A story needs a title")
    card = {
        "id": state["next_id"],
        "type": kind,
        "title": title,
        "acceptance": (acceptance or "").strip()[:600],
        "points": int(points_ or 0),
        "status": "backlog",
        "sprint": None,
        "priority": state["next_id"],
        "dev_notes": "",
        "test_cases": "",
        "test_result": "",
        "test_note": "",
        "work_left": None,
        "test_left": None,
        "failed_once": False,
        "accepted": None,
        "touched": -1,
        "related": related,
        "created_at_ceremony": state["ceremony_index"],
    }
    if card["points"]:
        card["estimated_by"] = f"{name('dev')} (Developer), in refinement"
    state["next_id"] += 1
    state["cards"].append(card)
    if kind == "story":
        ensure_test_ticket(state, card)         # every user story comes with its own test story
    return card


def estimate_story(state, card_id, pts, by=""):
    pts = int(pts)
    if pts not in (0, 1, 2, 3, 5, 8, 13):
        raise ValueError("Use Fibonacci points: 1, 2, 3, 5, 8 or 13 (0 removes the estimate)")
    card = get_card(state, card_id)
    if pts == 0:   # the team agreed to drop the estimate (e.g. the story is parked)
        card["points"] = 0
        card.pop("estimated_by", None)
        if card["type"] == "story":
            sync_test_points(state, card)
        return card
    card["points"] = pts
    card["estimated_by"] = (by or "the team").strip()[:80]
    if card["type"] == "story":
        sync_test_points(state, card)
    if card["status"] in ("todo",) and card.get("work_left") is not None:
        card["work_left"] = pts
    return card


def add_to_sprint(state, card_id, without_test=False):
    if not state["kickoff_done"]:
        raise ValueError("Hold the kickoff first")
    if state["sprint_state"] == "reviewed":
        raise ValueError("This sprint is already reviewed. Add it in the next Sprint Planning")
    sprint = state["sprint"]
    card = get_card(state, card_id)
    if card["status"] != "backlog":
        raise ValueError("Only backlog items can be added to the sprint")
    if card["type"] == "test":
        raise ValueError("A test story moves into the sprint together with its story")
    now, add, cap = committed(state, sprint), with_test(state, card), capacity(state)
    # Only when the team agreed that testing waits for a later sprint: the story goes in alone and its
    # test ticket stays in the backlog.
    defer_test = bool(without_test) and card["type"] == "story"
    if defer_test and not is_carried(card):
        add = pts_of(state, card)
    # Capacity is a forecast, not a limit: the team may take on more (the board shows it's over).
    if is_carried(card):
        card["carried_from"] = card.get("carried_from") or sprint - 1   # keeps costing only the work left
    card["status"] = "todo"
    card["sprint"] = sprint
    card["added_mid_sprint"] = state["sprint_state"] == "active"  # scope change after planning
    if card["work_left"] is None:
        card["work_left"] = points(card) or estimate_for(card["title"])
    ensure_test_ticket(state, card)
    t = test_ticket(state, card["id"])
    card["test_deferred"] = defer_test
    if t and t["status"] == "backlog" and not defer_test:
        t.update(status="todo", sprint=sprint)
    return card


TEST_SIZE = {1: 1, 2: 1, 3: 2, 5: 3, 8: 5, 13: 5}   # testing effort vs build effort (a first guess)


def test_estimate(story_points: int) -> int:
    return TEST_SIZE.get(int(story_points or 3), 2)


def sync_test_points(state, story):
    """The tester's first estimate for a test story follows the story's size; once the tester sets it, it stays."""
    t = test_ticket(state, story["id"])
    if t and not str(t.get("estimated_by", "")).startswith("Tester:") and "in refinement" not in str(t.get("estimated_by", "")):
        if not points(story):        # nothing estimated yet: the test ticket shows '?' too
            t["points"] = 0
            t.pop("estimated_by", None)
            return
        t["points"] = test_estimate(points(story))
        t["estimated_by"] = f"{name('tester')} (Tester), first estimate from the story size"


def pts_of(state, c) -> int:
    """Points of a card for charts and capacity (test stories included)."""
    if c["type"] == "test":
        if points(c):
            return points(c)
        story = next((x for x in state["cards"] if x["id"] == c.get("test_for")), None)
        return test_estimate(points(story) or estimate_for(story["title"])) if story else 1
    return points(c) or estimate_for(c["title"])


def is_carried(c) -> bool:
    """A story that was worked on in an earlier sprint but not finished (it has work and test time left)."""
    return c.get("type") == "story" and (bool(c.get("carried_from")) or (
        c.get("status") == "backlog" and c.get("work_left") is not None and c.get("test_left") is not None))


def remaining(state, c) -> int:
    """Work still left on a carried-over story, build + testing (whole points, at least 1).
    Testing not started yet counts its full test estimate."""
    test = c.get("test_left")
    if test is None:
        t = test_ticket(state, c["id"])
        test = pts_of(state, t) if t and t["status"] != "done" else 0
    return max(1, math.ceil(float(c.get("work_left") or 0) + float(test)))


def _load(state, c) -> int:
    """What a card costs this sprint's capacity: unfinished carried-over work counts what is left, not its full size."""
    if c["type"] == "test":
        story = next((x for x in state["cards"] if x["id"] == c.get("test_for")), None)
        if story and is_carried(story):
            return 0   # already in the story's remaining work
    if is_carried(c):
        return remaining(state, c)
    return pts_of(state, c)


def committed(state, sprint) -> int:
    """Points already in the sprint (stories, bugs and test stories)."""
    return sum(_load(state, c) for c in sprint_cards(state, sprint))


def with_test(state, c) -> int:
    """A story plus its test story: what it costs the team's capacity."""
    if is_carried(c):
        return remaining(state, c)
    t = test_ticket(state, c["id"])
    return pts_of(state, c) + (pts_of(state, t) if t else 0)


def test_ticket(state, story_id):
    """The tester's own ticket for a story (test plan + test cases), if any."""
    return next((c for c in state["cards"] if c.get("test_for") == story_id), None)


def ensure_test_ticket(state, story):
    """Every story in a sprint gets a separate testing ticket, so dev and tester work in parallel."""
    if story["type"] != "story" or test_ticket(state, story["id"]):
        return None
    nid = state["next_id"]
    t = create_story(state, f"Test: {story['title']}"[:120],
                     f"As a tester, I want to write and run test cases (with sample data) for story #{story['id']}, "
                     "so that we know it works. Done when the test cases are written, run, and the result is recorded.",
                     0, kind="test", related=story["id"])
    state["next_id"] = nid                      # test stories don't use up story numbers
    t.update(id=1000 + story["id"], status={"done": "done", "backlog": "backlog"}.get(story["status"], "todo"),
             sprint=story.get("sprint"), test_for=story["id"], work_left=0.5, priority=story["priority"] + 0.5)
    sync_test_points(state, story)
    return t


def _drop_test_ticket(state, story_id):
    state["cards"] = [c for c in state["cards"] if c.get("test_for") != story_id]


def delete_story(state, card_id):
    """Product Owner: remove a story that isn't needed (only from the backlog, never in-sprint work)."""
    card = get_card(state, card_id)
    if card["status"] != "backlog":
        raise ValueError("Only backlog stories can be deleted. Take it out of the sprint first.")
    state["cards"] = [c for c in state["cards"] if c["id"] != card_id]
    _drop_test_ticket(state, card_id)
    for ids in state.get("planned_ids", {}).values():
        if card_id in ids:
            ids.remove(card_id)
    return card


def update_story(state, card_id, acceptance):
    """Add what the team agreed (a rule, a limit, an edge case) to a story's acceptance criteria."""
    card = get_card(state, card_id)
    extra = (acceptance or "").strip()
    if not extra:
        raise ValueError("Nothing to add")
    if extra.lower() not in card["acceptance"].lower():
        card["acceptance"] = (card["acceptance"].rstrip(" .;") + "; " + extra).strip("; ")[:600]
    return card


def split_story(state, card_id, parts):
    """Split a big story in two: the card keeps part 1, a new card right below it gets part 2.
    Both need a fresh estimate."""
    card = get_card(state, card_id)
    if card["status"] != "backlog":
        raise ValueError("Only backlog stories can be split")
    (t1, a1), (t2, a2) = parts[:2]
    card.update(title=t1.strip()[:120] or card["title"], acceptance=(a1 or "").strip()[:600], points=0)
    card.pop("estimated_by", None)
    t = test_ticket(state, card_id)
    if t:
        t["title"] = f"Test: {card['title']}"[:120]
    new = create_story(state, t2, a2, 0, related=card_id)
    new["priority"] = card["priority"] + 0.5
    new["discussion"] = list(card.get("discussion", []))
    return card, new


_IN = re.compile(r"(?i)\b(commit|committed|pull|take|include|go with|in (the|this) sprint|sprint backlog|i want|i'd like|let's do|we do|add)\b")
_OUT = re.compile(r"(?i)\b(back to the backlog|backlog|next sprint|later|drop|not part|remove|leave out|stay out|move[sd]? to sprint)\b")
_IFFY = re.compile(r"(?i)\b(would|could|if|might|whether)\b")


def scope_change(state: dict, text: str, current: list[int], decider: bool = False) -> list[int]:
    """Planning talk -> the sprint scope. 'We commit 1 and 2' sets it; '3 goes back to the backlog'
    takes 3 out; 'adding 3 would be too much' changes nothing."""
    scope = list(current)
    for clause in re.split(r"(?<=[.!?;])\s+|,\s*(?:but|and then)\s+|\bbut\b", text):
        ids = story_refs(state, clause)
        if not ids:
            continue
        if _OUT.search(clause):
            scope = [i for i in scope if i not in ids]
        elif _IN.search(clause) and not _IFFY.search(clause) or (decider and not _IFFY.search(clause)):
            if re.search(r"(?i)\b(commit|only|just)\b", clause):
                scope = list(ids)
            else:
                scope += [i for i in ids if i not in scope]
    return scope


def heuristic_split(card: dict):
    """Practice mode: split on the acceptance criteria, else basic / extras."""
    bits = [b.strip(" .") for b in re.split(r";|\. | and ", card["acceptance"]) if len(b.strip()) > 8]
    t = re.sub(r"\s*\((basic|extras)\)$", "", card["title"])
    if len(bits) >= 2:
        half = (len(bits) + 1) // 2
        return [(f"{t} (basic)", "; ".join(bits[:half])), (f"{t} (extras)", "; ".join(bits[half:]))]
    return [(f"{t} (basic)", card["acceptance"] or "The simplest working version."),
            (f"{t} (extras)", "Error cases, polish and anything beyond the basic version.")]


def is_test_task(card: dict) -> bool:
    """'Test the app in Chrome' is the tester's work, not the developer's."""
    return card.get("type") == "test" or bool(re.search(r"(?i)\b(test|tests|testing|qa|regression)\b", card["title"]))


NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
_STOP = {"the", "a", "an", "and", "or", "with", "for", "to", "of", "in", "on", "can", "user", "users",
         "page", "my", "as", "is", "be"}


POINT_WORDS = {"one": 1, "two": 2, "three": 3, "five": 5, "eight": 8, "thirteen": 13,
               "1": 1, "2": 2, "3": 3, "5": 5, "8": 8, "13": 13}
_MATCH_STOP = (_STOP - {"in", "on"}) | {"about", "around", "roughly", "points", "point", "story", "stories", "it",
                                        "that", "this", "we", "i", "think", "would", "say", "so", "now", "keep", "at"}


def _stems(text: str) -> set[str]:
    return {w[:4] for w in re.findall(r"[a-z0-9]+", text.lower().replace("-", " ")) if w not in _MATCH_STOP}


def find_card(state: dict, text: str) -> int | None:
    """The one story a phrase is about: 'story 3' / '#3', else the best match on title words ('sitter search')."""
    ids = story_refs(state, text)
    if ids:
        return ids[-1]
    stories = [c for c in state["cards"] if c["type"] == "story"]
    known = set().union(*(_stems(c["title"]) for c in stories)) if stories else set()
    words = _stems(text) & known          # only words that appear in some story title
    if not words:
        return None
    need = min(2, len(words))             # 'sign-up' must match both 'sign' and 'up'
    scores = sorted(((len(_stems(c["title"]) & words), c["id"]) for c in stories if c["status"] != "done"), reverse=True)
    if scores and scores[0][0] >= need and (len(scores) == 1 or scores[0][0] > scores[1][0]):
        return scores[0][1]
    return None


_EST = re.compile(r"\b(1|2|3|5|8|13|one|two|three|five|eight|thirteen)\s*(?:story\s*)?points?\b"
                  r"|\b(?:is|at|be|as)\s+(?:about\s+|around\s+|roughly\s+|probably\s+|a\s+)?(2|3|5|8|13|two|three|five|eight|thirteen)\b(?!\s*(?:%|percent|days?|hours?|minutes?|weeks?))")


def estimates_in(state: dict, text: str) -> list[tuple[int, int]]:
    """'Sign-up is about three points, sign-in is five' -> [(1, 3), (2, 5)]. Last value per story wins."""
    low, out, prev = text.lower(), {}, 0
    for m in _EST.finditer(low):
        sent = max(low.rfind(ch, 0, m.start()) for ch in ".;!?") + 1
        window = low[max(prev, sent):m.start()]
        prev = m.end()
        cid = find_card(state, window)
        if cid is not None:
            out[cid] = POINT_WORDS[m.group(1) or m.group(2)]
    return list(out.items())


def story_refs(state: dict, text: str) -> list[int]:
    """Which backlog stories someone named: 'story 2', '#3', 'number one', or the title itself."""
    low = text.lower()
    for w, n in NUM_WORDS.items():
        low = re.sub(rf"\b(story|stories|number|item|#)\s*{w}\b", rf"\1 {n}", low)
    pool = [c for c in state["cards"] if c["status"] in ("backlog", "todo") and c["type"] != "test"]
    ids = {c["id"] for c in pool}
    found = []
    for m in re.finditer(r"(?:#|\bstory\s*|\bnumber\s*|\bitem\s*)(\d+)", low):
        found.append(int(m.group(1)))
    # "stories 1 and 2" / "1, 2 and 3"
    for m in re.finditer(r"\bstories\s+([\d ,and&]+)", low):
        found += [int(x) for x in re.findall(r"\d+", m.group(1))]
    for c in pool:
        words = [w for w in re.findall(r"[a-z]+", c["title"].lower()) if w not in _STOP and len(w) > 2]
        if words and (c["title"].lower() in low or all(w in low for w in words[:3])):
            found.append(c["id"])
    out = []
    for i in found:
        if i in ids and i not in out:
            out.append(i)
    return out


def remove_from_sprint(state, card_id):
    """Take a story that hasn't started back out of the sprint (with its test ticket)."""
    card = get_card(state, card_id)
    if card["status"] != "todo":
        raise ValueError("Only cards still in To Do can go back to the backlog")
    card["status"] = "backlog"
    card["sprint"] = None
    t = test_ticket(state, card_id)
    if t and t["status"] != "done":
        t.update(status="backlog", sprint=None)
    return card


ALLOWED_MOVES = {
    "todo": {"inprogress", "backlog"},
    "inprogress": {"todo", "testing"},  # a test task is moved by the tester
    "testing": {"inprogress"},  # done only through a passed test
    "done": set(),
    "backlog": set(),
}


def move_card(state, card_id, to_status):
    card = get_card(state, card_id)
    if to_status not in STATUSES:
        raise ValueError(f"Unknown column {to_status}")
    if to_status == card["status"]:
        return card
    if to_status not in ALLOWED_MOVES.get(card["status"], set()):
        raise ValueError(
            f"Can't move from {STATUS_LABELS[card['status']]} to {STATUS_LABELS[to_status]}. "
            "Cards reach Done only by passing a test.")
    if to_status == "testing" and not card["dev_notes"].strip() and card.get("type") != "test":
        card["dev_notes"] = "Built and ready for testing."  # notes are optional
    if to_status == "backlog":
        return remove_from_sprint(state, card_id)
    card["status"] = to_status
    if to_status == "testing":
        card["work_left"] = 0
        card["test_result"] = ""
        card["test_left"] = max(1.0, points(card) / 2)
    return card


def add_discussion(state, card_id, line):
    """A short note of what was said about a story (one sentence per remark, last 8 kept)."""
    try:
        card = get_card(state, card_id)
    except ValueError:
        return
    head, _, said = line.partition(": ")
    first = re.split(r"(?<=[.!?])\s+", said.strip())[0] if said else line
    if len(first.split()) < 4:
        return  # "ok", "sounds good": nothing to keep
    first = first if len(first) <= 110 else first[:107] + "..."
    note = f"{head.split(' - ')[-1]}: {first}" if said else first
    notes = card.setdefault("discussion", [])
    if note not in notes:
        notes.append(note)
        del notes[:-8]


def add_dev_notes(state, card_id, notes):
    card = get_card(state, card_id)
    card["dev_notes"] = (notes or "").strip()[:1500]
    return card


def set_work_left(state, card_id, work_left):
    card = get_card(state, card_id)
    card["work_left"] = max(0.0, round(float(work_left), 2))
    return card


def set_test_left(state, card_id, test_left):
    card = get_card(state, card_id)
    card["test_left"] = max(0.0, round(float(test_left), 2))
    return card


def record_test_result(state, card_id, result, test_cases="", note=""):
    card = get_card(state, card_id)
    if card.get("type") == "test" and card.get("test_for"):
        # Tester's own test story: the result applies to the story it tests.
        story = get_card(state, card["test_for"])
        if test_cases:
            card["test_cases"] = test_cases.strip()[:1500]
        if story["status"] != "testing":
            raise ValueError(f"Story #{story['id']} isn't in Testing yet (the developer is still on it). "
                             "Keep writing test cases; run them when it reaches Testing.")
        return record_test_result(state, story["id"], result, card["test_cases"], note)
    if card["status"] != "testing":
        raise ValueError("Only cards in Testing can be tested")
    if result not in ("pass", "fail"):
        raise ValueError("Result must be pass or fail")
    ticket = test_ticket(state, card["id"])
    if test_cases:
        card["test_cases"] = test_cases.strip()[:1500]
    if not card["test_cases"].strip() and ticket and ticket["test_cases"].strip():
        card["test_cases"] = ticket["test_cases"]   # the cases written on the test ticket
    if not card["test_cases"].strip():
        raise ValueError("Write test cases before recording a result")
    card["test_result"] = result
    card["test_note"] = (note or "").strip()[:400]
    if result == "pass":
        card["status"] = "done"
    else:
        card["status"] = "inprogress"
        card["failed_once"] = True
        card["work_left"] = 1
    if ticket:
        ticket["test_cases"] = ticket["test_cases"] or card["test_cases"]
        ticket["test_result"] = result
        ticket["test_note"] = card["test_note"]
        ticket["status"] = "done" if result == "pass" else "testing"   # failed: re-test after the fix
    return card


def write_test_cases(state, card_id, test_cases):
    """Tester: write the test plan / test cases on a story or its test ticket."""
    card = get_card(state, card_id)
    card["test_cases"] = (test_cases or "").strip()[:1500]
    story = get_card(state, card["test_for"]) if card.get("test_for") else card
    if not story["test_cases"]:
        story["test_cases"] = card["test_cases"]
    if card.get("type") == "test" and card["status"] == "todo":
        card["status"] = "inprogress"
    return card


def log_bug(state, title, description="", related_card_id=None):
    sprint_card = None
    if related_card_id:
        sprint_card = get_card(state, related_card_id)
        if sprint_card.get("test_for"):           # a bug found from a test story belongs to the story it tests
            related_card_id = sprint_card["test_for"]
            sprint_card = get_card(state, related_card_id)
    title = re.sub(r"(?i)^\s*(bug\s*:\s*)+", "", title or "")
    bug = create_story(state, "Bug: " + title, description, 1, kind="bug",
                       related=related_card_id)
    if sprint_card and sprint_card.get("sprint"):
        bug["status"] = "todo"
        bug["sprint"] = sprint_card["sprint"]
        bug["work_left"] = 1
    return bug


def set_sprint_goal(state, goal):
    sprint = state["sprint"]
    state["sprint_goals"][str(sprint)] = (goal or "").strip()[:200]
    return state["sprint_goals"][str(sprint)]


def accept_story(state, card_id, accepted=True, note=""):
    card = get_card(state, card_id)
    if card["status"] != "done":
        raise ValueError("Only Done stories can be accepted or rejected")
    card["accepted"] = bool(accepted)
    card["test_note"] = (note or card["test_note"])[:400]
    if not accepted:
        card["status"] = "backlog"
        card["sprint"] = None
        card["work_left"] = 1
        card["test_result"] = ""
        card["priority"] = -1  # top of backlog
    return card


def board_summary(state: dict) -> dict:
    cer = current_ceremony(state)
    return {
        "product": state["product"],
        "ceremony": cer["label"],
        "sprint": state["sprint"],
        "sprint_state": state["sprint_state"],
        "sprint_goal": state["sprint_goals"].get(str(state["sprint"]), ""),
        "cards": [
            {k: c[k] for k in ("id", "type", "title", "points", "status", "sprint",
                               "test_result", "accepted")}
            | {"acceptance": c["acceptance"][:160]}
            for c in sorted(state["cards"], key=lambda c: c["priority"])
        ],
    }


# --------------------------------------------------------------------------
# Charts data
# --------------------------------------------------------------------------
def remaining_points(state, sprint):
    return sum(pts_of(state, c) for c in sprint_cards(state, sprint) if c["status"] != "done")


def scope_points(state):
    return sum(pts_of(state, c) for c in state["cards"] if c["type"] in ("story", "test"))


def _story_ok(state, c):
    """A test story counts once its story is done and not rejected."""
    if c["type"] != "test":
        return c["accepted"] is not False
    s = next((x for x in state["cards"] if x["id"] == c.get("test_for")), None)
    return bool(s) and s["accepted"] is not False


def done_points(state):
    return sum(pts_of(state, c) for c in state["cards"]
               if c["type"] in ("story", "test") and c["status"] == "done" and _story_ok(state, c))


def record_chart_point(state, sprint, step):
    """step = 'Plan', 'SU1', 'SU2' ... or 'Review'."""
    bd = state["burndown"].setdefault(str(sprint), [])
    bd[:] = [p for p in bd if p["label"] != step]
    bd.append({"label": step, "remaining": remaining_points(state, sprint)})
    label = f"S{sprint} {step}"
    state["burnup"] = [p for p in state["burnup"] if p["label"] != label]
    state["burnup"].append({"label": label, "scope": scope_points(state),
                            "done": done_points(state)})


# --------------------------------------------------------------------------
# What the USER must do before the next ceremony (the "gate")
# --------------------------------------------------------------------------
def user_tasks(state: dict) -> list[dict]:
    """Your to-dos from the latest meeting. Not a hard gate: if you skip them,
    teammates bring it up in the next meeting (like a real team would)."""
    cer = current_ceremony(state)
    role = state["role"]
    m = state["meetings"][cer["index"]] if state["meetings"] else {}
    tasks = []
    if m.get("invalid"):
        return tasks

    def add(text, done):
        tasks.append({"text": text, "done": bool(done)})

    t, s = cer["type"], state["sprint"]
    if t == "kickoff" and role == "po":
        ok = [c for c in state["cards"] if c["type"] == "story" and c["acceptance"].strip()]
        add(f"Add at least 3 user stories with acceptance criteria ({len(ok)}/3)", len(ok) >= 3)
    if t == "planning":
        if role == "po":
            n = len(sprint_cards(state, s))
            add(f"Pull stories into Sprint {s} (Backlog tab) before ending the meeting ({n} pulled)",
                n >= 1 or state["sprint_state"] != "unplanned")
        if role == "sm":
            add("Set the sprint goal (Agenda panel)", state["sprint_goals"].get(str(s)))
        if role == "dev":
            top = backlog_cards(state)[:5] + sprint_cards(state, s)
            missing = [c for c in top if not points(c)]
            add(f"Estimate the top stories ({len(missing)} left)", not missing)
    if t == "daily":
        if role == "dev":
            open_cards = [c for c in sprint_cards(state, s) if c["status"] in ("todo", "inprogress")]
            moved = [c for c in sprint_cards(state, s) if c["touched"] == cer["index"]]
            if open_cards or moved:
                add("Work on your cards: move one forward or add pseudo-code notes", moved)
        if role == "tester":
            waiting = [c for c in sprint_cards(state, s) if c["status"] == "testing"]
            add(f"Test every card in Testing ({len(waiting)} waiting)", not waiting)
    if t == "refinement":
        if role == "po":
            missing = [c for c in backlog_cards(state) if not c["acceptance"].strip()]
            add(f"Every backlog story has acceptance criteria ({len(missing)} missing)", not missing)
        if role == "dev":
            missing = [c for c in backlog_cards(state) if not points(c)]
            add(f"Estimate all backlog stories ({len(missing)} left)", not missing)
    if t == "review":
        if role == "po":
            pending = [c for c in sprint_cards(state, s)
                       if c["status"] == "done" and c["accepted"] is None]
            add(f"Accept or reject each finished story before ending ({len(pending)} pending)",
                not pending)
        if role == "tester":
            waiting = [c for c in sprint_cards(state, s) if c["status"] == "testing"]
            add(f"Finish testing cards still in Testing ({len(waiting)} waiting)", not waiting)
    return tasks


# --------------------------------------------------------------------------
# Work simulation: decides what the AI developer / tester got done.
# It returns ACTIONS; the app executes them through MCP tools.
# --------------------------------------------------------------------------
DEV_NOTE_TEMPLATE = (
    "Pseudo-code for '{title}':\n"
    "1. Build the screen / form for the story\n"
    "2. Validate input on the client\n"
    "3. Call the API and handle success + error\n"
    "4. Unit tests for the happy path and errors"
)
TEST_CASE_TEMPLATE = (
    "TC1 Happy path - {ac}. Data: typical valid input (e.g. 'Alex', 2 items, tomorrow 10:00). Expected: works.\n"
    "TC2 Bad input - empty or invalid value. Data: '' and 'abc!!'. Expected: clear error, nothing saved.\n"
    "TC3 Edge case - smallest/largest allowed value. Data: 0, 1 and 999. Expected: handled, no crash.\n"
    "TC4 Small phone screen (375 px). Expected: everything readable and tappable."
)


def plan_work(state: dict, days: int, factor: float) -> tuple[list, dict]:
    """Return (actions, report). Each action = (agent_role, tool, args)."""
    sim = copy.deepcopy(state)
    sprint = state["sprint"]
    actions = []
    report = {"factor": round(factor, 2), "dev": [], "tester": []}
    role = state["role"]

    def act(agent, tool, args, fn):
        actions.append((agent, tool, args))
        fn()

    # Nobody does YOUR job: if you play the developer or tester, your cards only move when you move them.
    for _ in range(days):
        # ---- AI developer ----
        if role != "dev":
            cap = DEV_POINTS_PER_DAY * factor
            cards = sorted(
                [c for c in sim["cards"] if c.get("sprint") == sprint
                 and c["status"] in ("inprogress", "todo") and not is_test_task(c)],
                key=lambda c: (c["status"] != "inprogress", c["priority"]))
            for c in cards:
                if cap <= 0.01:
                    break
                if c["status"] == "todo":
                    wip = len([x for x in sim["cards"] if x.get("sprint") == sprint   # the developer's own cards only
                               and x["status"] == "inprogress" and not is_test_task(x)])
                    if wip >= 2:
                        break
                    notes = DEV_NOTE_TEMPLATE.format(title=c["title"])
                    act("dev", "add_dev_notes", {"card_id": c["id"], "notes": notes},
                        lambda c=c, n=notes: c.update(dev_notes=n))
                    act("dev", "move_card", {"card_id": c["id"], "to_status": "inprogress"},
                        lambda c=c: c.update(status="inprogress"))
                    report["dev"].append(f"started #{c['id']} {c['title']}")
                left = c["work_left"] if c["work_left"] is not None else points(c) or 3
                take = min(cap, left)
                cap -= take
                left = round(left - take, 2)
                if left <= 0.01:
                    act("dev", "move_card", {"card_id": c["id"], "to_status": "testing"},
                        lambda c=c: c.update(status="testing", work_left=0,
                                             test_left=max(1.0, points(c) / 2), test_result=""))
                    report["dev"].append(f"finished #{c['id']} {c['title']} -> Testing")
                else:
                    act("dev", "set_work_left", {"card_id": c["id"], "work_left": left},
                        lambda c=c, l=left: c.update(work_left=l))
                    pct = int(100 * (1 - left / max(1, points(c) or 3)))
                    report["dev"].append(f"#{c['id']} {c['title']} about {max(pct, 5)}% done")
        # ---- AI tester ----
        if role != "tester":
            cap = TEST_POINTS_PER_DAY * factor
            # Testing tasks (e.g. "Test the app in Chrome") belong to the tester, not the developer.
            for c in sorted([c for c in sim["cards"] if c.get("sprint") == sprint and c["type"] == "test"
                             and c["status"] in ("todo", "inprogress") and not c["test_cases"]],
                            key=lambda c: c["priority"]):
                if cap <= 0.01:
                    break
                story = next((x for x in sim["cards"] if x["id"] == c.get("test_for")), None)
                cases = TEST_CASE_TEMPLATE.format(ac=((story or c)["acceptance"] or c["title"]).split(".")[0][:110])
                cap -= 0.5
                act("tester", "write_test_cases", {"card_id": c["id"], "test_cases": cases},
                    lambda c=c, cs=cases, st=story: (c.update(test_cases=cs, status="inprogress"),
                                                      st and not st["test_cases"] and st.update(test_cases=cs)))
                report["tester"].append(f"wrote test cases TC1-TC4 (with sample data) for #{c['id']} {c['title']}")
            for c in sorted([c for c in sim["cards"] if c.get("sprint") == sprint and is_test_task(c)
                             and c["type"] != "test" and c["status"] in ("todo", "inprogress")],
                            key=lambda c: c["priority"]):
                if cap <= 0.01:
                    break
                if c["status"] == "todo":
                    act("tester", "move_card", {"card_id": c["id"], "to_status": "inprogress"},
                        lambda c=c: c.update(status="inprogress"))
                    report["tester"].append(f"started test task #{c['id']} {c['title']}")
                left = c["work_left"] if c["work_left"] is not None else points(c) or 2
                take = min(cap, left)
                cap -= take
                left = round(left - take, 2)
                if left > 0.01:
                    act("tester", "set_work_left", {"card_id": c["id"], "work_left": left},
                        lambda c=c, l=left: c.update(work_left=l))
                    continue
                cases = TEST_CASE_TEMPLATE.format(ac=(c["acceptance"] or c["title"])[:120])
                notes = "Test task: run the test cases and report results (no code)."
                act("tester", "add_dev_notes", {"card_id": c["id"], "notes": notes},
                    lambda c=c: c.update(dev_notes=notes))
                act("tester", "move_card", {"card_id": c["id"], "to_status": "testing"},
                    lambda c=c: c.update(status="testing", work_left=0, test_left=0))
                act("tester", "record_test_result",
                    {"card_id": c["id"], "result": "pass", "test_cases": cases, "note": "Test run complete."},
                    lambda c=c: c.update(status="done", test_result="pass", test_cases=cases))
                report["tester"].append(f"test task #{c['id']} {c['title']} done")
            for c in sorted([c for c in sim["cards"] if c.get("sprint") == sprint and c["type"] != "test"
                             and c["status"] == "testing"], key=lambda c: c["priority"]):
                if cap <= 0.01:
                    break
                left = c["test_left"] if c["test_left"] is not None else 1
                take = min(cap, left)
                cap -= take
                left = round(left - take, 2)
                if left > 0.01:
                    act("tester", "set_test_left", {"card_id": c["id"], "test_left": left},
                        lambda c=c, l=left: c.update(test_left=l))
                    report["tester"].append(f"testing #{c['id']} {c['title']}")
                    continue
                cases = TEST_CASE_TEMPLATE.format(ac=(c["acceptance"] or c["title"])[:120])
                # One realistic failure per project: the 2nd story fails its first test.
                fail = (c["type"] == "story" and not c["failed_once"] and c["id"] == 2)
                if fail:
                    note = "TC2 failed: no clear error for empty input."
                    act("tester", "record_test_result",
                        {"card_id": c["id"], "result": "fail", "test_cases": cases, "note": note},
                        lambda c=c: c.update(status="inprogress", failed_once=True, work_left=1,
                                             test_result="fail", test_cases=cases))
                    report["tester"].append(f"ran TC1-TC4 on #{c['id']} {c['title']}: {note} Back to the developer")
                else:
                    act("tester", "record_test_result",
                        {"card_id": c["id"], "result": "pass", "test_cases": cases,
                         "note": "All test cases passed."},
                        lambda c=c: (c.update(status="done", test_result="pass", test_cases=cases),
                                     [t.update(status="done", test_result="pass") for t in sim["cards"]
                                      if t.get("test_for") == c["id"]]))
                    report["tester"].append(f"ran TC1-TC4 on #{c['id']} {c['title']}: all passed -> Done")
    return actions, report


def advance_factor(state: dict) -> float:
    if REAL_SECONDS_PER_DAY <= 0:
        return 1.0
    elapsed = time.time() - state.get("last_work_ts", time.time())
    return max(0.1, min(1.0, elapsed / REAL_SECONDS_PER_DAY))


# --------------------------------------------------------------------------
# Closing a meeting
# --------------------------------------------------------------------------
def close_meeting_actions(state: dict) -> list:
    """Actions the AI team takes when a meeting ends (executed via MCP)."""
    cer = current_ceremony(state)
    role = state["role"]
    s = state["sprint"]
    actions = []
    t = cer["type"]
    if t == "kickoff" and not state["cards"]:
        # Starter backlog: drafted from the vision (AI mode) or generic account stories.
        # A Product Owner player can edit or replace them in the Backlog tab.
        for title, ac in state.get("drafted") or DEFAULT_BACKLOG:
            actions.append(("po", "create_story", {"title": title, "acceptance_criteria": ac}))
    if t in ("planning", "refinement") and role != "dev":
        pool = backlog_cards(state) + (sprint_stories(state, s) if t == "planning" else [])
        parked = {i for mt in state["meetings"] for i in (mt.get("parked") or [])}   # parked by the PO: stays unestimated
        for c in pool[:8]:
            if not points(c) and c["id"] not in parked:
                actions.append(("dev", "estimate_story",
                                {"card_id": c["id"], "points": estimate_for(c["title"]),
                                 "estimated_by": f"{name('dev')} (Developer), quick estimate after the meeting"}))
    new = growth_story(state)
    if t == "refinement" and role != "po" and new:
        actions.append(("po", "create_story", {"title": new[0], "acceptance_criteria": new[1],
                                               "points": estimate_for(new[0])}))
    if t == "planning" and state["sprint_state"] == "unplanned":
        talked = [x for x in (state["meetings"][-1].get("messages") or []) if x["who"] != "system"]
        if role != "sm" and talked and not state["sprint_goals"].get(str(s)):
            first = backlog_cards(state)[:1] or sprint_cards(state, s)[:1]
            goal = (f"Users can use: {first[0]['title'].lower()}" if first
                    else "Finish the committed stories")
            actions.append(("sm", "set_sprint_goal", {"goal": goal}))
        picked = state.get("planned_ids", {}).get(str(s))
        if not talked:
            pass   # ended before anything was said: nothing is picked, the sprint isn't started
        elif picked and not sprint_cards(state, s):
            # Exactly the stories the Product Owner named in the meeting ("story 1 and 2").
            total = 0
            for c in sorted((c for c in backlog_cards(state) if c["id"] in picked), key=lambda c: c["priority"]):
                if total + with_test(state, c) <= capacity(state):
                    actions.append(("po", "add_to_sprint", {"card_id": c["id"]}))
                    total += with_test(state, c)
        elif not picked and not sprint_cards(state, s):
            # Nobody named stories: the team pulls from the top of the backlog up to capacity.
            total = sum(pts_of(state, c) for c in sprint_cards(state, s))
            for c in backlog_cards(state):
                p = with_test(state, c)
                if total + p > capacity(state) and total > 0:
                    continue
                actions.append(("po", "add_to_sprint", {"card_id": c["id"]}))
                total += p
    if t == "retro" and role != "po" and s < MAX_SPRINTS:
        # New requests keep coming: the PO adds next sprint's request (and any missed earlier) to the backlog.
        for sp in range(1, s + 2):
            g = growth_story(state, sp)
            if g and not any(a[2].get("title") == g[0] for a in actions):
                actions.append(("po", "create_story", {"title": g[0], "acceptance_criteria": g[1]}))
    if t == "review":
        for c in sprint_stories(state, s):
            if c["status"] == "done" and c["accepted"] is None:
                actions.append(("po", "accept_story", {"card_id": c["id"], "accepted": True,
                                                        "note": "Meets the acceptance criteria."}))
    return actions


def after_meeting(state: dict) -> str | None:
    """State changes after a meeting ends. Returns a note to say out loud, if any."""
    cer = current_ceremony(state)
    t, s = cer["type"], state["sprint"]
    if t == "kickoff":
        state["kickoff_done"] = True
    if t == "planning" and state["sprint_state"] == "unplanned":
        cards = sprint_stories(state, s)
        for c in cards:
            ensure_test_ticket(state, c)
        if not cards:
            return ("We can't start a sprint with nothing in it. Pull stories in from the backlog "
                    "and run Sprint Planning again.")
        for c in cards:
            if not points(c):
                c["points"] = estimate_for(c["title"])
            if c["work_left"] is None:
                c["work_left"] = points(c)
        if not state["sprint_goals"].get(str(s)):
            state["sprint_goals"][str(s)] = "Finish the committed stories"
        state["sprint_state"] = "active"
        state["standups"] = 0
        state["last_work_ts"] = time.time()
        record_chart_point(state, s, "Plan")
        return f"Sprint {s} has started. Next: daily stand-ups."
    if t == "review" and state["sprint_state"] == "active":
        accepted = {c["id"] for c in sprint_stories(state, s) if c["status"] == "done" and c["accepted"]}
        state["velocity"][str(s)] = sum(
            pts_of(state, c) for c in sprint_cards(state, s) if c["status"] == "done"
            and (c["id"] in accepted or c.get("test_for") in accepted))
        state["sprint_state"] = "reviewed"
    if t == "retro" and state["sprint_state"] == "reviewed":
        last = s >= MAX_SPRINTS
        for c in sprint_stories(state, s):
            if c["status"] == "done":
                continue
            t = test_ticket(state, c["id"])
            if last:   # no next sprint: back to the backlog
                if t:
                    t.update(status="backlog", sprint=None, test_result="")
                c.update(status="backlog", sprint=None, priority=-1 if c["type"] == "story" else 0)
                state["open_items"].append(f"#{c['id']} {c['title']} was not finished and went back to the backlog.")
                continue
            # Not finished: it moves on to the next sprint automatically, with the progress it has.
            c.update(sprint=s + 1, carried_from=s, picked_live=False, added_mid_sprint=False)
            if t and t["status"] != "done":
                t.update(sprint=s + 1)
            state["open_items"].append(f"#{c['id']} {c['title']} was not finished in Sprint {s} and moved to Sprint {s + 1}.")
        if last:
            state["complete"] = True
            return "That was our last sprint. Great work, everyone!"
        state["sprint"] = s + 1
        state["sprint_state"] = "unplanned"
        state["standups"] = 0
        return f"Sprint {s} is closed. Next: Sprint Planning for Sprint {s + 1}."
    return None


def note_missed_items(state: dict) -> None:
    """Continuity: things the user skipped get raised in the next meeting."""
    cer = current_ceremony(state)
    if not state["meetings"]:
        return
    meeting = state["meetings"][cer["index"]]
    if meeting.get("invalid"):
        return
    if meeting.get("user_chars", 0) < 25:
        state["open_items"].append(
            f"The {ROLES[state['role']]['title']} said very little in the last {MEETING_SHORT[cer['type']].lower()}.")
    for t in user_tasks(state):
        if not t["done"]:
            state["open_items"].append(
                f"The {ROLES[state['role']]['title']} didn't finish: {t['text'].split(' (')[0].lower()}.")
