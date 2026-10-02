# SAINT Manual Testing — Remaining Work

Branch: `unified-release`

This checklist contains only tests that still need verification, fixes, or a real-device/manual pass. Working items from the previous checklist have been removed.

**Rule:** Do not mark an item complete because the code looks correct. Mark it complete only after the behavior is observed manually on the target machine/device.

## 1. Windows desktop launch / packaging

- [ ] Build the Windows installer successfully from a clean checkout.
- [ ] Build the standalone SAINT executable successfully.
- [ ] Resolve the current packaging/build failure if the build script still fails under the installed Python environment.
- [ ] Launch SAINT from the generated EXE with no terminal window.
- [ ] Confirm the EXE starts the correct application entry point.
- [ ] Confirm the SAINT icon appears correctly in the taskbar, Start Menu, shortcut, and installer.
- [ ] Install to a clean Windows user/profile and launch successfully.
- [ ] Uninstall and confirm SAINT is removed cleanly.
- [ ] Reinstall without leaving a broken configuration behind.
- [ ] Confirm required model/config/data directories are created correctly.
- [ ] Confirm an installed build can start without the source repository being present.

## 2. First launch / configuration

- [ ] Start SAINT on a clean configuration.
- [ ] Confirm all required dependencies/models report their real status.
- [ ] Confirm missing optional components produce a useful message instead of a crash.
- [ ] Open every Settings page and verify there are no UI exceptions.
- [ ] Change important settings, restart SAINT, and confirm they persist.
- [ ] Confirm sensitive configuration values are not exposed in normal UI/log output.

## 3. Voice activation

- [ ] Test “SAINT” from normal speaking distance at least 10 times.
- [ ] Test “Hey SAINT” at least 10 times.
- [ ] Test wake detection with normal background music.
- [ ] Test wake detection while a game is running.
- [ ] Test normal speech without the wake word for several minutes and confirm SAINT does not react unexpectedly.
- [ ] Test one-word commands while a conversation window is active: “pause”, “skip”, “yes”, “no”, “cancel”.
- [ ] Test a one-word confirmation immediately after SAINT asks a question.
- [ ] Test a confirmation after approximately 15 seconds.
- [ ] Test a short/quiet response and confirm SAINT reports that it did not catch it instead of silently failing.
- [ ] Test interruption while SAINT is speaking.
- [ ] Confirm SAINT never responds to its own TTS output.
- [ ] Confirm silence/background noise does not repeatedly end or restart conversation mode.
- [ ] Confirm conversation mode does not require the wake word for every follow-up.
- [ ] Test natural pauses between words and multi-step commands.
- [ ] Test filler words such as “uh”, “yeah”, and “okay so”.
- [ ] Test follow-ups such as “my gym playlist”, “smaller”, and “close, close, CS2”.
- [ ] Confirm unrelated room conversation is ignored.

## 4. Speech-to-text quality

- [ ] Test normal speech at the normal microphone distance.
- [ ] Test quiet speech.
- [ ] Test speech while music is playing.
- [ ] Test speech while a game is playing.
- [ ] Test names of apps, games, songs, playlists, and websites.
- [ ] Test numbers and percentages.
- [ ] Test short confirmations: “yes”, “no”, “yep”, “cancel”.
- [ ] Record repeated misrecognitions and add them to regression cases.
- [ ] Confirm STT latency is acceptable during a real conversation.

## 5. Spotify

- [ ] Confirm Spotify connects automatically after SAINT restarts.
- [ ] Test pause/resume/skip/back.
- [ ] Test volume commands.
- [ ] Test playing an artist, album, track, and personal playlist.
- [ ] Test “play something like this” against the currently playing song.
- [ ] Test personalized recommendations based on listening history.
- [ ] Test “search Spotify for X” followed by “play the second one”.
- [ ] Test playlist disambiguation when multiple playlists could match.
- [ ] Test a nonexistent personal playlist.
- [ ] Test mood/language/genre requests that should produce a playlist or queue rather than a single random track.
- [ ] Test queueing similar songs without skipping the current song.
- [ ] Test Smart Shuffle.
- [ ] Test lyrics display/hide in the mini player if enabled.
- [ ] Confirm song lyrics are not interpreted as voice commands.
- [ ] Test Spotify controls while a game is focused.
- [ ] Verify the Spotify tab, Now Playing panel, queue, album art, and mini player stay synchronized.
- [ ] Confirm a new generated queue replaces/clears the old queue appropriately instead of simply skipping through stale queued songs.
- [ ] Verify album requests actually play the requested album, not merely one matching track.
- [ ] Verify Spotify skip tracking does not accidentally skip songs that SAINT did not command to skip.
- [ ] Verify repeated skipping/removal behavior matches the intended learning rules.

