# SAINT guide

The full reference: every feature, setting and limitation. The [README](../README.md) is the short version.

## Contents

- [Highlights](#highlights)
- [What works today](#what-works-today)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running SAINT](#running-saint)
- [Talking to SAINT](#talking-to-saint)
- [Wake word](#wake-word)
- [Speech recognition (STT) and speech output (TTS)](#speech-recognition-stt-and-speech-output-tts)
- [Interrupting SAINT](#interrupting-saint)
- [AI provider / Ollama](#ai-provider--ollama)
- [Spotify](#spotify)
- [Memory](#memory)
- [Reminders and automations](#reminders-and-automations)
- [Desktop control and permissions](#desktop-control-and-permissions)
- [Screen awareness](#screen-awareness)
- [Your PC, hands-free](#your-pc-hands-free)
- [Languages](#languages)
- [SAINT Link](#saint-link)
- [Learning](#learning)
- [The interface](#the-interface)
- [Configuration](#configuration)
- [Logging and debugging](#logging-and-debugging)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Privacy and security](#privacy-and-security)

---

## What works today

Everything below was verified on the development machine: Windows 11, RTX 5060 Ti, Python 3.14,
Ollama 0.34 with `llama3.1`. The check combined the automated tests with a live end-to-end run of the
real runtime (wake model, faster-whisper, agent, Ollama, Kokoro TTS, scheduler and desktop tools).
That live run fed synthesized speech into SAINT's voice loop in place of the microphone.

| Area | Status |
|---|---|
| Wake word "Hey SAINT" and "SAINT" | ✅ Two stages: the local ONNX model (CPU, ~1 ms per 80 ms frame) plus a transcript check for short utterances the model misses (it barely scores a bare "SAINT"). On synthetic speech from 4 voices, clean and with music at −8 dB, 64/64 wake decisions were correct, with no false wakes on "paint", "She is a saint" or plain commands. Background speech is never acted on or shown. |
| Conversation mode (no wake word for follow-ups) | ✅ After SAINT answers, follow-ups such as "skip that" or "turn it down" work for 15 s (configurable), counted from when SAINT stops talking. While music plays, only follow-ups SAINT recognises as commands (or short questions to it) are taken, so lyrics and chatter are ignored. Verified live. |
| Wake → command → STT → agent → tool → TTS → back to wake listening | ✅ |
| Interrupting SAINT while it talks (barge-in) | ✅ Your speech interrupts it and SAINT's own voice doesn't. The interrupting request is then processed. |
| Memory ("my favorite language is Python" → later "what language do I like?") | ✅ Persists across restarts. You can view, edit and delete memories. |
| Reminders, timers, recurring reminders, scheduled commands | ✅ Persist across restarts, run in the background and can be cancelled. Reminders missed while SAINT was closed are delivered on start-up. |
| Desktop control: open/reuse/close apps, move/resize/snap windows across monitors, type, keys, click/double/right-click/hover UI elements, scroll | ✅ All through validated tools. Verified live on two monitors (see [the manual test checklist](MANUAL_TESTING.md)). Existing windows are reused, and SAINT asks which one if several match. |
| Multi-step requests ("open my browser, search YouTube for X, click the first video and pause") | ✅ Planned, then executed step by step with observe → act → verify, one retry and a clarifying question when needed. Verified live. |
| LLM function calling for requests the command router doesn't recognise | ✅ With tool-capable Ollama models (`llama3.1`, `llama3.2`). |
| Spotify: search, play track/artist/album/playlist/genre, pause/resume/skip/back, volume, queue, add to playlist, listening memory, recommendations | ✅ Covered by tests against a simulated Spotify API. ⚠️ Not yet run against a live account on this machine (see [Spotify](#spotify)). |
| Screen understanding ("what's on my second screen?", "what am I looking at?", "what is this error?") | ✅ From Windows APIs + UI Automation: monitors, windows per monitor, focus, buttons, links, fields, tabs, visible text. Image-level description additionally needs a vision-capable Ollama model (none is bundled). |
| Halo, overlay (global hotkey + edge tab), mini player, demo mode | ✅ Verified live with the real runtime. The Halo costs ~0.1–0.3 % CPU while animating. |
| Music hot-words ("skip", "pause", "louder" with no wake word while music plays) | ✅ Tested end-to-end through the voice loop with synthetic audio. ⚠️ One-word hot-words were fixed after a live run (Whisper scores single words near zero confidence) and need a live re-check. |
| Scenes (voice phrase, "run …", UI, schedule) and History | ✅ Covered by automated tests. |
| Steam: installed games, sizes, library/store, launch | ✅ Game list and sizes verified live against this PC's libraries. ⚠️ Launching and uninstalling by voice need a live check. |
| Drive space, biggest folders, junk report, Storage page | ✅ Verified live, read-only (a 500 GB drive with 1.9 M files scans in under a minute). ⚠️ Recycling and moving are covered by tests with a simulated Windows file operation; try them on a test folder first. |
| WinRAR extraction and compression | ✅ Verified with the installed WinRAR against test archives. |
| Windows controls (lock, power, mic, per-app volume, output device, brightness) | ✅ Audio devices and sessions read live. ⚠️ Changing them by voice needs a live check. |
| Stop / silent mode / whisper replies / "what are you doing?" | ✅ Covered by tests. ⚠️ Whisper loudness needs a live check. |
| Watchers, "what changed?", workspaces, notifications, clipboard | ✅ Covered by tests; the notification list was read live on this PC. ⚠️ Restoring a workspace across monitors needs a live check. |

### Known limitations

- **Bare "SAINT" relies on the transcript check.** The bundled `hey_saint.onnx` detects **"Hey SAINT"**
  well but barely scores a bare "SAINT" (≤ 0.02). A short utterance the model misses is transcribed
  once and accepted only if it *starts* with "SAINT" / "Hey SAINT". This costs one quick STT pass per
  short passive utterance, and none for speech longer than 7 s. Retraining with more bare-word
  samples would make the first stage catch it too (see [Retraining](#retraining)).
- **Wake-word accuracy on real voices** was measured on synthetic voices and background speech in
  the room. Tune it in Settings → Wake Word. A very low threshold (≈ 0.05) makes a bare "SAINT"
  reliable but can fire mid-sentence; SAINT keeps the rest of the request as one command either way.
- **Barge-in is energy based, not full acoustic echo cancellation.** With headphones it's excellent.
  With loud speakers right next to the microphone, raise *Echo margin* (Settings → Voice) if SAINT
  interrupts itself, or lower it if interrupting is too hard.
- **Smart Shuffle** isn't in Spotify's Web API. SAINT toggles it through the Spotify desktop app's own
  button (UI Automation) and falls back to regular shuffle when the app isn't open. **Removing songs
  from the queue** isn't possible through the API at all, and SAINT says so.
- **Spotify genre data** is no longer returned to new apps, so "something like X" uses the artist,
  their collaborators and your own listening history.
- **Notifications** are read from Windows' own notification list (read-only), because Windows reserves its
  notification-listener API for Store apps. A future Windows update could change that file's layout.
- **Do Not Disturb** has no API for regular apps: SAINT opens its settings. **Brightness** works on laptop
  screens; most desktop monitors don't let Windows change it.
- **An old Windows installation on a second drive** (like `E:\Windows` + `E:\Users`) is far too big for
  the Recycle Bin, so SAINT only reports it. Copy what you need from its `Users` folder, then format the
  drive yourself in Disk Management.
- **Steam libraries Steam doesn't list** (e.g. after reinstalling Windows) are shown, but their games can't
  be launched until you add the folder in Steam → Settings → Storage.
- **Workspaces** put programs and windows back, not browser tabs or open files.
- **Web pages** are read through the browser's accessibility tree. Pages that don't expose their
  content (canvas apps, some games) can't be clicked by name; use coordinates or a vision model.

---

## Architecture

```
MICROPHONE (sounddevice, 30 ms frames)
   │
   ├─ VAD: Silero (speech vs. music/TV, from faster-whisper) with an RMS fallback
   ├─ Wake word, stage 1: onnxruntime (CPU) melspectrogram → embeddings → hey_saint.onnx
   │      fires when N of the last W frames clear the threshold (or one frame is very confident)
   └─ Wake word, stage 2: a short utterance the model missed is transcribed once and accepted
          only if it *starts* with "SAINT" / "Hey SAINT" (otherwise dropped; nothing is shown or logged)
          ▼
   WAKE_DETECTED → COMMAND_LISTENING   ("Hey SAINT, play jazz" and "SAINT … play jazz" both work)
          ▼
   STT: faster-whisper (GPU) → transcript with the wake phrase stripped
          ▼
   CONVERSATION MANAGER ──► after SAINT answers: conversation window (no wake word needed)
          ▼
   AGENT
     1. pending question?  (yes/no confirmations, "which window?" choices)
     2. reminders / memory
     3. multi-step plan  → OBSERVE → ACT → VERIFY → (retry once / ask) → next step
     4. single intents: system · Spotify · desktop/screen/browser (desktop_intents) · legacy desktop
     5. otherwise the LLM (Ollama) with memories, the real clock and tool calling
          ▼
   TOOL REGISTRY  (typed parameters, validation, permission policy, confirmation, events, logs)
     spotify.* · memory.* · automation.* · desktop.* · screen.* · browser (open_url / web_search)
          ▼
   RESULT + VERIFICATION (window moved? page changed? track changed?)
          ▼
   OUTPUT GUARD (modules/agent/output.py): no JSON, tool names, schemas, code or unbacked
   "I did X" claims ever reach the chat or TTS
          ▼
   RESPONSE → TTS (Kokoro, GPU) → conversation window → back to WAKE_LISTENING
```

Key ideas:

- **One authoritative state.** `core/assistant_state.py` holds `WAKE_LISTENING`,
  `WAKE_DETECTED`, `COMMAND_LISTENING`, `PROCESSING`, `EXECUTING`, `SPEAKING`, `OFFLINE` and `ERROR`.
  The voice loop, conversation controller and tool registry drive it; the UI only renders it.
- **Independent of the UI.** `core/runtime.py` starts TTS, the conversation controller, the
  wake-word listener and the scheduler. Core services receive events directly on the emitting thread
  (`event_bus.subscribe`); only UI widgets receive them through Qt. SAINT keeps working when the
  window is minimised, hidden to the tray or not focused.
- **No pretending.** Actions go through explicit tools, and SAINT's reply is built from the tool's
  actual result. Failures are reported plainly (e.g. "Spotify isn't connected"). The LLM is told the
  real time and only the memories you stored.
- **No shell for the LLM.** Shell and file tools exist for the owner but are never offered to the
  model. High-risk tools require confirmation.

---

## Requirements

- **Windows 10/11** (desktop control uses Win32 and UI Automation).
- **Python 3.12+** (developed and tested on 3.14).
- **Ollama** for the language model (optional, but SAINT can only chat with a model).
- **NVIDIA GPU recommended.** STT and TTS fall back to the CPU, at lower speed. The RTX 50-series
  (Blackwell) needs the CUDA 12.8 PyTorch wheels.
- A microphone and speakers or headphones.

---

## Installation

```bat
git clone https://github.com/uberdiz/SAINT.git
cd SAINT
setup.bat
```

`setup.bat` creates `.venv` and installs the CUDA PyTorch build when it finds an NVIDIA GPU. It then
installs `requirements.txt`, pulls `llama3.1` into Ollama (if Ollama is installed) and runs the tests.

Manual install:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install --index-url https://download.pytorch.org/whl/cu128 torch torchaudio   & rem GPU; omit the index URL for CPU
pip install -r requirements.txt
ollama pull llama3.1
```

Models downloaded on first use: the faster-whisper STT model (e.g. `base.en`, ~150 MB) and the
Kokoro TTS model and voices (~330 MB, from Hugging Face). The wake-word models ship with the repo in
`data/wake/`.

---

## Building SAINT.exe and the installer

```bat
.venv\Scripts\pip install pyinstaller
winget install JRSoftware.InnoSetup
.venv\Scripts\python packaging\windows\build.py
```

- `build\windows\SAINT\SAINT.exe` — double-click to run (keep it with its `_internal` and `models` folders).
- `build\windows\SAINT-Setup.exe` — installs to `%LOCALAPPDATA%\Programs\SAINT`, no admin rights needed.
- The packaged app keeps its data in `%LOCALAPPDATA%\SAINT` (set `SAINT_DATA_DIR` to use another folder,
  e.g. your source checkout's `data`). Once SAINT is installed, running it from source uses that same folder
  and leaves the installed app's shortcuts alone, so there's only ever one SAINT: one memory, one history.
  Moving from a source checkout to the installer? Your old `data` folder isn't read anymore; its memories,
  skills, scenes, history and Spotify taste can be merged into `%LOCALAPPDATA%\SAINT` (the restore keeps a
  backup in `%LOCALAPPDATA%\SAINT-backups`).
- `build.py` downloads the ONNX voice first (`tools/get_kokoro_onnx.py`); PyTorch is not bundled.

---

## Running SAINT

```bat
run.bat                 & rem or: .venv\Scripts\python app.py
run.bat --background    & rem start hidden in the system tray
```

- SAINT starts listening for **"Hey SAINT"** straight away (turn this off in Settings → General).
- Closing the window keeps SAINT running in the **system tray** — the Halo around your screen shows
  it's still listening, and <kbd>Alt</kbd>+<kbd>`</kbd> opens the overlay. Right-click the tray icon to
  start/stop listening, toggle the Halo or mini player, run the demo, or quit.
- Only one instance runs at a time.

### Testing without your real data

```bat
run.bat --profile clean                 & rem a brand-new SAINT: first-run setup, nothing learned
run.bat --profile demo                  & rem sample scenes, answers and settings
python tools\profiles.py snapshot       & rem a copy of your data to test against safely
python tools\profiles.py list           & rem then: run.bat --profile <name>
```

A profile is its own data folder in `%LOCALAPPDATA%\SAINT-profiles\<name>` with its own memory,
history, scenes, settings, Spotify login and Link key; only the large voice models are shared. Your
real data (`%LOCALAPPDATA%\SAINT`) is never touched, and the window title says which profile is open.
Releases never contain anyone's data: `packaging/windows/build.py` refuses to package if a settings,
memory or history file lands in the bundle, or if shipped code contains an address from your own
scenes or saved answers.

---

## Talking to SAINT

Say **"Hey SAINT"** or **"SAINT"**, then your request, in one breath or after a short pause. After
the wake word SAINT plays a soft chime and waits up to 6 seconds for the command. **After it answers,
just keep talking**: for 15 seconds (Settings → Wake Word → *Conversation window*) follow-ups like
"skip that", "turn it down" or "now put it on the left" need no wake word. While music is playing,
SAINT only takes follow-ups it recognises as commands (or short questions to it), so song lyrics and
people talking don't trigger anything; say "Hey SAINT" first for anything else, even mid-conversation.
Long requests can be said with natural pauses between the steps. You can also type into the
Home page, the overlay or the command palette; typed text goes through exactly the same agent.

**Music hot-words.** While Spotify is playing (or paused with the mini player on screen), a short
playback command on its own works with **no wake word at all**: "skip", "skip this song", "next song",
"go back", "pause", "resume", "louder", "quieter", "turn it up", "I love this". The whole utterance must
be one of these, so lyrics and conversation around you are ignored. It rides on the wake word's existing
transcript check, so it costs nothing extra. Turn it off in Settings → Spotify → *Hands-free*.

References carry over: "it", "that", "that window", "there" and "the first one" mean the thing SAINT
just worked with, unless you've switched to another window since. "Turn it down" means Spotify after
a music command and the computer volume after a video. With several matching windows SAINT asks
("I found 3 browser windows: 1, … Which one?"). Answer "the second one", "the YouTube one" or "the one
on my second screen".

| You say | What happens |
|---|---|
| "Hey SAINT, play Blinding Lights by The Weeknd" | Resolves the actual track on Spotify and plays it |
| "…play Daft Punk" / "…play the album Discovery" / "…play my gym playlist" / "…play some jazz" | Artist / album / your playlist / genre playlist |
| "…pause" · "resume" · "skip" · "go back" · "turn it up" · "set the volume to 30" | Playback control |
| "…play something else" · "play a different song" · "change the song" · "I'm not feeling this one" | Skips (the last also records a dislike). Never treated as a song title. |
| "…play something like DAMN by Kendrick Lamar" · "play something by Kendrick Lamar" | Picks similar music from that album/track/artist · plays that artist |
| "…turn on Smart Shuffle" · "replay this song" · "skip ahead 30 seconds" · "who is this?" | Smart Shuffle (via the Spotify app), restart, seek, current artist |
| "…what am I listening to?" · "what have I listened to today?" | Live playback state · SAINT's listening memory |
| "…play something I'd like" · "play something similar" · "recommend something similar" | Personalised picks from your real listening history |
| "…add this to my chill playlist" · "queue Harder Better Faster Stronger" | Real playlist/queue changes |
| "…my favorite programming language is Python" | Stored in long-term memory |
| "I'm a software engineer" · "I hate horror movies" · "my sister's name is Mia" (in passing) | Learned quietly for your profile (Settings → Memory) |
| "…what programming language do I like?" | Answered from memory ("Your favorite programming language is Python.") |
| "…forget my favorite programming language" · "what do you know about me?" | Delete / list memories |
| "…remind me at 5 PM to work on AIDE" · "remind me in 30 minutes" · "set a timer for 10 minutes" | One-time reminders |
| "…every morning remind me to check my schedule" · "remind me every weekday at 8:30 to stand up" | Recurring reminders |
| "…every weekday at 9 play my focus playlist" | Scheduled command |
| "…what reminders do I have?" · "cancel the stretch reminder" | Manage automations |
| "…focus mode" · "run wind down" · "start the morning scene" | Runs a [scene](#scenes) you made |
| "skip" · "pause" · "louder" (music playing, no wake word) | Music hot-words |
| "…open Discord" · "switch to Spotify" · "close Notepad" (asks first) | Apps and windows |
| "…move this window to my second monitor" · "snap Chrome to the left" · "maximize this window" | Window placement |
| "…type hello world into the search box" · "press ctrl+t" · "click the send button" | Keyboard and UI elements |
| "…open my browser" · "open YouTube" · "search YouTube for Kendrick Lamar" | Uses the browser on your screen (asks once if several, then remembers) · site search |
| "…use a different browser" · "use this browser from now on" | Change which browser window SAINT uses |
| "…theater mode" · "1.5x speed" · "skip ahead 30 seconds" · "captions on" · "next video" · "set the quality to 1080p" · "turn off autoplay" · "loop this video" · "skip the ad" | YouTube's own shortcuts and player menus |
| "…click the first video" · "in that window, click the first result" · "click the button in the bottom right" | Finds visible results/elements (UI Automation) and verifies the page changed |
| "…scroll down" · "go back" · "refresh" · "copy that" · "put it in fullscreen" · "double click the recycle bin" | Scrolling, navigation, editing keys, element actions |
| "…move the browser to my second monitor" · "make it bigger" · "put it on the left" · "put this window next to Spotify" · "close all the browser windows" | Window management (closing asks first) |
| "…open my browser, search YouTube for Kendrick Lamar, click the first video, and turn the volume down" | Multi-step plan, each step verified |
| "…what's on my second screen?" · "what am I looking at?" · "what's currently open?" · "what is this error?" | Screen understanding per monitor. "Error" answers only from text actually on screen. |
| "…open the dashboard" · "go to history" · "turn off the mini player" · "hide the halo" · "dark mode" | SAINT's own window |
| "…launch Counter-Strike 2" · "open my Steam library and search Hades" · "what are my biggest games?" | Steam |
| "…how much space is left on E?" · "what's taking up space on D?" · "clean up my Downloads" · "only the installers" | Storage (removing always asks; Recycle Bin only) |
| "…go to my Downloads, click the first download and extract it using WinRAR to my games folder" | One extraction, newest archive → its own folder |
| "…lock my PC" · "mute my mic" · "set Discord to 30 percent" · "switch audio to my headphones" · "restart" (asks) | Windows controls |
| "…tell me when Claude finishes" · "take me back" · "what changed while I was away?" · "watch my left screen" | Watching |
| "…save this workspace as Coding" · "restore Coding" · "gaming mode" · "I'm done gaming" | Workspaces and scenes |
| "…read my clipboard" · "fix the code I copied" · "read my notifications" · "run the tests" · "handle this" | Clipboard, notifications, developer mode |
| "…more energetic" · "something darker" · "more like the last song" · "no more of this artist" | Spotify DJ mode |
| "…when I say the lab, I mean open my SAINT project" · "be quiet for 30 minutes" · "what are you doing?" | Aliases, silent mode, status |
| "…stop" · "cancel" · "stop everything" | Stops speaking, the running plan or scene, and typing (everything also stops background work) |

Anything else goes to the language model, which can also call the same tools.

---

## Wake word

- Model: `data/wake/hey_saint.onnx`, your custom openWakeWord classifier trained on "hey saint" and
  "saint", with near-miss negatives such as "faint", "paint" and "saved".
- Runtime: `modules/voice/wake_word.py` runs openWakeWord's three-stage pipeline (mel-spectrogram →
  speech embedding → classifier) **directly on onnxruntime**. Its scores match the reference
  openWakeWord implementation to within 0.0003. SAINT does not import the `openwakeword` package,
  because it pulls in scikit-learn, which Windows Application Control blocked on the development
  machine.
- **Stage 1 (model):** fires when *confirmation frames* (default 1) of the last *detection window*
  frames (default 4 × 80 ms) score ≥ *threshold* (default 0.50), or one frame is very confident, outside
  a *cooldown* (default 2 s). Strictly consecutive frames (the old rule) missed clear "Hey SAINT"s that
  peaked for a single frame. On 50 synthetic near-miss phrases ("paint", "sent", "Hey Sam", "She is a
  saint", …) nothing fired at thresholds down to 0.2.
- **Stage 2 (transcript check):** a short passive utterance (≤ 7 s) the model didn't fire on is
  transcribed once. It's accepted only if it starts with "SAINT" / "Hey SAINT". "SAINT, <pause> skip
  this" is judged as one utterance, and very short clips are padded so Whisper doesn't hallucinate on
  them. Bystander speech is never shown, acted on or logged.
- **Music:** the Silero VAD separates speech from music (0 % of loud synthetic music counted as speech,
  vs 62 % with the old energy VAD), so commands end cleanly while music plays. Utterances are capped
  at 15 s.
- While SAINT itself is speaking, wake detections are ignored so it can't wake itself. Interrupting is
  handled by barge-in (below), and the conversation window only starts counting once SAINT stops
  talking.
- If the model file is missing or invalid, Home and Settings show the exact error. SAINT
  then falls back to transcript-gated listening; nothing is faked.

Settings → **Wake Word**: enable/disable, model file, sensitivity, confirmation frames, detection
window, transcript check (and its length limit), cooldown, command timeout, conversation window,
follow-up confidence, chime, score logging, and a live score meter. Settings → **Voice**: speech
detector (Silero/RMS), longest utterance.

### Retraining

`hey_saint.onnx` was trained locally with
[Open-Wake-Word-Training](https://github.com/DisasterofPuppets/Open-Wake-Word-Training) (a Windows
wrapper around openWakeWord and Piper), using the config in `tools/wake_word/hey_saint.yaml`. To
improve bare-"SAINT" detection, increase the weight or number of `saint` samples and retrain:

1. Use Python 3.12 (`uv venv --seed --python 3.12`) and replace `webrtcvad` with `webrtcvad-wheels`.
2. Build in a **short path** such as `C:\oww`; torch's CUDA headers exceed Windows' 260-character limit.
3. `python install.py --gpu`, then download the training assets (~16 GB).
4. Delete stray `README.txt` files inside the augmentation asset folders.
5. Train: `python openWakeWord/openwakeword/train.py --training_config config/hey_saint.yaml --generate_clips --augment_clips --train_model`.
6. The model is `training/hey_saint.onnx`; the TFLite conversion step afterwards fails, which is
   harmless. If a `hey_saint.onnx.data` file is produced next to it, merge it into one file:
   ```python
   import onnx
   m = onnx.load("training/hey_saint.onnx", load_external_data=True)
   onnx.save_model(m, "hey_saint.onnx", save_as_external_data=False)
   ```
7. Copy the result to `data/wake/hey_saint.onnx`, or point Settings → Wake Word at it.

---

## Speech recognition (STT) and speech output (TTS)

- **STT:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper), `base.en` by default,
  float16 on CUDA. It only runs on speech captured after the wake word (or on every utterance when
  the wake word is off). SAINT biases it toward spelling "SAINT" correctly (`voice.stt_hotwords`).
  Typical latency here: 90–215 ms per command. Settings → Voice: model, device, precision, language.
- **TTS:** [Kokoro](https://github.com/hexgrad/kokoro), voice `af_heart`, on CUDA with CPU
  fallback. Out-of-dictionary words (names, bands) use the bundled espeak-ng fallback. Qwen3-TTS is
  available as an alternative engine.
- Device selection and CUDA diagnostics are centralised in `core/device.py` and logged at start-up.

---

## Interrupting SAINT

The microphone is **never muted** while SAINT speaks:

1. The TTS playback thread reports the level of every audio block it plays (`core/audio_echo.py`).
2. The voice loop compares microphone energy against the echo predicted from that level, using a
   continuously learned speaker-to-mic coupling. Only speech clearly above the predicted echo,
   sustained for about 240 ms, counts as you talking.
3. On barge-in SAINT stops speaking at once and captures what you're saying, including the start of
   the utterance. The request is then handled as an interruption, so the model knows its last answer
   was cut off.
4. A second, text-level echo filter drops transcripts that simply repeat SAINT's own words.

You can also say "stop" or "never mind", or press **Stop speaking**. Tune this in Settings → Voice →
Interruptions.

While SAINT is *working* (a multi-step automation, a lesson step) rather than talking, a new request
interrupts too: the automation stops at its next step and the new request runs (it waits up to
`conversation.turn_wait_sec`, 20 s, for a step that's mid-way, e.g. a page loading). "Stop" ends it
without a new request. Talking over a question SAINT is asking only answers the question.

---

## AI provider / Ollama

1. Install [Ollama](https://ollama.com) and run `ollama pull llama3.1`. `llama3.2` also supports
   tool calling; `llama3.2:1b` is faster but weaker.
2. Settings → **AI**: provider `ollama`, model, base URL (`http://localhost:11434`). Use **Test
   connection** and **Refresh list** to check.

If the configured model isn't installed, SAINT uses the closest installed one and says so. If Ollama
isn't running, SAINT answers "I can't reach my language model right now…" and never makes up a
reply. An OpenAI-compatible endpoint also works (provider `openai`, with base URL and API key), but
tool calling is currently implemented for Ollama.

---

## Spotify

SAINT talks to the Spotify Web API through your own developer app.

1. Create an app at <https://developer.spotify.com/dashboard>.
2. Add the redirect URI **`http://127.0.0.1:8888/callback`** (or the one shown in Settings).
3. Settings → **Spotify**: enable Spotify, paste the **Client ID**, press **Connect Spotify** and log
   in through the browser. SAINT uses Authorization Code + PKCE, so no client secret is needed. Tokens
   are stored in the **Windows Credential Manager** (`keyring`), never in files or logs.
4. Playback control requires **Spotify Premium** (a Spotify API rule).

What SAINT does:

- **Resolves real entities.** It searches tracks, artists, albums and playlists, and picks the best
  match with fuzzy scoring. "X by Y" means a track; "my … playlist" searches your playlists first;
  "some jazz" finds a genre playlist.
- **Recovers when no device is active.** It activates your preferred or most recent device, or opens
  the Spotify desktop app and waits for it.
- **Keeps a listening memory** (`data/memory/spotify_memory.db`): tracks actually observed playing
  (a background poller every 15 s), what you asked for, what you skipped and how far in, songs added
  to playlists, and whether you kept or skipped its recommendations. Artist genres are cached for
  genre-level taste.
- **Personalised picks** ("play something I'd like") are ranked from that memory and your Spotify top
  artists, excluding recently played and skipped tracks. With no history yet, SAINT says so instead
  of guessing. Spotify retired its `/recommendations` endpoint for new apps, so SAINT builds
  candidates from search.

Status: the Spotify layer is covered by automated tests against a simulated API, including device
recovery, skips and honest failures. On the development machine no Spotify account was connected, so
live playback wasn't exercised. Connect your account in Settings to use it.

---

## Memory

| Kind | Where | What |
|---|---|---|
| Short-term | RAM (`modules/ai/context.py`) | The current conversation (last N turns). Not persisted. |
| Long-term | `data/memory/saint_memory.db` | Facts you tell SAINT, stored as key/value (`favorite programming language = Python`), with categories *preference*, *personal*, *fact* and *project*. Saying something again updates it; the old value is kept in its history. |
| Spotify | `data/memory/spotify_memory.db` | Listening history, requests, skips, feedback, playlist aliases, genres. |
| Automations | `data/memory/automations.db` | Reminders and scheduled commands. |
| Scenes | `data/scenes.json` | Your scenes. |
| History | `data/history.jsonl` | What you asked, how (voice, typed, hot-word, scene), SAINT's reply, tools used and timings. Powers the History page. Local only; turn it off or cap its size in Settings → Memory, clear or export it on the History page. |

Memories come from two places:

- **Told** — clear statements ("my favorite X is Y", "call me Sam", "I live in…", "remember that…").
  Kept until you delete them.
- **Learned** — things you mention in passing ("I'm a nurse", "I'm learning Japanese", "I hate horror
  movies"), and — with *Learn with the AI* on — lasting facts the local model finds in what you say
  (it runs after SAINT has answered, on your PC). Learned memories carry a confidence, grow stronger
  each time they come up again, and fade after *Forget unconfirmed after* (60 days by default) if they
  never do. A learned guess never overwrites something you told SAINT. *Keep it* on the Memory page
  makes one permanent.

SAINT does not store every conversation. Answers such as "Your favorite programming language is Python"
come straight from the database. The LLM gets the few core facts about you plus the memories relevant
to the request, and is told not to invent others.

The **Memory** page opens with your **profile**: name and what you do, likes, dislikes, people and pets,
projects and goals, routines, music taste (from Spotify's listening memory) and habits (when and how you
use SAINT, the sites and apps you ask for — from the local history), with a short summary the local
model writes from exactly those facts (cached in `data/memory/profile.json`). Below it, every memory
with how SAINT knows it; add, edit, delete, or confirm. By voice: "what do you know about me?", "forget
my …". Settings → Memory controls learning, context injection and wiping.

---

## Reminders and automations

- **Time expressions:** "at 5 PM", "at 17:30", "at noon", "in 30 minutes", "in an hour and a half",
  "twenty minutes from now", "tomorrow at 9", "tonight", "on Friday", "every morning", "every weekday
  at 8:30", "every Monday and Thursday at 7 PM", "every 2 hours", "set a timer for 10 minutes".
- **Reminders** are spoken (TTS) and shown as a tray notification.
- **Scheduled commands** run any command through the agent at the set time, with the same tools and
  permissions, e.g. "every weekday at 9 play my focus playlist". Optional condition: "…if Spotify is
  playing" or "…is not playing".
- **Persistence:** the SQLite store survives restarts. One-time reminders missed while SAINT was
  closed are delivered on start-up if they're within *missed_grace_hours* (default 12); recurring
  ones resume on schedule.
- **Management:** Automations → **Scheduled** (create with a live preview of how the time was
  understood; pause, resume, run now, cancel, delete) or by voice.

### Scenes

A scene is a name, an optional extra voice phrase, and a list of commands run in order — anything you
could say to SAINT (“play lofi beats”, “set volume to 35”, “open notion”). Create them in Automations →
**Scenes** (templates included), then trigger them by saying the name or phrase (“Hey SAINT, focus
mode”, “run wind down”), from the overlay, Home or the command palette, or on a schedule (“every
weekday at 8”). Scenes are stored in `data/scenes.json`; a scheduled scene is an ordinary scheduled
command that says “run <name>”, so it survives restarts like any other automation.

Steps can also talk to you: “ask which email to send from, personal or school”, “ask for email if not
saved”, “make a title based on the description and ask to send or edit”, “click send if yes”. A scene
with steps like these asks **every** question before it does anything on screen, reads drafts back for
changes, and never runs a step after a “confirm” without your yes (`modules/automation/scene_plan.py`).
A condition SAINT can't check itself becomes your step — it waits for “next” instead of guessing.

---

## Desktop control and permissions

Every desktop action is an explicit tool with validated inputs:

| Tool | Notes |
|---|---|
| `desktop.open_app` | **Reuses a running app's window** instead of launching a duplicate. With several windows it picks the one the request, your focus or the conversation points at, else asks. "Browser" means the running or default browser; "a new window" really opens one. Resolved only against what's installed. Launched without a shell. |
| `desktop.close_app` / `desktop.close_windows` | Graceful `WM_CLOSE`, then the title-bar close command. **Asks for confirmation** by default (always for "close all …"). Reports honestly if the app stays open (e.g. a save prompt). |
| `desktop.scale_window`, `desktop.place_beside` | "Make it bigger", "put this next to Spotify". Results are measured afterwards. |
| `desktop.open_url`, `desktop.web_search` | Navigate / search sites (YouTube, Google, Amazon, …) in your existing browser window. Verified by the page title changing. |
| `desktop.focus_window`, `desktop.move_window`, `desktop.arrange_window`, `desktop.resize_window` | Switch, move to monitor N/next/left/right, maximise/minimise/snap/centre. |
| `desktop.type_text` | Unicode typing via SendInput, optionally into a named field ("search box") found through UI Automation. Length-limited. |
| `desktop.press_keys` | Key names are validated; Alt+F4, Win+L, Ctrl+Alt+Del, Win+R and Win+X are refused. |
| `desktop.click_element` / `desktop.list_ui_elements` | Real UI elements from the Windows accessibility tree, by label, position ("button in the bottom right") or order ("first video": visible results only, sidebar and off-screen carousel items skipped). Click, double, right, middle and hover. Reports whether the UI changed. |
| `desktop.mouse_move` / `desktop.mouse_click` / `desktop.drag` / `desktop.scroll` | Coordinates checked against the virtual screen. Scrolling moves the pointer over the intended window first. |

Commands act on the window SAINT is working with, unless you've switched to another window since.
They never act on SAINT's own window when you typed into it. Keyboard input only goes to a window
that was verified to be in front. The process is per-monitor DPI aware, so coordinates stay exact
with mixed display scaling.

Safety:

- **Permission mode** (Settings → Automation): *Safe* (low-risk only), *Confirm* (default: high-risk
  actions ask first) or *Autonomous*. Per-tool overrides (allow/confirm/deny) are in Settings →
  Desktop Control.
- **Switches** for app launching, window control, keyboard and mouse.
- The LLM never gets `run_command`, `write_file` or background shell tasks.
- pyautogui's fail-safe stays on: move the mouse into a screen corner to abort mouse automation.

---

## Screen awareness

| Stage | Status |
|---|---|
| Screen capture (`screen.capture`, all monitors or one) | ✅ |
| Window / monitor / focus context | ✅ |
| UI element understanding: buttons, fields and links of the active window (Windows UI Automation) | ✅ |
| Visual analysis of the image | ⚠️ Only with a vision-capable Ollama model (Settings → Advanced → Vision, e.g. `llama3.2-vision`). Otherwise SAINT says it's not configured. |
| Action planning → mouse/keyboard | ✅ Through the desktop tools. The LLM can combine `screen.context` with `desktop.*`. |

`screen.context` covers every monitor: which is the main one, where each sits, resolution and
scaling, and the windows on it, front-most first. For the window you're looking at, or the front
window of the monitor you asked about, it lists buttons, links, fields, menus, tabs and visible
text. It runs on demand only; nothing is captured in the background. "What's on my screen?", "What's
on my second screen?" and "What's currently open?" answer from this. Image analysis is added when a
vision model is configured, and is told to say when it can't tell rather than guess. Settings →
Vision → *Screen reading* turns it off.

---

## Your PC, hands-free

Everything here also works typed, and every destructive action asks first. Nothing is ever deleted
permanently: removals go to the Recycle Bin, and SAINT checks the Recycle Bin can hold each item first.

**SAINT itself** — "open the dashboard", "go to history / settings / music", "turn off the mini player",
"hide the halo" / "halo always on", "open the overlay", "turn off action notices", "dark mode",
"minimize yourself".

**Steam** — reads `libraryfolders.vdf` and each `appmanifest_*.acf`, plus `steam.extra_libraries`
(default `D:\SteamLibrary`). Launching uses `steam://rungameid/…`, so Steam does the work.

**Files and storage** (`modules/files`) — drive overview, background scans (cached for
`files.scan_cache_hours`), a junk report, and cleanup that asks before anything moves. "My games folder" is
`files.games_dir` (default `D:\Games` — set it in Settings); name more folders in `files.known`. Never touched: Windows, Program
Files, ProgramData, drive roots, your profile folder itself, code repositories and SAINT. Never suggested:
Steam libraries, your games folder, emulators and ROMs.

**Archives** — `.rar` through WinRAR's `UnRAR.exe` (with progress), `.zip` through WinRAR or Python,
`.7z` through `WinRAR.exe`. The archive goes into its own folder unless it's already wrapped in one.

**Windows** — lock, sleep, restart, shut down (asks; restart and shutdown wait 60 s), mic mute, per-app and
whole-PC volume, output device, brightness, screenshots of everything, one screen or one window
(Pictures\Screenshots). Audio uses `pycaw`. With **Voicemeeter** running (`audio.voicemeeter.enabled`:
auto), "mute my mic" mutes its microphone strip (`mic_strip`, -1 = find it) and "switch audio to my
headphones" moves every strip from the speakers' A-bus to the headset's. Bare "mute" means the mic
(`audio.bare_mute`: mic / system).

**Stop and silence** — "stop", "cancel", "shut up" stop what's happening now; "stop everything" also stops
background scans, extraction and watchers. "Silent mode" / "be quiet for 30 minutes" / "you can talk again".
Quiet requests get quiet answers — "quiet" compared with how loud you usually talk to SAINT
(Settings: `voice.whisper_replies`, `whisper_rms`, `whisper_ratio`, `whisper_gain`).

**Watching** (`modules/watch`) — up to five watches at a time, each ending after two hours. "Finished"
means the window changed and then stayed still for 8 seconds (a reply finished streaming, a build
finished). "What changed?" compares window titles and tiny per-screen fingerprints kept in memory for 30
minutes.

**Workspaces** — `data/workspaces.json`: each window's program, monitor, position and state, plus the
Spotify context. **Scenes** *Gaming mode*, *Done gaming* and *Dev environment* are added once; edit or
delete them on the Automations page.

**Notifications** — off until you say "read my notifications out loud" (or enable the module). Then new
notifications that match `notifications.keywords`, or come from an app in `notifications.allow`, are
announced; apps in `notifications.deny` never are. "Read my notifications" works any time.

**Aliases** — "when I say X, I mean Y" (`data/aliases.json`); "what aliases do I have", "forget the alias X".

**Storage page** — drive space and the biggest folders on top; below, **What you can clear**: *Check for
junk* looks through Downloads (duplicates, archives you've already extracted, old installers, big downloads
untouched for months), temp files older than `files.temp_age_days`, graphics shader caches, browser caches
(Chrome, Edge, Brave, Opera, Firefox), Discord / Spotify / VS Code caches, crash dumps, developer caches
(pip, uv, npm, Yarn — shown, never pre-ticked), old game recordings, Windows Update leftovers and old
Windows installs, plus what's already in the Recycle Bin. A stacked bar and a per-category chart show where
the space is; the list below is ticked for what's safe. *Move ticked to Recycle Bin* asks once more, then
uses Windows' own Recycle Bin (never a permanent delete). Checks started by voice show up here too.

**Short-term memory** (`modules/agent/recent.py`) — every tool result and finished background task leaves
a note for 30 minutes: a screenshot taken, a folder extracted, made, moved or renamed, a junk check's
findings, an app opened, a playlist played. "It" / "that" on their own only reach back 5 minutes; a named
reference ("that screenshot", "the folder you just extracted") reaches the full 30. Removing ("delete it")
only ever points at something SAINT made or found — never at a folder it merely opened — and still asks.
The same notes go to the language model as facts, so it doesn't invent results. Nothing is saved to disk.

---

## Languages

`modules/lang` — SAINT understands Spanish, French, Portuguese, German and Italian as well as English, answers in the language you spoke, and copes with sentences that mix two ("pon some jazz", "recuérdame to call mom at 5").

- **Per-word detection.** Each word votes for the languages it belongs to; words that several languages share ("no", "la", "a") go to whichever language the clearer words point at; names, titles and numbers don't vote at all (a capitalised word in mid-sentence is a name). A sentence is *mixed* when a second language has a real share of the votes. A one-word answer ("ok") keeps the language of the conversation for `language.sticky_minutes`.
- **Commands.** Only the *shape* of a command is translated, into the canonical English the router already understands; what you named is copied exactly as you said it. `"pon música de Bad Bunny"` → `play Bad Bunny`; `"recuérdame llamar a mamá a las 5 de la tarde"` → `remind me at 5 pm to llamar a mamá`. Times and durations in any pack's language ("a las cinco y media", "um 17 Uhr", "às 5 da tarde") become the English time phrases `modules/automation/timeparse.py` reads.
- **Replies.** SAINT's English replies go back through the language's phrasebook, sentence by sentence, with titles and names passed through untouched. What the phrasebook can't say is translated by your local model (`language.llm_translate`), so there is always an answer. Mixed requests are answered in the same mix (`language.mixed_mode: "mirror"`), or in your main language (`"dominant"`).
- **Speech.** Kokoro speaks each language with its own voice, and a sentence that mixes two is split into stretches that each get the right voice. For recognition, turn on `language.multilingual_stt` (Whisper then auto-detects the language; it uses `voice.stt_model_multilingual`), and the wake phrase also works as "hola SAINT", "salut SAINT", and so on.
- **Adding or fixing a language.** The packs are plain JSON in `modules/lang/lexicon/` (vocabulary, command patterns, time phrases, the reply phrasebook). The same files are used by the iPhone app, and `tests/data/lang_cases.json` is checked by both the Python and the Swift tests.

```json
"language": {
  "auto_detect": true, "reply_in_user_language": true, "preferred": ["es", "en"],
  "mixed_mode": "mirror", "llm_translate": true, "sticky_minutes": 10, "multilingual_stt": false
}
```

---

## SAINT Link

`modules/link` — connect SAINT to your **phone**, your **other PCs**, and **friends' SAINTs** over `IP:port`: on the same network, through Tailscale, or over the internet with **Reach this PC from anywhere** (below — nothing to install on the phone). **Off by default** — turn it on in *Devices*, or set `link.enabled`.

### Two kinds of device

| | **My device** (`own`) | **Collaborator** (a friend's SAINT) |
|---|---|---|
| Learned things (memories, skills, aliases, scenes, reminders, language settings) | synced both ways, automatically | never synced; you can *share* one thing, and the receiver decides whether to keep it |
| What you're doing right now | shared, so "what did I just ask?" works on whichever device you're using (memory only, 30 minutes) | not shared |
| Talk to its SAINT / control it | yes | no — only the closed list of automations below |
| Files | yes | yes (executables are saved as `.unsafe`) |

**Automations a collaborator can run on your PC** (each is `allow` / `ask` / `deny` per person, edited in *Devices → the person → Permissions*; "ask" waits for your yes, by voice or button):
`send_prompt` (type a prompt into Claude, ChatGPT, Gemini, Copilot, Perplexity or Grok), `message` (SAINT reads it out), `open_url` (http/https only), `run_scene` (only scenes you shared), `play_music`, `ask` (a plain answer, not your tools). Nothing else is reachable: a friend cannot run arbitrary commands, read your files or use your PC's tools.

### Voice

- *"pair my phone"*, *"add Gian as a friend"* — opens a pairing window and shows a QR code and a short code (`ABCD-EFGH`).
- *"pair with code ABCD-EFGH"* — on another PC: finds the SAINT with that window open on your Wi-Fi (or among your Tailscale devices) by itself; no address to type.
- *"send this prompt to Gian's PC on Claude: summarise my notes"* · *"send a message to Gian: dinner is ready"* · *"play lofi on Gian's PC"* · *"open https://… on Gian's PC"*
- *"ask my laptop to lock itself"* · *"ask Gian's SAINT what the capital of Peru is"*
- *"send that to Gian"* (the file Explorer has selected) · *"accept what Gian shared"* · *"share this scene with Gian"*
- *"what devices are connected?"* · *"sync my devices"* · *"unpair Gian"*

The common ones work in the languages above too ("manda este prompt al PC de Gian en Claude: …").

### Pairing and security

- **Pairing** uses a one-time code (the QR code holds it), valid for five minutes and for one device; ten wrong tries close the window. It is the pre-shared key of a Noise `XXpsk3` handshake, so someone on your Wi-Fi who never saw the code cannot pair, and the code itself is never sent. Codes are eight characters (`ABCD-EFGH`, 40 bits) with the key stretched by PBKDF2 (20 000 rounds), so it can't be brute-forced in the five minutes it lives; the older 26-character codes still work.
- **Own device or friend** is decided by the code's owner. If the person joining picked the other option, both sides keep the stricter one ("friend") instead of failing — a friend can never be upgraded to your own device by the joiner.
- **Every address at once.** Devices tell each other all their addresses (Wi-Fi, Tailscale) and dial them in parallel, so away from home the Tailscale address answers instead of waiting out the home one. *Devices → Check connection* says which address answers and what's in the way.
- **After pairing**, each device remembers the other's public key and reconnects with Noise `IK`: mutual authentication, forward secrecy, ChaCha20-Poly1305. A device that isn't in your list gets nothing and is counted as a failed probe. Device ids are the first 16 hex digits of the SHA-256 of the key, so an id can't be claimed without the key.
- **Implementation.** `Noise_IK_25519_ChaChaPoly_SHA256` and `Noise_XXpsk3_25519_ChaChaPoly_SHA256` (Noise revision 34), written with the standard library so it runs where compiled packages are blocked by Application Control (it uses `cryptography` automatically when installed). It is cross-checked byte for byte against the `noiseprotocol` package, and the known-answer vectors in `tests/data/link_vectors.json` are checked by the iPhone app's tests too.
- **A paired phone is you.** Your own devices can control this PC, so keep your phone locked, and unpair it from *Devices* if you lose it. For friends, start from the defaults (they can message you and send files; most everything else asks, or is off) and open up per person.
- **Discovery** is mDNS (`_saint._tcp`, via `zeroconf`) plus a small UDP beacon on port 8766. Both only *advertise* an address; they carry no trust. Paired devices dial each other by the last address they saw, or you can type `IP:port`.
- **Firewall.** When Link starts, SAINT asks once (an admin prompt) to add a Windows Firewall rule for port 8765 from the local network and Tailscale (100.64.0.0/10) only — a dismissed "allow access?" prompt, or a Wi-Fi Windows calls "public", used to block devices that were "connected" in Tailscale. `link.manage_firewall: false` turns this off.

### Reach this PC from anywhere (no Tailscale)

*Devices → Away from home → Reach this PC from anywhere* (`link.remote_access`, off by default; `modules/link/remote.py`). When it's on, SAINT:

1. asks your router to forward TCP 8765 to this PC (**UPnP**; the mapping is renewed every 25 minutes and removed when you turn it off) and learns the router's public address;
2. also listens on **IPv6** — most home connections give each PC a public IPv6 address and most mobile networks are IPv6, so the phone can often dial the PC directly with no port forwarding;
3. asks once (admin prompt) for a firewall rule *SAINT Link (anywhere)* for the port;
4. lists those addresses — plus one you type yourself (`link.public_address`: your own port forward's address or a dynamic-DNS name) — in the pairing code and in every hello, so a phone that connected once at home dials them later.

The card says what worked. It can't help when the router has UPnP off (turn it on, or forward the port yourself and type the address), behind carrier-grade NAT, or when the router's IPv6 firewall drops incoming connections — Tailscale still works then. It's safe to expose: every connection is a Noise handshake that needs a paired device's key, or the one-time pairing code while a pairing window is open. If your home's public address changes while you're away, the phone learns the new one the next time it connects at home (a dynamic-DNS name avoids that).

### Every device's log in one place

While your own devices are connected, this PC collects their logs into `logs/devices/<device>-<id>.log` in SAINT's data folder (`link.collect_logs`, on by default): it asks each one for the lines it hasn't fetched yet (`log.get`), once a minute and whenever a device connects. The iPhone app and other PCs keep their recent log in memory for this. *Devices → Device logs* opens the folder. Only your own devices are asked, and only your own devices get an answer.

### Configuration

```json
"link": {
  "enabled": false, "port": 8765, "bind": "0.0.0.0", "device_name": "", "discoverable": true,
  "auto_connect": true, "sync_interval_sec": 60, "share_context": true, "announce": true,
  "approval_timeout_sec": 60, "max_file_mb": 1024, "max_prompt_chars": 2000, "inbox_dir": "",
  "prompt_targets": {}, "shared_scenes": [], "manage_firewall": true,
  "remote_access": false, "public_address": "", "collect_logs": true
}
```

`link.prompt_targets` adds apps for "send this prompt to … on `<app>`": `{"notion": {"app": "Notion", "url": "https://www.notion.so", "wait": 4.0}}`. `link.shared_scenes` lists the scenes collaborators may run. Received files are in `data/link/inbox/<device>/`.

### The iPhone app

The iPhone app (SwiftUI) is in [`mobile/`](../mobile/README.md): always listening for "SAINT", the same language layer, reminders, Spotify, and control of this PC from the phone. It dials *out* to this PC, so nothing on the phone listens for connections. [`mobile/docs/IPHONE_SETUP.md`](../mobile/docs/IPHONE_SETUP.md) explains how to get it onto a phone, including from Windows without a Mac.

---

## MCP servers

`modules/mcp` — SAINT is an MCP client: any [Model Context Protocol](https://modelcontextprotocol.io) server's tools become SAINT tools you can use by voice ("add *buy milk* to my notes", "what are my open pull requests on GitHub?").

- **Set up** in *Settings → MCP*: paste the server's JSON the way its README shows it for Claude Desktop (`{"mcpServers": {"name": {"command": "npx", "args": [...]}}}` for a local program, `{"url": "https://…", "headers": {...}}` for a remote one). Saved in `data/mcp.json`; `mcp.servers` in the config works too. Say *"reload MCP servers"* after a change, *"what MCP servers do you have?"* to list them.
- **Safety**: each tool is registered as `mcp.<server>.<tool>` with its input schema and goes through the same validation, permission policy and confirmations as SAINT's own tools. Tools a server marks read-only run straight away; anything else asks first, unless that server has `"trust": "allow"` (`"confirm"` makes every tool ask). A server that won't start is reported in *Settings → MCP* and the Startup panel; SAINT carries on without it.
- **Small models stay focused**: only the MCP tools that fit the request (a server named in it, or tools whose name and description match it) are shown to the model.
- Uses the local model's tool calling (Ollama), like the rest of SAINT's free-form requests.

---

## Learning

`modules/learning` — SAINT getting better at *your* requests without anyone writing a new command.

```
request ─► learned skill? ─► run its steps
        └► router ─► ok ─► done
                  └► not known / "couldn't find…" ─► planner (local model)
                                                    ├─ steps SAINT understands? ─► run, verify ─► learn
                                                    └─ no plan ─► "want to show me?" ─► yes ─► watch
"no, I meant X" ─► run X ─► learn it for the request before
```

* **Planner** (`planner.py`) — the model sees the command shapes in `catalog.py` (every one is tested to
  route), the open windows, and what SAINT already learned, and answers with JSON steps. A step is only
  run if the router understands it and it's grounded in what you said (no "this window" unless you said
  it, no typing you didn't dictate, the monitor you named, no "…but YouTube" you never mentioned). Steps go
  through the normal router, permission checks and confirmations. It may only click something that
  sounds like what you said, and only press keys if you asked it to press something.
* **Skills** (`skills.py`, `data/skills.json`) — request → commands, learned three ways (worked out,
  corrected, shown). Matched before the router so a learned fix beats the old guess; close speech-to-text
  variants match too. A skill that fails three times in a row is dropped (unless you wrote or edited it).
  Requests that point at something ("switch back", "delete it", "open that folder") are never learned. Edit
  or add skills on **Automations → Learned**, or say "save that as …" / "make a shortcut called … that …".
* **Corrections** (`corrections.py`) — "no, I meant …", "I meant for you to …", "that's not what I asked
  for, …" within 2½ minutes of a request. A correction that names only the thing ("I meant my moe
  playlist", "I said Claude") is fitted into the last request: the same kind of thing is swapped, or the
  word that sounds most like it, or the same action is done on it. Letters spelled out ("M-O-E") are joined.
* **Watch and learn** (`demonstration.py`) — polls the mouse buttons, keyboard *shortcuts* and the window
  list while a lesson runs (up to 2 minutes, ends 15 s after your last action or when you say "done").
  Clicks are named with Windows' accessibility info; Start-menu and taskbar clicks that only opened an app
  become "open X". A program another program started by itself isn't a step; windows moved to another
  screen, maximized or minimized are. Programs SAINT couldn't find by name are added to `desktop.apps` with
  the .exe that ran. Plain typing is never recorded, only that it happened. Nothing is kept but the
  commands. After a failure SAINT only *offers* to watch; "yes" starts it.
* **Lessons** (`lesson.py`) — a task with several steps that SAINT was never taught ("write an email",
  "post on Reddit") is never guessed at. SAINT says so and asks where to start, then does each step as you
  say it (one at a time, so a wrong step is caught at once) and keeps it:

  ```
  you   write an email                         SAINT  I haven't learned how … where do I start?
  you   go to https://mail.google.com/…        (done, kept)
  you   ask me which account to send from, personal or school
  SAINT Which account should I send from, personal or school?        you  school
  you   click me@school.edu     SAINT  Is that the one for "school"? … and for "personal"?
  you   click compose · ask me who it's to · type it in the to box · ask me what to write about
  you   write a short subject line · click the subject box · type the subject
  you   write the email · click the message body · type the email
  you   ask me before you send it · click send · done
  ```

  Next time "send an email to Sam about the trip" runs it: Sam and the trip are taken from the request, so
  it only asks which account; it reads each draft out ("Should I use it, or what should I change?" —
  "make it shorter" rewrites it), and stops before "click send" unless you say yes. The model is used only
  for the *write* steps; everything else is the normal router. A step that fails asks "How should I do it
  now?" and the answer replaces just that step; "skip" skips it, "I'll do it" makes it your part.
  "ask me … if you don't know it" keeps the answer for that exact question (e.g. each person's address).
  Answers you give that appear in a step become `{names}`, so the next run uses the new ones. Say "undo"
  to drop the last step, "cancel" to throw the lesson away, "stop" while it runs. Start one yourself with
  "let me teach you how to …". Lessons are skills with step lines such as `ask: Who's it to? -> recipient`,
  `write: a short subject line -> subject`, `type {subject}`, `confirm: Should I send it?`,
  `you: hover over the profile picture`; on the iPhone they run on your PC.
* Settings: `learning.planner`, `planner_timeout_sec`, `watch_and_learn`, `watch_after_failure`,
  `watch_max_sec`, `watch_idle_sec`, `guided_lessons` (ask to be walked through unknown tasks; on).

---

## The interface

Everything you see is driven by real events from the core runtime — the UI never guesses what SAINT is
doing.

| Page | What it shows |
|---|---|
| **Home** | The state orb (colour, breathing, rings and ripples follow the real assistant state and your voice), wake-word status with live score and mic meters, the conversation with typed input, now playing, a **Live** feed of tools as they run, what's up next, one-click scenes, and system stats. |
| **Music** | A cover-tinted now-playing hero (sharp album art, live progress, shuffle / previous / play / next / like / volume), the hands-free words, your queue and quick actions. |
| **Automations** | **Scenes** (editor with templates, voice phrase, steps, optional schedule, run now) and **Scheduled** reminders and commands. |
| **History** | Totals, today, last 7 days, how much you use SAINT hands-free, a 30-day chart, your most-used tools and a searchable, filterable timeline. Export or clear it. |
| **Memory** | Your profile (who you are, likes, people, projects, routines, music and habits, with a written summary), then every memory — told or learned — to search, filter, add, edit, confirm or delete. |
| **Activity** | Every event, live, with filters (voice, agent & tools, Spotify, problems) and pause. |
| **System** | Health (status, CPU, memory, uptime, language model reachability, STT, voice, wake word), voice-pipeline latency against goals, and module status. |
| **Settings** | General · Voice · AI · Wake Word · Spotify · Memory · Automation · Desktop Control · Appearance · Advanced, with search. |

**Beyond the window**

| | |
|---|---|
| **Halo** | Light around the screen edge while SAINT is minimized (or always — Settings → Appearance → *Halo*; one or all monitors). Four thin click-through windows, so it never blocks anything. |
| **Edge tab** | Rest the cursor at the top-centre edge of the screen for a moment and a small SAINT tab slides down; click it for the overlay. |
| **Overlay** | <kbd>Alt</kbd>+<kbd>`</kbd> (configurable) from anywhere. A frosted copy of your screen with SAINT on top: state, input, now playing, live tools, conversation, up next, scenes, and toggles for the Halo, mini player and demo. <kbd>Esc</kbd> or a click on the background closes it. |
| **Mini player** | Floating, always on top, draggable (it remembers where you put it). Double-click it to open Music. It flashes when it hears a hot-word. |
| **Command palette** | <kbd>Ctrl</kbd>+<kbd>K</kbd>: pages, scenes, music controls, the Halo, the mini player, theme, demo — or “Ask SAINT: …”. <kbd>Ctrl</kbd>+<kbd>1</kbd>…<kbd>8</kbd> jump to pages. |
| **Tray** | Open SAINT, open the overlay, mini player, Halo, start/stop listening, demo, quit. |

**Appearance** settings: dark, light or system theme; accent colour (presets or custom); font and
size; compact density; animations on/off; window opacity; always on top; sidebar labels; Halo, edge tab
and overlay hotkey. Changes apply immediately. On Windows 11 the title bar follows the theme.

![Settings](screenshots/settings.png)

---

## Configuration

- Settings are saved to **`data/config.json`**. It's created on first run, migrated automatically
  when new options are added, and written atomically. `data/` is git-ignored.
- All paths are **relative to the SAINT folder**, so there are no machine-specific paths. Set
  `SAINT_DATA_DIR` to keep runtime data elsewhere (the tests do this).
- Most settings apply live when you press **Save settings**: the wake word reloads, the microphone
  restarts, TTS re-initialises, and the log level and appearance update.

Main sections: `voice.*` (mic, VAD, barge-in, STT, TTS, wake word, `music_hotwords`), `ai.*`,
`agent.*`, `memory.*`, `history.*`, `automation.*`, `desktop.*`, `spotify.*`, `vision.*`,
`appearance.*`, `overlay.*` (Halo mode, all screens, edge tab, hotkey), `widgets.*` (mini player),
`notifications.*`, `permissions.overrides`, `logging.*`.

---

## Logging and debugging

- Log file: `data/logs/saint.log` (rotating, 2 MB × 5), also printed to the terminal.
- Your phone's and other PCs' logs: `data/logs/devices/` (see [SAINT Link](#saint-link)), collected while they're
  connected; *Devices → Device logs* opens it.
- Recorded: wake detections and scores, listening state changes, STT results and latency, the
  selected intent, tool start/finish/failure with duration, Spotify actions and errors, memory
  operations, automation creation/execution, desktop actions, TTS start/stop, barge-ins and
  interruptions, and all errors.
- High-frequency events (audio levels, tokens, per-chunk TTS timing, wake scores) are never logged
  per occurrence; what's playing is logged when the song or play state changes, not every few seconds.
- Settings → Advanced: log level (Verbose / Normal / Errors Only) and **Debug mode**. Settings →
  Wake Word → *Debug* logs every wake score above 0.1.

---

## Tests

```bat
.venv\Scripts\python -m pytest
```

About 700 automated tests cover:

- wake-word model loading, silence and noise rejection, the windowed trigger and cooldown
- music hot-words (accepted only while music is active, whole-utterance match, confidence floor)
- the local history log (sources, tools, trimming, off switch) and scenes (voice matching, schedules)
- a headless build of the whole UI: every page, the command palette, overlay, Halo, mini player, and
  demo mode never reaching the core (no real actions, nothing in your history)
- the listening state machine (transcript wake for bare "SAINT", passive speech never acted on,
  wake → command, the conversation window that waits while SAINT talks, timeouts)
- natural-language intent understanding: many phrasings per action, action phrases never becoming
  song titles, context-dependent meaning ("turn it down", "go back", "pause") and multi-step plans
- which window a command acts on, and answering "which one?" questions
- the echo gate, plus interruption handling in the conversation controller
- intent routing for Spotify, memory, reminder and desktop phrasing
- memory store, recall, update and delete, including persistence across a restart
- the scheduler (parse, persist, execute, missed reminders, recurring, cancel)
- tool validation, permissions and confirmations, and desktop safety checks
- Spotify resolution, device recovery and listening memory against a simulated API, including skips
  counted once, paused time, skip sources and DJ mode
- SAINT's own window by voice, and every misrouted request from the live logs
- Steam file parsing and game matching; drive scans (junctions, cloud files), junk rules, WinRAR
  listing, zip-slip protection, and Recycle Bin / move safety against a simulated Windows file operation
  (a test fails if permanent deletion ever appears in `modules/files`)
- stop / silent mode / whisper replies, cancelling multi-step plans, aliases, the follow-up intent gate
- History streaks, heatmap levels and the never-trimmed daily summary
- Windows controls, clipboard (secret detection), workspaces, watchers with a fake clock, "what
  changed?", error-location parsing, "handle this" validation, notification parsing

Tests use a temporary data directory and mock STT, TTS and LLM backends, so they never touch your
settings, microphone, Spotify account or desktop. `tools/dev/` holds manual diagnostic scripts, and
`python tools/dev/screenshots.py` regenerates the README screenshots from synthetic data.

Before a release, run through the **[manual testing checklist](MANUAL_TESTING.md)** (voice,
Spotify, screen, mouse/keyboard, windows, browser, multi-step, natural answers).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Mic meter flat / SAINT never hears you | Settings → Voice → Input device, then **Test (3 s)**. With Voicemeeter, make sure your microphone is actually routed to the bus SAINT listens on (e.g. "Voicemeeter Out B1"). |
| Bluetooth headphones (AirPods) sound bad on the PC | Something opened their microphone, which switches them to call quality. SAINT remembers its mic by name and never opens a Bluetooth headset's mic unless you pick it (Settings → Voice; `voice.allow_bluetooth_mic`). If they still drop, check Voicemeeter's hardware inputs and Windows' default communications device. |
| The phone can't reach the PC away from home | *Devices → Reach this PC from anywhere* (or Tailscale); open the app once at home afterwards. *Check connection* on the device says which address fails. |
| Wake word never triggers | Say "**Hey** SAINT". Raise the sensitivity and watch the live score in Settings → Wake Word. Check the status line for model errors. |
| Wake word triggers on TV or speech | Lower the sensitivity or add a confirmation frame. |
| "Wake word unavailable" | The status line gives the reason (missing model, missing feature models, onnxruntime not installed). |
| SAINT interrupts itself | Raise Settings → Voice → Echo margin, lower the speaker volume or use headphones. |
| "I can't reach my language model" | Start Ollama (`ollama serve`) and check the base URL. |
| "Model … isn't installed" | `ollama pull llama3.1`, or pick an installed model in Settings → AI. |
| TTS on CPU / "CUDA unavailable" | Install the CUDA PyTorch build (see Installation). The reason is logged at start-up. |
| Spotify: "isn't connected" / "no device" / "needs Premium" | Connect in Settings → Spotify. Open Spotify on a device. Playback control needs Premium. |
| "I can't see a 'search box'" | The app doesn't expose that element through UI Automation; click into the field and say "type …" instead. |
| DLL "blocked by Application Control" | A Windows Smart App Control / WDAC policy blocks a compiled package. SAINT avoids scikit-learn for this reason; allow the package or use a different Python environment. Smart App Control can also intermittently block SciPy DLLs that Kokoro TTS loads — System shows TTS as not ready; restart SAINT or turn Smart App Control off. |
| "Overlay hotkey is taken" toast | Another app already owns that shortcut. Pick a different one in Settings → Appearance → *Overlay hotkey* (e.g. `ctrl+alt+s`). |
| No Halo | It shows while SAINT is minimized or in the tray (or always — Settings → Appearance → *Halo*). Exclusive-fullscreen games draw over it; borderless-windowed games don't. |
| “Skip” without the wake word does nothing | Hot-words work while Spotify is playing (or paused with the mini player showing). Check Settings → Spotify → *Hands-free*. |

---

## Project layout

```
app.py                  entry point (Qt app, tray, single instance, runtime start)
core/
  runtime.py            starts/owns TTS, conversation controller, voice, scheduler (UI-independent)
  assistant_state.py    authoritative assistant state
  conversation.py       turn orchestration, TTS queue, interruption, announcements
  audio_echo.py         playback monitor + echo gate (barge-in)
  events.py             event bus (direct core subscribers + Qt signal for UI)
  config.py, paths.py   settings (+ migration) and project-relative paths
  permissions.py        allow / confirm / deny policy
  history.py            local usage log (data/history.jsonl) + never-trimmed daily summary
  history_stats.py      streaks, heatmap levels and totals for the History page
  cancel.py, activity.py  "stop" across plans/scenes/typing; "what are you doing?"
  ui_link.py            lets tools drive SAINT's window on the GUI thread
  logger.py, device.py, analytics.py, state.py, setup.py
modules/
  voice/                mic capture, VAD, wake word (ONNX), STT, TTS
  agent/                intent router, confirmations, LLM tool calling, short-term memory (recent.py)
  automation/           tool registry, scheduler, time parser, scenes, background tasks
  spotify/              OAuth PKCE, API client, tools, listening memory
  memory/               SQLite store + structured memory service
  desktop/              apps, windows, keyboard/mouse, UI Automation, clipboard, Windows/audio controls,
                        Voicemeeter, fuzzy window matching (misheard names, running Steam games)
  files/                drive scans, junk rules, Recycle Bin / move (undoable), WinRAR archives
  steam/                Steam library files, game matching, steam:// actions
  learning/             planner ("try harder"), learned skills, corrections, watch-and-learn
  watch/                watchers ("tell me when…") and the "what changed?" log
  workspace/            saved window layouts
  notifications/        Windows notifications (read-only)
  dev/                  run tests, open the file behind an error
  ui_control/           voice control of SAINT's own window
  vision/               screen capture, UI context, optional vision model
  ai/                   providers (Ollama/OpenAI-compatible/mock), context
ui/
  main_window.py        shell: sidebar, pages, tray, hotkey, palette, toasts
  pages/                home, music (+ NowPlaying), automations, history, memory, activity, storage, system
  halo.py, overlay.py   screen-edge Halo + edge tab, Steam-style overlay
  spotify_widget.py     floating mini player
  palette.py, toast.py, demo.py
  reactive.py           ui_bus: the UI's view of SAINT (and the demo's scripted feed)
  theme.py, icons.py, motion.py, widgets.py, win.py, actions.py, settings_ui.py
data/wake/              wake-word models (committed); everything else in data/ is local
tests/                  pytest suite
tools/                  wake-word training config, dev scripts
```

---

## Privacy and security

- Wake word, speech recognition, speech output and the language model all run **locally**. Nothing
  is sent anywhere except Spotify API calls, and an OpenAI-compatible endpoint if you configure one.
- Secrets never go in the repo. Spotify tokens live in the Windows Credential Manager. `data/`
  (config, memories, history, scenes, logs) and `.env` files are git-ignored — a fresh clone starts
  clean, and your data stays on your PC.
- **History** is a plain local file (`data/history.jsonl`) you can turn off, cap, export or clear. It
  is never uploaded. Demo mode never writes to it.
- The overlay's frosted background is a snapshot of your screen taken the moment it opens; it's kept
  in memory only and dropped when the overlay closes.
- **Files are never deleted permanently.** Removals go to the Recycle Bin after you confirm; moving,
  recycling, uninstalling games and power actions always ask, whatever the permission mode.
- **Clipboard and notifications stay on your PC** and are never offered to the language model as tools.
  Anything that looks like a password, key or token is never read aloud.
- "What changed?" keeps window titles and 64-bit screen fingerprints in memory only, for 30 minutes.
- The LLM can only act through explicit, validated tools. It has no shell access, and high-risk
  actions need your confirmation. It can't ban artists, record dislikes or force-quit programs; those
  only happen when you say so. Questions ("what are you doing on my Spotify?") never trigger tools.
- **Learning stays local.** Plans are made by the local model. While SAINT watches you show it something
  it's announced on screen, lasts at most two minutes, and records only apps opened, the names of what
  you clicked and keyboard shortcuts — never what you type. Only the resulting commands are saved
  (`data/skills.json`), and "forget that" or Automations → Learned removes them.
