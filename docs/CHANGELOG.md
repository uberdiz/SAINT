# SAINT changelog

Moved out of the README on 2026-10-05; newest first.


**0.4.0 — 2026-10-07 — SAINT does whole tasks and checks its work:**

SAINT's version now starts again at 0.4.0. Your data is migrated automatically on first start (see the end of
this entry).

- **Agent tasks.** A request with several parts ("set up my coding workspace for SAINT, open Discord and put
  Spotify on my second monitor", "start the dev server and tell me when it's ready") becomes a task SAINT runs
  step by step: it observes first, plans (a procedure it learned → a built-in goal → your own clauses → the
  local model only when nothing else fits), acts through the same tools and permission checks as speech, checks
  each step really happened (window, process, port, web page, file, monitor, Spotify, the browser's address, text
  on screen), and recovers within limits (2 retries per step, 6 per task, 15 minutes). Simple requests still go
  straight to one tool with no planner and no model call.
- **Talk to the task.** "Pause", "continue", "what's next?", "why did that fail?", "do that again", "actually put
  Spotify on my main monitor" — only while there is a task (a bare "pause" with music playing is still the
  music). A question in the middle of a task ("which window?") waits for your answer and carries on from that
  step. A task interrupted by a restart comes back paused.
- **Risk levels and permission modes.** Every step is low, medium or high risk. Safe runs only low steps;
  Confirm (the default) asks once for a plan with medium steps and at each high one; Autonomous asks only for
  high ones. Deleting, uninstalling, shell commands, power and payments are always high.
- **Recovery that finds another way.** An app it can't find is looked up again among your installed apps; a
  missing window is opened first; Spotify with no device is opened and retried; a dev server that crashed has
  its output read — missing packages are installed (asks first in Confirm mode) and a busy port is rechecked. Only
  then is the local model asked, with a few lines instead of the whole conversation, and its answer must be a
  command SAINT understands.
- **Learning procedures.** A finished task is saved as a procedure (its steps stay in `skills.json`, so it shows on
  Automations › Learned and older SAINT versions still run it). A procedure only changes when a real run proves
  the change: a recovery that passed its check, or a step you corrected mid-task. "Remember how I just did that"
  saves the last task.
- **Development helper.** Finds a project, works out how to start, test and install it, runs tests with the
  failures summarised, starts and stops dev servers (output kept, URL detected, stopped when SAINT quits),
  diagnoses why something won't start, installs dependencies, clones a repository and finds a definition. Commands
  that run things are medium risk; nothing edits your code on its own.
- **Reading the screen locally.** Windows' built-in OCR reads text on screen before any vision model is used:
  app/API data → windows and processes → UI Automation → OCR → a vision model (only when allowed).

**The interface, redesigned around tasks:**

- **Overview** (the current task card with its steps and checks, what needs your attention, devices, health),
  **Tasks** (current and past tasks, with pause / continue / cancel) and **Activity** (each tool call and its
  result, in plain words). Home now opens Overview.
- **Your layout.** Settings → Appearance → Layout: hide and reorder sidebar pages and Overview panels, turn the
  status bar off, choose the monitor SAINT opens on. Applied without a restart.
- **Health panel** on Overview and System: microphone, wake word, speech, voice, model, Spotify, Link, with what
  to do when one is down. Device cards show each device's connection (Wi-Fi, Tailscale, internet) and latency.
- A consistent design system (spacing, type, status colours) and reusable components behind every page.
- The window fits on smaller screens (down to 760 × 520) and opens on the monitor you chose.

**Fixed:**

- **The pairing QR code was cut off** on smaller or scaled screens. The cause was the inbox folder path on the
  Devices page, which never wrapped and pushed the page wider than the window. The QR now draws whole at any
  scaling, with sharp edges and its quiet zone, and opens full size when clicked; the code and address have Copy
  buttons.
