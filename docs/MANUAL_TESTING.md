# SAINT manual testing checklist

Run `run.bat`, press **Start listening** (or enable *Start listening on launch*), and work through the
list. Speak naturally from where you normally sit. Tick each line; note the exact words when
something goes wrong. `data/logs/saint.log` has the matching entries: set Settings → General → Log
level to *Verbose* for wake scores, tool arguments/results, observations and retries.

Legend: ✅ = verified automatically/live during development · 🔲 = needs your real voice/room/account.

## Voice

I still cant give 1 word answers!!!!

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
| Y | Music playing, in a conversation window: "pause" (one word) | Pauses — one-word commands no longer need "SAINT" in front |
| Y | Music playing, SAINT idle: say just "skip" (one word, no wake word) | Skips (hot-word). With no music playing it is ignored |
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
| 🔲 fixed — was: *clicked something on my other screen; ignored the screen I named* | "Double click the recycling bin" · "click the recycle bin on my main screen" · "right click the desktop on my main screen" | Uses the desktop icon (never text in a window that mentions it) on that screen; right click opens the desktop menu there |
| Y | "Right click <something>" · "hover over <something>" | Acts in the window you're looking at (not one SAINT used minutes ago) |
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
| Y | One browser window open: "Open my browser and search YouTube" | Reuses it, opens YouTube, no new window |
| Y | "Open up a new tab in my browser and search YouTube" | New tab in your browser (not the window in front), YouTube opens there |
| Y | Several browser windows, none in front: "Open my browser" | "I found N browser windows: 1, … Which one?" → answer "the second one" / "the YouTube one" / "the one on my second screen" (no wake word needed, even with music) |
| Y | "Open my Monkeytype browser" / "switch to the YouTube browser window" | Switches to the browser window with that title |
| Y | Several browser windows: "Search Google for NVIDIA news" | Uses the browser you used last — doesn't ask which one |
| ✅ | "Open a new browser window" | A genuinely new window |
| ✅ | "Search YouTube for Kendrick Lamar" → "click the first video" | First visible video opens (sidebar/channel card skipped) |
| ✅ | "Put it in fullscreen" / "exit full screen" on a video | YouTube fullscreen toggles |

## Multi-step

| | Test | Expected |
|---|---|---|
| ✅ | "Open a new browser window, search YouTube for Kendrick Lamar, click the first video, and pause." | All steps, each verified, in that window |
|Y | Said aloud with natural pauses: "Hey SAINT, open my browser, search YouTube for Kendrick Lamar, click the first video, and turn the volume down." | The whole request arrives as one command (no fragment like "and" is answered), then all four steps run |
| Y | "Open my browser search YouTube for Kendrick Lamar, click the first video and turn down the volume" (said in one go, no pause) | Same four steps — never "couldn't find an app called …" |
| Y | "Open my browser, go to YouTube, search for Kendrick Lamar, click the first video, put it in fullscreen, and turn the volume down." | Six steps; stops and says where if a step fails |

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
| Y | "Turn off the mini player" · "show the mini player" (with and without a YouTube tab in front) | SAINT's mini player hides/shows; YouTube's miniplayer only when you say "YouTube mini player" |
| Y | "Open the dashboard" · "go to history" · "go to settings" · "open settings" | SAINT's pages; plain "open settings" still opens Windows Settings |
| Y | "Hide the halo" · "dark mode" · "minimize yourself" | Applied at once |

## Stop, silence, status

| | Test | Expected |
|---|---|---|
| Y | Start a long multi-step request or a scene, then "stop" | It stops before the next step; no keys left held down |
| Y | "Be quiet for 5 minutes", then "skip" / "what time is it" / "open blahblah" | Acts silently on the first two (reply shown on screen), still says the error for the third |
| ⚠️ known limit — was: *doesn't listen to me at all when I whisper* | Talk *softly* (not a whisper): "Hey SAINT, what time is it" | Quieter answer. A true whisper is below what the speech and wake-word detectors hear; loosening them brings back random speech, so it's left alone |
| Y | During a long request: "what are you doing?" | Names the current step without cancelling it |
| Y | In a follow-up window, talk to someone else ("OK, why not?") | Ignored |

## Steam

