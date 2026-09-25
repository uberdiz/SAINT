# SAINT manual testing checklist

Run `run.bat`, press **Start listening** (or enable *Start listening on launch*), and work through the
list. Speak naturally from where you normally sit. Tick each line; note the exact words when
something goes wrong. `data/logs/saint.log` has the matching entries: set Settings → General → Log
level to *Verbose* for wake scores, tool arguments/results, observations and retries.

Legend: ✅ = verified automatically/live during development · 🔲 = needs your real voice/room/account.

## Voice

| | Test | Expected |
|---|---|---|
| ✅ | "SAINT" (bare), then wait, then "what time is it" | Chime after "SAINT"; answers the time |
| ✅ | "Hey SAINT, what time is it" (one breath) | Answers the time |
| ✅ | "SAINT, skip this song" / "Hey SAINT … play some jazz" (pause after the wake word) | Command runs — also when you say "Hey SAINT" during an open conversation window |
| ✅ | Say the wake word 10× at normal distance, 10× from across the room | Note how many were missed |
| ✅ | Wake word while music plays at a normal volume | Wakes; command ends when you stop talking |
| ✅ | Wake word / "stop" while SAINT is speaking | SAINT stops and listens; it never wakes on its own voice |
| ✅ | Normal conversation near the mic without "SAINT" (≥ 2 min) | Never reacts |
| ✅ | "Hey SAINT, open Spotify" → "play Kendrick Lamar" → "skip that" (no wake word after the first) | All three run |
| Y | Music playing: "Hey SAINT, find the settings button" → "click it" → "skip that" (no wake word after the first) | All three run; chatter in between ("that's amazing", "I think it's good") is ignored |
| 🔲 | Music playing, in a conversation window: "pause" (one word) | Pauses — one-word commands no longer need "SAINT" in front |
| 🔲 | Music playing, SAINT idle: say just "skip" (one word, no wake word) | Skips (hot-word). With no music playing it is ignored |
| ✅ | Synthetic "SAINT"/"Hey SAINT"/near-misses, clean and with music (4 voices) | 64/64 correct wake decisions |

## Spotify

| | Test | Expected |
|---|---|---|
| ✅ | "Play Kendrick Lamar" / "put on DAMN" / "play my <playlist> playlist" | Correct artist / album / your playlist |
| ✅ | "Pause" · "resume" · "skip" · "skip the song" · "go back" | Playback changes |
| ✅ | "Play a different song" · "play something else" · "change the song" · "I'm not feeling this one" | Skips (never plays a song called "A Different Song") |
| ✅ | "Play something like DAMN by Kendrick Lamar" | Similar music starts (Kendrick + collaborators + your taste) |
| ✅ | "Turn on shuffle" · "turn on Smart Shuffle" · "turn Smart Shuffle off" | Spotify app's shuffle button changes |
| ✅ | "Turn it down" right after a music command | Spotify volume drops |
| ✅ | "Play something I'd like" · "recommend something" | Picks from your history |
| ✅ | "Search Spotify for Kendrick Lamar" → "no" | Lists results, offers to play, respects "no" |

## Screen

| | Test | Expected |
|---|---|---|
| ✅ | "What's on my screen?" / "What am I looking at?" | The window you're looking at + what else is open (no JSON) |
| ✅ | "What's on my second screen?" | Front window of monitor 2, what's behind it, tabs/text |
| ✅ | "What's currently open?" | Windows grouped per monitor |
| ✅ | "What is this error?" with no error visible | "I don't see an error…" (no invented error) |
| 🔲 | "What is this error?" with a real error dialog open | Explains the dialog's text |
| Y | "Find the settings button" → "click it" · "find the settings button in my taskbar and click it" | Finds the taskbar Settings button (an exact name beats a partial one like "Dictation settings"), then clicks it |

## Mouse / keyboard

| | Test | Expected |
|---|---|---|
| ✅ | "Type hello" · "select all" · "copy" (in Notepad) | Text typed/selected/copied |
| 🔲 | "Double click the recycle bin" (other windows open) | Shows the desktop if the icon is covered, then opens the recycle bin |
| 🔲 | "Right click <something>" · "hover over <something>" | Acts in the window you're looking at (not one SAINT used minutes ago) |
| ✅ | "Scroll down" · "go back" · "refresh the page" | In the window SAINT is working with |
| 🔲 | "Click the button in the bottom right" · "click the X button in the top right" | Nearest button to that corner · the Close button in that corner |
| ✅ | "Press ctrl+t" · "press escape" | Keys go to the intended window |

