# SAINT manual testing — what's left

Everything you marked **Y** before 2026-10-05 is confirmed and has been removed. What's here is untested,
fixed since your last pass (re-check), or still broken. Tick an item only after you've seen it work on the
real machine or phone, and add notes the same way as before (`[N — what happened]`).

**Test without your real data:** `run.bat --profile clean` (fresh install) or `run.bat --profile demo` (sample
scenes). `python tools\profiles.py snapshot` then `run.bat --profile snapshot-…` tests on a copy of your data.

## 1. Installer and voice (new this round)

- [ ] Build: `.venv\Scripts\python packaging\windows\build.py` ends with “Kokoro voice packages bundled.” and
      “No user data in the bundle.”, and makes `build\windows\SAINT-Setup.exe`.
- [ ] Clean install (rename `%LOCALAPPDATA%\SAINT\tts` first): the installer shows “Downloading SAINT's voice”,
      then SAINT's first reply is the Kokoro voice, not the Windows (robotic) one.
- [ ] Reinstall over it: no voice download this time (already there).
- [ ] Install with the network off: setup says the voice couldn't be downloaded and still finishes; SAINT
      speaks with the Windows voice, says Kokoro is downloading, and switches once you're back online.
- [ ] Uninstall: the app is gone; `%LOCALAPPDATA%\SAINT` (data + voice) stays.
- [ ] A clean install has none of your data (no memories, scenes, history, Spotify login).
- [ ] From source (`run.bat`): replies are in the Kokoro voice (GPU). Log: `Kokoro TTS` loaded, no “Windows voice”.
- [ ] Settings → Voice → Volume at 40 %: the next reply is quieter; Spotify and Windows volume unchanged.
      “Talk louder” / “voice volume 120” work.

## 2. Fixed after your last notes — re-check

- [ ] **Email scene asks first.** Run “write me an email”: it asks which account, who, their address (only the
      first time per person) and what about *before* opening anything; opens Gmail as that account; reads the
      subject and body back; stops at “Should I send it?”.
- [ ] “Write me an email to Sam about Friday” runs the same scene without asking who/what.
- [ ] The scene from Automations → Run asks its questions in the chat.
- [ ] “Make it more formal” while it reads the draft back rewrites it.
- [ ] “Stop” during the scene stops it; the next sentence is a normal request.
- [ ] **Misheard app names.** “Open clad” / “open clawed” → Claude. “Open cloud” asks “Claude or iCloud?”, and
      after you pick Claude, “open cloud” opens Claude directly. “Open a rocket leak” → Rocket League.
- [ ] Try a few names that used to be misheard (apps, games, slang) and note any that still fail.
- [ ] **“Open Antigravity”** opens Antigravity IDE — never “you cancelled the action”.
- [ ] A quick “Nope, open Claude” right after a reply isn't dropped.
- [ ] **Skip N.** “Skip 3 songs” / “skip the next two” (music playing, no wake word).
- [ ] **Queue replaced.** “Play something chill”, then “play something energetic”: none of the chill songs play
      first; a song you queued yourself stays.
- [ ] **Mini player in a game.** Gaming Mode turned off by voice while the game runs, then turn the mini player
      on: it stays visible and changes with the track.

## 3. Still broken from your notes (not fixed yet — test again after the fix)

- [ ] Mood/genre queue: songs don't match the mood; “not this kind of song” isn't understood; removing a song
      from the queue fails. (Spotify's API can't remove queue items — SAINT should say so and skip instead.)
- [ ] “Play the album X” plays one song, says it'll play more like it, and keeps the old queue.
- [ ] “Hover over …” created a reminder instead of hovering.
- [ ] Mini player: smallest size barely smaller; wanted free resizing in both directions — square shows just
      the album art, hover shows title, progress bar and controls.
- [ ] Mini player lyrics don't update until the window is moved; add volume control to it.
- [ ] Move the mini player by voice: “top left of my second screen” (inset from the edge), “right a bit”,
      “down a couple pixels”.
- [ ] Start Ollama automatically when SAINT starts (and check it's answering).
- [ ] “Make a new folder in downloads” then “add a text file in that folder”: spoke code instead of doing it.
- [ ] “Type out a summary of what SAINT is” in Notepad typed “a sssss…”.
- [ ] Middle step of a multi-step task fails, then “where were we?” / “continue what we were doing”.
- [ ] Start a new request before the previous one finishes: the old task stops at a safe point.
- [ ] iPhone + AirPods: audio switches to call quality; the audio-route UI is static and too big; output should
      follow the phone (speaker for Spotify, Bluetooth mic for input).
- [ ] iPhone “Sync now” crashed the app, and the PC then said you were on a call.
- [ ] iPhone: the keyboard's Send key doesn't send.
- [ ] iPhone: text too large on some screens; check every supported iPhone size.

## 4. Never tested yet

**Watching and notifications**
- [ ] “Tell me when Claude finishes.”
- [ ] A download finishing gives a notification.
- [ ] Away for 2+ minutes, then “what changed while I was away?”
- [ ] “Watch my right screen”, then “use all screens”.
- [ ] Save and restore a two-monitor workspace; “gaming mode” / “I'm done gaming” restores it.
- [ ] Summarize copied text; fix copied code and paste it; open the file from a copied traceback.
- [ ] Important notifications are read aloud.
- [ ] An update dialog is handled only after you confirm.

**Lessons and email**
- [ ] Teach a step SAINT can't do: it asks you to do that step.
- [ ] Teach the Gmail email by demonstration: the read-back lists the steps and asks who / about / what.

**Instagram**
- [ ] “Open what Gian sent me on Instagram” — with Instagram open, and with the browser fallback.
- [ ] SAINT never claims it found a message it didn't.

**SAINT Link and sync**
- [ ] PC ↔ PC on the same Wi-Fi: only the 8-character code is needed to pair.
- [ ] PC ↔ PC over Tailscale only (different networks): the code alone pairs.
- [ ] First Link start: one admin prompt for the firewall; afterwards the phone connects over Tailscale away from home.
- [ ] Devices → a disconnected device → “Check connection” names each address and what's blocking it.
- [ ] A friend's phone joins with “A friend's SAINT” on their own code (and the reverse): Friend on both sides.
- [ ] iPhone away from home (cellular + Tailscale) answers open questions with the PC's model within a few seconds.
- [ ] Ollama stopped on the PC: the iPhone answers with Claude / its own model instead of reading the error.
- [ ] A command on PC A shows in PC B's History (“Other PC”) and the iPhone's Activity tab; an “ask once”
      answer taught on A is known on B.

**MCP**
- [ ] Settings → MCP: add a filesystem or notes server, Save & connect, use one of its tools by voice; a tool
      that changes things asks first.

## 5. Release gate

- [ ] Core voice / wake / STT pass on real hardware.
- [ ] iPhone + AirPods tests pass.
- [ ] Section 1 passes on a clean Windows user.
- [ ] Final manual run on the exact commit being released.

## Test record

| Date | Commit | Area | Result | Notes |
|---|---|---|---|---|
| | | Installer / voice | | |
| | | Scenes / email | | |
| | | Names / apps | | |
| | | Spotify | | |
| | | Mini player | | |
| | | Link / iPhone | | |