## 6. Desktop screen / vision

- [ ] Test “what is this error?” with a real error dialog visible.
- [ ] Test FLUX vision with weights missing and verify the setup message is actionable.
- [ ] Test FLUX vision with weights installed.
- [ ] Test Ollama vision with a vision model pulled.
- [ ] Confirm vision uses the explicitly requested physical monitor rather than only the active window.
- [ ] Confirm vision never invents UI elements that are not visible.

## 7. Mouse / keyboard / desktop control

- [ ] Test corner-relative commands such as “click the X in the top right”.
- [ ] Confirm right-click/hover always targets the currently intended window.
- [ ] Confirm actions on a named monitor never fall through to another monitor.
- [ ] Confirm SAINT verifies important actions after performing them.

## 8. Windows / application management

- [ ] Test “close that window” immediately after SAINT opens an application.
- [ ] Test several similarly named windows and verify the correct one is selected.
- [ ] Test moving/resizing windows across both monitors during a long task.
- [ ] Test app reuse after SAINT restarts.

## 9. Browser automation

- [ ] Test an existing browser window with YouTube already open.
- [ ] Test multiple browser windows and explicit window selection.
- [ ] Test browser automation while music is playing.
- [ ] Test a multi-step browser task in one spoken request.
- [ ] Confirm failed browser actions are reported honestly.

## 10. Multi-step agent behavior

- [ ] Test a 3-step request.
- [ ] Test a 5+ step request.
- [ ] Test a request spoken naturally with pauses.
- [ ] Test the same request spoken quickly in one breath.
- [ ] Confirm steps execute sequentially and are verified.
- [ ] Interrupt a multi-step task and confirm it stops safely.
- [ ] Ask “what are you doing?” during a long task.
- [ ] Confirm one failed step does not produce a false success message.
- [ ] Test a failed middle step followed by “where were we?” and “continue what we were doing”.
- [ ] Start a new request before the previous one finishes and confirm the old task stops at a safe boundary.

## 11. Game Mode / Gaming Mode

- [ ] Test Auto Gaming Mode off with a supported single-player game.
- [ ] Test “Gaming mode on” with two monitors.
- [ ] Confirm SAINT and the mini player move to the monitor without the game.
- [ ] Confirm screen capture/vision is disabled in Gaming Mode where intended.
- [ ] Test Gaming Mode spoken replies off.
- [ ] Test Minimal notification mode.
- [ ] Test “Set up my gaming workspace”.
- [ ] Test mini player right-click controls, opacity, size, monitor movement, edge dragging, and ctrl+scroll.
- [ ] Confirm Gaming Mode does not interfere with anti-cheat-protected games.
- [ ] Confirm Gaming Mode exits/restores state correctly after the game closes.

## 12. Interface / mini player

- [ ] Test mini player controls independently of YouTube's mini player.
- [ ] Test mini player opacity, size, monitor movement, and resizing.
- [ ] Verify the mini player is present in the final desktop build.
- [ ] Verify lyrics UI works in the final desktop build.
- [ ] Test Start with Windows from a clean login.
- [ ] Confirm Task Manager > Startup Apps shows SAINT with the correct icon.
- [ ] Test the app on a second monitor.

## 13. Memory / learning

- [ ] Teach SAINT a new simple action and confirm it can repeat it later.
- [ ] Confirm SAINT does not record unrelated actions during a demonstration.
- [ ] Confirm long demonstrations are summarized/reviewed before being saved.
- [ ] Test “no, I didn't ask for that” after an unwanted learned action.
- [ ] Confirm the unwanted skill is removed from Learned.
- [ ] Confirm the complaint is written to the learning journal.
- [ ] Test saving a successful action as a named automation.
- [ ] Restart SAINT and verify intended learned data persists.

