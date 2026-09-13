"""
tests/test_memory.py

Tests for the Memory module.
"""

import sys
import os
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from modules.memory.module import MemoryModule
from modules.memory.database import MemoryType, MemoryDatabase


@pytest.fixture
def memory_module():
    """Create a fresh memory module for each test with isolated database."""
    # Create a temporary database file
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    
    # Create isolated database instance
    test_db = MemoryDatabase(db_path)
    
    # Patch the singleton
    import modules.memory.database as db_module
    original_db = db_module._memory_db
    db_module._memory_db = test_db
    
    m = MemoryModule()
    m.enable()
    yield m
    m.disable()
    
    # Restore and cleanup
    db_module._memory_db = original_db
    try:
        os.unlink(db_path)
    except OSError:
        pass


def test_conversation_history(memory_module):
    """Test storing and retrieving conversation history."""
    m = memory_module
    
    id1 = m.remember_conversation("Hello", "Hi there!")
    id2 = m.remember_conversation("How are you?", "I'm doing well!")
    
    assert id1 > 0
    assert id2 > 0
    
    conversations = m.get_recent_conversations(limit=10)
    assert len(conversations) == 2
    assert "Hello" in conversations[1]["content"]  # Most recent first
    assert "How are you?" in conversations[0]["content"]


def test_long_term_memory(memory_module):
    """Test storing and retrieving long-term facts."""
    m = memory_module
    
    id1 = m.remember_fact("User prefers dark mode", confidence=0.9)
    id2 = m.remember_fact("User is a Python developer", tags=["work"])
    
    assert id1 > 0
    assert id2 > 0
    
    results = m.search("dark mode", type_=MemoryType.LONG_TERM)
    assert len(results) == 1
    assert "dark mode" in results[0]["content"].lower()
    
    results = m.search("Python", type_=MemoryType.LONG_TERM)
    assert len(results) == 1
    assert "Python" in results[0]["content"]


def test_preferences(memory_module):
    """Test user preferences."""
    m = memory_module
    
    m.set_preference("theme", "dark")
    m.set_preference("language", "en")
    
    assert m.get_preference("theme") == "dark"
    assert m.get_preference("language") == "en"
    assert m.get_preference("nonexistent") is None


def test_tasks(memory_module):
    """Test task management."""
    m = memory_module
    
    id1 = m.create_task("Task 1", "Description 1", "high")
    id2 = m.create_task("Task 2", "Description 2", "low")
    
    tasks = m.list_tasks()
    assert len(tasks) == 2
    
    # Update task status
    m.update_task(id1, status="completed")
    tasks = m.list_tasks(status="completed")
    assert len(tasks) == 1
    assert tasks[0]["metadata"]["status"] == "completed"
    
    tasks = m.list_tasks(status="pending")
    assert len(tasks) == 1
    assert tasks[0]["metadata"]["status"] == "pending"


def test_project_state(memory_module):
    """Test project state storage."""
    m = memory_module
    
    m.set_project_state("current_module", "memory")
    m.set_project_state("progress", "50%")
    
    assert m.get_project_state("current_module") == "memory"
    assert m.get_project_state("progress") == "50%"
    assert m.get_project_state("nonexistent") is None


def test_search(memory_module):
    """Test memory search."""
    m = memory_module
    
    m.remember_conversation("Talk about Python", "Python is great")
    m.remember_fact("Python is a programming language")
    
    results = m.search("Python", limit=10)
    assert len(results) >= 2
    
    # Search with type filter
    results = m.search("Python", type_=MemoryType.CONVERSATION)
    assert len(results) == 1
    assert "conversation" in results[0]["type"]


def test_stats(memory_module):
    """Test memory statistics."""
    m = memory_module
    
    stats = m.get_stats()
    assert "total" in stats
    assert "conversation" in stats
    assert "long_term" in stats
    assert "preference" in stats
    assert "task" in stats
    assert "project" in stats


def test_persistence():
    """Test that memory persists across module restarts."""
    import modules.memory.database as db_module
    import tempfile
    
    # Create a temporary database file
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    
    try:
        # First session
        test_db = MemoryDatabase(db_path)
        db_module._memory_db = test_db
        m1 = MemoryModule()
        m1.enable()
        m1.remember_fact("Persistent fact", tags=["test"])
        m1.set_preference("test_key", "test_value")
        m1.disable()
        
        # Second session (simulates app restart) - new instance pointing to same file
        test_db2 = MemoryDatabase(db_path)
        db_module._memory_db = test_db2
        m2 = MemoryModule()
        m2.enable()
        
        assert m2.get_preference("test_key") == "test_value"
        results = m2.search("Persistent fact")
        assert len(results) == 1
        assert "Persistent fact" in results[0]["content"]
        
        m2.disable()
    finally:
        db_module._memory_db = None
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_forget_old_conversations(memory_module):
    """Test forgetting old conversation history."""
    m = memory_module
    
    m.remember_conversation("Old 1", "Response 1")
    m.remember_conversation("Old 2", "Response 2")
    m.remember_conversation("New 1", "Response 3")
    
    # Forget conversations older than 0 days (should delete all)
    deleted = m.forget_conversation_older_than(0)
    assert deleted == 3
    
    conversations = m.get_recent_conversations()
    assert len(conversations) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])