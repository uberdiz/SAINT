"""v0.4 interface: Overview / Tasks pages, the task card, health with actions, the status bar,
layout customization, and panels re-measuring when their content changes."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def window():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from core.config import config
    config.set("overlay.hotkey", "", persist=False)
    from ui.main_window import MainWindow
    w = MainWindow(app, None)
    w.show()
    yield app, w
    w.demo.stop()
    w.halo.shutdown()
    w.overlay.hide()
    w.widget.hide()
    for key in ("layout.sidebar_hidden", "layout.sidebar_order", "layout.overview_hidden", "layout.overview_order"):
        config.set(key, [], persist=False)


def _pump(app, secs=0.1):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def _task(status="executing"):
    from modules.agent.autonomy.model import AgentTask, PlanStep, StepStatus
    t = AgentTask(request="set up my coding workspace", goal="Set up your coding workspace", source="goal")
    t.plan = [PlanStep("open code", "Opening VS Code"), PlanStep("start the dev server", "Starting the dev server",
                                                                 risk="medium"),
              PlanStep("open discord", "Opening Discord")]
    t.plan[0].status = StepStatus.DONE
    t.plan[1].status = StepStatus.RUNNING
    t.current, t.status, t.reason = 1, status, "The project uses npm"
    t.note("ACTION", "Opening VS Code")
    return t


def test_overview_is_the_main_page_and_home_still_works(window):
    app, w = window
    w.navigate("Home", animate=False)
    assert w.stack.currentWidget() is w.overview
    assert w.sidebar.current() == "Overview"
    from modules.ui_control.tools import page_for
    assert page_for("tasks") == "Tasks" and page_for("dashboard") == "Overview"


def test_task_card_shows_goal_steps_reason_and_controls(window):
    app, w = window
    card = w.overview.panels["task"]
    t = _task()
    card._task = t.summary()
    card.render()
    _pump(app)
    assert card.goal.text() == "Set up your coding workspace"
    assert "step 2 of 3" in card.current.text()
    assert card.steps.count() == 3
    assert "npm" in card.reason.text()
    assert card.pause_btn.isVisibleTo(card) and card.stop_btn.isVisibleTo(card)
    assert not card.yes_btn.isVisibleTo(card)
    t.status = "waiting_for_user"
    t.reason = "Starting the dev server means: … Go ahead?"
    card._task = t.summary()
    card.render()
    assert card.yes_btn.isVisibleTo(card) and card.no_btn.isVisibleTo(card)


def test_task_card_idle_suggests_what_to_say(window):
    app, w = window
    card = w.overview.panels["task"]
    card._task = None
    card.render()
    assert card.goal.text() == "Nothing running"
    assert "coding workspace" in card.hint.text()


def test_health_offers_retry_and_cpu_for_a_gpu_failure(window):
    app, w = window
    from core.startup import startup
    from ui.components.health_panel import HealthPanel, HealthRow
    from PySide6.QtWidgets import QPushButton

    def boom():
        raise RuntimeError("CUDA backend unavailable")
    startup.run("tts", "Speech output", boom)
    panel = HealthPanel(w, compact=True)
    panel.show()
    _pump(app)
    rows = panel.findChildren(HealthRow)
    assert rows, "the failed subsystem has a row"
    labels = {b.text() for b in rows[0].findChildren(QPushButton)}
    assert {"Retry", "Use CPU", "Settings"} <= labels
    startup.run("tts", "Speech output", lambda: None)
    panel.deleteLater()


def test_status_bar_says_what_saint_listens_for(window):
    from ui.components.agent_status import listening_hint
    assert "Hey SAINT" in listening_hint("wake_listening")
    assert "go ahead" in listening_hint("command_listening").lower()
    assert "stop" in listening_hint("executing")
    assert "answer" in listening_hint("idle", "Waiting for your reply")


def test_sidebar_pages_can_be_hidden_and_reordered(window):
    app, w = window
    from core.config import config
    from ui.reactive import ui_bus
    config.set("layout.sidebar_hidden", ["Storage", "Overview"], persist=False)       # Overview can't be hidden
    config.set("layout.sidebar_order", ["Tasks", "Overview"], persist=False)
    ui_bus.setting_changed.emit("layout.sidebar")
    _pump(app)
    keys = w.sidebar.keys()
    assert "Storage" not in keys and "Overview" in keys and keys[0] == "Tasks" and keys[-1] == "Settings"
    w.navigate("Storage", animate=False)                    # a hidden page still opens (voice, palette)
    assert w.stack.currentWidget() is w.page_map["Storage"]
    config.set("layout.sidebar_hidden", [], persist=False)
    config.set("layout.sidebar_order", [], persist=False)
    ui_bus.setting_changed.emit("layout.sidebar")


def test_overview_panels_follow_the_layout_settings(window):
    app, w = window
    from core.config import config
    config.set("layout.overview_hidden", ["next", "devices"], persist=False)
    config.set("layout.overview_order", ["health", "task"], persist=False)
    w.overview.arrange(force=True)
    _pump(app)
    assert not w.overview.panels["next"].isVisibleTo(w.overview)
    assert w.overview.panels["health"].isVisibleTo(w.overview)
    from ui.pages.overview import panel_order
    assert panel_order()[:2] == ["health", "task"]
    config.set("layout.overview_hidden", [], persist=False)
    config.set("layout.overview_order", [], persist=False)
    w.overview.arrange(force=True)


def test_narrow_window_is_one_column(window):
    app, w = window
    w.navigate("Overview", animate=False)
    w.resize(800, 600)
    _pump(app, 0.2)
    w.overview.arrange()
    assert not w.overview.side_w.isVisibleTo(w.overview)
    w.resize(1500, 900)
    _pump(app, 0.2)
    w.overview.arrange()
    assert w.overview.side_w.isVisibleTo(w.overview)


def test_rebuilt_panel_content_is_measured_again(window):
    """Rows rebuilt inside a panel that's already laid out used to be squeezed to a few pixels
    (a stale height-for-width in the column's layout item)."""
    app, w = window
    w.resize(900, 700)
    w.navigate("Overview", animate=False)
    _pump(app, 0.3)
    w.overview.arrange(force=True)
    _pump(app, 0.3)
    att = w.overview.panels["attention"]
    att._sig = None
    from core.startup import startup

    def boom():
        raise RuntimeError("a long reason that wraps " * 4)
    startup.run("mcp", "MCP servers", boom)
    att.refresh()
    _pump(app, 0.4)
    assert att.height() >= att.minimumSizeHint().height() - 2
    startup.run("mcp", "MCP servers", lambda: None)


def test_tasks_page_lists_tasks_with_their_trail(window, tmp_path, monkeypatch):
    app, w = window
    from modules.agent.autonomy import manager as mgr
    m = mgr.AgentTaskManager(path=str(tmp_path / "t.json"))
    m._loaded = True
    t = _task("completed")
    t.result = "Done — your coding workspace is ready."
    m._tasks.append(t)
    monkeypatch.setattr(mgr, "agent_tasks", m)
    page = w.page_map["Tasks"]
    w.navigate("Tasks", animate=False)
    page.refresh()
    _pump(app)
    assert page.list.count() == 1
    assert page.goal.text() == "Set up your coding workspace"
    assert page.again_btn.isVisibleTo(page) and page.remember_btn.isVisibleTo(page)
    assert page.trail.lay.count() >= 1


def test_device_transport_and_latency_text():
    from ui.components.device_card import describe
    assert describe({"connected": True, "transport": "Wi-Fi", "rtt_ms": 8.2}) == "Wi-Fi · 8 ms"
    assert describe({"connected": True, "transport": "Internet", "rtt_ms": None}) == "Internet"
    assert describe({"connected": False, "last_seen": time.time() - 5}).startswith("seen")
