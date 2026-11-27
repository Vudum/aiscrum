"""
app.py - the app server (Flask).

It serves the web page, keeps each visitor's project, runs the ceremony
flow, and acts as the MCP client host for the AI agents.

Run locally:
    pip install -r requirements.txt
    python app.py          -> open http://localhost:5050
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from flask import Flask, jsonify, request, send_from_directory, session  # noqa: E402

import agents  # noqa: E402
import board  # noqa: E402
import guardrails  # noqa: E402
import agendas  # noqa: E402
import kickoff  # noqa: E402
import mcp_client  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")

_key_file = board.DATA_DIR / "secret.key"
_key_file.parent.mkdir(parents=True, exist_ok=True)
if not _key_file.exists():
    _key_file.write_text(secrets.token_hex(32))
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or _key_file.read_text().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

LAST_AI_ERROR = {"text": ""}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class UserError(Exception):
    pass


@app.errorhandler(UserError)
@app.errorhandler(ValueError)
def _user_error(e):
    return jsonify({"error": str(e)}), 400


def _path() -> Path:
    pid = session.get("project_id")
    if not pid:
        raise UserError("No project yet - pick a role to start.")
    p = board.project_path(pid)
    if not p.exists():
        session.pop("project_id", None)
        raise UserError("Project not found - please start again.")
    return p


def _meeting(state: dict) -> dict:
    """The latest meeting record (an empty placeholder before the kickoff)."""
    if not state["meetings"]:
        return {"messages": [], "opened": False, "ended": False, "type": "kickoff"}
    return state["meetings"][-1]


def _msg(role: str, text: str) -> dict:
    if role == "user":
        return {"who": "user", "name": "You", "title": "", "text": text, "ts": time.time()}  # named in meeting_say
    if role == "system":
        return {"who": "system", "name": "", "title": "", "text": text, "ts": time.time()}
    sp = agents.speaker(role)
    return {"who": role, "name": sp["name"], "title": sp["title"], "text": text, "ts": time.time()}


def _log_calls(state: dict, calls: list[dict]) -> None:
    for c in calls:
        agent = agents.speaker(c["agent"])
        board.log_mcp(state, f"{agent['name']} ({agent['title']})", c["tool"], c["args"], c["result"])


def _run_team(path: Path, jobs: list[tuple[str, str, str]], extra_actions: list | None = None):
    """
    Let AI teammates speak, one after another, inside ONE MCP session.
    jobs = [(role, kind, text)]  kind: open | reply | close
    Returns (messages, mcp_calls).
    """
    async def go():
        out = []
        async with mcp_client.board_session(str(path)) as client:
            for role, kind, text in jobs:
                state = board.load(path)
                transcript = _meeting(state)["messages"] + out
                reply = None
                if agents.ai_enabled():
                    try:
                        instruction = {
                            "open": ("The ceremony is starting. Say your part, like in a real meeting. "
                                     "If a teammate just said something you'd build on or push back on, "
                                     "talk to them by name."),
                            "reply": (f'The human just said: "{text}". Respond from your role. '
                                      "If a teammate already answered, react to them by name instead of repeating."),
                            "close": "Close the meeting: summarise decisions in 2 sentences and say what's next.",
                        }[kind]
                        reply = await agents.ai_turn(client, state, role, instruction, transcript)
                        LAST_AI_ERROR["text"] = ""
                    except guardrails.BudgetExceeded:
                        reply = None
                    except Exception as e:  # bad key, model name, network...
                        LAST_AI_ERROR["text"] = f"{type(e).__name__}: {str(e)[:200]}"
                        reply = None
                if not reply:
                    reply = {"open": lambda: agents.mock_opening(state, role),
                             "reply": lambda: agents.mock_reply(state, role, text),
                             "close": lambda: agents.mock_closing(state)}[kind]()
                out.append(_msg(role, reply))
            for agent, tool, args in (extra_actions or []):
                await client.call(agent, tool, args)
            return out, client.calls

    return asyncio.run(go())


def _speak_one(path: Path, role: str, instruction: str, fallback):
    """One AI teammate says one thing (Claude if a key is set, else the script)."""
    # A spoken line uses no board tools, so no board server is started for it (saves time per turn).
    async def go():
        state = board.load(path)
        transcript = _meeting(state)["messages"]
        reply = None
        ready = _SPEC.pop(str(path), None)
        if ready and ready[:3] == (role, instruction, len(transcript)):
            reply = ready[3]          # written while the previous speaker was talking
        elif agents.ai_enabled():
            try:
                reply = await agents.ai_turn(None, state, role, instruction, transcript)
                LAST_AI_ERROR["text"] = ""
            except guardrails.BudgetExceeded:
                reply = None
            except Exception as e:  # bad key, model name, network...
                LAST_AI_ERROR["text"] = f"{type(e).__name__}: {str(e)[:200]}"
        if not reply:
            reply = fallback(state)
        return _msg(role, reply), []
    return asyncio.run(go())


_KEEPERS: dict[str, threading.Thread] = {}


def _keep_board_later(path: Path) -> None:
    """Run the Board Keeper in the background so the speaker's line reaches the browser right away.
    The next change to this project waits for it (see _wait_keeper), so two writers never overlap."""
    t = threading.Thread(target=_keep_board_safe, args=(path,), daemon=True)
    _KEEPERS[str(path)] = t
    t.start()


def _keep_board_safe(path: Path) -> None:
    try:
        _keep_board(path)
    except Exception as e:  # noqa: BLE001
        agents.log_ai("keeper_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
    _prepare_next_later(path)   # board is up to date: write the next line while this one is spoken


@app.before_request
def _wait_keeper():
    """Before anything changes the project, let a running Board Keeper finish its write."""
    if request.method != "POST" or request.path.endswith("/api/tts"):
        return
    pid = session.get("project_id")
    t = _KEEPERS.get(str(board.project_path(pid))) if pid else None
    if t and t.is_alive():
        t.join(timeout=30)


def _keep_board(path: Path) -> None:
    """Board Keeper agent: after every message it turns what was agreed into board changes (via MCP).
    If the board refuses a change (e.g. over capacity), the meeting hears why."""
    state = board.load(path)
    m = _meeting(state)
    if not agents.ai_enabled() or not m.get("opened") or m.get("ended") or not m.get("messages") \
            or m.get("type") == "kickoff":   # the backlog is drafted from the vision right after the kickoff
        return

    async def go():
        async with mcp_client.board_session(str(path)) as client:
            try:
                note = await agents.ai_board_keeper(client, board.load(path), m["messages"])
            except guardrails.BudgetExceeded:
                note = ""
            except Exception as e:  # noqa: BLE001
                agents.log_ai("keeper_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
                note = ""
            return note, client.calls

    note, calls = asyncio.run(go())
    if not calls and not note:
        return
    state = board.load(path)
    for c in calls:
        board.log_mcp(state, "Board Keeper", c["tool"], c["args"], c["result"])
    # Sprint scope changes are said out loud (from the board, not the AI), so everyone works from the same facts.
    moved = {"add_to_sprint": "added to the sprint", "remove_from_sprint": "taken out of the sprint"}
    ok = [c for c in calls if c["result"].startswith("OK")]
    done = [f"story {c['args'].get('card_id')} {moved[c['tool']]}"
            + (" (its testing waits for the next sprint)" if c["args"].get("without_test") else "")
            for c in ok if c["tool"] in moved]
    said = {"split_story": lambda c: "split: " + c["result"][4:],
            "create_story": lambda c: f"new story in the backlog: {c['result'][4:].split(' -> ')[0]}",
            "log_bug": lambda c: f"bug logged: {c['result'][4:].split(' -> ')[0]}",
            "accept_story": lambda c: f"story {c['args'].get('card_id')} "
                                      + ("accepted" if c["args"].get("accepted", True) else
                                         "rejected, back to the backlog")}
    other = [said[c["tool"]](c) for c in ok if c["tool"] in said]
    if done or other:
        s = state["sprint"]
        line = _msg("system", "Board: " + "; ".join(other + done) + "."
                    + (f" Sprint {s} now has {board.committed(state, s)} of {board.capacity(state)} points."
                       if done else ""))
        line["kind"] = "board"
        _meeting(state)["messages"].append(line)
    if note:
        msg = _msg("system", "Board: " + note)
        msg["kind"] = "board"
        _meeting(state)["messages"].append(msg)
    board.save(path, state)


def _keeper_on() -> bool:
    """With AI on, the Board Keeper agent updates the board; the keyword rules are only for Practice mode."""
    return agents.ai_enabled()


def _minutes(state: dict) -> list[str]:
    m = _meeting(state)
    transcript = m["messages"]
    if not [x for x in transcript if x["who"] != "system"]:
        return ["The meeting was ended before anything was discussed. Nothing changed on the board."]
    if agents.ai_enabled():
        try:
            lines = agents.ai_minutes(state, transcript, board.CEREMONY_INFO[m["type"]])
            if lines:
                return lines
        except Exception as e:  # budget, network... fall back to the script
            agents.log_ai("minutes_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
    return agendas.summary_script(state, m["type"], transcript)[1]


def _draft_from_vision(path: Path) -> None:
    """After the kickoff: the backlog is drafted from the product vision (AI), so it fits the product."""
    state = board.load(path)
    if _meeting(state).get("type") != "kickoff" or state["cards"] or not agents.ai_enabled():
        return
    po_who = "user" if state["role"] == "po" else "po"
    po_said = "\n".join(m["text"] for m in _meeting(state)["messages"] if m["who"] == po_who)[:3000]
    if state["role"] == "po" and po_said:
        state["product"]["vision"] = po_said[:400]
    try:
        out = agents.ai_product_and_stories(state, need_name=state["product"]["name"] == board.NEW_PRODUCT,
                                            po_said=po_said)
    except Exception as e:
        agents.log_ai("draft_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        out = None
    if not out or not out.get("stories"):
        board.save(path, state)
        return
    if out.get("name") and state["product"]["name"] == board.NEW_PRODUCT:
        state["product"]["name"] = str(out["name"])[:60]
    if out.get("vision") and state["role"] == "po":
        state["product"]["vision"] = str(out["vision"])[:400]
    stories = [(str(s.get("title", ""))[:80], str(s.get("acceptance", ""))[:400])
               for s in out["stories"] if s.get("title")]
    state["drafted"] = stories[:3]
    state["growth"] = stories[3:6]
    board.save(path, state)


def _fold_discussion(path: Path) -> None:
    """After refinement: what was agreed about each story is written into its acceptance criteria."""
    state = board.load(path)
    m = _meeting(state)
    if m.get("type") != "refinement" or not agents.ai_enabled():
        return
    cards = [c for c in state["cards"] if c["id"] in m.get("discussed", []) and c.get("discussion")]
    if not cards:
        return
    try:
        upd = agents.ai_update_stories(state, cards)
    except Exception as e:  # noqa: BLE001
        agents.log_ai("fold_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        return
    for c in cards:
        new = upd.get(str(c["id"]))
        if isinstance(new, str) and new.strip():
            c["acceptance"] = new.strip()[:600]
            board.log_mcp(state, "po", "update_story", {"card_id": c["id"]}, "Acceptance criteria updated from the discussion")
    board.save(path, state)


PROMISE = re.compile(r"(?i)\b(i'll|i will|i'm going to|let me|i can)\b[^.?!]{0,80}\b(re-?estimate|estimate|confirm|check|"
                     r"send|update|add|write|look into|get back|share|clarify|finish)\b"
                     r"|\b(i'll have it|by (early )?(this )?(afternoon|tomorrow|end of day)|within the hour|later today)\b")


def _keep_promise(state: dict, m: dict, role: str, msg: dict) -> None:
    """A teammate says 'I'll re-estimate it' or 'I'll confirm the rules': it's done on the spot,
    and they say the result straight away. No promise is left hanging for the next meeting."""
    if not agents.ai_enabled() or _keeper_on() or role not in board.ai_roles(state) or not PROMISE.search(msg["text"]):
        return    # with the Board Keeper on, board changes come only from it (no "Done: ... it's on the board" claims)
    try:
        out = agents.ai_followups(state, m["messages"][-6:], only_role=role)
    except Exception as e:  # noqa: BLE001
        agents.log_ai("promise_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        return
    done = []
    for e in ([] if _keeper_on() else out.get("estimates") or []):   # the Board Keeper owns estimates
        try:
            cid, pts = int(e.get("card_id")), int(e.get("points"))
            card = board.get_card(state, cid)
        except (TypeError, ValueError):
            continue
        if role == "dev" and pts in (1, 2, 3, 5, 8, 13):
            board.estimate_story(state, cid, pts, f"{board.name(role)}, {m.get('label', m['type'])}")
            board.log_mcp(state, f"{board.name(role)} ({board.ROLES[role]['title']})", "estimate_story",
                          {"card_id": cid, "points": pts}, f"#{cid} '{card['title']}' -> {pts} pts")
            done.append(f"story {cid}, {card['title']}, is {pts} points")
    for d in out.get("done") or []:
        text = " ".join(str(d.get("text", "")).split())[:200]
        if d.get("role") == role and text:
            done.append(text.rstrip("."))
            try:
                board.add_discussion(state, int(d.get("card_id")), f"{m.get('label', m['type'])} - {board.name(role)}: {text}")
            except (TypeError, ValueError):
                pass
    if done:
        reply = _msg(role, "Done: " + "; ".join(done[:2]) + ". It's on the board.")
        reply["kind"] = "followup"
        m["messages"].append(reply)


def _follow_ups(path: Path) -> None:
    """Promises AI teammates made ('I'll re-estimate', 'I'll confirm the rules') are done right after the
    meeting, so nobody turns up to the next one still owing them."""
    state = board.load(path)
    m = _meeting(state)
    if not agents.ai_enabled() or not m.get("messages"):
        return
    try:
        out = agents.ai_followups(state, m["messages"])
    except Exception as e:  # noqa: BLE001
        agents.log_ai("followups_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        return
    ai = board.ai_roles(state)
    ids = {c["id"] for c in state["cards"]}
    actions, done = [], []
    for e in ([] if _keeper_on() else out.get("estimates") or []):   # the Board Keeper owns estimates
        try:
            cid, pts = int(e.get("card_id")), int(e.get("points"))
        except (TypeError, ValueError):
            continue
        if "dev" in ai and cid in ids and pts in (1, 2, 3, 5, 8, 13):
            card = board.get_card(state, cid)
            if card.get("points") != pts:
                actions.append(("dev", "estimate_story", {"card_id": cid, "points": pts,
                                                          "estimated_by": f"{board.name('dev')} (follow-up)"}))
                done.append(f"{board.name('dev')} re-estimated story {cid} '{card['title']}': {pts} points.")
    for d in out.get("done") or []:
        role, text = d.get("role"), " ".join(str(d.get("text", "")).split())[:220]
        if role in ai and text:
            done.append(f"{board.name(role)}: {text}")
            try:
                board.add_discussion(state, int(d.get("card_id")), f"Follow-up - {board.name(role)}: {text}")
            except (TypeError, ValueError):
                pass
    state["followups"] = done[:6]
    if done and m.get("summary") is not None:
        m["summary"] += [f"Done after the meeting: {t}" for t in done[:4]]
    board.save(path, state)
    if actions:
        calls = mcp_client.run_actions(str(path), actions)
        state = board.load(path)
        _log_calls(state, calls)
        board.save(path, state)


def _finish(path: Path) -> dict:
    """Close the meeting: the team's follow-up actions (via MCP) and bookkeeping."""
    _draft_from_vision(path)
    _fold_discussion(path)
    _follow_ups(path)
    state = board.load(path)
    actions = board.close_meeting_actions(state)
    if _keeper_on() and _meeting(state).get("type") == "refinement":
        # Estimates come from the developer in the meeting, not from a guess afterwards.
        actions = [a for a in actions if not (a[1] == "estimate_story" and "quick estimate" in a[2].get("estimated_by", ""))]
    calls = mcp_client.run_actions(str(path), actions)
    state = board.load(path)
    _log_calls(state, calls)
    note = board.after_meeting(state)
    m = _meeting(state)
    if note and note.startswith("We can't") and m.get("summary") is not None:
        m["summary"].append(note)
    m["ended"] = True
    m["awaiting"] = None
    m["queue"] = []
    m["hands"] = []
    board.save(path, state)
    return state


