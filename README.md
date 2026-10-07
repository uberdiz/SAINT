<p align="center"><img src="SAINT.png" width="88" alt="SAINT logo"></p>

<h1 align="center">SAINT</h1>

<p align="center"><b>A local, voice-first AI assistant for Windows.</b><br>
Say “Hey SAINT” — it listens, does the real thing, and answers out loud. Everything runs on your PC.</p>

<p align="center">Version 0.4.0 — <a href="docs/CHANGELOG.md">what's new</a></p>

![SAINT — Home](docs/screenshots/home.png)

SAINT listens for its wake word, works out what you want, does it with real tools (Spotify, apps, windows,
keyboard and mouse, browser, files, reminders) and answers out loud. The language model runs locally through
[Ollama](https://ollama.com); wake word, speech recognition and the voice are local too.

**More:** [full guide](docs/GUIDE.md) · [architecture](docs/ARCHITECTURE.md) · [changelog](docs/CHANGELOG.md) ·
[manual test checklist](docs/MANUAL_TESTING.md) ·
[iPhone app](mobile/README.md) · [onboarding design](docs/design/ONBOARDING.md)

## What it does

- **Voice first.** “Hey SAINT” / “SAINT”, GPU speech recognition, Kokoro voice, interruptions. After a reply,
  follow-ups need no wake word. While music plays, “skip”, “skip 3 songs”, “pause”, “louder” work on their own.
- **Does whole tasks, and checks its work.** “Set up my coding workspace for SAINT, open Discord and put Spotify on
  my second monitor” becomes a plan SAINT runs step by step: it looks before it acts, checks each step really
  happened (the window is there, the server answers, the song is playing), tries another way when one fails, and
  learns the finished task as a procedure. Pause, continue, “what's next?”, “why did that fail?”, or change it
  mid-task (“actually use my main monitor”). Simple requests stay instant — no planner, no model call.
- **Safe, Confirm or Autonomous.** Every step has a risk (low / medium / high). Confirm mode asks once for a plan
  with medium steps and at each high one; Safe refuses anything beyond low; Autonomous only asks for high ones.
- **A development helper.** “Open SAINT and run the tests”, “start the dev server”, “why won't it start?” — it finds
  the project, runs the commands with their output kept, reads the errors and offers the fix (installing a missing
  package asks first). It never edits your code on its own.
- **Asks before it acts.** A task it doesn't fully know (“write me an email”) gets its questions first —
  which account, who to, what about — and anything that sends, deletes or closes waits for your yes.
- **Your PC by voice.** Open, switch, close, move and snap apps and windows across monitors; click, type and
  scroll; browser and YouTube control; Steam games; drive clean-up; per-app volume and audio devices.
  App names are matched by sound against what's installed, so “open clad” opens Claude.
- **Spotify.** Artists, albums, playlists, moods, “something like this”, queues that replace SAINT's old ones,
  a listening memory, and a mini player that stays over games.
- **Scenes and learning.** One phrase runs many steps; show SAINT something once and it can do it again.
- **Memory, reminders, schedules, History** — all stored on your PC.
- **An interface built around what SAINT is doing.** Overview (the current task, what needs you, devices,
  health), Tasks (every task with its steps and checks) and Activity (each tool and result). Pages, panels and the
  status bar can be hidden and reordered (Settings → Appearance → Layout).
- **The Halo, overlay (<kbd>Alt</kbd>+<kbd>`</kbd>) and command palette (<kbd>Ctrl</kbd>+<kbd>K</kbd>).**
- **SAINT Link.** Your iPhone and other PCs talk to this SAINT, encrypted — on Wi-Fi, over Tailscale, or from
  anywhere with *Reach this PC from anywhere* (router port / IPv6, nothing to install on the phone). Their logs are
  collected on this PC too.

## Install

**Installer:** download `SAINT-Setup.exe` from the
[latest release](https://github.com/uberdiz/SAINT/releases/tag/unified-latest) and run it (no admin needed).
It downloads SAINT's voice (Kokoro, ~350 MB) during setup; the speech-recognition model (~150 MB) downloads
on first start. Install [Ollama](https://ollama.com) and `ollama pull llama3.1` for conversation.

**From source** (Windows 10/11, Python 3.12, NVIDIA GPU recommended):

```bat
git clone https://github.com/uberdiz/SAINT.git
cd SAINT
setup.bat
run.bat
```

`setup.bat` creates `.venv`, installs CUDA PyTorch when it finds an NVIDIA GPU, installs `requirements.txt`,
downloads the Kokoro voice (`tools/get_kokoro_onnx.py`) and runs the tests. Installing by hand? Use
`pip install -r requirements.txt` **without** `--upgrade` — that swaps the CUDA torch for a CPU one and breaks
Kokoro.

## Run

```bat
run.bat                    & rem or: .venv\Scripts\python app.py
run.bat --background       & rem start hidden in the tray
run.bat --profile clean    & rem test as a brand-new install
run.bat --profile demo     & rem test with sample scenes and answers
python tools\profiles.py snapshot   & rem a copy of your data to test against
```

Closing the window keeps SAINT in the tray. Only one SAINT runs at a time. A test profile has its own folder
in `%LOCALAPPDATA%\SAINT-profiles` (own memory, history, settings, Spotify login); your real data in
`%LOCALAPPDATA%\SAINT` is never touched.

## Things to say

| You say | What happens |
|---|---|
| “Hey SAINT, play my gym playlist” · “play the album Discovery” · “play something chill” | Spotify |
| “skip” · “skip 3 songs” · “pause” · “louder” (music playing, no wake word) | Playback |
| “open Discord” · “switch to Spotify” · “close Notepad” (asks first) | Apps and windows |
| “move this window to my second monitor” · “snap Chrome to the left” | Window placement |
| “search YouTube for Kendrick Lamar, click the first video and pause” | Multi-step, each step checked |
| “write me an email to Sam about Friday” | Runs your email scene: asks what it needs, stops before Send |
| “what's on my second screen?” · “what is this error?” | Screen understanding |
| “remind me at 5 PM to stretch” · “every weekday at 9 play my focus playlist” | Reminders and schedules |
| “my sister's name is Mia” · “what do you know about me?” | Memory |
| “talk louder” · “voice volume 60” | SAINT's own voice volume |
| “gaming mode on” · “I'm done gaming” | Moves SAINT off the game's screen and back |
| “set up my coding workspace for SAINT, open Discord and tell me when it's ready” | An agent task: plan, act, check each step |
| “open SAINT and run the tests” · “start the dev server” · “why won't it start?” | Development helper |
| “pause” · “continue” · “what's next?” · “why did that fail?” · “remember how I just did that” | The current task |
| “stop” · “cancel” | Stops speaking and whatever is running |

Anything else goes to the language model, which can use the same tools. The [guide](docs/GUIDE.md#talking-to-saint)
has the full list.

## Build the installer

```bat
.venv\Scripts\python packaging\windows\build.py
```

Builds `build\windows\SAINT\SAINT.exe` and `build\windows\SAINT-Setup.exe` (needs Python 3.12 and Inno Setup 6,
which it installs with winget if missing). The build stops if Kokoro's packages aren't in the bundle (the app
would fall back to the robotic Windows voice) or if any personal data — settings, memory, history, your own
addresses — would ship. It finishes by running the packaged **`SAINT.exe --selftest`** with a fresh data folder
and no Python on `PATH` (libraries, bundled models, every page, the pairing QR, OCR, the agent); a failed check
fails the build. `--selftest-voice` adds a full voice round trip (Kokoro → wake word → Whisper), `--no-selftest`
skips it. Pushing to the `unified-release` branch builds and publishes the release on GitHub.

## Where things live

| | |
|---|---|
| Settings, memory, history, scenes, logs | `%LOCALAPPDATA%\SAINT` (`logs\` for debugging) |
| Voice and speech models | `%LOCALAPPDATA%\SAINT\tts`, `…\models` — or `data\tts` in a source checkout |
| Backups made before an upgrade migrates your data | `%LOCALAPPDATA%\SAINT-backups\before-<version>-<time>` |
| Installed app | `%LOCALAPPDATA%\Programs\SAINT` |
| Spotify login, Link key | Windows Credential Manager |

## Troubleshooting

- **SAINT sounds robotic** — it's using the Windows voice because Kokoro didn't load. The System page says why;
  from source run `.venv\Scripts\python tools\get_kokoro_onnx.py` and check `import kokoro_onnx` works.
- **It doesn't hear “SAINT”** — Settings → Wake Word → lower the threshold; check the right microphone is picked.
- **No answers to questions** — Ollama isn't running or has no model: `ollama pull llama3.1`, then Retry on the System page.
- **Spotify does nothing** — Settings → Spotify → Connect, with the Spotify app open on one device.
- **Is the install complete?** — `SAINT.exe --selftest report.json` (or `python app.py --selftest`) checks the
  libraries, models, interface and agent without a microphone and writes what it found.
- **Upgrading from an older SAINT** — your data is migrated in place on first start, after a full backup to
  `%LOCALAPPDATA%\SAINT-backups`; nothing is deleted. `python -m core.migration --check` lists what's pending.

More in the [guide](docs/GUIDE.md#troubleshooting).

## Tests

```bat
.venv\Scripts\python -m pytest -q
```

Manual checks still to do are in [docs/MANUAL_TESTING.md](docs/MANUAL_TESTING.md).

## Privacy

Wake word, speech recognition, voice and the language model run on your PC. Memory, history and settings stay
in `%LOCALAPPDATA%\SAINT`. Network use is only what you ask for: Spotify, web search if you enable a provider,
model downloads, and SAINT Link between your own devices.
