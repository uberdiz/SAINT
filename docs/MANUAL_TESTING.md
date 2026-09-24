# SAINT manual testing checklist

Run `run.bat`, press **Start listening** (or enable *Start listening on launch*), and work through the
list. Speak naturally from where you normally sit. Tick each line; note the exact words when
something goes wrong. `data/logs/saint.log` has the matching entries: set Settings → General → Log
level to *Verbose* for wake scores, tool arguments/results, observations and retries.

Legend: ✅ = verified automatically/live during development · 🔲 = needs your real voice/room/account.

## Voice

| | Test | Expected |
|---|---|---|
| 🔲 | "SAINT" (bare), then wait, then "what time is it" | Chime after "SAINT"; answers the time |
| 🔲 | "Hey SAINT, what time is it" (one breath) | Answers the time |
| 🔲 | "SAINT, skip this song" / "Hey SAINT … play some jazz" (pause after the wake word) | Command runs |
| 🔲 | Say the wake word 10× at normal distance, 10× from across the room | Note how many were missed |
| 🔲 | Wake word while music plays at a normal volume | Wakes; command ends when you stop talking |
| 🔲 | Wake word / "stop" while SAINT is speaking | SAINT stops and listens; it never wakes on its own voice |
| 🔲 | Normal conversation near the mic without "SAINT" (≥ 2 min) | Never reacts |
| 🔲 | "Hey SAINT, open Spotify" → "play Kendrick Lamar" → "skip that" (no wake word after the first) | All three run |
| 🔲 | Wait > 15 s after SAINT answers, then say "skip" | Ignored (back to wake-word listening) |
| ✅ | Synthetic "SAINT"/"Hey SAINT"/near-misses, clean and with music (4 voices) | 64/64 correct wake decisions |

## Spotify

| | Test | Expected |
|---|---|---|
| 🔲 | "Play Kendrick Lamar" / "put on DAMN" / "play my <playlist> playlist" | Correct artist / album / your playlist |
| 🔲 | "Pause" · "resume" · "skip" · "skip the song" · "go back" | Playback changes |
| 🔲 | "Play a different song" · "play something else" · "change the song" · "I'm not feeling this one" | Skips (never plays a song called "A Different Song") |
| 🔲 | "Play something like DAMN by Kendrick Lamar" | Similar music starts (Kendrick + collaborators + your taste) |
| 🔲 | "Turn on shuffle" · "turn on Smart Shuffle" · "turn Smart Shuffle off" | Spotify app's shuffle button changes |
| 🔲 | "Turn it down" right after a music command | Spotify volume drops |
| 🔲 | "Play something I'd like" · "recommend something" | Picks from your history |
| ✅ | "Search Spotify for Kendrick Lamar" → "no" | Lists results, offers to play, respects "no" |

## Screen

| | Test | Expected |
|---|---|---|
| ✅ | "What's on my screen?" / "What am I looking at?" | The window you're looking at + what else is open (no JSON) |
| ✅ | "What's on my second screen?" | Front window of monitor 2, what's behind it, tabs/text |
| ✅ | "What's currently open?" | Windows grouped per monitor |
| ✅ | "What is this error?" with no error visible | "I don't see an error…" (no invented error) |
| 🔲 | "What is this error?" with a real error dialog open | Explains the dialog's text |
| 🔲 | "Find the settings button" → "click it" | Locates, then clicks it |

## Mouse / keyboard

| | Test | Expected |
|---|---|---|
| ✅ | "Type hello" · "select all" · "copy" (in Notepad) | Text typed/selected/copied |
| 🔲 | "Double click the recycle bin" (desktop visible) | Opens the recycle bin |
| 🔲 | "Right click <something>" · "hover over <something>" | Context menu / hover |
| ✅ | "Scroll down" · "go back" · "refresh the page" | In the window SAINT is working with |
| 🔲 | "Click the button in the bottom right" | Nearest button to that corner |
| 🔲 | "Press ctrl+t" · "press escape" | Keys go to the intended window |

