"""
modules/learning/catalog.py

The command shapes the planner may use. Every line here is a real command
SAINT's router understands (tests/test_learning.py routes each one), so a
plan built from them can actually run. Words in the examples (app names,
songs, sites) are placeholders the planner swaps for what the user asked.
"""

CATALOG = {
    "Apps and windows": [
        "open notepad",
        "open disk cleanup",
        "close discord",
        "close this window",
        "switch to spotify",
        "minimize discord",
        "maximize chrome",
        "show the desktop",
        "clear everything off the screen but youtube",
        "move spotify to my second monitor",
        "snap discord to the left",
        "what windows are open",
    ],
    "Mouse and keyboard": [
        "click the settings button",
        "double click the recycle bin",
        "right click the desktop",
        "click the first video",
        "click the first result",
        "press ctrl+t",
        "press escape",
        "press enter",
        "type hello world",
        "scroll down",
        "go back",
        "refresh the page",
    ],
    "Browser and YouTube": [
        "open my browser",
        "open a new tab",
        "search google for nvidia news",
        "search youtube for lofi beats",
        "go to reddit.com",
        "open youtube",
        "pause the video",
        "put the video in full screen",
    ],
    "Music (Spotify)": [
        "play kendrick lamar",
        "play my chill playlist",
        "play my liked songs",
        "pause the music",
        "resume the music",
        "skip this song",
        "turn up spotify",
        "turn down spotify",
        "set spotify volume to 30",
        "turn spotify down a little",
        "what's in my queue",
    ],
    "Sound and system": [
        "set the system volume to 40",
        "mute my mic",
        "unmute my mic",
        "switch audio to my headphones",
        "switch audio to my speakers",
        "take a screenshot",
        "lock my pc",
        "open sound settings",
        "open task manager",
        "turn the volume down a little",
        "what's running",
        "end task on discord",
        "check my pc health",
    ],
    "Files and games": [
        "open my downloads folder",
        "open my games folder",
        "how much space is left on my c drive",
        "clean up my downloads",
        "extract the latest download to my games folder",
        "launch terraria",
        "open my steam library",
        "search steam for hades",
    ],
    "SAINT itself": [
        "open the dashboard",
        "turn off the mini player",
        "silent mode for 10 minutes",
        "remind me in 10 minutes to stretch",
        "what's on my screen",
        "read my clipboard",
    ],
}


def examples():
    for group in CATALOG.values():
        yield from group


def as_prompt() -> str:
    return "\n".join(f"{name}:\n" + "\n".join(f"  - {c}" for c in cmds) for name, cmds in CATALOG.items())