def _menu(state: dict, suggested: str) -> list[dict]:
    items = []
    for t in ["kickoff"] + board.MEETING_TYPES:
        if state["complete"]:
            ok, reason = False, "The project is complete."
        elif not state["kickoff_done"]:
            ok, reason = t == "kickoff", "Start with the kickoff."
        elif t == "kickoff":
            ok, reason = False, "Done"
        else:
            ok, _, reason = board.check_meeting(state, t)
        items.append({"type": t, "label": board.MEETING_SHORT[t], "enabled": ok, "reason": reason,
                      "suggested": ok and t == suggested, "done": t == "kickoff" and state["kickoff_done"]})
    return items


def _view(state: dict) -> dict:
    cer = board.current_ceremony(state)
    m = _meeting(state)
    s = state["sprint"]
    spent = guardrails.spent_today()
    budget_out = agents.ai_enabled() and spent >= guardrails.DAILY_BUDGET_USD
    team = [{"role": r, **agents.speaker(r), "color": board.ROLES[r]["color"],
             "is_user": r == state["role"]} for r in board.ROLES]
    live = m.get("opened") and not m.get("ended")
    suggested = board.suggested_meeting(state)
    types = ["kickoff"] if not state["kickoff_done"] else board.MEETING_TYPES
    return {
        "project": {k: state[k] for k in ("id", "role", "product", "sprint_goals", "velocity",
                                          "complete", "open_items", "kickoff_done")},
        "user_role_title": board.ROLES[state["role"]]["title"],
        "player_name": state.get("player_name", ""),
        "team": team,
        "ceremony": cer,
        "meeting": {"messages": m.get("messages", []), "opened": m.get("opened", False),
                    "ended": m.get("ended", False), "invalid": m.get("invalid", False),
                    "live": bool(live), "count": len(state["meetings"]),
                    "type": m.get("type"), "awaiting": m.get("awaiting"),
                    "hands": m.get("hands", []), "summary": m.get("summary"),
                    "agenda_left": (len(m.get("agenda", [])) - m.get("step", 0)) if m.get("agenda") else None},
        # Only meetings that make sense right now can be started.
        "menu": _menu(state, suggested),
        "sprint": {"number": s, "state": state["sprint_state"], "standups": state["standups"],
                   "max": board.MAX_SPRINTS},
        "tasks": board.user_tasks(state),
        # Stand-up memory: what everyone said last time.
        "last_standup": ([{"name": board.player(state) if r == state["role"] else board.name(r),
                           "title": board.ROLES[r]["title"], "text": t}
                          for r, t in agendas.last_standup(state, len(state["meetings"]) - 1).items()]
                         if m.get("type") == "daily" else []),
        "coach": agents.coaching(state),
        "cards": sorted(state["cards"], key=lambda c: c["priority"]),
        "statuses": board.STATUS_LABELS,
        "charts": {
            "burndown": state["burndown"], "burnup": state["burnup"], "velocity": state["velocity"],
            "capacity": board.capacity(state),
            "capacity_note": board.capacity_note(state),
        },
        "current_sprint": s,
        "sprint_goal": state["sprint_goals"].get(str(s), ""),
        # Finished meetings, so past sprints can be looked at again (read-only).
        "history": [{"label": x.get("label") or x["type"], "type": x["type"], "sprint": x.get("sprint", 1),
                     "summary": x.get("summary") or [],
                     "messages": [{"name": y.get("name") or ("Board" if y.get("who") == "system" else board.player(state)),
                                   "text": y.get("text", "")} for y in x.get("messages", [])]}
                    for x in state["meetings"] if x.get("ended")],
        "work_report": state.get("work_report"),
        "mcp_log": state["mcp_log"][-60:],
        "mode": {
            "ai": agents.ai_enabled() and not budget_out,
            "model": agents.MODEL if agents.ai_enabled() else None,
            "budget": guardrails.DAILY_BUDGET_USD,
            "last_error": LAST_AI_ERROR["text"],
            # Why teammates are scripted right now (None = live AI team)
            "scripted_reason": ("no_key" if not agents.ai_enabled() else "budget" if budget_out
                                else "error" if LAST_AI_ERROR["text"] else None),
            "contact_url": guardrails.CONTACT_URL,
        },
    }


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