## 14. Storage / files

- [ ] Test “clean up my D drive” followed by “what did you find?”, “is that safe?”, and “delete it”.
- [ ] Test Storage → Check for junk if enabled.
- [ ] Confirm destructive actions always request confirmation where required.

## 15. Windows audio controls

- [ ] Verify per-app volume changes affect only the intended application.
- [ ] Verify Spotify volume commands do not accidentally change Windows/global volume.
- [ ] Verify global volume commands do not accidentally change Spotify's own volume.
- [ ] Test audio-device switching in the final build.

## 16. Current information / web

- [ ] Provider disabled: ask for current information and confirm SAINT explains that web search is unavailable without unexpectedly opening a browser.
- [ ] Provider enabled: verify a current-information answer actually uses the configured provider.
- [ ] Confirm SAINT does not claim current information when no current source was used.

## 17. Watching / workspace / notifications

- [ ] Ask SAINT to notify you when Claude finishes.
- [ ] Test download-completion notification.
- [ ] Step away for at least two minutes and test “what changed while I was away?”.
- [ ] Test “watch my right screen” then “use all screens”.
- [ ] Save and restore a two-monitor workspace.
- [ ] Test “Gaming mode” / “I'm done gaming” workspace save/restore.
- [ ] Summarize copied text.
- [ ] Fix copied code and paste it.
- [ ] Read important notifications aloud.
- [ ] Open the file causing a copied traceback.
- [ ] Test handling an update dialog with confirmation.

## 18. Email / demonstration learning

- [ ] Walk SAINT through the complete “write an email” lesson and confirm every step happens only when requested.
- [ ] Test “send an email to <someone> about <topic>” and confirm SAINT asks for the account, generates subject/body, and stops before Send.
- [ ] Test rewriting the generated email with “make it more formal”.
- [ ] Test an impossible demonstration step and verify SAINT asks the user to perform it.
- [ ] Test “stop” during a lesson.
- [ ] Confirm the next sentence after stopping is treated as a normal request.

## 19. Instagram / social app control

- [ ] Test “Open what Gian sent me on Instagram”.
- [ ] Test when Instagram is already open on the desktop.
- [ ] Test the browser fallback if supported.
- [ ] Confirm SAINT does not claim it found the message if it did not.

## 20. Mobile / iOS

- [ ] Build the iOS app from the unified repo.
- [ ] Produce a fresh IPA successfully.
- [ ] Install the IPA on the test iPhone.
- [ ] Confirm the app launches without crashes.
- [ ] Test wake-word detection on iPhone.
- [ ] Test wake-word detection with AirPods.
- [ ] Fix/verify the AirPods microphone route does not sound like a phone call.
- [ ] Test iPhone speech recognition in a quiet room.
- [ ] Test iPhone speech recognition with music/background noise.
- [ ] Test one-word commands and short confirmations.
- [ ] Test voice control while the app is backgrounded/locked where iOS permits it.
- [ ] Verify Spotify authentication/connection.
- [ ] Resolve/verify the “no matching configuration” Spotify connection issue.
- [ ] Test mobile Spotify play/pause/skip/search/recommendation commands.
- [ ] Confirm mobile Spotify behavior matches desktop intent behavior where applicable.
- [ ] Test the iPhone → PC link when the PC is online.
- [ ] Test the iPhone fallback when the PC is offline.
- [ ] Test “Sync now” and its status messages.
- [ ] Confirm a song request can route playback through the PC when phone Spotify is not signed in.
- [ ] Confirm mobile UI works across supported iPhone sizes.

## 21. Cross-platform consistency

- [ ] Run the same basic commands on desktop and mobile.
- [ ] Compare interpretation of identical Spotify requests.
- [ ] Compare short conversational follow-ups.
- [ ] Confirm mobile-specific limitations are communicated rather than silently producing different behavior.
- [ ] Confirm desktop changes do not break the mobile project.

## 22. Reliability / failure states

