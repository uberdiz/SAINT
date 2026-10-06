# SAINT manual testing — what's left

Everything you marked **Y** up to 2026-10-06 is confirmed and has been removed (the release installer, the mini
player's album-art mode / presets / voice sizing / live updates / moving it by voice, file and folder replies,
English replies for shortcuts, "stop speaking", "Hey SAINT" alone, the email scene in one breath, typing in
Notepad, music during a lesson, "make it shorter", moving files into a new folder, short replies after multi-step
tasks, nicknames for Instagram chats, the iPhone keyboard's Send key, text fitting on the iPhone, the iPhone away
from home over Tailscale, the iPhone answering when Ollama is down, and History across devices).

What's here was fixed since your last pass (re-check), is still open, or was never tested. Tick an item only after
you've seen it work on the real machine or phone, and add notes the same way as before (`[N — what happened]`).

**Test without your real data:** `run.bat --profile clean` (fresh install) or `run.bat --profile demo` (sample
scenes). `python tools\profiles.py snapshot` then `run.bat --profile snapshot-…` tests on a copy of your data.

## 1. Fixed after your 2026-10-06 notes — re-check

**Your phone away from home, without Tailscale** (new: Devices → *Reach this PC from anywhere*)
- [unsure on my home PC but doesnt work on a public network at school which is fine] Turn it on and accept the Windows prompt (a firewall rule for SAINT's port from anywhere). The card says what
      worked: the router port (UPnP), IPv6, or both. Your Xfinity gateway didn't answer UPnP when tested — if the card
      says so, either turn on UPnP in the Xfinity app, or forward TCP 8765 to this PC there and type your address
      (or a dynamic-DNS name) in the box under the switch.
- [Y] Open SAINT on the iPhone once at home (so it learns the new addresses), then switch to cellular with Tailscale
      off: the iPhone reaches the PC and answers with its model. Devices → your PC says “over the internet”.
- [Y] Turn the switch off: the iPhone can no longer reach the PC from cellular (Tailscale still works).

**Mini player**
- [Y] Drag a corner out into a big square: the album art keeps growing, wide and tall (it stopped at about 250 px
      wide). A wide strip shows the full player, and its cover is bigger too.
- [Y] Right-click → Size presets → **Big album art**. Turning lyrics on from a big square switches to the player
      with lyrics.
- [Y] Drag it past any screen edge: it stops at the edge. It still moves onto your other monitor.
- [N still doesnt sync with spotify, when i change the volume on the miniplayer it goes down but resets back at full and doesnt change the volume of spotify.] Volume: change Spotify's volume on the phone or in Spotify — the slider follows; move the slider — Spotify's own
      volume changes (it used to change the Windows mixer level for Spotify instead). A YouTube video still uses the
      browser's volume.

**Spotify**
- [Y] “Play Fancy That by PinkPantheress” (and “by Pink Panthers”, misheard), “play the album Fancy That”, “play
      Fancy That album by PinkPantheress”: the whole album from track 1, on repeat, shuffle as you had it — no single
      song with “songs like it” queued.
- [ ] “That's not the type of music I said” / “that's not the vibe I asked for”: it skips and says the song isn't
      counted as one you dislike. A song you actually dislike (“I'm not feeling this song”) still counts.

**AirPods on the PC**
- [ ] Connect AirPods, then start SAINT: music and SAINT's voice stay in full quality (not call quality). Cause: the
      microphone was saved as a number, and connecting AirPods renumbered the devices so SAINT opened the AirPods'
      hands-free mic. It's now saved by name (your Voicemeeter Out B1).
- [ ] Settings → Voice → Input device lists mics by name; a Bluetooth headset mic is labelled. If SAINT had to use a
      different mic than the one you picked, it says which and why, once.

**Logs from every device in one place**
- [ ] With the iPhone (new build) and any other PC connected, Devices → **Device logs** opens
      `%LOCALAPPDATA%\SAINT\logs\devices` with one log per device, and new lines keep arriving each minute.
- [ ] `saint.log` no longer has a “media.changed” line every 5 seconds — only when the song or play state changes.

**iPhone** (needs a new build)
- [Y] Tabs: Talk, Music, **History**, **Memory**, Settings. History has a summary (today / 7 days / % worked) and a
      search; Memory has a search. Devices opens from the PC chip on Talk and from Settings (top of *This phone*).
- [Y] Settings on the smallest supported iPhone: no text runs under switches, pickers or buttons. Output's picker
      sits under its label.
- [Y] **Sync now** (Devices toolbar, a device's page, Memory, History's menu, pull to refresh): a spinner, then a
      banner saying what synced — on whichever tab you're on (it used to show only on Talk, so it looked dead).

## 2. Still open (not fixed yet)

- [ ] **Mood mixes** match the mood (“kinda works but some songs aren't the mood I asked for and it doesn't listen to
      that very well”).
- [ ] **Changing the email account** in the email scene (“can't change my email, it doesn't know how”). The scene used
      to ask “What's Personal's email address?” — the account name was taken as the recipient.
- [ ] Say a new person's address (“J. Norton at EssexTech.net”): SAINT says “I've got j.norton@essextech.net — is that
      right?”; after “yes” the next email to them doesn't ask again.
- [ ] An email body of 600+ characters goes in complete (no 500-character error).
- [ ] One request after a multi-step task still got a long reply — note which one when you see it again.
- [ ] iPhone “Sync now” doesn't crash, and the PC doesn't then say you're on a call (the button now responds — see 1).

## 3. Never tested yet

**SAINT Link (PC ↔ PC, phone)**
- [ ] PC ↔ PC on the same Wi-Fi: only the 8-character code is needed to pair.
- [ ] PC ↔ PC over Tailscale only (different networks): the code alone pairs.
- [Y] First Link start: one admin prompt for the firewall.
- [Y] Devices → a disconnected device → “Check connection” names each address (Wi-Fi, Tailscale, internet) and
      what's blocking it.
- [cant connect QR code is cut off on my PC at school and doesnt show its address but my PC at home it works fine but with the long connection code so it may not be the same.] A friend's phone joins with “A friend's SAINT” on their own code (and the reverse): Friend on both sides.

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

- [ ] The release installer (`SAINT-Setup.exe` from the GitHub release) still installs and runs on a clean Windows
      user (passed 2026-10-06; re-check on the release commit).
- [ ] Section 1 passes.
- [ ] iPhone + AirPods and **PC + AirPods** pass.
- [ ] The iOS workflow on GitHub is green for the release commit (the iPhone changes were not compiled locally).
- [ ] Final manual run on the exact commit being released.

## Test record

| Date | Commit | Area | Result | Notes |
|---|---|---|---|---|
| 2026-10-05 | v0.3 | Installer / voice | Pass | Build, clean install, offline install, uninstall, voice volume |
| 2026-10-06 | 033dc49 | Mini player, voice, email, typing, Link | Mostly pass | Album-art width, off-screen drag, volume sync, album play, off-vibe skip, PC + AirPods, iPhone Settings and Sync now failed — fixed in the next commit |
| | | Remote access (no Tailscale) | | |
| | | Mini player | | |
| | | Spotify | | |
| | | PC + AirPods | | |
| | | Device logs | | |
| | | iPhone tabs / Settings / Sync | | |