# Realistic voices: Microsoft neural text-to-speech (free, no key) via edge-tts.
# The browser falls back to its own voices if this fails or edge-tts isn't installed.
_TTS_VOICE = re.compile(r"en-(US|GB|AU|IN|IE|ZA)-[A-Za-z]+Neural")
_tts_cache: dict = {}


@app.post("/api/tts")
def tts():
    try:
        import edge_tts
    except ImportError:
        return jsonify({"error": "edge-tts not installed"}), 503
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()[:900]
    voice = str(data.get("voice", ""))
    rate = max(-30, min(30, int(data.get("rate", 0) or 0)))
    if not text or not _TTS_VOICE.fullmatch(voice):
        return jsonify({"error": "bad request"}), 400
    key = (voice, rate, text)
    audio = _tts_cache.get(key)
    if audio is None:
        async def run():
            out = b""
            async for ch in edge_tts.Communicate(text, voice, rate=f"{rate:+d}%").stream():
                if ch["type"] == "audio":
                    out += ch["data"]
            return out
        try:
            audio = asyncio.run(asyncio.wait_for(run(), 20))
        except Exception as e:  # network down, service changed...
            return jsonify({"error": f"tts failed: {e}"}), 503
        if not audio:
            return jsonify({"error": "tts returned nothing"}), 503
        if len(_tts_cache) > 400:
            _tts_cache.clear()
        _tts_cache[key] = audio
    return app.response_class(audio, mimetype="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/meta")
def meta():
    return jsonify({
        "roles": {k: {**v, "job": agents.YOUR_JOB[("kickoff", k)]} for k, v in board.ROLES.items()},
        "default_product": board.DEFAULT_PRODUCT,
        "has_project": bool(session.get("project_id")
                            and board.project_path(session["project_id"]).exists()),
        "ai": agents.ai_enabled(),
        "contact_url": guardrails.CONTACT_URL,
    })


@app.post("/api/project")
def create_project():
    data = request.get_json(force=True)
    visitor = (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0]
    # No limit when you run it on your own computer; the limit is for the public site.
    local = visitor.strip() in ("127.0.0.1", "::1", "localhost")
    if not local and not guardrails.allow_new_project(visitor):
        raise UserError("You've started the maximum number of projects for today. "
                        "Reset is limited to keep the app free - please come back tomorrow.")
    if data.get("role") not in ("po", "sm"):
        raise UserError("Pick Product Owner or Scrum Master. The Developer and Tester are played by the AI team.")
    state = board.new_project(data.get("role", ""), data.get("product_name", ""),
                              data.get("vision", ""), data.get("player_name", ""))
    path = board.project_path(state["id"])
    board.save(path, state)
    session["project_id"] = state["id"]
    return jsonify(_view(state))


@app.get("/api/state")
def get_state():
    return jsonify(_view(board.load(_path())))


@app.post("/api/reset")
def reset():
    session.pop("project_id", None)
    return jsonify({"ok": True})


NOTHING = re.compile(r"(?i)\W*(no|nope|none|nothing|all good|no questions|nothing from me)\b[^?]*")


