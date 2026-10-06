# SAINT v0.4 architecture

How the parts added in v0.4 fit into SAINT. The [guide](GUIDE.md#architecture) has the full pipeline
(microphone → wake word → STT → agent → tools → TTS); this page is about the agent task loop, the
interface built around it, data migration and the packaged app.

## 1. Two paths for a request

```
"pause Spotify"                         "set up my coding workspace for SAINT, open Discord
        │                                and put Spotify on my second monitor"
        ▼                                        │
deterministic router (modules/agent/router.py)   ▼
one intent → one validated tool          agent task loop (modules/agent/autonomy)
reply from the tool's real result        observe → plan → act → verify → recover → learn
```

`agent.handle()` keeps its order (pending answers, "stop", lessons, scenes, aliases, corrections…).
Two hooks were added:

1. **Task control** right after the meta commands: "pause", "continue", "what's next?", "why did
   that fail?", "do that again", "remember how I just did that", "actually put Spotify on my main
   monitor" — only when there *is* a task (a bare "pause" with music playing is still the music).
2. **Task start** before learned skills: a learned procedure, a built-in goal, or several steps
   with "tell me when it's ready" become an agent task. A complex request the router doesn't
   understand may be planned by the local model — only when no deterministic option applies.

Everything else is unchanged: simple requests never wait for a planner or a model call.

## 2. The task loop (`modules/agent/autonomy`)

| File | Role |
|---|---|
| `model.py` | `AgentTask` (request, goal, context, plan, current step, status, observations, retries, failures, result, learned procedure, trail) and `PlanStep` (action, label, tool/args, expect, verify, fallback, risk, status, attempts, notes). Plain data, saved to `agent_tasks.json`. |
| `planner.py` | Request → plan, cheapest first: learned procedure → built-in goal → the user's own clauses → the local model. Every model-proposed step must be a command SAINT understands and be grounded in the request; one bad step rejects the plan. |
| `goals.py` | Built-in goals built from observations: coding workspace, run project / dev server, run tests, find-why-it-won't-start-and-fix, download-and-run, meeting prep. |
| `observe.py` | One `Observer`: windows, foreground, processes, installed apps, monitors, files, ports, HTTP, clipboard, Spotify state, the browser's address bar, on-screen text. Cached for under a second; never raises. |
| `verify.py` | Per-step checks (window, foreground, process, port, http, file, monitor, spotify, browser_url, screen_text), polled with bounded timeouts; inferred from the command when the plan has none. |
| `recover.py` | Classify a failure and find another way (§4). |
| `policy.py` | Risk per step and what the permission mode allows (§5). |
| `executor.py` | Runs one task on its own thread (§3). |
| `manager.py` | Current task, history, events, voice control, learning (§6). |
| `control.py` | The spoken task-control commands. |

### States

`understanding → observing → planning → executing ⇄ verifying ⇄ recovering → completed`, plus
`waiting_for_user`, `paused`, `failed`, `cancelled`. Steps are `pending / running / done / failed /
skipped`. A task interrupted by a restart comes back `paused`.

## 3. Executing a step

```
permission  policy.decide(risk)  →  run | one "go ahead?" for the plan | ask at this step | not allowed
act         a built-in tool (step.tool + args) or a SAINT command through the router — the same
            permission checks and confirmations as speech
ask?        the action asked something ("Which window?", "Do you want me to…?"): the task parks
            (WAITING_FOR_USER), the question is spoken with the reply window open, and the
            answer resumes the task from that step
verify      verify.py; a reported failure is checked once, a reported success is polled
recover     recover.py, within budget; then the step is retried
```

Bounds: 2 recoveries per step (3 attempts), 6 per task, a 15-minute wall clock
(`agent.task_timeout_sec`), at most 24 steps including inserted ones. Pause / resume / cancel are
checked between steps and while waiting; while the user is talking to SAINT the runner waits at
the next step boundary (`AgentTaskManager.user_turn`), so "actually use the other monitor" never
races a step that's halfway through. A new task pauses the current one and resumes it afterwards.

## 4. Recovery

Deterministic first:

| Failure | Recovery |
|---|---|
| app not found | re-scan installed apps (Start menu, Start apps, Program Files) and retry with the real name |
| app / window missing | open the app first, then retry the step |
| Spotify has no device | open Spotify, wait, retry |
| check didn't pass after a reported success | bring the window to the front, or wait and look again |
| dev server exited | read its output (`modules/dev/project.diagnose`): missing packages → install them (MEDIUM, asks in Confirm mode), missing Python module → pip install it, busy port → check again |
| the plan's own fallbacks | tried in order before anything else |

Only then one compact reasoning call to the local model: the goal, the failed step, the error and a
one-line observation — never the conversation or SAINT's whole state. Its answer must be a single
command SAINT understands and grounded in the request, or it's ignored.

## 5. Risk and permission modes

| Risk | Examples |
|---|---|
| LOW | open / switch / move windows, read files and the clipboard, Spotify playback and volume, navigate and read websites |
| MEDIUM | type text, edit or move files, install packages, send ordinary messages, close apps |
| HIGH | delete, uninstall, shell commands, system / security settings, power, payments, sensitive information |

| Mode | LOW | MEDIUM | HIGH |
|---|---|---|---|
| Safe | run | refused up front, with why | refused |
| Confirm (default) | run | one "go ahead?" for the whole plan | asked at that step |
| Autonomous | run | run | asked at that step (unless `automation.confirm_dangerous` is off) |

Tools keep their own policy underneath (`core/permissions.py`). A HIGH step the user just approved
doesn't ask twice, except the tools that always ask with their own specific question (Recycle Bin,
moving files, uninstalling, power, force quit, end task).