## Windows

| | Test | Expected |
|---|---|---|
| ✅ | "Open Notepad" twice | Second time switches to the same window (no duplicate) |
| ✅ | "Move it to my second monitor" · "make it bigger" · "put it on the left" · "move Notepad to my main screen" · "make this window smaller" | Each verified by measured position/size |
| ✅ | "Close it" → "yes" | Closes |
| 🔲 | "Put this window next to Spotify" | Side by side on Spotify's monitor |
| 🔲 | "Move that window over there" (pointer on the other monitor) | Moves to the pointer's monitor |
| 🔲 | "Close all the browser windows" | Asks first, then closes them |
| 🔲 | "Minimize it" / "maximize it" / "restore it" | Window state changes |

## Browser

| | Test | Expected |
|---|---|---|
| 🔲 | One browser window open: "Open my browser and search YouTube" | Reuses it, opens YouTube, no new window |
| 🔲 | Several browser windows, none in front: "Open my browser" | "I found N browser windows: 1, … Which one?" → answer "the second one" / "the YouTube one" / "the one on my second screen" |
| ✅ | "Open a new browser window" | A genuinely new window |
| ✅ | "Search YouTube for Kendrick Lamar" → "click the first video" | First visible video opens (sidebar/channel card skipped) |
| 🔲 | "Put it in fullscreen" / "exit full screen" on a video | YouTube fullscreen toggles |

## Multi-step

| | Test | Expected |
|---|---|---|
| ✅ | "Open a new browser window, search YouTube for Kendrick Lamar, click the first video, and pause." | All steps, each verified, in that window |
| 🔲 | "Open my browser, search YouTube for Kendrick Lamar, click the first video, and turn the volume down." | Same, then the computer volume drops |
| 🔲 | "Open my browser, go to YouTube, search for Kendrick Lamar, click the first video, put it in fullscreen, and turn the volume down." | Six steps; stops and says where if a step fails |

## Natural responses

| | Test | Expected |
|---|---|---|
| ✅ | "How tall is the Burj Khalifa?" | "…828 meters…" in plain words |
| ✅ | Any request above | Never shows JSON, Python, tool names (`screen.context`, `composite:…`), schemas or agent traces |
| ✅ | "Never mind" / "forget it" / "cancel" | "Okay." (no action, no memory deleted) |
| 🔲 | An odd request the router doesn't know (e.g. "hide everything except Spotify") | The LLM either does it through tools or says plainly it can't; it never claims an action that didn't happen |
| 🔲 | "Skip this." · "Open Spotify." · "Pause Spotify." · "Turn on shuffle." | Each answer is ONE short sentence — no narration, no repetition |
| 🔲 | "What's 2+2?" · "What's the capital of France?" | Direct one-line answer, no filler |

## Current-information routing (Settings > AI > Web)

| | Test | Expected |
|---|---|---|
| 🔲 | Provider = *none*: "What's the latest news about NVIDIA?" | Honest reply — does NOT open a browser tab automatically |
| 🔲 | Provider = *duckduckgo*: same question | Short spoken summary from DuckDuckGo, browser stays closed |
| 🔲 | "Search Google for NVIDIA news" | Uses browser automation (regardless of web provider) |
| 🔲 | "Open YouTube and search for Minecraft" | Browser automation, not web search |

## Vision (Settings > AI > Vision)

| | Test | Expected |
|---|---|---|
| ✅ | Backend = *none* + "what's on my screen?" | Uses Windows structured info; no fake vision output |
| 🔲 | Backend = *flux*, weights missing | Clear message pointing at `hf download black-forest-labs/FLUX.2-Klein-4B --local-dir data/vision/flux2-klein-4b` |
| 🔲 | Backend = *flux*, weights installed | First call loads the model (slow); subsequent calls fast |
| 🔲 | Backend = *ollama* with a vision model pulled | "What's on my second screen?" returns a real description |