def _apply_effect(state: dict, turn: dict | None, text: str, by: str = "", speaker_role: str = "") -> None:
    """What's said in a meeting lands on the board: estimates, extra criteria, the PO's sprint picks."""
    if not turn:
        return
    m = _meeting(state)
    # Everything said about a story is kept on that story's card.
    if turn.get("kind") in ("ack", "summary", "open_floor"):
        return
    about = [turn["card_id"]] if turn.get("card_id") else []
    if not about and m.get("type") == "refinement":
        cur = next((st for st in reversed(m["agenda"][:m["step"]]) if st.get("card_id")), None)
        if cur:
            about = [cur["card_id"]]
    if m.get("type") in ("refinement", "planning", "review", "daily"):
        about += [i for i in board.story_refs(state, text) if i not in about]
    for cid in about[:3]:
        board.add_discussion(state, cid, f"{m.get('label', m['type'])} - {by or 'Team'}: {text}")
        if cid not in m.setdefault("discussed", []):
            m["discussed"].append(cid)
    if _keeper_on():
        return        # the Board Keeper agent turns the discussion into board changes
    if speaker_role == "dev" and turn.get("kind") != "estimate":
        # The developer gives or changes an estimate out loud: it lands on the card right away.
        for cid, pts in board.estimates_in(state, text):
            board.estimate_story(state, cid, pts, f"{by}, {m.get('label', m['type'])}")
    if m.get("type") == "planning" and state["sprint_state"] == "unplanned":
        key = str(state["sprint"])
        cur = state.setdefault("planned_ids", {}).get(key) or []
        new = board.scope_change(state, text, cur, decider=speaker_role == "po")
        if new != cur:
            state["planned_ids"][key] = new
            _sync_sprint(state, new)
    if not turn.get("card_id"):
        return
    try:
        card = board.get_card(state, turn["card_id"])
    except ValueError:
        return
    if turn["kind"] == "estimate":
        said = [p for _, p in board.estimates_in(state, card["title"] + " is " + text)] \
            or [board.POINT_WORDS[w] for w in re.findall(r"\b(1|2|3|5|8|13|one|two|three|five|eight|thirteen)\b", text.lower())]
        pts = said[-1] if said else (board.estimate_for(card["title"]) if state["role"] != "dev" else 0)
        if pts:
            board.estimate_story(state, card["id"], pts, f"{by}, in refinement" if by else "")
    if turn["kind"] == "edge_case":
        # The tester's estimate for their own test story ("testing is about 2 points").
        t = board.test_ticket(state, card["id"])
        said = re.findall(r"(?i)\b(1|2|3|5|8|13|one|two|three|five|eight|thirteen)\s*(?:story\s*)?points?\b", text)
        if t and said and speaker_role == "tester":
            who = f"Tester: {by}" if state["role"] == "tester" else f"{by}, in refinement"
            board.estimate_story(state, t["id"], board.POINT_WORDS[said[0].lower()], who)
        extra = re.sub(r"(?is)^.*?\badd:\s*", "", text.strip())[:200]
        if extra and extra.lower() not in card["acceptance"].lower():
            card["acceptance"] = (card["acceptance"] + " QA: " + extra).strip()[:600]


SPLIT = re.compile(r"(?i)\bsplit")
NO_SPLIT = re.compile(r"(?i)\b(no need|don't|do not|not|no|without)\b[^.?!]{0,25}\bsplit")


