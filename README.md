# Scrum Gym

Practice a real Scrum project in your browser. Pick a role (Product Owner or Scrum Master).
AI teammates play everyone else, including the Developer and the Tester. They run the ceremonies with you,
push back, do their work over time, and demo a clickable prototype at every Sprint Review.
Three sprints, kickoff to final retro. After the kickoff you choose the next meeting
(Refinement, Planning, Stand-up, Review, Retro). The usual next one is marked "Suggested";
pick one that doesn't make sense yet and a teammate tells you why and closes it.

## Run it on your Mac (5 minutes)

1. Open Terminal and go to this folder:
   ```
   cd "/Users/vud/Documents/claude scripts/aiscrum"
   ```
2. Create a virtual environment and install the libraries (needs Python 3.10 or newer):
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Make your settings file:
   ```
   cp .env.example .env
   ```
   - Leave `ANTHROPIC_API_KEY` empty to test for free in **Practice mode** (scripted team).
   - Paste your key from https://console.anthropic.com to get the **real AI team**.
   - For fast testing set `REAL_SECONDS_PER_DAY=0`.
4. Start it:
   ```
   python app.py
   ```
5. Open http://localhost:5050

Stop with `Ctrl+C`. Next time, only steps 1, `source .venv/bin/activate` and 4 are needed.

## How it works

```
Browser (role select, meeting room, board, backlog, charts, demo)
   |
App server - app.py (Flask)
   |-- runs the ceremony order, the gates, the $1/day cap        board.py, guardrails.py
   |-- AI agents: PO, SM, Dev, QA (new names each project) agents.py  -> Claude API
   |-- MCP client                                                 mcp_client.py
          |
       MCP server: the Scrum board tools                          board_server.py
          (create_story, estimate_story, add_to_sprint, move_card,
           record_test_result, log_bug, set_sprint_goal, accept_story ...)
          |
       Project file (data/projects/<id>.json)
```

- **Claude decides, the MCP client carries, the MCP server does.** When the developer says
  "I finished the login story", the request travels as an MCP `CallToolRequest` to the
  board server, which moves the card. The **Behind the scenes** tab lists every call.
- **Rules are code, words are AI.** Which meeting makes sense right now, your to-dos,
  how much work gets done between stand-ups and the pass/fail of tests are plain Python.
  The agents only talk and ask for board actions.
- **Time-based work.** Each stand-up (and the review) counts as one working day: the AI
  developer does about 3 points and the tester about 5, scaled by how much real time passed
  since the last check-in (`REAL_SECONDS_PER_DAY`).
- **Charts.** Burndown (per sprint, one point per stand-up), burn-up (scope vs done, shows scope creep) and velocity.

### Files

| File | What it is |
|---|---|
| `app.py` | Web server and API routes |
| `board.py` | Project data, Scrum rules, gates, work simulator, chart data |
| `board_server.py` | MCP server exposing the board tools |
| `mcp_client.py` | MCP client that the app uses to call those tools |
| `agents.py` | Personas, Claude agent loop, practice-mode script, coaching, demo mock-ups |
| `guardrails.py` | Daily budget, message limits, project limits |
| `static/` | The web page (HTML, CSS, JavaScript) |

### Guardrails

| Guardrail | Default |
|---|---|
| Daily AI budget for all visitors | $1.00 (then the team falls back to Practice mode and shows your contact link) |
| Message length | 500 characters |
| Messages per meeting | 20 |
| New projects per visitor per day | 3 |
| Sprints | 3 |
| Agents | Can't move cards to Done, can't do the human's job, stay on topic |

### Try the MCP server in Claude Desktop (optional)

The board server is a normal MCP server. Add it to Claude Desktop's config with
`command: python`, `args: ["<path>/board_server.py"]` and env
`SCRUMGYM_PROJECT_FILE=<path>/data/projects/<id>.json`, and Claude can read and move
your cards too.

## Put it on lvudum.com (cPanel / Aquahost)

1. cPanel -> **Setup Python App** -> Create application:
   Python 3.10+ (3.11 or newer is best), application root `scrumgym`,
   application URL `lvudum.com/scrumgym` (or a subdomain), startup file `passenger_wsgi.py`,
   entry point `application`.
2. Upload this folder's files into the application root (not `.venv`, `data` or `.env`).
3. In the app page, run pip install with `requirements.txt`.
4. Add the settings from `.env.example` as environment variables (or upload a `.env`).
   Use a fresh API key with a monthly spend limit set in the Anthropic console.
5. If AI teammates never answer on the host, set `MCP_TRANSPORT=memory`
   (some shared hosts block starting a second process).
6. Restart the app.

The page uses relative links, so it works at a path like `lvudum.com/scrumgym/` (keep the trailing slash) or on a subdomain.