- **Mini player volume didn't change Spotify** — moving the slider dipped and then jumped back to full. The slider,
  mouse wheel and Now Playing page now set Spotify's own volume (after a short pause while you drag), never the
  Windows mixer, and follow changes made in Spotify or on your phone within a few seconds while a player is
  open. Devices whose volume Spotify can't control say so instead of pretending.
- **Blurry album art.** Covers are loaded at Spotify's highest resolution (falling back to the normal size),
  decoded off the interface thread and scaled once. A site's app icon (Chrome's logo for a YouTube video) is
  no longer stretched across the mini player as if it were album art.
- Task steps that were removed stayed painted on top of new ones, and rebuilt Overview panels could be squeezed
  to a few pixels high.

**Upgrading from an earlier SAINT:**

- On first start SAINT backs up every file in `%LOCALAPPDATA%\SAINT` to
  `%LOCALAPPDATA%\SAINT-backups\before-v0.4.0-<time>` (and checks the copy), then migrates: settings to version 8
  (every value kept, new defaults added), older tasks into the Tasks page, learned skills into procedures, and a
  version marker. Each step is recorded, so an interrupted upgrade resumes where it stopped; nothing is deleted.
  Data from an older SAINT folder is imported when the new one is empty. `python -m core.migration --check` lists
  what's pending.
- A source checkout now uses `%LOCALAPPDATA%\SAINT` once SAINT has data there, so a new checkout no longer starts
  empty (`SAINT_DATA_DIR` still overrides it).

**Packaging:**

- `SAINT.exe --selftest report.json` checks an install without a microphone or window: libraries, bundled models,
  the data folder, every page, the pairing QR, OCR, the agent and (optionally) a full voice round trip. The build
  runs it on the packaged app with a fresh data folder and no Python on `PATH`, and fails if a required check fails.
- The installer was tested end to end: silent install, the self-test from the installed folder, uninstall
  (program folder, Start menu entry and uninstall entry removed; `%LOCALAPPDATA%\SAINT` kept).
- 1,432 automated tests pass (1,343 before this release).


**2026-10-06 — your phone from anywhere without Tailscale, every device's log on your PC:**

- **Reach this PC from anywhere.** Devices → *Away from home*: SAINT forwards its port on your router (UPnP),
  listens on IPv6, adds the firewall rule, and gives your phone the addresses — no Tailscale or other app needed.
  You can also type your own port forward's address or a dynamic-DNS name. The card says what worked.
- **Logs in one place.** Your PC collects the iPhone's and your other PCs' logs into `logs/devices/` while they're
  connected (*Devices → Device logs*). "Now playing" is no longer logged every five seconds.
- **AirPods on the PC keep full quality.** The microphone was remembered by number; connecting AirPods renumbered the
  devices and SAINT opened their hands-free mic, which drops them to call quality. It's now remembered by name, and a
  Bluetooth headset's mic is never opened unless you choose it.
- **Albums play as albums.** "Play Fancy That by PinkPantheress" played her song "Tonight" plus a radio of similar
  songs; an album whose name matches better now wins (misheard artists too), and "X album by Y" / "Y's album X" work.
- **"That's not the vibe I asked for"** skips the song and keeps it out of that mix without counting it as a dislike.
- **Mini player:** the album art grows to any size (it stopped at about 250 px wide), a *Big album art* preset, it
  can't be dragged off screen, and its volume slider is Spotify's own volume.
- **iPhone:** History and Memory tabs (with search; Devices opens from the PC chip and Settings), Settings text no
  longer runs under switches and pickers, and Sync now shows a spinner and its result on every tab.


**Since 2.2 — SAINT learns long tasks by asking, and interrupting works mid-automation:**

