# SAINT v0.1 — "Core"

**SAINT is a framework, not a chatbot.** This is the minimal, working
skeleton: a desktop app that loads modules, displays system status,
talks to an LLM, and exposes internal analytics. Everything communicates
through the Core (Event Bus, Module Manager, Config, Logging, Analytics,
State) — no module talks to another module directly.

## Quick start

```bash
cd saint
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

The app launches straight to the Dashboard. By default the AI module
uses a **mock provider** so you can exercise the whole pipeline
(prompt → event → log → analytics → response) with zero configuration
and no API key.

## Using a real model

Go to **Settings** and set:

- **AI Provider**: `openai` (any OpenAI-compatible endpoint) or `ollama`
  (a local model server)
- **AI Model**: e.g. `gpt-4o-mini`, or a local model name like `llama3`
- **Base URL**: e.g. `https://api.openai.com/v1` or `http://localhost:11434`
- **API Key**: required for `openai`, not needed for `ollama`

Click **Save Settings** — this persists to `data/config.json` and takes
effect on your next prompt, no restart required.

## What's here (v0.1 scope)

| Feature | Status |
|---|---|
| Dashboard (status, CPU/RAM, uptime, events, errors) | ✅ |
| AI module (prompt → provider → response → log) | ✅ |
| Module Manager (list/enable/disable, completion %) | ✅ |
| Event Bus (everything is an event) | ✅ |
| Logging (rotating file logs, mirrored to Console) | ✅ |
| Analytics (runtime, commands, avg response, errors) | ✅ |
| Health Monitor | ✅ |
| Console (live event stream) | ✅ |
| Settings (theme, AI config, logging, analytics, auto-save) | ✅ |
| Voice / Automation / Vision / Memory | Present as real, disabled, 0%-complete modules — reserved for v0.2–v0.5 |

## Project layout

```
saint/
├── app.py                  # entry point
├── core/
│   ├── config.py            # JSON-backed settings, persisted across restarts
│   ├── events.py            # Event Bus — the spine of the app
│   ├── logger.py             # rotating file logger, wired to the event bus
│   ├── state.py               # uptime, CPU/RAM, event/error counters
│   ├── analytics.py            # commands, response times, errors, crashes
│   └── module_manager.py        # owns every module instance
├── modules/
│   ├── base.py               # BaseModule: enable/disable/completion %
│   ├── ai/
│   │   ├── module.py          # the only active module in v0.1
│   │   └── providers.py        # mock / openai-compatible / ollama backends
│   ├── voice/module.py        # disabled stub (v0.2)
│   ├── memory/module.py       # disabled stub (v0.3)
│   ├── automation/module.py   # disabled stub (v0.4)
│   └── vision/module.py       # disabled stub (v0.5)
├── ui/
│   ├── main_window.py        # sidebar + page stack
│   ├── dashboard.py           # home screen + AI test panel
│   ├── module_manager_ui.py   # module list, toggles, subtasks
│   ├── analytics_ui.py        # analytics tiles
│   ├── health_ui.py            # health monitor tiles
│   ├── console_ui.py           # live event/log stream
│   ├── settings_ui.py          # settings form
│   ├── workers.py               # QThread wrapper so AI calls never block the UI
│   └── theme.py                 # dark/light QSS stylesheets
└── data/
    ├── config.json             # persisted settings
    ├── analytics.json           # persisted analytics counters
    ├── logs/                     # rotating log files
    └── memory/                    # reserved for v0.3
```

## Definition of Done (v0.1) — verified

- [x] Application launches to the dashboard without crashing
- [x] Module Manager can discover and enable/disable modules
- [x] The AI module accepts a prompt and returns a response
- [x] Every action generates an event through the Event Bus
- [x] All events are written to log files (`data/logs/saint.log`)
- [x] Analytics update live from those events
- [x] Settings persist across restarts (`data/config.json`)
- [x] The UI remains responsive while the AI processes requests (via `QThread`)
- [x] No unhandled exceptions occur during normal use

## Expansion path

Every future capability plugs into this same Core:

```
v0.2  Voice Input / Output / Wake Word
v0.3  Long-Term Memory / Conversation History / Memory Search
v0.4  Desktop Automation / Keyboard / Mouse / Window Management
v0.5  Vision / OCR / Screen Understanding / Object Detection
v0.6  Plugin SDK / Third-Party Modules / Marketplace / Permissions
v1.0  Fully Integrated Assistant
```

To add a new module: subclass `BaseModule` in `modules/<name>/module.py`,
register it in `core/module_manager.py`, and it automatically shows up
in the Module Manager UI with its own completion bar — no other code
needs to change.