- [ ] Restart SAINT after Spotify is connected.
- [ ] Restart SAINT during/after a voice conversation.
- [ ] Restart SAINT after changing settings.
- [ ] Restart SAINT with the mini player enabled.
- [ ] Restart SAINT with multiple monitors connected.
- [ ] Restart SAINT while a game is running.
- [ ] Start SAINT with Spotify closed.
- [ ] Stop Ollama, start SAINT, and verify the Startup/System page shows the AI model as failed with a Retry action.
- [ ] Start Ollama and use Retry; verify the model returns to Running.
- [ ] Start SAINT with optional model files missing.
- [ ] Confirm failures are isolated and SAINT still launches when optional components are unavailable.

## 23. 2026-10-02 round (Link, MCP, lessons, iPhone)

- [ ] PC ↔ PC on the same Wi-Fi: “Pair another PC” on one, type only the 8-character code into Join on the other — pairs without an address.
- [ ] PC ↔ PC over Tailscale only (different networks): type only the code — pairs (the other PC is found among Tailscale devices).
- [ ] First Link start shows one admin prompt for the firewall rule; after accepting, the phone connects over Tailscale away from home.
- [ ] Devices → a disconnected device → “Check connection” names each address and what's in the way.
- [ ] Friend's phone joins your PC with “A friend's SAINT” on an *own* code (and the reverse) — pairs as Friend on both sides.
- [ ] iPhone away from home (cellular + Tailscale) answers open questions with the PC's model within a few seconds.
- [ ] Stop Ollama on the PC: the iPhone answers with Claude / the on-device model instead of reading the PC's error.
- [ ] iPhone: the keyboard's Send key sends the typed message (and the arrow button still does).
- [ ] iPhone: “SAINT …” wakes reliably at normal distance (10 tries); “Hey SAINT” too; normal conversation doesn't wake it.
- [ ] Spotify (desktop): press Connect, close the browser tab, press “Cancel sign-in”, Connect again — works without waiting 3 minutes.
- [ ] Spotify (iPhone): a failed sign-in can be retried at once.
- [ ] Alt+` overlay: “Shut down” → “Click again to quit” → SAINT quits.
- [ ] “make a new folder in downloads” then “add a text file in that folder” → New Text Document.txt inside it; no code is spoken.
- [ ] “type out a summary of what SAINT is” in Notepad → a written summary, not the words.
- [ ] Teach the Gmail email (open Gmail, click Compose, “write mr norton's email (jnorton@essextech.net)”, click Subject, “type out a summary of what SAINT is”, done) → the read-back lists the steps and asks who / about / what to say; “write an email to Mr Norton about the meeting” fills his address by itself.
- [ ] MCP: paste a filesystem or notes server in Settings → MCP, Save & connect, then use one of its tools by voice; a non-read-only tool asks first.
- [ ] Sync: a command run on PC A appears in PC B's History (“Other PC”) and on the iPhone's Activity tab; an “ask once” answer (an email address) taught on A is known on B.

## 24. Final release gate

Do not consider `unified-release` ready until:

- [ ] Automated tests pass.
- [ ] Windows EXE/installer builds successfully.
- [ ] Windows clean-install test passes.
- [ ] Core voice/wake/STT tests pass on real hardware.
- [ ] Spotify tests pass, including queue replacement and album playback.
- [ ] Two-monitor screen/action tests pass.
- [ ] Gaming Mode tests pass.
- [ ] Browser and multi-step tests pass.
- [ ] Learning/memory tests pass.
- [ ] iOS app builds and installs.
- [ ] IPA build succeeds.
- [ ] iPhone wake/STT tests pass.
- [ ] iPhone + AirPods tests pass.
- [ ] Mobile Spotify connection works.
- [ ] No release-blocking crash remains.
- [ ] README/setup instructions match the actual unified-release build.
- [ ] Final manual test run is performed against the exact commit being released.

## Test record

| Date | Commit | Area | Result | Notes |
|---|---|---|---|---|
| | | Windows packaging | | |
| | | Voice / wake / STT | | |
| | | Spotify | | |
| | | Screen / vision | | |
| | | Desktop control | | |
| | | Browser / multi-step | | |
| | | Gaming Mode | | |
| | | UI / mini player | | |
| | | Learning / memory | | |
| | | Mobile / iOS | | |
| | | Release build | | |
