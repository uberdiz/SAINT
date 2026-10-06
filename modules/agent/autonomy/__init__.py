"""
modules/agent/autonomy

SAINT's task loop for requests that take more than one action:

    USER REQUEST -> UNDERSTAND -> OBSERVE -> PLAN -> EXECUTE -> VERIFY -> RECOVER -> COMPLETE -> LEARN

    model.py     AgentTask / PlanStep and their states (plain data, saved to agent_tasks.json)
    observe.py   what's true on the PC right now, cheapest source first (APIs, windows, processes,
                 files, ports, UI Automation, OCR, a vision model last)
    verify.py    did a step's expected result actually happen?
    recover.py   a step failed: classify why and find another way (bounded)
    policy.py    risk of each step and what the permission mode allows without asking
    goals.py     built-in goals ("set up my coding workspace", "run the project") and project finding
    planner.py   request -> plan: a learned procedure, a goal, the user's own steps, or the model
    executor.py  runs one task step by step on its own thread (pause / resume / cancel)
    manager.py   the current task and history, voice control ("pause", "what's next?", "why did
                 that fail?"), events for the UI, and learning from finished tasks
    control.py   the spoken task-control commands

Simple requests ("pause Spotify") never come here: the deterministic router handles them. The
language model is only asked to plan when no learned procedure, goal or deterministic split
fits, and to recover when the deterministic recoveries didn't.
"""
