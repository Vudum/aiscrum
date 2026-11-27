# Contributing to Scrum Simu

Thanks for helping! This guide gets you from clone to a working change. Read
[architecture.md](architecture.md) first if you want the full picture.

## 1. Set up

```
git clone https://github.com/Vudum/aiscrum.git
cd aiscrum
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py            # http://localhost:5050
```

- **No API key needed to develop.** With `ANTHROPIC_API_KEY` empty the app runs in Practice mode:
  every AI line falls back to a script, so meetings, the board and charts all work for free.
- Add a key only when you work on AI behaviour (prompts, routing, the Board Keeper).
- `REAL_SECONDS_PER_DAY=0` makes the simulated team's work happen instantly between meetings.
- There is no build step. Edit `static/*` and hard-refresh the browser (Cmd/Ctrl+Shift+R).
  Python changes need a restart (`Ctrl+C`, `python app.py`).

## 2. Where to change what

| I want to change... | Look in |
|---|---|
| What happens in a meeting, step by step (who speaks, about what) | `agendas.py` (`kickoff.py` for the kickoff) |
| Turn-taking: whose turn, who answers you, pass/skip/park, nudges | `app.py` (`meeting_next`, `meeting_say`, `_route`, `_pick_turn`, `_instruction`) |
| Scrum rules: capacity, velocity, sprint end, carry-over, which meeting is allowed | `board.py` (`capacity`, `add_to_sprint`, `after_meeting`, `check_meeting`) |
| The simulated developer/tester work between meetings | `board.py` (`plan_work`) |
| What AI teammates say and know (persona, context, prompt) | `agents.py` (`_system`, `_context`, `ai_turn`) |
| How agreements become board changes | `agents.py` (`KEEPER_SYSTEM`, `ai_board_keeper`) and the tools in `board_server.py` |
| A new board tool for the AI | add a rule in `board.py`, expose it in `board_server.py`, add its name to `KEEPER_TOOLS` in `agents.py` |
| Scripted lines for Practice mode | `agents.py` (`mock_opening`, `mock_reply`, `mock_closing`) and `script` fields in `agendas.py` |
| Budget and limits | `guardrails.py` and `.env.example` |
| The page: meeting room, captions, voice, board, backlog, charts | `static/app.js`, `static/style.css`, `static/index.html` |
| Voices (text-to-speech) | `/api/tts` in `app.py` (neural voices via edge-tts) and `speak()` in `static/app.js` |

## 3. Ground rules for the code

- **Rules in code, words from AI.** Anything that must be correct (points, capacity, what's Done) is
  decided in Python, never by the model. The AI only talks and *asks* for board changes.
- **The board changes through `board.py` functions**, called directly or via the MCP tools. Don't edit
  card dicts from scattered places.
- **Always keep a fallback.** Every AI call must work without a key or when the API fails: return a
  scripted line or do nothing, and log the problem with `agents.log_ai(...)` (goes to `data/ai_log.jsonl`).
- **Don't let the AI claim what didn't happen.** If a teammate says "I've updated the board", make sure a
  tool really does it (or change the prompt so it doesn't say so).
- **Keep teammates short and human.** Spoken lines are trimmed to about 2 sentences (`agents.brief`).
- **Plain language** in everything a player sees: short sentences, no jargon.
- Python 3.10+, standard library + the packages in `requirements.txt`. Front end is plain JavaScript,
  no framework, no build.

## 4. Test your change

There is no automated test suite yet (adding one is a welcome contribution, see below). Before opening
a pull request:

1. `python -c "import app"` and `node --check static/app.js` run without errors.
2. Play it in Practice mode: start a project, run the kickoff and at least the meeting you changed.
3. If you changed AI behaviour, play it once with a key too, and check `data/ai_log.jsonl` for new errors.
4. Look at the **Behind the scenes** tab: board changes you expect should appear there as tool calls.

To test against a copy of a real project, copy `data/projects/<id>.json` to a new id and drive it with
Flask's test client (`app.app.test_client()`) instead of touching the original.

## 5. Pull requests

1. Fork the repo and create a branch: `git checkout -b fix-short-description`.
2. Keep each pull request to one change, and describe **what a player will notice** and how you tested it.
3. Never commit `.env`, `data/` or API keys (they're in `.gitignore`; double-check `git status`).

## Good first contributions

- Automated tests for `board.py` rules (capacity, carry-over, `add_to_sprint`, `estimate_story`).
- A test that plays a whole meeting in Practice mode through the Flask test client.
- Captions/voice polish in `static/app.js`.
- Ideas listed under "Possible next steps" in [architecture.md](architecture.md).

## 6. Deploy on cPanel

1. cPanel -> **Setup Python App** -> Create application: Python 3.10+, application root `scrumsimu`,
   startup file `passenger_wsgi.py`, entry point `application`.
2. Put the code in the application root (`git clone https://github.com/Vudum/aiscrum.git .`
   via SSH / cPanel Git, or upload the files without `.venv`, `data` and `.env`).
3. Run pip install with `requirements.txt` from the app page.
4. Add the settings from `.env.example` as environment variables (or create a `.env`). Use an API key
   with a monthly spend limit.
5. If AI teammates never answer on the host, set `MCP_TRANSPORT=memory` (some hosts block a second process).
6. Restart the app. It works under a path like `yourdomain.com/scrumsimu/` (keep the trailing slash).

## Settings and limits

| Setting (`.env`) | Default |
|---|---|
| `DAILY_BUDGET_USD` (all visitors together; then scripted mode) | 1.00 |
| `MAX_MESSAGE_CHARS` | 2500 |
| `MAX_MESSAGES_PER_MEETING` | 20 |
| `MAX_PROJECTS_PER_VISITOR_PER_DAY` (public site only) | 3 |
| `REAL_SECONDS_PER_DAY` (0 = instant) | 90 |

The board server is a normal MCP server: add it to Claude Desktop with `command: python`,
`args: ["<path>/board_server.py"]`, env `SCRUMGYM_PROJECT_FILE=<path>/data/projects/<id>.json`.