| | Test | Expected |
|---|---|---|
| ✅ | "What games do I have" · "what are my biggest games" · "how big is Apex Legends" | Lists from Steam's files (verified live) |
| Y | "Launch Terraria" · "start cs2" | Game starts through Steam |
| 🔲 fixed — was: *"open my games folder" had SAINT unzipped into it; Apex won't launch* | "Open my games folder" · "launch Apex Legends" | Opens **C:\Games** (your games folder is now C:\Games; the SAINT-main folder in D:\Games came from an earlier "extract the latest download" test — delete it yourself if you like). Apex still needs D: added in Steam → Settings → Storage → "+" |
| 🔲 fixed — was: *I had to open Steam myself* | With Steam **closed**: "Open my Steam library and search how many dudes" | "Starting Steam — I'll open your library as soon as it's up", then the library (and store search) opens by itself |

## Files, storage and WinRAR (try removals on a test folder first)

| | Test | Expected |
|---|---|---|
| ✅ | "How much space is left on E" · "how full are my drives" | Real numbers (verified live) |
| ✅ | "What's taking up space on E" | Announces the biggest folders when the background scan ends (E: ≈ 50 s) |
| 🔲 fixed — was: *follow-up questions didn't know what I meant* | "Clean up my Downloads" → "no" → "what did you find?" → "delete the old installers too" | Answers from the real result (no invented paths), then asks to recycle just the installers |
| Y improved — was: *works; wants visual feedback / percent* | "What can I delete on E", then "what are you doing?" | A pill at the bottom of the screen fills up with the percent; "what are you doing?" says "about N% done" |
| Y | "Empty the recycle bin" | Refuses, opens the Recycle Bin |
| 🔲 fixed — was: *should go to C:\Games; couldn't move it from D: to C:* | "Extract the latest download to my games folder" → "open that folder" → "move it to the D drive" → "yes" → "move it back to my C: games folder" | Extracts into C:\Games\…, opens that folder, moves it (asks first) and back |
| Y | "Unzip the latest download and delete it afterwards" → "yes" | Extracts, then asks, then recycles the archive |
| 🔲 fixed — was: *Nah* | Select a test folder in Explorer → "move this folder to the D drive" → "yes" · or right after SAINT made/extracted a folder: "move it to the D drive" | Windows' own move; Ctrl+Z in Explorer undoes it. If Explorer's selection can't be read (Windows 11 tabs), use the second form and tell me |
| 🔲 new — was: *no visual for the junk; didn't find much; could be its own tab* | Sidebar → **Storage** → *Check for junk* | A chart by category (shader caches, Spotify/Discord/browser caches, temp, crash dumps, dev caches, installers, Windows Update, Recycle Bin…) and a ticked list; on this PC it should find ~20 GB+. *Move ticked to Recycle Bin* asks first |

## Windows controls

| | Test | Expected |
|---|---|---|
| Y fixed — was: *should mute Voicemeeter Stereo Input 1 / B1, and bare "mute" should work* | "Mute" · "unmute" · "mute my mic on voicemeeter" | Voicemeeter's Stereo Input 1 (your LCS USB mic) mutes/unmutes (audio.voicemeeter.mic_strip to change) |
| Y | "Set Spotify to 30 percent" · "mute the game" (a game in front) | Volume mixer changes for that app only |
| Y fixed — was: *use Voicemeeter: A2 (speakers) ↔ A1 (headphones)* | "Switch my audio to my headphones" · "use my speakers" | In Voicemeeter, strips playing on A2 move to A1 (HyperX), and back |
| Y | "Restart my PC" → "yes" → "cancel the shutdown" | Windows shows the 60 s warning, then it's cancelled |
| 🔲 fixed — was: *couldn't delete the screenshot afterwards* | "Take a screenshot of Claude" → "delete that screenshot" → "yes" | It goes to the Recycle Bin |

## Short-term memory ("it", "that folder", "what did you find?")

