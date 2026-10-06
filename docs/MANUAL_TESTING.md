# SAINT manual testing — what's left

Everything you marked **Y** up to 2026-10-05 is confirmed and has been removed (installer + Kokoro voice,
misheard app names, skip N, scene stop, Ollama auto-start, task resume, hover, Gmail lesson by demonstration).
What's here is fixed since your last pass (re-check), still open, or never tested. Tick an item only after
you've seen it work on the real machine or phone, and add notes the same way as before (`[N — what happened]`).

**Test without your real data:** `run.bat --profile clean` (fresh install) or `run.bat --profile demo` (sample
scenes). `python tools\profiles.py snapshot` then `run.bat --profile snapshot-…` tests on a copy of your data.

## 1. Fixed after your 2026-10-05 notes — re-check

**Mini player**
- [ ] Drag any edge or corner: it resizes up/down *and* side to side, and keeps that size after a restart.
- [ ] Make it small or square: only the album art shows. Hovering shows title, progress, play/skip and volume;
      a new track shows its name for a few seconds.
- [ ] Right-click → Size presets (Album art, Compact, Normal, Wide, Large) and the Lyrics toggle work.
- [ ] “Make the mini player smaller” / “bigger” / “just show the album art” / “normal size”.
- [ ] Track, progress bar and lyrics change on their own — no clicking or moving it — on the desktop and in a game
      (turn it on by voice while the game runs: it appears and is the right size straight away).
- [ ] Volume: the slider and mute button change Spotify's volume; with a YouTube video playing they change the
      browser's volume. Scrolling over the player changes volume; ctrl+scroll still fades it.
- [ ] “Move the mini player to the top left of my second screen”, then “right a bit” and “down a couple pixels”:
      it moves each time and never turns off.

**Voice and language**
- [ ] “Make a new folder in my Downloads and add a text file to it”: the reply says “Downloads”, not
      `C:\Users\…`, in SAINT's normal (not French) accent.
- [ ] “Open this "C:\…\Antigravity IDE.lnk" for Antigravity” gets an English reply; “Open Antigravity” opens
      Antigravity IDE.
- [ ] “Stop speaking, friend!” while SAINT talks: it stops talking and the mic stays on.
- [ ] “Hey SAINT” alone (no wake chime) → “I'm here — what do you need?”.

**Email and typing**
- [ ] “Hey SAINT, write an email to Mr Norton about Friday” in one breath runs the email scene without asking
      who or what.
- [ ] Say a new person's address (“J. Norton at EssexTech.net”): SAINT says “I've got j.norton@essextech.net —
      is that right?”; after “yes” the next email to them doesn't ask again.
- [ ] An email body of 600+ characters goes in complete (no 500-character error).
- [ ] “Type out a summary of what SAINT is” in Notepad: clean text, no “pppp gggg”; what you'd copied before is
      still on the clipboard afterwards.

**Spotify**
- [ ] “Play the album, Fancy That”: the whole album plays (not one song), on repeat, shuffle as you had it.
- [ ] “This is not the kind of song I was talking about” skips the song and steers away from it.
- [ ] With an unfinished lesson open (e.g. after “I haven't learned how to …”), “play …”, “skip” and “move the
      mini player …” just happen — no “What's next?” after each.

## 2. Still open (not fixed yet)

- [ ] “Make it shorter” / “reduce the character count” while SAINT reads a draft back rewrites it (only worked on
      pasted text).
- [ ] Mood mixes match the mood (“play something hype” played “RUN” by Brahman); the new mix replaces the whole
      old queue.
- [ ] “Make a new folder in Downloads”, then “put that text file in the new folder” moves the file.
- [ ] Replies after a multi-step task are short (it read out every step and path).
- [ ] The email scene asked “What's Personal's email address?” — the account name was taken as the recipient.
- [ ] “When I say John I mean Gian”, then “open my messages with John” opens the Instagram chat with Gian (it
      looked for an app called “messages with john”).
- [ ] iPhone + AirPods: audio stays out of call quality; the audio-route UI is compact; output follows the phone
      (speaker for Spotify, Bluetooth mic for input).
- [ ] iPhone “Sync now” doesn't crash, and the PC doesn't then say you're on a call.
- [ ] iPhone: the keyboard's Send key sends.
- [ ] iPhone: text fits on every supported iPhone size.

## 3. Never tested yet

**SAINT Link (PC ↔ PC, phone)**
- [ ] PC ↔ PC on the same Wi-Fi: only the 8-character code is needed to pair.
- [ ] PC ↔ PC over Tailscale only (different networks): the code alone pairs.
- [ ] First Link start: one admin prompt for the firewall; afterwards the phone connects over Tailscale away
      from home.
- [ ] Devices → a disconnected device → “Check connection” names each address and what's blocking it.
- [ ] A friend's phone joins with “A friend's SAINT” on their own code (and the reverse): Friend on both sides.
- [ ] iPhone away from home (cellular + Tailscale) answers open questions with the PC's model within a few seconds.
- [ ] Ollama stopped on the PC: the iPhone answers with Claude / its own model instead of reading the error.
- [ ] A command on PC A shows in PC B's History (“Other PC”) and the iPhone's Activity tab; an “ask once” answer
      taught on A is known on B.

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

## 4. Release gate

- [ ] The release installer (`SAINT-Setup.exe` from the GitHub release) installs and runs on a clean Windows
      user: Kokoro voice downloads, no personal data included.
- [ ] Section 1 passes.
- [ ] iPhone + AirPods tests pass.
- [ ] Final manual run on the exact commit being released.

## Test record

| Date | Commit | Area | Result | Notes |
|---|---|---|---|---|
| 2026-10-05 | v0.3 | Installer / voice | Pass | Build, clean install, offline install, uninstall, voice volume |
| | | Mini player | | |
| | | Voice / language | | |
| | | Email / typing | | |
| | | Spotify | | |
| | | Link / iPhone | | |