- **Walk SAINT through a task once.** Ask for something it was never taught that takes several steps ("write an
  email", "post on Reddit", "fill out the form") and it no longer guesses ("write an email" used to type the words
  "an email"): it asks where to start, does each step as you say it, and keeps them. Steps can be questions it
  asks every time ("ask me who it's to", "ask me which account, personal or school"), text the model writes
  ("write a short subject line"), a yes before something final ("ask me before you send it"), or a part only you
  can do ("I'll do it" — next time it waits for "next"). Next time, "write an email to Sam about the trip" fills in
  what you said and asks only the rest; a step that stops working is asked about and replaced. See
  [Learning](#learning).
- **Interrupting a long automation.** A new request while SAINT is still working stops the old one at its next step
  and runs — it used to wait 5 s and then be dropped. The Stop button stops automations too. Talking over a question
  SAINT is asking still just answers it.
- **Spotify stops skipping songs you wanted.** When SAINT only noticed something else playing it marked the whole
  auto-queue as leftovers and skipped them one after another; now only music SAINT itself replaced is skipped, at
  most three songs in two minutes, and a queued song playing under a relinked Spotify id is recognised. "Next" or
  "skip it" sung by the song (too quiet to be you) no longer skips.
- **iPhone uses your PC when it can.** Anything the phone doesn't do itself goes to your PC — dialled on demand when
  the link is down — and only then to the phone's own model. Music the phone's Spotify can't play is played through
  the PC. The Activity log says where each request ran and why ("Answered on this phone — Home PC wasn't
  reachable"), and Sync now reaches the PC even when it wasn't connected and says "Already in sync" instead of
  "received 0, sent 0".

**2.2 — a 3x smaller install, your iPhone in History, and SAINT from anywhere:**

- **Smaller, faster installer.** SAINT.exe speaks with the same Kokoro voice through onnxruntime instead of PyTorch +
  CUDA, so the install is a fraction of 2.1's ~3 GB. All models (voice, speech recognition, wake word) are in the
  installer; nothing downloads on first run. From source, `python tools/get_kokoro_onnx.py` gets the ONNX voice
  (SAINT uses it automatically when PyTorch isn't installed; `voice.tts_backend = "kokoro_onnx"` forces it).
- **iPhone activity in History.** Everything SAINT Mobile does syncs to the PC and appears in History → iPhone.
- **Reach your PC from anywhere.** With Tailscale installed, the pairing QR code includes the PC's Tailscale address,
  and Devices shows "Tailscale on". The QR code works in the packaged app. Paired devices can be renamed.
- **SAINT Mobile 0.3** — new orange UI, phone control by voice, wake-word fix: see [mobile/README.md](mobile/README.md).

**2.1 — Gaming Mode you control, multi-monitor, tasks that continue, and a real Windows app:**

- **SAINT.exe and an installer.** `python packaging/windows/build.py` builds `build/windows/SAINT/SAINT.exe`
  (no Python or terminal needed; speech models bundled, works offline) and `build/windows/SAINT-Setup.exe`
  (per-user install, Start menu and optional desktop shortcut, clean uninstall; your settings and memory in
  `%LOCALAPPDATA%\SAINT` are kept).
- **Gaming Mode is a switch, not a side effect.** A game running no longer means Gaming Mode — turn it on by
  voice ("gaming mode on"), from the tray or in Settings → Gaming Mode, or switch on *Auto Gaming Mode*.
  Each part of SAINT has its own Gaming Mode setting: wake word, spoken replies, mini player, Spotify,
  notifications (all / minimal / off), vision, screen automation, Halo, AI chat, performance mode, and
  moving SAINT off the game's monitor. Anti-cheat safety (no see-through overlay on a game, no clicks into
  it) applies to any running game.
- **Multi-monitor.** While gaming, SAINT's window, overlay and mini player go to a monitor without the game
  (or the one you pick). "Move the mini player to my second monitor", "put SAINT on the left screen".
- **Mini player.** Shows what SAINT is doing (listening, thinking, the task step) and Gaming Mode. Right-click
  for keep-on-top, opacity, size and monitor; drag its right edge to resize; ctrl+scroll to fade; it remembers
  a separate position for Gaming Mode.
- **Tasks that continue.** Multi-step requests are remembered step by step (even across a restart):
  "where were we?", "continue what we were doing", "finish it", "do the same thing for Discord",
  "set up my gaming workspace".
- **Startup you can see.** Settings → System → *Startup* lists every subsystem (voice, speech output, AI model,
  Spotify, Link, monitors…) with why it failed and a **Retry** button; one failing part never stops SAINT.

**Earlier — Game Mode, Better Listening & Dialog State:**

**Latest — Game Mode, Better Listening & Dialog State:**

- **Game Mode:** SAINT automatically detects fullscreen and protected games. It hides overlays (like the Halo), pauses screen hashing, and prevents input injection so it stays out of your game's way and avoids anti-cheat flags. Settings restore when the game exits.
- **Short-Answer & Dialog Fixes:** A new dialog state machine fixes the dropped "yes/no" answers. SAINT now listens better for single-word replies during follow-ups, with shortened end-of-speech waits and clear countdown indicators when waiting for your choice.
- **Improved Follow-ups:** Follow-up commands automatically strip filler words ("uh", "yeah"), and you can reference items naturally (like "the second song" for Spotify results).
- **Better Demonstrations:** The demonstration recorder now filters out unrelated windows and asks you to review steps before saving them.
- **Packaging Groundwork:** Path resolution, frozen-app support, AppUserModelID, and multi-size taskbar icons are now built-in, laying the foundation for an upcoming standalone EXE installer.
- **UI & LLM Polish:** Added a "Didn't catch that" visual cue for dropped speech, LLM argument repairs, and output guards to prevent raw tool-syntax from being spoken out loud.

**Earlier — SAINT remembers what just happened:**

- **Short-term memory.** SAINT keeps track of what it did, made or found in the last half hour — the
  screenshot it took, the folder it extracted, what a junk check found, the app it opened, the playlist it
  played. So you can follow up naturally: "delete that screenshot", "open that folder", "move it to my C:
  games folder", "rename it to TheLoop", "copy the path", "what did you find?", "delete it", "the other old
  installers too", "go to my most recent download and delete it". Removing or moving anything still asks
  first, and only ever uses the Recycle Bin. The language model is given the same list as facts, so it
  answers "what did you find?" from what really happened instead of guessing a path.
- **Corrections that name only the thing.** "I meant my moe playlist", "I said Claude" (after "switch to
  Glod"), "no, the folder you just made" — SAINT fits the right thing into your last request. Spelled
  letters work too: "…spelled M-O-E". A correction also answers an open "Did you mean …?" question.
- **Files by voice.** "Make a new folder in my games folder called The Loop", "move it to the D drive",
  "rename it", "show it in Explorer", "open the screenshot". Spoken places like "my C: games folder" or
  "games/the loop" resolve to real folders.
- **Storage page.** A new sidebar page: drive space, what's using it, and **What you can clear** — a chart
  by category (graphics shader caches, Spotify/Discord caches, browser caches, old temp files, crash dumps,
  developer caches, duplicate downloads, old installers, Windows Update leftovers, what's already in the
  Recycle Bin) and a list you tick. The junk check now looks in far more places.
- **Learning you can edit.** Automations → Learned has an editor: change what a phrase does, or add your
  own ("gaming time" → open Steam, open Discord). By voice: "save that as game time" right after something
  worked, or "make a shortcut called gaming time that opens Steam and Discord". "Switch back", "delete it"
  and other pointing phrases are never saved as recipes.
- **Watching asks first.** After something fails SAINT asks "Want to show me?" — only "yes" starts watching,
  so it no longer records the wrong thing at the wrong time. More ways to say it ("watch me do it", "I'm
  going to do it now, watch"), more ways to finish ("I'm all done"), and it notices windows moved to
  another screen or maximized. Programs it sees you start that it couldn't find by name (taskbar-only apps
  like Bloxstrap) are added to its app list; things an app starts by itself aren't recorded as steps.
- **Fixes from the 2026-09-25 evening log.** Spotify no longer needs reconnecting at every launch (a test
  had been wiping the saved login; a Spotify hiccup no longer forgets it either). Scrolling really scrolls
  (it was moving 1/20 of a wheel notch) and "scroll more" / "keep scrolling" go further each time.
  One-word answers ("Yeah.") and short commands ("Press enter.") aren't dropped as low-confidence speech.
  "Delete …" never means "forget a memory". "The loop layer" isn't YouTube's loop. "Close that window"
  names the window it means. The planner can't click things you never named or press Delete on its own.
  Opening the Steam library when Steam is closed waits for Steam, then opens it. Apps pinned only to the
  taskbar or the desktop are found by name ("open Bloxstrap", "block strap").

**Earlier — SAINT learns what it can't do yet:**

- **It tries harder.** When a request isn't one SAINT knows ("hop over to Discord", "open disk, clean up")
  or what it tried fails ("I couldn't find a window for all my windows"), the local model rewrites the
  request as commands SAINT *does* know ("switch to discord"; "show the desktop" then "double click the
  recycle bin"). Every step is checked before anything runs, so the model only ever picks real,
  permission-checked commands. If it works, SAINT remembers: next time it's instant.
- **"No, I meant …" teaches it.** "Play my gym playlist" → "no, I meant play my moe playlist" plays moe
  *and* remembers that's what "my gym playlist" means. "No, close the finals" answers a question and does
  it.
- **Show it once.** If SAINT still can't work it out, it offers to watch (a pill shows "Watching how you
  do it"). Do it yourself, say "done", and SAINT turns what you did into steps it can repeat — apps you
  opened, buttons and icons you clicked (by name), shortcuts you pressed. It never records what you type.
  "Let me show you how to …" starts a lesson any time.
- **Automations → Learned** lists everything learned, how (worked out, corrected, shown) and how often it's
  used, with *Try it* and *Forget*. By voice: "what have you learned", "forget that".
- **Fixes from the 2026-09-25 log.** Video audio and room conversation no longer become requests (the
  follow-up check now covers the window after an action and talking over SAINT). "Minimize all my
  windows", "right click the desktop on my main screen", "double left click", "the recycling bin" (the
  desktop icon, not text on screen), "close the finals" / "close to area" (finds the game by its Steam
  folder or a misheard name and asks with the real name; offers a force quit if it won't close), "take a
  screenshot of Claude", "turn down Spotify", "open YouTube and fullscreen it", "my gym playlist" never
  silently becomes a stranger's playlist, "never play that playlist again", a repeated request isn't
  dropped as a loop, and the model can't ban artists or record dislikes from a chat reply anymore.
- **Voicemeeter.** "Mute" / "unmute" / "mute my mic" mute Voicemeeter's mic strip; "switch my audio to my
  headphones" / "use my speakers" move Voicemeeter's A-bus routing (A2 speakers ↔ A1 headset).
- **Progress you can see.** Scans and junk checks fill a ring on the action pill; "what are you doing?"
  says how far along they are.

**Earlier — hands-free PC control:**

- **SAINT controls itself.** "Open the dashboard", "go to history", "turn off the mini player", "hide the
  halo", "dark mode", "minimize yourself". "The mini player" means SAINT's; YouTube's only when YouTube is
  named or being watched.
- **Steam.** "Launch Counter-Strike 2", "start cs2", "play Terraria on Steam", "open my Steam library",
  "search Steam for Hades", "what games do I have", "what are my biggest games", "uninstall Celeste" (asks
  first, and Steam asks again). SAINT reads Steam's own files, including libraries Steam has forgotten
  about, and tells you how to add them back.
- **Files and storage.** "How much space is left on E", "what's taking up space on D", "clean up my
  Downloads", "what can I delete on E", "only the installers", "find my emulators", "open my games
  folder". Duplicate downloads, archives you've already extracted, temp files, shader caches and old
  recordings are found for you. Games, emulators, ROMs and Steam libraries are never suggested. Anything
  removed goes to the Recycle Bin **after you say yes** — SAINT never deletes permanently and refuses
  anything the Recycle Bin couldn't hold. The Storage page has every drive, a scan and
  the biggest folders.
- **WinRAR.** "Go to my Downloads folder, click the first download and extract it using WinRAR to my games
  folder" is one action: the newest archive, into its own folder, with a free-space check, progress, no
  overwriting, and "delete it afterwards" if you ask. "Compress this folder as a rar."
- **Windows.** "Lock my PC", "restart" / "shut down" (asks, then waits 60 s — "cancel the shutdown"), "mute
  my mic", "set Discord to 30 percent", "mute the game", "switch audio to my headphones", "brightness 60",
  "take a screenshot".
- **Stop, silent mode, whispering.** "Stop" / "cancel" / "stop everything" interrupt speech, multi-step
  plans, scenes and typing mid-way. "Be quiet for 30 minutes" keeps SAINT working without talking (it
  still asks questions and reports errors). Whisper a request and SAINT answers quietly. "What are you
  doing?" describes the plan in progress.
- **Watching.** "Tell me when Claude finishes", "tell me when this download finishes", "tell me when Steam
  closes", then "take me back". "What changed while I was away?" "Watch my left screen" makes "click",
  "read this" and "what's on screen" use that monitor.
- **Workspaces and scenes.** "Save this workspace as Coding" / "restore Coding" reopens and places every
  window on the right monitor and resumes your music. New starter scenes: *Gaming mode* ("let's game"),
  *Done gaming* ("I'm done gaming") and *Dev environment*.
- **Clipboard, notifications, developer mode.** "Read my clipboard" (never secrets), "summarize / translate
  what I copied", "fix the code I copied" (the fix goes back on the clipboard). "Read my notifications",
  "only tell me about important notifications", "always tell me about Discord". "Run the tests", "open the
  file causing the error". "Handle this" proposes one step for the window in front and asks first.
- **Personal aliases.** "When I say the lab, I mean open my SAINT project in VS Code."
- **Spotify learns from what you actually skip.** Skips in the Spotify app are caught within seconds
  (through Windows' media controls) and recorded once — voice skips used to be counted twice. Paused time
  isn't "listening". Your Spotify top tracks, saved songs and recently played feed the picks. **DJ mode:**
  "more energetic", "something darker", "more like the last song", "something I haven't heard", "no more
  of this artist".
- **History.** A GitHub-style year of day squares, current and longest streak, success rate, typical reply
  time, time of day, busiest weekday and how you ask (voice, hot-words, typed, scenes).
- **Fixes from the logs.** "Moved Otis" now says "Moved Spotify" (and YouTube instead of Opera). "Press F to
  full screen", "one time speed", "go to my browser on my right screen", "clear everything off the screen
  but YouTube", "go to Astral Games" (a link on the page) all work. SAINT uses the browser window you used
  last instead of asking "which one?". Follow-ups without the wake word ignore chatter like "OK? Why
  not?", and Whisper's "No, no, no, no…" loops are dropped. The `llama3` model-missing warning is gone
  (your setting is updated to `llama3.1`).

**Previous update:**

- **Rearrange the overlay.** Drag any card in the <kbd>Alt</kbd>+<kbd>`</kbd> overlay by its title bar to
  move it, or drag an edge or corner to resize it. The layout is remembered; *Reset layout* puts it back.
- **Action notices.** A small pill at the bottom of the screen shows what SAINT is doing ("YouTube · 1.5x
  speed", "Search YouTube for lofi") and whether it worked. Click-through, never steals focus.
- **Switches you can read.** Every on/off setting is now a switch with a plain *On* / *Off*. The Halo,
  action notices and the mini player apply the moment you flip them — and show themselves (the Halo glows
  for a few seconds, even over the overlay; a sample notice pops up), so you can tell without leaving
  the menu. All three are also switches in the overlay.
- **YouTube, the way you'd use it.** "Theater mode", "full screen", "1.5x speed", "normal speed",
  "skip ahead 30 seconds", "captions on", "next video", "next chapter", "mute", "jump to 50 percent",
  "set the quality to 1080p", "turn off autoplay", "loop this video", "sleep timer 30 minutes",
  "skip the ad", "like this video"… SAINT uses YouTube's own keyboard shortcuts, and its player menus for
  the settings that have none. While a YouTube tab is in front, bare commands ("pause", "faster",
  "mute") mean the video.
- **The mini player shows anything that's playing** — a YouTube video, VLC, a browser tab, not just
  Spotify — with its artwork, progress and working controls (through Windows' media controls).
- **No more "which browser?" every time.** One browser window on your screen: SAINT just uses it.
  Several: it asks once and keeps using the one you picked until that window closes ("use a different
  browser" to change it). None on screen: it offers the minimized ones or a new window; no browser at
  all: it offers to open yours.
- **Memory that learns, and a profile of you.** Besides what you tell it outright, SAINT now picks up
  things you mention in passing ("I'm a nurse", "I hate horror movies", "my sister's name is Mia") and,
  optionally, lets the local model find lasting facts in what you say. Learned facts are marked as such,
  get stronger when they come up again and fade if they never do; a guess never overwrites something you
  said. The Memory page opens with your profile: who you are, likes, dislikes, people, projects,
  routines, music taste and usage habits, plus a short summary written from those facts.

A ground-up rewrite of the interface:

- New minimal design system: near-black surfaces, hairline borders, one accent, a monoline icon set,
  and motion everywhere it helps (page transitions, a sliding nav pill, a living state orb, cross-fading
  covers, toasts). One switch in Settings turns all animation off.
- **Halo**, **overlay** (global hotkey + edge tab), **floating mini player**, **music hot-words**,
  **scenes**, **History** with usage stats, **command palette**, and a merged **System** page (health,
  latency, modules).
- **Demo mode** — SAINT drives its own UI for 30 seconds (wake word, a request, music, a hands-free
  skip, the palette and the overlay). It's fully scripted: nothing real runs, nothing is written to your
  history. Start it from the palette, the overlay or the tray; <kbd>Esc</kbd> stops it.
- Sharp album art everywhere (SAINT now uses Spotify's 640 px cover), loaded off the UI thread.
- **Fixes from live testing:** follow-ups without the wake word now work while music plays (anything
  SAINT recognises as a command is kept; chatter and lyrics are ignored), one-word commands ("pause",
  "skip") are no longer dropped for low speech-recognition confidence, a long request said with
  pauses arrives as one command, run-on multi-step requests ("open my browser search YouTube for …")
  are split correctly, "find the settings button" / "the X button in the top right" / "the recycle bin"
  find the right element (including on the taskbar and desktop), new commands ("hide everything except
  Spotify", "open a new tab", "summarize this page", "open my Monkeytype browser"), news questions answered
  from the Google News feed, and the overlay shows your Spotify queue.

| | |
|---|---|
| ![Overlay](screenshots/overlay.png) | ![The Halo and the mini player](screenshots/halo.png) |
| **Overlay** — over whatever you're doing | **Halo** — light around the screen edge, plus the mini player |
| ![Music](screenshots/music.png) | ![History](screenshots/history.png) |
| **Music** — cover-tinted player, hands-free words, queue | **History** — a year of activity, streaks and timeline |
| ![Automations](screenshots/automations.png) | ![System](screenshots/system.png) |
| **Scenes** — one phrase, many actions | **System** — health, storage, latency, modules |
| ![Learned](screenshots/automations-learned.png) | ![Storage](screenshots/storage.png) |
| **Learned** — what SAINT worked out, was corrected on, or was shown | |

<sub>Screenshots are generated by `tools/dev/screenshots.py` from synthetic demo data (fictional tracks
with generated covers) — no personal data.</sub>

---
