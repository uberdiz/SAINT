# SAINT manual testing — what's left

Everything you marked **Y** up to 2026-10-06 is confirmed and has been removed: the release installer, the mini
player's album-art mode / presets / voice sizing / live updates / moving it by voice, its art growing to any size,
the *Big album art* preset and stopping at screen edges, file and folder replies, English replies for shortcuts,
"stop speaking", "Hey SAINT" alone, the email scene in one breath, typing in Notepad, music during a lesson, "make it
shorter", moving files into a new folder, short replies after multi-step tasks, nicknames for Instagram chats, the
iPhone keyboard's Send key, text fitting on the iPhone, the iPhone away from home over Tailscale and over cellular
without Tailscale (and the switch turning that off), the iPhone answering when Ollama is down, History across
devices, album play ("Fancy That"), the iPhone's History / Memory tabs, Settings layout and Sync now, the first
Link start's single firewall prompt, and "Check connection".

What's here is new in **0.4.0**, was fixed since your last pass (re-check), is still open, or was never tested.
Tick an item only after you've seen it work on the real machine or phone, and add notes the same way as before
(`[N — what happened]`).

**Test without your real data:** `run.bat --profile clean` (fresh install) or `run.bat --profile demo` (sample
scenes). `python tools\profiles.py snapshot` then `run.bat --profile snapshot-…` tests on a copy of your data.

**Upgrading:** the first 0.4.0 start migrates `%LOCALAPPDATA%\SAINT` after backing it up to
`%LOCALAPPDATA%\SAINT-backups\before-v0.4.0-<time>`. Your PC's copy was migrated on 2026-10-07 at 07:40 (backup
`before-v0.4.0-20261007-074025`).

## 1. Fixed after your 2026-10-06 notes — re-check

