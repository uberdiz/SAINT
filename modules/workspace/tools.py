"""Workspace tools: save / restore / list / forget window layouts."""

from modules.automation.tools import P, PermissionLevel, Tool, ToolError
from modules.workspace import store


def _available():
    from core.config import config
    if not config.get("modules.desktop", True) or not config.get("desktop.enabled", True):
        return False, "Desktop control is turned off in Settings."
    return True, ""


def save(name: str):
    snap = store.capture()
    if not snap["windows"]:
        raise ToolError("There are no windows open to save.", "EMPTY")
    store.workspaces.save(name, snap)
    return {"name": name, "windows": len(snap["windows"]),
            "summary": f"Saved {name}: {store.describe(snap)}."}


def restore(name: str):
    snap = store.workspaces.get(name)
    if snap is None:
        names = ", ".join(v.get("name", k) for k, v in store.workspaces.all().items())
        raise ToolError(f"I don't have a workspace called {name}." + (f" You have: {names}." if names else ""),
                        "NOT_FOUND")
    from modules.automation.tasks import task_manager
    task = task_manager.create_callable_task(f"restoring {snap.get('name', name)}",
                                             lambda progress, cancel: store.restore(snap, progress, cancel))
    return {"started": task.id, "summary": f"Restoring {snap.get('name', name)}."}


def list_all():
    items = store.workspaces.all()
    return {"workspaces": [{"name": v.get("name", k), "windows": len(v.get("windows", []))} for k, v in items.items()]}


def forget(name: str):
    return {"forgotten": store.workspaces.delete(name), "name": name}


def register_workspace_tools(registry):
    tools = [
        Tool("workspace.save", "Save the current window layout as a named workspace", {"name": "string"},
             PermissionLevel.LOW, save, parameters={"name": P("string")}, llm_exposed=True, category="workspace"),
        Tool("workspace.restore", "Restore a saved workspace (reopen and place its windows)", {"name": "string"},
             PermissionLevel.MEDIUM, restore, parameters={"name": P("string")}, llm_exposed=True,
             category="workspace"),
        Tool("workspace.list", "List saved workspaces", {}, PermissionLevel.LOW, list_all, parameters={},
             llm_exposed=True, category="workspace"),
        Tool("workspace.forget", "Forget a saved workspace (only the saved layout)", {"name": "string"},
             PermissionLevel.LOW, forget, parameters={"name": P("string")}, category="workspace"),
    ]
    for t in tools:
        t.availability = _available
        registry.register(t)