## 6. Learning (`modules/learning/procedures.py`)

A finished task (from a goal, the user's own steps or a model plan) becomes a **procedure**: its
steps stay in `skills.json` (so it appears on Automations › Learned, can be edited there, and older
SAINT versions still run it), and `procedures.json` adds each step's label, check, fallbacks and
risk, a version and a history. Next time the same request runs the procedure instead of planning.

A procedure changes only when it's validated by a real run: a step that failed and was replaced by
a recovery that then passed its check, or a step the user changed mid-task ("actually …"), becomes
the new step (the old one is kept as a fallback) with a version bump and the reason. A
model-suggested replacement that didn't run and pass is never saved. "Remember how I just did
that" / "save that as X" saves the last task explicitly.

## 7. Memory, kept apart

| Kind | Where | Goes into a prompt? |
|---|---|---|
| Conversation | `modules/ai/context.py` (last turns) | the chat model, as before |
| About you | `memory/saint_memory.db` | the few memories that match the request, plus a handful of core facts |
| Tasks | `agent_tasks.json`, `tasks.json` | never; the UI and "where were we?" read them |
| Skills / procedures | `skills.json`, `procedures.json` | the planner gets recent learned steps |
| Observations | the observer's cache, `modules/agent/recent.py` | one compact line for planning / recovery |
| History | `history.jsonl` | never |

The planner and recovery prompts are built from: the command catalog, one line of observation, and
the goal / failure. That's why a model call stays small and fast.

## 8. Development agent (`modules/dev`)

`project.py` finds a project (SAINT's registry, then the usual code folders), detects how it runs
(start, test, install commands and the port), runs tests and installs with the output captured,
manages dev servers (output kept, URL detected, stopped with SAINT) and diagnoses failures. Tools
that run commands are MEDIUM and never offered to the LLM. Nothing edits source files on its own.

## 9. Observation ladder

```
structured API (Spotify, files, sockets)  →  OS (windows, processes, monitors)  →  UI Automation
  →  OCR (Windows.Media.Ocr, local)  →  a vision model (only when allowed and configured)
```

`Observer.locate()` and `screen_text()` stop at the first rung that answers and record which one it
was in the trail.

## 10. The interface (`ui/design`, `ui/components`)

Tokens (`ui/design/tokens.py`) for spacing, radii, type, motion and status colours; colours from
`ui/theme.py` (theme + accent). Components: `Panel`, `TaskCard`, `TrailView`, `HealthPanel`,
`DevicesPanel`, `AttentionPanel`, `AgentStatusBar`, `Sidebar`, `OrderedChecklist`, `QrView`. Pages
compose components and only read data handed to them (`AgentTask.summary()`, startup steps, Link
device states); buttons go through `ui/actions.py`, which runs anything slow off the GUI thread.

Layout is the user's: `layout.sidebar_hidden/order`, `layout.overview_hidden/order`,
`layout.status_bar`, `layout.start_monitor` (Settings › Appearance › Layout, applied live).

Two Qt details worth knowing: `clear_layout()` detaches widgets before `deleteLater()` (otherwise
removed rows stay painted until the event loop deletes them), and `Panel` calls
`updateGeometry()` on every `LayoutRequest` while the Overview moves panels with `removeWidget()`
— a stale height-for-width in the column's layout item otherwise squeezed rebuilt rows to a few
pixels.

## 11. Data migration (`core/migration.py`)

Runs at startup before the settings load: pending steps from `migrations.json`; a checked backup of
every user file to `%LOCALAPPDATA%\SAINT-backups\before-<version>-<time>\`; idempotent steps
recorded one by one (an interrupted run resumes); nothing deleted. A source run uses
`%LOCALAPPDATA%\SAINT` once SAINT has data there (`core/paths.py`), so a new checkout never starts
empty. `python -m core.migration --check [folder]` lists what's pending.

## 12. The packaged app

`packaging/windows/build.py` builds `SAINT.exe` (PyInstaller, one folder) and `SAINT-Setup.exe`
(Inno Setup, per-user), refuses to ship personal data, and finishes by running the packaged
**`SAINT.exe --selftest`** with a fresh data folder, its own folder as the working directory and no
Python on `PATH` — imports, bundled assets, the UI, the pairing QR, OCR, the agent and (with
`--selftest-voice`) a full voice round trip: Kokoro speaks "Hey SAINT, open Discord", the wake-word
model scores it, Whisper writes it back. A failing required check fails the build.