def _do_split(state: dict, card: dict) -> tuple[dict, dict]:
    parts = None
    if agents.ai_enabled():
        try:
            parts = agents.ai_split_story(state, card)
        except Exception as e:  # noqa: BLE001
            agents.log_ai("split_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
    a, b = board.split_story(state, card["id"], parts or board.heuristic_split(card))
    board.log_mcp(state, "po", "split_story", {"card_id": a["id"]}, f"Split into #{a['id']} and #{b['id']}")
    return a, b


def _maybe_split(state: dict, m: dict, text: str) -> None:
    """In refinement, when someone says to split the story, it is split on the spot."""
    if _keeper_on() or m.get("type") != "refinement" or not SPLIT.search(text) or NO_SPLIT.search(text):
        return
    ids = board.story_refs(state, text)
    if not ids:
        cur = next((st for st in reversed(m["agenda"][:m["step"]]) if st.get("card_id")), None)
        ids = [cur["card_id"]] if cur else []
    if not ids or ids[0] in m.setdefault("split_done", []):
        return
    try:
        card = board.get_card(state, ids[0])
    except ValueError:
        return
    if card["status"] != "backlog":
        return
    old = card["title"]
    a, b = _do_split(state, card)
    m["split_done"] += [a["id"], b["id"]]
    who = "po" if state["role"] != "po" else "sm"
    msg = _msg(who, f"Agreed, let's split it. '{old}' is now story {a['id']}, {a['title']}, and story {b['id']}, "
                    f"{b['title']}. Both need a new estimate.")
    msg["kind"] = "split"
    m["messages"].append(msg)


def _sync_sprint(state: dict, ids: list[int]) -> None:
    """Planning: the stories the Product Owner names go onto the sprint board right away."""
    if state["sprint_state"] != "unplanned":
        return
    for c in board.sprint_cards(state, state["sprint"]):
        if c["id"] not in ids and c["status"] == "todo" and c.get("picked_live"):
            board.remove_from_sprint(state, c["id"])
    named = [board.get_card(state, cid) for cid in ids]
    for c in sorted((c for c in named if c["status"] == "backlog" and c["type"] != "test"), key=lambda c: c["priority"]):
        try:
            board.add_to_sprint(state, c["id"])
        except ValueError as e:   # over capacity: it stays in the backlog
            board.log_mcp(state, "po", "add_to_sprint", {"card_id": c["id"]}, f"Not added: {e}")
            continue
        c["picked_live"] = True
        board.log_mcp(state, "po", "add_to_sprint", {"card_id": c["id"]}, f"Story {c['id']} added to Sprint {state['sprint']}")


def _refine_new_stories(state: dict, m: dict) -> None:
    """Refinement: before the closing questions, any unestimated backlog story not covered yet (e.g. one the
    PO just added) gets its turn too, so nothing is left 'pending' when the meeting closes."""
    if m.get("type") != "refinement" or m["agenda"][m["step"]].get("kind") not in ("confirm", "open_floor"):
        return
    covered = {st.get("card_id") for st in m["agenda"]} | set(m.get("parked") or [])
    todo = [c for c in board.backlog_cards(state) if not board.points(c) and c["id"] not in covered][:3]
    if todo:
        m["agenda"][m["step"]:m["step"]] = [st for c in todo for st in agendas.story_steps(state, c)]


def _lead(state: dict) -> str:
    return "sm" if state["role"] != "sm" else "po"


@app.post("/api/meeting/open")
def meeting_open():
    path = _path()
    state = board.load(path)
    mtype = (request.get_json(force=True) or {}).get("type", "kickoff")
    last = _meeting(state)
    if state["complete"]:
        raise UserError("The project is complete. Start over to play again.")
    if last.get("opened") and not last.get("ended"):
        raise UserError("End the current meeting first.")
    if not state["kickoff_done"] and mtype != "kickoff":
        raise UserError("Every project starts with the kickoff.")
    if state["kickoff_done"] and mtype not in board.MEETING_TYPES:
        raise UserError("Pick one of the meetings at the top.")
    ok, _, reason = board.check_meeting(state, mtype)
    if not ok:
        raise UserError(reason)

    board.note_missed_items(state)
    for c in [c for c in state["cards"] if c["type"] == "story"]:   # older projects: every story gets its test story
        board.ensure_test_ticket(state, c)
        t = board.test_ticket(state, c["id"])
        if t and "(Developer)" in str(t.get("estimated_by", "")):          # was quick-estimated as dev work by mistake
            t["estimated_by"] = ""
            board.sync_test_points(state, c)
    label = board.meeting_label(state, mtype)  # before the stand-up counter moves on
    calls = []
    state["work_report"] = None
    if mtype in ("daily", "review"):
        # Time has passed since the last check-in: the AI developer/tester did some work.
        if mtype == "daily":
            state["standups"] += 1
        factor = board.advance_factor(state)
        actions, report = board.plan_work(state, 1, factor)
        state["last_work_meeting"] = len(state["meetings"])
        state["work_report"] = report
        board.save(path, state)
        calls = mcp_client.run_actions(str(path), actions)
        state = board.load(path)
        board.record_chart_point(state, state["sprint"], f"SU{state['standups']}" if mtype == "daily" else "Review")
        state["last_work_ts"] = time.time()
        _log_calls(state, calls)

    state["meetings"].append({
        "type": mtype, "sprint": state["sprint"], "label": label,
        "messages": [], "opened": True, "ended": False, "invalid": False,
        "user_spoke": False, "user_chars": 0, "user_count": 0,
        "standup": state["standups"] if mtype == "daily" else 0,
        "agenda": agendas.agenda(state, mtype), "step": 0, "queue": [], "awaiting": None,
        "hands": [], "summary": None,
    })
    state["ceremony_index"] = len(state["meetings"]) - 1
    state["open_items"] = []
    board.save(path, state)
    return jsonify(_view(state))


def _close_with_summary(path: Path, spoken_by: str | None, after_user: bool = False, early: bool = False) -> dict:
    """Summary (spoken + written), then close the meeting and do the follow-up work."""
    state = board.load(path)
    m = _meeting(state)
    mtype = m["type"]
    if spoken_by:
        # A clear, short end: no more questions, what's next, back to work. The full summary is written.
        nxt = agendas.NEXT.get(mtype, "")
        text = (f"Thanks, {board.player(state)}. Back to work, everyone!" if after_user else
                "Let's stop here. Notes are on screen. Thanks, everyone!" if early else
                "No more questions. Thanks, everyone, back to work!")
        msg = _msg(spoken_by, text)
        msg["kind"] = "summary"
        state = board.load(path)
        _meeting(state)["messages"].append(msg)
        board.save(path, state)
    state = board.load(path)
    m = _meeting(state)
    m["summary"] = _minutes(state)
    board.save(path, state)
    return _finish(path)


def _pick_turn(state: dict, m: dict):
    """Take the next turn off the queue or the agenda (changes m in memory only). (None, None) = agenda done."""
    si = None
    if m["queue"]:
        turn = m["queue"].pop(0)
    elif m["step"] < len(m["agenda"]):
        _refine_new_stories(state, m)
        si = m["step"]
        turn = m["agenda"][m["step"]]
        m["step"] += 1
    else:
        return None, None
    if turn["kind"] == "capacity_check":
        cards, total = agendas.proposal(state, m["messages"])
        names = ", ".join(f"story {c['id']} ({board.with_test(state, c)} incl. testing)" for c in cards) or "nothing yet"
        cap = board.capacity(state)
        fits = total <= cap
        turn = {**turn,
                "instruction": (f"The proposal is {names}: {total} points in total. Team capacity is about {cap} points "
                                "per sprint (our estimate of how much we can finish). " + ("Say it fits." if fits else
                                "Say it's over our estimate and that the last one may not get finished; the Product Owner decides, unfinished work moves to the next sprint.")
                                + " Use these real numbers."),
                "prompt": (f"Check the proposal against capacity: {names} = {total} points; team capacity is about "
                           f"{cap} points per sprint. Say if it fits or push back."),
                "script": (f"Those add up to {total} points, and our capacity is about {cap}, so that fits."
                           if fits else f"That's {total} points, more than our estimate of {cap}. Your call; anything unfinished moves to the next sprint.")}
    return turn, si


def _instruction(state: dict, m: dict, turn: dict):
    """What the AI teammate is asked to say for this turn, and the scripted line if the AI is unavailable."""
    last_was_user = bool(m["messages"]) and m["messages"][-1]["who"] == "user"
    if turn["kind"] == "ack":
        instruction = (f'{board.player(state)} just said: "{turn["text"]}". Answer that in a few words '
                       "(for example confirm you can hear them), nothing else.")
        fallback = lambda st: {"sm": "Yes, loud and clear.", "po": "Yes, I can hear you.",  # noqa: E731
                               "dev": "Hearing you fine.", "tester": "Yes, all good here."}[turn["role"]]
    elif turn["kind"] == "reply":
        asker = turn.get("from") or f"{board.player(state)} ({board.ROLES[state['role']]['title']})"
        instruction = (f'{asker} just said to you: "{turn["text"]}". Read it carefully and answer exactly that, '
                       "in 1-2 sentences from your role. If it's a question, give a real answer (facts, a number, "
                       "a decision), not a promise to look later.")
        if not turn.get("from"):
            instruction += " Don't ask them a new question: the meeting moves straight on after you."
        if turn.get("closing"):
            instruction += " The meeting is closing: answer in under 15 words, no new questions."
        fallback = lambda st: agents.mock_reply(st, turn["role"], turn["text"])  # noqa: E731
    elif turn["kind"] == "floor_answer":
        instruction = "Say briefly that you have nothing more."
        fallback = lambda st: "Nothing more from me, all clear."  # noqa: E731
    else:
        instruction = turn["instruction"] + " One short sentence, then move the meeting forward."
        if last_was_user:
            me = board.player(state)
            instruction = (f'{me} just said: "{m["messages"][-1]["text"]}". Start by responding to that in a few words '
                           f"(agree, answer, or build on it), then: " + instruction)
        script = turn["script"]
        if turn["kind"] == "answer" and last_was_user:
            script = ("Good question. Let's keep the first release small and clear. I'll make sure the "
                      "acceptance criteria cover that, and anything bigger goes in the backlog.")
        ack = f"Thanks, {board.player(state)}. " if last_was_user and not script.startswith("Thanks") else ""
        fallback = lambda st: ack + script  # noqa: E731
    return instruction, fallback


# The next teammate's line, written while the current one is still being spoken.
_SPEC: dict[str, tuple] = {}
_SPEC_THREADS: dict[str, threading.Thread] = {}


def _prepare_next(path: Path) -> None:
    """Write the next AI teammate's line ahead of time. Used only if nothing changed in between
    (same speaker, same instruction, same transcript); otherwise it is thrown away."""
    try:
        state = board.load(path)
        m = _meeting(state)
        if not agents.ai_enabled() or not m.get("opened") or m.get("ended") or m.get("awaiting"):
            return
        turn, _ = _pick_turn(state, m)
        if not turn or turn["role"] not in board.ROLES or turn["role"] == state["role"] or turn["kind"] == "summary":
            return
        instruction, _ = _instruction(state, m, turn)
        reply = asyncio.run(agents.ai_turn(None, state, turn["role"], instruction, m["messages"]))
        if reply:
            _SPEC[str(path)] = (turn["role"], instruction, len(m["messages"]), reply)
    except Exception as e:  # noqa: BLE001  (a missed head start only costs time)
        agents.log_ai("prepare_failed", error=f"{type(e).__name__}: {str(e)[:200]}")


def _prepare_next_later(path: Path) -> None:
    _SPEC.pop(str(path), None)
    t = threading.Thread(target=_prepare_next, args=(path,), daemon=True)
    _SPEC_THREADS[str(path)] = t
    t.start()


@app.after_request
def _head_start(resp):
    """After you speak or a meeting opens, start writing the next teammate's line right away."""
    if resp.status_code == 200 and request.method == "POST" and request.path.endswith(
            ("/api/meeting/say", "/api/meeting/open", "/api/meeting/pass")):
        pid = session.get("project_id")
        if pid and board.project_path(pid).exists():
            k = _KEEPERS.get(str(board.project_path(pid)))
            if not (k and k.is_alive()):      # a running keeper starts it itself when done
                _prepare_next_later(board.project_path(pid))
    return resp


@app.post("/api/meeting/next")
def meeting_next():
    """The next turn. Returns after ONE person has spoken, or when it's your turn."""
    path = _path()
    t = _SPEC_THREADS.get(str(path))
    if t and t.is_alive():
        t.join(timeout=15)
    state = board.load(path)
    m = _meeting(state)
    if not m.get("opened") or m.get("ended") or m.get("awaiting"):
        return jsonify(_view(state))
    m["hands"] = []
    turn, si = _pick_turn(state, m)
    if turn is None:
        board.save(path, state)
        return jsonify(_view(_close_with_summary(path, None if state["role"] == "sm" else "sm")))
    role = turn["role"]
    if role == "__reawait__":
        m["awaiting"] = {"prompt": turn["prompt"], "kind": turn["kind"], "card_id": turn.get("card_id"),
                         "asker": turn.get("asker"), "si": turn.get("si"), "tries": turn.get("tries", 0)}
        if turn.get("speak") and turn["kind"] != "floor_answer":
            # Say clearly what's still needed from you (once), instead of silently waiting.
            ask = turn["prompt"].replace("Your turn: ", "").strip()
            ask = ask if ask[-1:] in ".?!" else ask + "."
            back = _msg(_lead(state), f"{board.player(state)}, over to you." if state["role"] == "sm" else
                        f"{board.player(state)}, one thing we still need from you: {ask} "
                        "If you'd rather skip it, just say so and I'll suggest one.")
            back["kind"] = "nudge"
            m["messages"].append(back)
        board.save(path, state)
        return jsonify(_view(state))
    if role == "user_floor":
        if state["role"] == "sm":
            # You asked for last questions; the team has none.
            role, turn = "dev", {**turn, "role": "dev", "kind": "floor_answer"}
        else:
            m["awaiting"] = {"prompt": turn["prompt"], "kind": "floor_answer"}
            board.save(path, state)
            return jsonify(_view(state))
    if role == state["role"]:
        m["awaiting"] = {"prompt": turn["prompt"], "kind": turn["kind"], "card_id": turn.get("card_id"), "si": si}
        board.save(path, state)
        return jsonify(_view(state))
    nudge = bool((request.get_json(silent=True) or {}).get("nudge"))
    last_line = next((x["text"] for x in reversed(m["messages"]) if x["who"] != "system"), "")
    if nudge and role in board.ROLES and role not in ("sm", state["role"]) and state["role"] != "sm" \
            and turn["kind"] != "summary" and board.name(role).lower() not in last_line.lower():
        # (skipped when the last line already asked them by name: no "over to you" right after "Felix, please...")
        # Nobody spoke for a while: Sam reminds the next person, and they answer.
        reminder = _msg("sm", f"{board.name(role)}, over to you.")
        reminder["kind"] = "nudge"
        m["messages"].append(reminder)
    board.save(path, state)

    if turn["kind"] == "summary":
        return jsonify(_view(_close_with_summary(path, role)))

    instruction, fallback = _instruction(state, m, turn)
    msg, calls = _speak_one(path, role, instruction, fallback)
    msg["kind"] = turn["kind"]
    msg["card_id"] = turn.get("card_id")
    state = board.load(path)
    m = _meeting(state)
    m["messages"].append(msg)
    _apply_effect(state, turn, msg["text"], board.name(role) + f" ({board.ROLES[role]['title']})", role)
    _follow_question(state, m, role, msg["text"], turn)
    _maybe_split(state, m, msg["text"])
    _keep_promise(state, m, role, msg)
    if turn["kind"] == "goal" and "sprint goal:" in msg["text"].lower() and not _keeper_on():
        goal = re.split(r"(?i)sprint goal:", msg["text"], 1)[1].split("?")[0].replace(" Agreed", "").strip(" .")
        board.set_sprint_goal(state, goal)
    _log_calls(state, calls)
    board.save(path, state)
    _keep_board_later(path)   # the board catches up while this line is being spoken
    return jsonify(_view(state))


@app.post("/api/meeting/say")
def meeting_say():
    path = _path()
    state = board.load(path)
    m = _meeting(state)
    if not m["opened"] or m["ended"]:
        raise UserError("This meeting isn't running.")
    if m["user_count"] >= guardrails.MAX_MESSAGES_PER_MEETING:
        raise UserError(f"Timebox reached ({guardrails.MAX_MESSAGES_PER_MEETING} messages). "
                        "Time to end the meeting!")
    text = guardrails.clean_message(request.get_json(force=True).get("text", ""))
    awaited = m.get("awaiting")
    msg = _msg("user", text)
    msg["name"] = board.player(state)
    msg["title"] = board.ROLES[state["role"]]["title"]
    if awaited and awaited["kind"] == "vision" and state["role"] == "po":
        state["product"]["vision"] = text[:400]  # the Product Owner decides what we build
    msg["kind"] = awaited["kind"] if awaited else "interjection"
    msg["card_id"] = awaited.get("card_id") if awaited else None
    m["messages"].append(msg)
    m["user_spoke"] = True
    m["user_chars"] += len(text)
    m["user_count"] += 1
    m["awaiting"] = None
    reopen_summary = None
    if awaited and awaited["kind"] == "summary":
        if _is_wrap_up(state, m, text, awaited):
            board.save(path, state)
            return jsonify(_view(_close_with_summary(path, None)))   # you closed it: no AI line after you
        # A question or concern instead of the wrap-up: the team answers it, then closing is yours again.
        reopen_summary, awaited = awaited, None
        msg["kind"] = "interjection"
    _apply_effect(state, awaited or {"kind": "interjection"}, text, board.player(state), state["role"])
    _maybe_split(state, m, text)
    if awaited and awaited["kind"] == "goal" and not _keeper_on() and not is_pass(text) and len(text.split()) >= 4:
        board.set_sprint_goal(state, re.sub(r"(?i)^\s*sprint goal:\s*", "", text))
    if _keeper_on():
        board.save(path, state)
        _keep_board(path)
        state = board.load(path)
        m = _meeting(state)
    if awaited and awaited["kind"] == "floor_answer" and NOTHING.fullmatch(text):
        board.save(path, state)
        return jsonify(_view(state))  # nothing more: straight to the summary
    if awaited and awaited["kind"] == "answer_q" and awaited.get("asker"):
        # You answered a teammate's question: they take it on board, then the meeting goes on.
        # If you turned to someone else instead ("Yuki, what edge cases...?"), that person answers.
        other = addressee(state, state["role"], text)
        who = other if other in board.ai_roles(state) else awaited["asker"]
        m["queue"][:0] = [{"role": who, "kind": "reply", "text": text, "depth": 9}]
        m["hands"] = [who]
        board.save(path, state)
        return jsonify(_view(state))
    if awaited and awaited["kind"] == "floor_answer":
        # A real question at the end: it gets answered, then "anything else?" until you're done.
        responders, _, _ = _route(state, m, text, None)
        loops = sum(1 for x in m["messages"] if x.get("kind") == "floor_answer" and x["who"] == "user")
        turns = [{"role": r, "kind": "reply", "text": text, "closing": True} for r in (responders or [_lead(state)])[:1]]
        if loops < 2:
            turns.append({"role": "__reawait__", "kind": "floor_answer",
                          "prompt": "Anything else before we close? Say it, or click 'Nothing from me'."})
        m["queue"][:0] = turns
        m["hands"] = [t["role"] for t in turns if t["role"] in board.ROLES]
        board.save(path, state)
        return jsonify(_view(state))
    if m.get("type") == "refinement" and PARK.search(text) and _park_stories(state, m, text, awaited):
        # "Not important for now" / "the rest later": those stories leave today's agenda; Sam confirms it once.
        m["awaiting"] = None
        m["queue"][:0] = [{"role": _lead(state), "kind": "reply", "text": text,
                           "closing": not any(st.get("card_id") for st in m["agenda"][m["step"]:])}]
        m["hands"] = [_lead(state)]
        board.save(path, state)
        return jsonify(_view(state))
    if awaited and awaited["kind"] not in ("answer_q",) and is_pass(text):
        # "Sounds good" / "no questions" / "pass": don't trap you on this item. Sam covers it, then moves on.
        _cover_turn(state, m, awaited)
        board.save(path, state)
        return jsonify(_view(state))
    # Who answers what you just said, and did it cover your agenda task?
    responders, advance, greeting = _route(state, m, text, awaited)
    nxt = m["agenda"][m["step"]] if m.get("agenda") and m["step"] < len(m["agenda"]) else None
    if advance and nxt and not greeting:
        # The next agenda speaker answers you anyway: no second line from the same person.
        responders = [r for r in responders if r != nxt["role"]]
    turns = [{"role": r, "kind": "ack" if greeting else "reply", "text": text} for r in responders]
    if awaited and not advance and awaited.get("tries", 0) >= 1:
        # Twice off this item: answer you, then cover it and move on (no endless loop).
        _cover_turn(state, m, awaited, passed=False)      # queued first ...
        m["queue"][:0] = turns              # ... so the replies to you come before it
        m["hands"] = responders
        board.save(path, state)
        return jsonify(_view(state))
    if awaited and not advance:
        # You said something else (e.g. "can you all hear me?"): they answer, then it's your turn again,
        # and Sam says clearly what's still needed.
        turns.append({"role": "__reawait__", "kind": awaited["kind"], "prompt": awaited.get("prompt", ""),
                      "card_id": awaited.get("card_id"), "si": awaited.get("si"),
                      "tries": awaited.get("tries", 0) + 1, "speak": True})
    if reopen_summary:
        if not turns:
            turns = [{"role": _lead(state), "kind": "reply", "text": text}]
        turns.append({"role": "__reawait__", "kind": "summary", "si": reopen_summary.get("si"),
                      "prompt": "Wrap up and close (sum up what was agreed), or raise anything else first."})
    m["queue"][:0] = turns
    m["hands"] = [t["role"] for t in turns if t["role"] in board.ROLES]
    board.save(path, state)
    return jsonify(_view(state))


PARK = re.compile(r"(?i)\b(not (that |so |very )?(important|needed|necessary|a priority|urgent)( for now| right now| now)?|"
                  r"park(ed|ing)?|skip (that|this|it|the) (story|one|stories)|(do|discuss|refine|look at|take|cover) "
                  r"(it|that|this|them|those|the rest|the others|others)( \w+){0,2} later|"
                  r"leave (it|that|this|them) for (now|later)|don'?t (have|need) to refine|not refine|"
                  r"focus on the (stories|ones) (we|that))\b")
REST = re.compile(r"(?i)\b(others|the rest|remaining|all (the )?other)\b")


def _park_stories(state: dict, m: dict, text: str, awaited: dict | None) -> list[int]:
    """The PO parks stories in refinement: their remaining agenda steps go, and nobody brings them back today."""
    ids = set(board.story_refs(state, text))
    if awaited and awaited.get("card_id"):
        ids.add(awaited["card_id"])
    upcoming = [st.get("card_id") for st in m["agenda"][m["step"]:] + m["queue"] if st.get("card_id")]
    if REST.search(text):
        ids |= set(upcoming)
    if not ids and len(set(upcoming)) == 1:      # "that story is not important": the one still to come
        ids = set(upcoming)
    if not ids:
        return []
    m["agenda"][m["step"]:] = [st for st in m["agenda"][m["step"]:] if st.get("card_id") not in ids]
    m["queue"] = [st for st in m["queue"] if st.get("card_id") not in ids]
    m["parked"] = sorted(set(m.get("parked") or []) | ids)
    return sorted(ids)


PASS = re.compile(r"(?i)\b(sounds good|looks good|all good|agreed|fine by me|no (other |more |further )?questions?|"
                  r"nothing (else|more|from me)|i'?m (done|good|fine)|that'?s (all|it)|i don'?t have (any|anything)|"
                  r"pass|skip( it| me)?|move on|go ahead|you decide|up to you)\b")
NO_FILL = ("floor_answer", "answer_q", "update", "interjection", "summary", "open_floor")


SUBSTANCE = re.compile(r"(?i)\b(\d+|one|two|three|four|five|eight|story|stories|points?|goal|vision|should|need|want|"
                       r"like|build|add|take|commit|propose|suggest|because|test|bug|estimate|priority|first)\b")


WRAP = re.compile(r"(?i)\b(summary|to sum up|summing up|wrap(ping)? up|let'?s close|close the meeting|meeting (is )?closed|"
                  r"that'?s (all|it)( for today)?|bye|goodbye|talk to you( all)? later|see you( all)?|end the meeting)\b")
THANKS = re.compile(r"(?i)\b(thanks?|thank you)\b")


ASKING = re.compile(r"(?i)(^|[,.;]\s*|\b(before we close|but|so|and|also)\s+)(is|are|do|does|did|can|could|should|"
                    r"will|would|have|has|what|why|how|when|where|who|which)\b"
                    r"|\b(do|does|did|is|are|can|could|should|will) (we|you|they|it|there|the|this|that|anyone)\b")


MORE = re.compile(r"(?i)\b(as well|one more|also|another|still|pending|left|next (story|item|one)|refine|estimate|"
                  r"discuss|let'?s (go ahead|do|take|look|cover|continue)|go ahead (and|with))\b")


def _is_wrap_up(state: dict, m: dict, text: str, awaited: dict) -> bool:
    """Your closing turn: a wrap-up closes the meeting; a question or a concern doesn't."""
    if NOTHING.fullmatch(text):
        return True
    if "?" in text or ASKING.search(text):
        return False      # a question is never the wrap-up: it gets answered first
    if WRAP.search(text):
        return True       # "Summary: ...", "that's it for today", "talk to you later, bye"
    if MORE.search(text):
        return False      # "let's go ahead and refine story 4 as well": more work first, not the end
    if THANKS.search(text):
        return True       # "thanks everyone"
    if agents.ai_enabled():
        try:
            return agents.ai_route(state, m["messages"], text, awaited, None)[1]
        except Exception as e:  # noqa: BLE001
            agents.log_ai("route_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
    return is_pass(text)


def is_pass(text: str) -> bool:
    """'Sounds good', 'no questions', 'I'm done', 'pass': you don't want to do this turn.
    Not a pass if you also said something real ('story 1 and 2, sounds good')."""
    return bool(NOTHING.fullmatch(text)) or ("?" not in text and bool(PASS.search(text))
                                              and not SUBSTANCE.search(text))


def _cover_turn(state: dict, m: dict, awaited: dict, passed: bool = True) -> None:
    """You passed (or twice said something else): the facilitator does that agenda item as a suggestion you
    can change, and the meeting goes on."""
    m["awaiting"] = None
    si = awaited.get("si")
    step = m["agenda"][si] if isinstance(si, int) and si < len(m["agenda"]) else None
    if not step or awaited["kind"] in NO_FILL or not (step.get("instruction") or step.get("script")):
        return
    me, lead = board.player(state), _lead(state)
    why = f"{me} passed on this" if passed else f"{me} spoke about something else, so this item is still open"
    m["queue"].insert(0, {**step, "role": lead,
                          "instruction": (f"{why}. Do it for them as a suggestion they can change later "
                                          f"(don't ask them again, don't say they passed unless they did). You are "
                                          f"{board.name(lead)}: never address yourself; if the task says to ask "
                                          f"{board.name(lead)} something, answer it yourself. "
                                          f"Task: {step.get('instruction') or step.get('prompt')}"),
                          "script": "Let me suggest one, and we can change it later. " + (step.get("script") or ""),
                          "covered": True})


@app.post("/api/meeting/pass")
def meeting_pass():
    """Skip your turn (Pass button, or you stayed quiet): the meeting moves on."""
    path = _path()
    state = board.load(path)
    m = _meeting(state)
    awaited = m.get("awaiting")
    if not m.get("opened") or m.get("ended") or not awaited:
        return jsonify(_view(state))
    if awaited["kind"] == "summary":
        board.save(path, state)
        return jsonify(_view(_close_with_summary(path, None)))   # you closed it: no AI line after you
    _cover_turn(state, m, awaited)
    board.save(path, state)
    return jsonify(_view(state))


QUESTION_KINDS = ("question", "invite", "open_floor", "summary", "ack", "floor_answer", "nudge")
THIRD_PERSON = r"(?:'s|\s+(?:said|says|wants|asked|mentioned|suggested|proposed|thinks|meant|decided|needs|has|is))\b"


def _names_for(state: dict, r: str) -> list[str]:
    if r == state["role"]:
        return [n for n in (board.player(state).lower(), board.ROLES[r]["title"].lower()) if n]
    return [board.name(r).lower(), board.ROLES[r]["title"].lower()]


def addressee(state: dict, speaker: str, text: str) -> str | None:
    """Who a line is really addressed to: 'Marcus, ...?' or '..., Marcus?' wins over someone merely mentioned
    ('...what Priya said'). Returns a role key or None (said to the room)."""
    sentences = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    questions = [p for p in sentences if p.endswith("?")]
    if not questions:
        return None
    others = [r for r in board.ROLES if r != speaker]
    q = questions[-1].lower()
    lead = sentences[0].lower()
    called = []  # 1. called by name in the question: "Marcus, ...?" / "..., Marcus?" / "Can you, Marcus, ..."
    for r in others:
        for n in _names_for(state, r):
            e = re.escape(n)
            if (re.match(rf"\W*(?:ok|okay|so|and|thanks|great|right)?,?\s*{e}\b\s*[,:]", q)
                    or re.search(rf",\s*{e}\s*[?!.]*\s*$", q) or re.search(rf",\s*{e}\s*,", q)
                    or re.match(rf"\W*\w+\s*,\s*{e}\b\s*[,:]", q)):
                called.append(r)
                break
    if called:   # "Lakshmi, Leo, what's your estimate?": the teammate who does that work answers, not you
        return next((r for r in called if r != state["role"]), called[0])
    named_in_q = any(re.search(rf"\b{re.escape(n)}\b", q) for r in others for n in _names_for(state, r))
    for r in ([] if named_in_q else others):  # 1b. "Marcus, <statement>. <question to the room>?"
        for n in _names_for(state, r):
            if re.match(rf"\W*{re.escape(n)}\b\s*[,:]", lead):
                return r
    hits = []  # 2. otherwise the first person named in the question who isn't just talked about
    for r in others:
        for n in _names_for(state, r):
            for mt in re.finditer(rf"\b{re.escape(n)}\b", q):
                if not re.match(THIRD_PERSON, q[mt.end():]):
                    hits.append((mt.start(), r))
    return min(hits)[1] if hits else None


def _follow_question(state: dict, m: dict, speaker: str, text: str, turn: dict) -> None:
    """Someone asked a teammate a question by name: that person answers next.
    If it's you, it becomes your turn; if it's another AI, they reply before the agenda goes on."""
    if turn["kind"] in QUESTION_KINDS or turn.get("depth", 0) >= 2 or "?" not in text:
        return
    questions = [p for p in re.split(r"(?<=[.!?])\s+", text) if p.strip().endswith("?")]
    target = addressee(state, speaker, text)
    if not target:
        return
    nxt = m["queue"][0] if m["queue"] else (m["agenda"][m["step"]] if m["step"] < len(m["agenda"]) else None)
    nxt_role = nxt["role"] if nxt else None
    if target == state["role"]:
        if nxt_role == state["role"]:
            return  # it's your turn next anyway
        m["queue"].insert(0, {"role": "__reawait__", "kind": "answer_q", "asker": speaker,
                              "prompt": f"{board.name(speaker)} asked you: {questions[-1] if questions else text}"})
    elif nxt_role != target:
        m["queue"].insert(0, {"role": target, "kind": "reply", "text": text, "depth": turn.get("depth", 0) + 1,
                              "from": f"{board.name(speaker)} ({board.ROLES[speaker]['title']})"})


GREETING = re.compile(r"(?i)\b(hear me|can you (all )?hear|everyone|every one|all of you|good (morning|afternoon)|hello|hi (team|all|everyone))\b")


def _route(state: dict, m: dict, text: str, awaited: dict | None) -> tuple[list[str], bool, bool]:
    """Returns (responders, advance, is_greeting)."""
    nxt = m["agenda"][m["step"]] if m.get("agenda") and m["step"] < len(m["agenda"]) else None
    # Did they actually say something (a statement), or only ask / check?
    statements = [p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p and not p.endswith("?")
                  and len(p.split()) >= 6]
    greeting = bool(GREETING.search(text)) and len(text) < 90 and not statements
    if agents.ai_enabled():
        try:
            roles, advance = agents.ai_route(state, m["messages"], text, awaited, nxt)
            called = addressee(state, state["role"], text) if "?" in text else None
            if called and called != state["role"] and called not in roles:
                roles = [called] + roles[:1]
            return roles[:2] if not greeting else roles, (advance if awaited else True), greeting and not advance
        except Exception as e:  # fall back to simple rules
            agents.log_ai("route_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
    low = text.lower()
    ai = [r for r in ("sm", "po", "dev", "tester") if r != state["role"]]
    named = [r for r in ai if board.name(r).lower() in low or board.ROLES[r]["title"].lower() in low]
    if greeting:
        return ai, False, True
    if awaited:
        asked = "?" in text and not statements and awaited["kind"] not in ("question", "invite", "open", "open_floor")
        who = [r for r in named if not nxt or r != nxt["role"]][:2]
        if asked and not who:
            who = agents.responders_for(state, text)[:1]
        return who, not asked, False
    return (named[:2] or agents.responders_for(state, text)[:1]), True, False


@app.post("/api/meeting/end")
def meeting_end():
    """End now: the lead still summarises what was covered, then it closes."""
    path = _path()
    state = board.load(path)
    m = _meeting(state)
    if m.get("ended") or not m.get("opened"):
        return jsonify(_view(state))
    m["ended_by"] = "end_button"
    board.save(path, state)
    return jsonify(_view(_close_with_summary(path, None)))   # you ended it: nobody else speaks


@app.post("/api/goal")
def set_goal():
    path = _path()
    state = board.load(path)
    board.set_sprint_goal(state, request.get_json(force=True).get("goal", ""))
    board.save(path, state)
    return jsonify(_view(state))


@app.post("/api/card")
def add_card():
    path = _path()
    state = board.load(path)
    d = request.get_json(force=True)
    card = board.create_story(state, d.get("title", ""), d.get("acceptance", ""))
    board.mark_touched(state, card)
    board.save(path, state)
    return jsonify(_view(state))


@app.post("/api/card/<int:card_id>/<action>")
def card_action(card_id: int, action: str):
    path = _path()
    state = board.load(path)
    d = request.get_json(force=True) or {}
    card = board.get_card(state, card_id)
    role = state["role"]
    if action == "edit":
        if "title" in d and role == "po":
            card["title"] = d["title"].strip()[:120] or card["title"]
        if "acceptance" in d and role == "po":
            card["acceptance"] = d["acceptance"].strip()[:600]
        if "dev_notes" in d and role == "dev":
            board.add_dev_notes(state, card_id, d["dev_notes"])
        if "test_cases" in d and role == "tester":
            board.write_test_cases(state, card_id, d["test_cases"])
        if d.get("points") and role == "dev" and card["type"] != "test":
            board.estimate_story(state, card_id, d["points"])
        if d.get("points") and role == "tester" and card["type"] == "test":
            board.estimate_story(state, card_id, d["points"], f"Tester: {board.player(state)}")
    elif action == "split":
        if role != "po":
            raise UserError("The Product Owner splits stories (usually in refinement).")
        _do_split(state, card)
    elif action == "delete":
        if role != "po":
            raise UserError("Only the Product Owner removes stories from the backlog.")
        board.delete_story(state, card_id)
        board.log_mcp(state, "you", "delete_story", {"card_id": card_id}, f"Deleted #{card_id} {card['title']}")
        board.save(path, state)
        return jsonify(_view(state))
    elif action == "move":
        if role != "dev" and not (role == "tester" and board.is_test_task(card)):
            raise UserError("Only the developer moves cards between work columns (the tester moves testing tasks).")
        board.move_card(state, card_id, d.get("to", ""))
    elif action == "sprint":
        planning_live = _meeting(state).get("type") == "planning" and not _meeting(state).get("ended")
        if role != "po" and not planning_live:
            raise UserError("The Product Owner decides what goes into the sprint (during Sprint Planning anyone can pull in what the team agreed).")
        if d.get("add"):
            board.add_to_sprint(state, card_id)
            now, cap = board.committed(state, state["sprint"]), board.capacity(state)
            if now > cap:
                board.log_mcp(state, "po", "add_to_sprint", {"card_id": card_id},
                              f"Added. Sprint {state['sprint']} now has {now} of {cap} points: over the capacity estimate.")
        else:
            board.remove_from_sprint(state, card_id)
    elif action == "test":
        if role != "tester":
            raise UserError("Only the tester records test results.")
        board.record_test_result(state, card_id, d.get("result", ""),
                                 d.get("test_cases", ""), d.get("note", ""))
    elif action == "bug":
        if role != "tester":
            raise UserError("Only the tester logs bugs here.")
        board.log_bug(state, d.get("title", "").strip() or "Untitled bug",
                      d.get("description", ""), card_id)
    elif action == "accept":
        if role != "po":
            raise UserError("Only the Product Owner accepts stories.")
        if board.current_ceremony(state)["type"] != "review" or _meeting(state).get("ended"):
            raise UserError("Stories are accepted during the Sprint Review meeting.")
        board.accept_story(state, card_id, bool(d.get("accepted", True)), d.get("note", ""))
    elif action == "priority":
        if role != "po":
            raise UserError("Only the Product Owner orders the backlog.")
        backlog = board.backlog_cards(state)
        i = next(i for i, c in enumerate(backlog) if c["id"] == card_id)
        j = i - 1 if d.get("dir") == "up" else i + 1
        if 0 <= j < len(backlog):
            backlog[i]["priority"], backlog[j]["priority"] = backlog[j]["priority"], backlog[i]["priority"]
            if backlog[i]["priority"] == backlog[j]["priority"]:
                backlog[i]["priority"] += -0.5 if d.get("dir") == "up" else 0.5
    else:
        raise UserError("Unknown action")
    board.mark_touched(state, card)
    board.save(path, state)
    return jsonify(_view(state))


@app.get("/api/prototype/<int:sprint>")
def prototype(sprint: int):
    path = _path()
    state = board.load(path)
    if agents.ai_enabled():
        todo = [c for c in state["cards"] if c["status"] == "done" and c.get("sprint") and c["sprint"] <= sprint
                and c["type"] == "story" and not c.get("screen_html")][:4]
        for c in todo:
            try:
                c["screen_html"] = agents.ai_screen(state, c) or ""
            except Exception as e:  # noqa: BLE001
                agents.log_ai("screen_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
                break
        if todo:
            board.save(path, state)
    html = agents.build_prototype(state, sprint)
    return html, 200, {"Content-Type": "text/html; charset=utf-8",
                       "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    print(f"\n  Scrum Simu running:  http://localhost:{port}\n"
          f"  Mode: {'AI (' + agents.MODEL + ')' if agents.ai_enabled() else 'Practice (no API key)'}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