- [ ] **Mini player volume** (you: “goes down but resets back at full and doesn't change the volume of Spotify”).
      Drag the slider and scroll on the art: Spotify's own volume changes (check in Spotify) and the slider stays
      where you left it. Change the volume in Spotify or on the phone: the slider follows within ~3 seconds. Same on
      Music → Now Playing. A YouTube video still uses the browser's volume; a device Spotify can't set says so.
      Cause: every poll overwrote the slider with Spotify's old value while the change was still on its way, and the
      slider set the Windows mixer level for Spotify instead of Spotify's volume.
- [ ] **QR code cut off on your school PC** (you: “QR code is cut off… and doesn't show its address”). Devices →
      Add a device on the school PC (and at a high display scaling, e.g. 150 %): the whole QR with a white border,
      the address and the code are visible, each with a Copy button; clicking the QR opens it full size; the phone
      scans it. Cause: the inbox folder path on the Devices page never wrapped and forced the page wider than the
      window, which cut off the right side.
- [ ] **A friend's phone joins** with “A friend's SAINT” on their own code (and the reverse): Friend on both sides.
      Untested because of the QR above.
- [ ] **Chrome's logo as album art** (screenshots in `docs/screenshots/reports/`): play a YouTube video — the mini
      player and Now Playing show the icon small as a badge instead of stretched across the card. Spotify covers look
      sharp at large mini-player sizes (they're loaded at Spotify's highest resolution now).
- [ ] **Reach this PC from anywhere** at home (you weren't sure it worked there; school's public network blocking it
      is expected): the card says what worked — router port (UPnP), IPv6 or neither.

## 2. New in 0.4.0

**Upgrade and install**
- [ ] SAINT 0.4.0 started on your real data: settings, scenes, learned skills, memory, History and Spotify login are
      all as before. Overview → Tasks shows your older tasks.
- [ ] `SAINT-Setup.exe` (from `build\windows`, or the release once published) installs, starts, and Windows' Installed
      apps lists SAINT 0.4.0. Uninstalling removes the program and Start menu entry and keeps `%LOCALAPPDATA%\SAINT`.
      (Automated 2026-10-07: silent install to a test folder, `SAINT.exe --selftest` passed, uninstall clean, data
      kept. Not yet done by hand.)
- [ ] The installed app hears “Hey SAINT” and answers out loud (the packaged self-test skipped the voice; the source
      one passed).

**Agent tasks** (Settings → Automation → Safety → Permission mode; Confirm is the default)
- [ ] “Set up my coding workspace for SAINT, open Discord and put Spotify on my second monitor”: Overview shows a task
      card with each step; each step is checked (✓) before the next; it says when it's done.
- [ ] “Open Discord, open Spotify and tell me when everything is ready” — one task, one reply at the end.
- [ ] During a task: “pause”, “continue”, “what's next?”, “why did that fail?”. With music playing and no task,
      “pause” still pauses the music.
- [ ] “Actually put Spotify on my main monitor” mid-task changes that step; the next run of the same request uses it.
- [ ] A step that asks (“Which window?”) waits for your answer and continues from there.
- [ ] Close an app SAINT just opened mid-task: the check fails, SAINT opens it again and carries on.
- [ ] Restart SAINT during a task: the task is on Tasks as paused; “continue” resumes it.
- [ ] Permission modes: in **Safe** a task with a medium step (typing, installing) is refused up front and says why;
      in **Confirm** it asks once for the plan; in **Autonomous** it doesn't ask; a high step (deleting, a shell
      command) asks in every mode.
- [ ] “Remember how I just did that” after a task: it appears under Automations › Learned and runs again from one
      phrase without planning.
- [ ] A simple request (“pause Spotify”, “open Discord”) is still instant — no task card.

**Development helper**
- [ ] “Open SAINT and run the tests”: it finds the project, runs the tests, and says how many passed / which failed.
- [ ] “Start the dev server” on a web project: it says the URL when it answers; “stop the server” stops it; quitting
      SAINT stops it too.
- [ ] A project with a missing package: “why won't it start?” names the package and offers to install it (asks first).

**Screen reading**
- [ ] “What does it say on my screen?” / “click Submit” on a page without accessibility info uses Windows OCR
      (Activity shows “read the screen (OCR)”), without a vision model.

**Interface**
- [ ] Overview: the current task, what needs you, devices and health. Tasks: current and past tasks with pause /
      continue / cancel. Activity: each tool and its result. Saying “go to Home” opens Overview.
- [ ] Settings → Appearance → Layout: hide and reorder sidebar pages and Overview panels, turn the status bar off,
      pick the start monitor — applied without a restart and kept after one.
- [ ] Health panel (Overview and System): stop Ollama — the model row turns red with what to do; Retry recovers it.
- [ ] Devices: each card shows how it's connected (Wi-Fi / Tailscale / internet) and its latency.
- [ ] On a small window (or a laptop at 150 %) nothing is cut off and pages scroll.

## 3. Still open (not fixed yet)

- [ ] **Mood mixes** match the mood (“kinda works but some songs aren't the mood I asked for and it doesn't listen to
      that very well”).
- [ ] **Changing the email account** in the email scene (“can't change my email, it doesn't know how”). The scene used
      to ask “What's Personal's email address?” — the account name was taken as the recipient.
- [ ] Say a new person's address (“J. Norton at EssexTech.net”): SAINT says “I've got j.norton@essextech.net — is that
      right?”; after “yes” the next email to them doesn't ask again.
- [ ] An email body of 600+ characters goes in complete (no 500-character error).
- [ ] One request after a multi-step task still got a long reply — note which one when you see it again.
- [ ] iPhone “Sync now” doesn't crash, and the PC doesn't then say you're on a call.

## 4. Never tested yet

**From 2026-10-06**
- [ ] “That's not the type of music I said” / “that's not the vibe I asked for”: it skips and says the song isn't
      counted as one you dislike. A song you actually dislike (“I'm not feeling this song”) still counts.
- [ ] Connect AirPods, then start SAINT: music and SAINT's voice stay in full quality (not call quality).
- [ ] Settings → Voice → Input device lists mics by name; a Bluetooth headset mic is labelled. If SAINT had to use a
      different mic than the one you picked, it says which and why, once.
- [ ] With the iPhone and any other PC connected, Devices → **Device logs** opens `%LOCALAPPDATA%\SAINT\logs\devices`
      with one log per device, and new lines keep arriving each minute.
- [ ] `saint.log` no longer has a “media.changed” line every 5 seconds — only when the song or play state changes.

**SAINT Link (PC ↔ PC)**
- [ ] PC ↔ PC on the same Wi-Fi: only the 8-character code is needed to pair.
- [ ] PC ↔ PC over Tailscale only (different networks): the code alone pairs.

**Watching and notifications**
- [ ] “Tell me when Claude finishes.”
- [ ] A download finishing gives a notification.
- [ ] Away for 2+ minutes, then “what changed while I was away?”
- [ ] “Watch my right screen”, then “use all screens”.
- [ ] Save and restore a two-monitor workspace; “gaming mode” / “I'm done gaming” restores it.
- [ ] Summarize copied text; fix copied code and paste it; open the file from a copied traceback.
- [ ] Important notifications are read aloud.
- [ ] An update dialog is handled only after you confirm.

**Instagram and MCP**
- [ ] “Open what Gian sent me on Instagram” — with Instagram open, and with the browser fallback.
- [ ] Settings → MCP: add a filesystem or notes server, Save & connect, use one of its tools by voice; a tool
      that changes things asks first.

## 5. Release gate

- [ ] `SAINT-Setup.exe` for 0.4.0 installs and runs on a clean Windows user, and upgrades an existing install with
      your data kept.
- [ ] Sections 1 and 2 pass.
- [ ] iPhone + AirPods and **PC + AirPods** pass.
- [ ] The iOS workflow on GitHub is green for the release commit (the iPhone changes were not compiled locally).
- [ ] Final manual run on the exact commit being released.

## Test record

| Date | Commit | Area | Result | Notes |
|---|---|---|---|---|
| 2026-10-05 | v0.3 | Installer / voice | Pass | Build, clean install, offline install, uninstall, voice volume |
| 2026-10-06 | 033dc49 | Mini player, voice, email, typing, Link | Mostly pass | Album-art width, off-screen drag, volume sync, album play, off-vibe skip, PC + AirPods, iPhone Settings and Sync now failed — fixed in the next commit |
| 2026-10-06 | a296f72 | Remote access, mini player, Spotify, iPhone, Link | Mostly pass | Mini player volume still didn't reach Spotify; QR cut off on the school PC — both fixed in 0.4.0 |
| 2026-10-07 | 0.4.0 (automated) | Tests, migration, packaging | Pass | 1,432 tests; migration on a copy of real data (backup complete, database rows identical, no setting changed, second run does nothing); packaged and installed `SAINT.exe --selftest` (libraries, models, 11 pages, QR, OCR, agent); voice round trip from source; install / uninstall keeps data |
| | 0.4.0 | Agent tasks | | |
| | 0.4.0 | Interface | | |
| | 0.4.0 | Mini player volume, QR | | |
