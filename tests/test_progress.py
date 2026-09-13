"""
tests/test_progress.py

Tests for the Project Progress tracking system.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core.progress import ProgressTracker, ComponentStatus


@pytest.fixture
def progress_tracker():
    """Create a fresh progress tracker with temp file."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        path = f.name
    
    tracker = ProgressTracker(path)
    # Reset all criteria to ensure clean state
    for comp in tracker.get_all().values():
        for c in comp.criteria:
            c.met = False
            c.evidence = ""
        comp.update_status()
    tracker.save()
    
    yield tracker
    
    try:
        os.unlink(path)
    except OSError:
        pass


def test_component_creation(progress_tracker):
    """Test that all components are created with criteria."""
    components = progress_tracker.get_all()
    
    expected = ["core", "ai", "gui", "voice", "tts", "memory", "automation", "vision", "analytics"]
    for exp in expected:
        assert exp in components
        assert len(components[exp].criteria) > 0


def test_completion_percentage(progress_tracker):
    """Test completion percentage calculation."""
    comp = progress_tracker.get_component("core")
    assert comp.completion_percentage() == 0
    
    # Mark some criteria as met
    progress_tracker.set_criterion("core", "event_bus", True, "Implemented in core/events.py")
    progress_tracker.set_criterion("core", "config", True, "Implemented in core/config.py")
    
    comp = progress_tracker.get_component("core")
    assert comp.completion_percentage() == 33  # 2/6 = 33%


def test_status_update(progress_tracker):
    """Test status updates based on completion."""
    comp = progress_tracker.get_component("core")
    assert comp.status == ComponentStatus.NOT_STARTED
    
    progress_tracker.set_criterion("core", "event_bus", True)
    comp = progress_tracker.get_component("core")
    assert comp.status == ComponentStatus.PARTIAL
    
    # Mark all as met
    for c in comp.criteria:
        progress_tracker.set_criterion("core", c.name, True)
    
    comp = progress_tracker.get_component("core")
    assert comp.status == ComponentStatus.COMPLETE


def test_persistence(progress_tracker):
    """Test that progress persists across instances."""
    progress_tracker.set_criterion("core", "event_bus", True, "Evidence")
    progress_tracker.set_criterion("core", "config", True)
    
    # Create new tracker with same file
    tracker2 = ProgressTracker(progress_tracker._path)
    comp = tracker2.get_component("core")
    
    assert comp.criteria[0].met is True
    assert comp.criteria[0].evidence == "Evidence"
    assert comp.criteria[1].met is True


def test_overall_progress(progress_tracker):
    """Test overall progress calculation."""
    overall = progress_tracker.get_overall_progress()
    assert overall["overall_percentage"] == 0
    
    # Mark a few criteria across components
    progress_tracker.set_criterion("core", "event_bus", True)
    progress_tracker.set_criterion("ai", "mock_provider", True)
    
    overall = progress_tracker.get_overall_progress()
    assert overall["overall_percentage"] > 0


def test_format_report(progress_tracker):
    """Test formatted report generation."""
    report = progress_tracker.format_report()
    assert "SAINT REVITALIZED" in report
    assert "Core" in report
    assert "AI" in report
    assert "Overall" in report


def test_evidence_tracking(progress_tracker):
    """Test that evidence is stored with criteria."""
    progress_tracker.set_criterion("core", "event_bus", True, "core/events.py:111")
    
    comp = progress_tracker.get_component("core")
    c = comp.criteria[0]
    assert c.evidence == "core/events.py:111"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])