## Windows

| | Test | Expected |
|---|---|---|
| ✅ | "Open Notepad" twice | Second time switches to the same window (no duplicate) |
| ✅ | "Move it to my second monitor" · "make it bigger" · "put it on the left" · "move Notepad to my main screen" · "make this window smaller" | Each verified by measured position/size |
| ✅ | "Close it" → "yes" | Closes |
| ✅ | "Put this window next to Spotify" | Side by side on Spotify's monitor |
| ✅ | "Move that window over there" (pointer on the other monitor) | Moves to the pointer's monitor |
| ✅ | "Close all the browser windows" | Asks first, then closes them |
| ✅ | "Minimize it" / "maximize it" / "restore it" | Window state changes |

## Browser

| | Test | Expected |
|---|---|---|
| 🔲 | One browser window open: "Open my browser and search YouTube" | Reuses it, opens YouTube, no new window |
| 🔲 | "Open up a new tab in my browser and search YouTube" | New tab in your browser (not the window in front), YouTube opens there |
| 🔲 | Several browser windows, none in front: "Open my browser" | "I found N browser windows: 1, … Which one?" → answer "the second one" / "the YouTube one" / "the one on my second screen" (no wake word needed, even with music) |
| 🔲 | "Open my Monkeytype browser" / "switch to the YouTube browser window" | Switches to the browser window with that title |
| 🔲 | Several browser windows: "Search Google for NVIDIA news" | Uses the browser you used last — doesn't ask which one |
| ✅ | "Open a new browser window" | A genuinely new window |
| ✅ | "Search YouTube for Kendrick Lamar" → "click the first video" | First visible video opens (sidebar/channel card skipped) |
| ✅ | "Put it in fullscreen" / "exit full screen" on a video | YouTube fullscreen toggles |

## Multi-step

| | Test | Expected |
|---|---|---|
| ✅ | "Open a new browser window, search YouTube for Kendrick Lamar, click the first video, and pause." | All steps, each verified, in that window |
| 🔲 | Said aloud with natural pauses: "Hey SAINT, open my browser, search YouTube for Kendrick Lamar, click the first video, and turn the volume down." | The whole request arrives as one command (no fragment like "and" is answered), then all four steps run |
| 🔲 | "Open my browser search YouTube for Kendrick Lamar, click the first video and turn down the volume" (said in one go, no pause) | Same four steps — never "couldn't find an app called …" |
| 🔲 | "Open my browser, go to YouTube, search for Kendrick Lamar, click the first video, put it in fullscreen, and turn the volume down." | Six steps; stops and says where if a step fails |

## Natural responses

| | Test | Expected |
|---|---|---|
| ✅ | "How tall is the Burj Khalifa?" | "…828 meters…" in plain words |
| ✅ | Any request above | Never shows JSON, Python, tool names (`screen.context`, `composite:…`), schemas or agent traces |
| ✅ | "Never mind" / "forget it" / "cancel" | "Okay." (no action, no memory deleted) |
| ✅ | "Hide everything except Spotify" · "summarize this page" · an odd request the router doesn't know | Does it (Spotify stays up, everything else minimized / a 2–3 sentence summary of the page), or the LLM says plainly it can't; never claims an action that didn't happen |
| ✅ | "Make it louder" / "make it quieter" (no music playing) | Computer volume changes; never "Increased volume." without doing it |
| ✅ | "Skip this." · "Open Spotify." · "Pause Spotify." · "Turn on shuffle." | Each answer is ONE short sentence — no narration, no repetition |
| ✅ | "What's 2+2?" · "What's the capital of France?" | Direct one-line answer, no filler |

## Current-information routing (Settings > AI > Web)