| | Test | Expected |
|---|---|---|
| 🔲 | "Clean up my D drive" → wait for the result → "what did you find?" → "is that safe to delete?" → "delete it" | Talks about what it really found (e.g. D:\WUDownloadCache), then asks before recycling (Windows Update files in Windows itself → opens Disk Cleanup instead) |
| 🔲 | "Take a screenshot of my left screen" → "open it" → "show it in Explorer" → "rename it to test shot" | Opens the picture, selects it in Explorer, renames it (keeps .png) |
| 🔲 | "Make a new folder in my games folder called The Loop" → "copy the path" → paste somewhere | C:\Games\The Loop exists; the path is on the clipboard |
| 🔲 | "Go to my most recent download and delete it" → "yes" | The newest finished download goes to the Recycle Bin (asks first; never presses Delete for you) |
| 🔲 | "Open that folder" with nothing made in the last minutes | "I haven't made or opened a folder in the last few minutes…" — no "couldn't find an app called that folder" |
| 🔲 | "Switch to Glod" (a mishearing) → "I said Claude btw" | Switches to Claude |
| 🔲 | "Close that window" right after "open disk cleanup" | "Do you want me to close Disk Cleanup?" |
| 🔲 | "Scroll down" → "scroll more" → "keep scrolling" | Each one moves further; plain "scroll down" now moves a real amount |
| 🔲 | "Click on the loop layer" (a page link) | Clicks it — never turns on YouTube's loop, never clicks something else |
| 🔲 | Automations → Learned → select a row → change it → Save; *New* → "gaming time" / "open Steam" + "open Discord" → Save → say "gaming time" | Edits stick; the new one runs both |
| 🔲 | Do something that works, then "save that as game time" → say "game time" | Repeats it |
| 🔲 | Restart SAINT twice | Spotify stays connected (no Connect click) |

## Learning (SAINT working things out)

Learned requests show on **Automations → Learned** (Try it / Forget). Say "what have you learned" or
"forget that" any time.

| | Test | Expected |
|---|---|---|
| Y | "Minimize all my windows and double left click the recycling bin" | Every window minimizes, then the Recycle Bin opens |
| 🔲 fixed — was: *I can't give 1-word answers* | "Close Disk Cleanup" → just "Yeah." · "press enter" as a follow-up | Both are heard (they used to be dropped as low-confidence speech) |
| Y | Something it doesn't know: "hop over to Discord" · "get Spotify onto my left monitor" | It works it out ("switch to discord"), does it, and says it'll remember |
| 🔲 fixed — was: *it just played my liked songs* | "Play my gym playlist" → "no, I meant my moe playlist" (or "…spelled M-O-E") → later "play my gym playlist" (use your own names) | Plays it and remembers; the old wrong "liked songs" lesson was removed |
| 🔲 check — was: *it asked but didn't play it* | "Play my party playlist" (none of yours) → "yes" within 30 s | Says "You don't have a playlist called party" and plays a public one after yes. (The log only showed you answering "no, I meant…" — tell me if yes fails) |
| 🔲 changed — was: *sometimes it watches the wrong thing at the wrong time* | Something it can't do → it asks "Want to show me?" → "yes" → do it → "I'm all done" | Only starts watching after yes; "I'm all done" finishes it |
| 🔲 fixed — was: *showed it how to open Bloxstrap, it didn't remember* | "Open Bloxstrap" · "open block strap" · "let me show you how to put Claude on my right screen" → drag it → "done" | Bloxstrap opens (taskbar pins are now known apps); the lesson becomes "move Claude to my right screen" |
| Y | "Forget that" right after it learned something | That request goes back to how it was |
| Y | "Close the finals" / "close to area" (misheard) with the game running | "Do you want me to close THE FINALS / Terraria?" (the real name) → "yes" closes it; if the game ignores it, offers to force it to quit |
| Y | "No, close the finals" while SAINT asks something else | Answers the question *and* closes it |
| Y | "Never play that playlist again" while a playlist plays | Pauses; that playlist is skipped from then on |
| Y | Play a YouTube video out loud (no wake word), talk to someone in the room | Nothing is treated as a request (the follow-up check now also covers the window after actions and talking over SAINT) |
| Y | "Turn off the mini-player… Hey SAINT, turn off the mini-player" | Turns it off (a repeated request isn't dropped as a loop anymore) |

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
| Y | Skip a song in the Spotify app itself | One `skips` row with source `app` within ~2 s |
| Y | Say "skip" | Exactly one `skips` row, source `voice` |
| Y | "More energetic" · "more like the last song" · "no more of this artist" | Picks change accordingly; the artist stops appearing |

## History

| | Test | Expected |
|---|---|---|
| ✅ | History page with a year of synthetic data, dark and light theme | Day squares fill the card, hover shows "N requests · date", streak / success / time-of-day cards |
