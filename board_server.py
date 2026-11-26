"""
board_server.py - the MCP SERVER for the Scrum board.

It owns the tools (create_story, move_card, ...) and is the only part that
changes the board. The app's MCP client starts it and asks it to run tools
on behalf of the AI agents.

Restaurant analogy: Claude = manager (decides), MCP client = waiter's tablet
(carries the order), this file = the cook (does the work).

Run on its own (e.g. to plug into Claude Desktop):
    SCRUMGYM_PROJECT_FILE=data/projects/<id>.json python board_server.py
"""
from __future__ import annotations

import json
import logging
import os

from mcp.server.fastmcp import FastMCP

import board

mcp = FastMCP("scrum-board", log_level="WARNING")
logging.getLogger("mcp").setLevel(logging.WARNING)

# In "memory" transport mode the app sets this directly instead of an env var.
PROJECT_FILE_OVERRIDE: str | None = None


def _path() -> str:
    path = PROJECT_FILE_OVERRIDE or os.environ.get("SCRUMGYM_PROJECT_FILE")
    if not path:
        raise RuntimeError("SCRUMGYM_PROJECT_FILE is not set")
    return path


def _run(fn, *args, **kwargs) -> str:
    """Load the board, run one rule from board.py, save, and report back."""
    path = _path()
    state = board.load(path)
    try:
        result = fn(state, *args, **kwargs)
    except ValueError as e:
        return f"ERROR: {e}"
    board.save(path, state)
    if isinstance(result, dict) and "id" in result and "title" in result:
        return (f"OK: #{result['id']} '{result['title']}' -> "
                f"{board.STATUS_LABELS[result['status']]}, {result['points'] or '?'} pts")
    return f"OK: {result}"


@mcp.tool()
def get_board() -> str:
    """Read the whole Scrum board: product, sprint goal and every card with its status."""
    return json.dumps(board.board_summary(board.load(_path())), indent=1)


@mcp.tool()
def create_story(title: str, acceptance_criteria: str = "", points: int = 0) -> str:
    """Product Owner: add a new user story to the product backlog."""
    return _run(board.create_story, title, acceptance_criteria, points)


@mcp.tool()
def estimate_story(card_id: int, points: int, estimated_by: str = "") -> str:
    """Developer: give a story point estimate (1, 2, 3, 5, 8 or 13). points=0 removes the estimate."""
    return _run(board.estimate_story, card_id, points, estimated_by)


@mcp.tool()
def add_to_sprint(card_id: int, without_test: bool = False) -> str:
    """Product Owner: pull a backlog story into the current sprint (To Do), with its test ticket.
    without_test=true only when the team agreed the story's testing waits for a later sprint."""
    return _run(board.add_to_sprint, card_id, without_test)


@mcp.tool()
def remove_from_sprint(card_id: int) -> str:
    """Take a story that hasn't started (To Do) back to the product backlog, with its test ticket."""
    return _run(board.remove_from_sprint, card_id)


@mcp.tool()
def update_story(card_id: int, acceptance_criteria: str) -> str:
    """Add an agreed rule, limit or edge case to a story's acceptance criteria."""
    return _run(board.update_story, card_id, acceptance_criteria)


@mcp.tool()
def split_story(card_id: int, title_1: str, acceptance_1: str, title_2: str, acceptance_2: str) -> str:
    """Split a backlog story in two smaller stories. The card keeps part 1, a new card gets part 2."""
    def go(state):
        a, b = board.split_story(state, card_id, [(title_1, acceptance_1), (title_2, acceptance_2)])
        return f"#{a['id']} '{a['title']}' and #{b['id']} '{b['title']}' (both need a new estimate)"
    return _run(go)


@mcp.tool()
def move_card(card_id: int, to_status: str) -> str:
    """Move a card between columns: todo, inprogress, testing. Done is reached only by passing a test."""
    return _run(board.move_card, card_id, to_status)


@mcp.tool()
def add_dev_notes(card_id: int, notes: str) -> str:
    """Developer (or the tester, for a testing task): add implementation notes to a card."""
    return _run(board.add_dev_notes, card_id, notes)


@mcp.tool()
def set_work_left(card_id: int, work_left: float) -> str:
    """Work simulator: record how many story points of development are left on a card."""
    return _run(board.set_work_left, card_id, work_left)


@mcp.tool()
def set_test_left(card_id: int, test_left: float) -> str:
    """Work simulator: record how much testing is left on a card."""
    return _run(board.set_test_left, card_id, test_left)


@mcp.tool()
def write_test_cases(card_id: int, test_cases: str) -> str:
    """Tester: write the test plan / test cases (with sample data) on a story or its test ticket."""
    return _run(board.write_test_cases, card_id, test_cases)


@mcp.tool()
def record_test_result(card_id: int, result: str, test_cases: str = "", note: str = "") -> str:
    """Tester: record 'pass' (card goes to Done) or 'fail' (card goes back to In Progress)."""
    return _run(board.record_test_result, card_id, result, test_cases, note)


@mcp.tool()
def log_bug(title: str, description: str = "", related_card_id: int = 0) -> str:
    """Tester: log a bug. If it relates to a sprint card it joins the sprint."""
    return _run(board.log_bug, title, description, related_card_id or None)


@mcp.tool()
def set_sprint_goal(goal: str) -> str:
    """Scrum Master / Product Owner: set the goal for the current sprint."""
    return _run(board.set_sprint_goal, goal)


@mcp.tool()
def accept_story(card_id: int, accepted: bool = True, note: str = "") -> str:
    """Product Owner: accept a Done story at Sprint Review, or reject it back to the backlog."""
    return _run(board.accept_story, card_id, accepted, note)


if __name__ == "__main__":
    mcp.run()  # stdio transport
