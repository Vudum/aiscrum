# Scrum Simu

**Practise Scrum by voice with an AI team.** You play the Product Owner or Scrum Master; AI teammates
(Scrum Master, PO, Developer, Tester) run real ceremonies with you: kickoff, refinement, planning,
stand-ups, review and retro, across three sprints. They talk, push back, do their work between
meetings, and update a live Scrum board.

## What it shows

- **Multi-agent AI:** each teammate is a Claude agent with its own role; a separate "Board Keeper" agent
  turns what the team agrees into board changes.
- **MCP (Model Context Protocol):** the board is an MCP server; agents change it only through tools,
  and every call is visible in the app.
- **Voice UI:** speech-to-text in, neural voices out, captions in step with the voice.
- **Guardrails:** Scrum rules in code (not left to the AI), a daily cost cap, and a free scripted mode.

Python · Flask · Claude API · MCP · plain JavaScript

## Run it

```
git clone https://github.com/Vudum/aiscrum.git && cd aiscrum
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add ANTHROPIC_API_KEY for the AI team, or leave empty for free scripted mode
python app.py               # open http://localhost:5050 (Chrome is best for voice)
```

## More

- [How it works](architecture.md): architecture and design choices
- [Contributing](CONTRIBUTING.md): setup, code map, testing
- Deploying on cPanel hosting: see [Contributing → Deploy](CONTRIBUTING.md#6-deploy-on-cpanel)