| | Test | Expected |
|---|---|---|
| 🔲 | Provider = *none*: "What's the latest news about NVIDIA?" | Honest reply ("Web search is turned off…") — does NOT open a browser tab automatically |
| ✅ | Provider = *duckduckgo*: same question · "use DuckDuckGo and find the latest news about NVIDIA" | Short spoken summary of today's headlines (Google News feed), browser stays closed |
| ✅ | Music playing: "Hey SAINT, search Google for NVIDIA news" → "click the first link" | Uses browser automation; the follow-up works without the wake word |
| ✅ | "Open YouTube and search for Minecraft" | Browser automation, not web search |

## Vision (Settings > AI > Vision)

| | Test | Expected |
|---|---|---|
| ✅ | Backend = *none* + "what's on my screen?" | Uses Windows structured info; no fake vision output |
| 🔲 | Backend = *flux*, weights missing | Clear message pointing at `hf download black-forest-labs/FLUX.2-Klein-4B --local-dir data/vision/flux2-klein-4b` |
| 🔲 | Backend = *flux*, weights installed | First call loads the model (slow); subsequent calls fast |
| 🔲 | Backend = *ollama* with a vision model pulled | "What's on my second screen?" returns a real description |

## Interface: Halo, overlay, mini player, hot-words, scenes

| | Test | Expected |
|---|---|---|
| ✅ | Minimize SAINT | Halo appears around the screen edge; restoring the window hides it |
| ✅ | Watch the Halo while you say "Hey SAINT, what time is it" | Slow orbit → bright fast sweep while you talk → twin comets while thinking → pulse while speaking |
| ✅ | <kbd>Alt</kbd>+<kbd>`</kbd> from another app, then again | Overlay opens over it (frosted), then closes |
| ✅ | Music playing, open the overlay | "Next in queue" under Now playing lists the next 5 tracks and updates when the song changes |
| ✅ | Rest the cursor at the top-centre screen edge (Halo visible) | SAINT tab slides down; clicking it opens the overlay; moving away hides it |
| ✅ | Type in the overlay: "what time is it" | Reply appears under the input; the conversation card updates |
| ✅ | Turn on the mini player, play music, drag it, restart SAINT | Real cover art; position remembered |
| Y | Music playing: say just "skip", "next", "pause", "louder" (no wake word) | Each runs; the mini player flashes "Heard “skip” ✓" (if not, `voice.hotword.rejected` in the log says why) |
| ✅ | Music playing: talk normally / let lyrics play for 2 min | No hot-word fires |
| ✅ | Create a scene "Focus mode" (play lofi beats · set volume to 35 · minimize all windows besides Claude), then say "Hey SAINT, focus mode" | All steps run; a toast confirms; History shows a Scene entry |
| ✅ | History page after a few requests | Counts, 30-day chart, most-used tools and the timeline match what you did |
| ✅ | Ctrl+K → "demo" → Enter, then Esc part-way | Demo drives the UI; Esc stops it and your real state and chat come back |

## SAINT's own window

| | Test | Expected |
|---|---|---|
| 🔲 | "Turn off the mini player" · "show the mini player" (with and without a YouTube tab in front) | SAINT's mini player hides/shows; YouTube's miniplayer only when you say "YouTube mini player" |
| 🔲 | "Open the dashboard" · "go to history" · "go to settings" · "open settings" | SAINT's pages; plain "open settings" still opens Windows Settings |
| 🔲 | "Hide the halo" · "dark mode" · "minimize yourself" | Applied at once |

## Stop, silence, status

| | Test | Expected |
|---|---|---|
| 🔲 | Start a long multi-step request or a scene, then "stop" | It stops before the next step; no keys left held down |
| 🔲 | "Be quiet for 5 minutes", then "skip" / "what time is it" / "open blahblah" | Acts silently on the first two (reply shown on screen), still says the error for the third |
| 🔲 | Whisper "Hey SAINT, what time is it" | Answer is noticeably quieter |
| 🔲 | During a long request: "what are you doing?" | Names the current step without cancelling it |
| 🔲 | In a follow-up window, talk to someone else ("OK, why not?") | Ignored |

## Steam

| | Test | Expected |
|---|---|---|
| ✅ | "What games do I have" · "what are my biggest games" · "how big is Apex Legends" | Lists from Steam's files (verified live) |
| 🔲 | "Launch Terraria" · "start cs2" | Game starts through Steam |
| 🔲 | "Launch Apex Legends" (in the unregistered D:\SteamLibrary) | Explains how to add the library in Steam → Settings → Storage |
| 🔲 | "Open my Steam library and search how many dudes" | Opens the library, then "you don't have it" + store search |

## Files, storage and WinRAR (try removals on a test folder first)

| | Test | Expected |
|---|---|---|
| ✅ | "How much space is left on E" · "how full are my drives" | Real numbers (verified live) |
| ✅ | "What's taking up space on E" | Announces the biggest folders when the background scan ends (E: ≈ 50 s) |
| 🔲 | Put a file and an identical "file (1)" copy in Downloads → "clean up my Downloads" → "yes" | The copy goes to the Recycle Bin; restore it from there |
| 🔲 | "What can I delete on E" | Reports the old Windows install on E: as report-only, plus recordings |
| 🔲 | "Empty the recycle bin" | Refuses, opens the Recycle Bin |
| 🔲 | Download a .rar → "go to my Downloads folder, click the first download and extract it using WinRAR to my games folder" | Progress, then "Extracted … to D:\Games\…"; nothing overwritten |
| 🔲 | "Unzip the latest download and delete it afterwards" → "yes" | Extracts, then asks, then recycles the archive |
| 🔲 | "Move this folder to the D drive" (a test folder selected in Explorer) → "yes" | Windows' own move with progress; Ctrl+Z in Explorer undoes it |
| 🔲 | System page → Storage → Scan / Check for junk | Bars, biggest folders with Open, junk summary |

## Windows controls

| | Test | Expected |
|---|---|---|
| 🔲 | "Mute my mic" / "unmute my mic" | The Windows mic mute icon changes |
| 🔲 | "Set Spotify to 30 percent" · "mute the game" (a game in front) | Volume mixer changes for that app only |
| 🔲 | "Switch audio to my headphones" · "use my speakers" | Default output changes (HyperX = headphones) |
| 🔲 | "Restart my PC" → "yes" → "cancel the shutdown" | Windows shows the 60 s warning, then it's cancelled |
| 🔲 | "Lock my PC" · "take a screenshot" | Locks · PNG in Pictures\Screenshots |

## Watching, workspaces, clipboard, notifications, developer mode

| | Test | Expected |
|---|---|---|
| 🔲 | Ask Claude something, then "tell me when Claude finishes" → switch away → "take me back" | Announced a few seconds after the reply stops streaming; focuses Claude |
| 🔲 | Start a download → "tell me when this download finishes" | Announced when the file is complete |
| 🔲 | Step away ≥ 2 min, come back → "what changed while I was away?" | Windows opened/closed/retitled |
| 🔲 | "Watch my right screen" → "what's on my screen?" → "use all screens" | Uses the right monitor, then follows you again |
| 🔲 | Arrange windows on both monitors → "save this workspace as Coding" → move them → "restore Coding" | Windows go back to their monitors and sizes; closed apps reopen |
| 🔲 | "Gaming mode" → "I'm done gaming" | Saves the layout, pauses music, opens Steam, goes quiet · restores and talks again |
| 🔲 | Copy a paragraph → "summarize what I copied"; copy buggy code → "fix the code I copied" → "paste" | Spoken summary; fixed code pasted |
| 🔲 | "Read my notifications out loud", then get a Discord message / a failed build | Announced if important; "always tell me about Discord" |
| 🔲 | Copy a Python traceback → "open the file causing the error" | VS Code opens at that line |
| 🔲 | A dialog asking to update → "handle this" | Proposes one button and asks before clicking |

## Spotify learning

| | Test | Expected |
|---|---|---|
| 🔲 | Skip a song in the Spotify app itself | One `skips` row with source `app` within ~2 s |
| 🔲 | Say "skip" | Exactly one `skips` row, source `voice` |
| 🔲 | "More energetic" · "more like the last song" · "no more of this artist" | Picks change accordingly; the artist stops appearing |

## History

| | Test | Expected |
|---|---|---|
| ✅ | History page with a year of synthetic data, dark and light theme | Day squares fill the card, hover shows "N requests · date", streak / success / time-of-day cards |
