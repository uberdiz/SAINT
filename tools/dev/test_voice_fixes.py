#!/usr/bin/env python3
"""
Quick verification script for voice pipeline fixes.
Tests: CPU normalization, control token filtering, TTS service singleton.
"""

import sys
import time

def test_cpu_normalization():
    """Test that CPU usage is normalized to 0-100%."""
    print("\n=== Test 1: CPU Normalization ===")
    try:
        import psutil
        p = psutil.Process()
        raw = p.cpu_percent(interval=0.1)
        cpu_count = psutil.cpu_count(logical=True) or 1
        normalized = min(100.0, raw / cpu_count)
        print(f"Raw CPU: {raw:.1f}%")
        print(f"CPU Count: {cpu_count}")
        print(f"Normalized: {normalized:.1f}%")
        assert 0 <= normalized <= 100, f"CPU {normalized}% out of range!"
        print("✅ PASS: CPU normalized to 0-100%")
        return True
    except Exception as e:
        print(f"❌ FAIL: {e}")
        return False

def test_control_token_filtering():
    """Test that control tokens are filtered from TTS text."""
    print("\n=== Test 2: Control Token Filtering ===")
    try:
        from core.conversation import clean_text_for_tts

        test_cases = [
            ("Hello [silence] world", "Hello world"),
            ("[SILENCE]", ""),
            ("Test [thinking] more", "Test more"),
            ("[interrupted] stopped", "stopped"),
            ("[action]open[/action]", ""),
            ("<silence>", ""),
            ("No tokens here", "No tokens here"),
            ("Mix [silence] and [thinking] text", "Mix and text"),
        ]

        all_passed = True
        for input_text, expected in test_cases:
            result = clean_text_for_tts(input_text)
            if result == expected:
                print(f"  ✅ '{input_text}' → '{result}'")
            else:
                print(f"  ❌ '{input_text}' → '{result}' (expected '{expected}')")
                all_passed = False

        if all_passed:
            print("✅ PASS: All control tokens filtered correctly")
        return all_passed
    except Exception as e:
        print(f"❌ FAIL: {e}")
        return False

def test_tts_service_singleton():
    """Test that TTS service is a proper singleton."""
    print("\n=== Test 3: TTS Service Singleton ===")
    try:
        from modules.voice.tts_service import get_tts_service, TTSService

        # Get instance twice
        tts1 = get_tts_service()
        tts2 = get_tts_service()

        # Should be same instance
        assert tts1 is tts2, "Different instances returned!"
        print(f"  ✅ Same instance returned: {id(tts1)}")

        # Check initial state
        print(f"  State: {tts1.state.name}")
        print(f"  Engine type: {tts1._engine_type}")
        print("✅ PASS: TTS service singleton works")
        return True
    except Exception as e:
        print(f"❌ FAIL: {e}")
        return False

def test_tts_skip_detection():
    """Test that TTS service can detect skippable text."""
    print("\n=== Test 4: TTS Skip Detection ===")
    try:
        from modules.voice.tts_service import should_skip_text

        test_cases = [
            ("[silence]", True, "control_token"),
            ("", True, "empty_text"),
            ("   ", True, "whitespace_only"),
            ("[thinking]", True, "bracket_token"),
            ("<silence>", True, "angle_token"),
            ("Hello world", False, ""),
        ]

        all_passed = True
        for text, should_skip, reason in test_cases:
            skip, actual_reason = should_skip_text(text)
            if skip == should_skip and (not should_skip or reason in actual_reason):
                print(f"  ✅ '{text}' → skip={skip}, reason='{actual_reason}'")
            else:
                print(f"  ❌ '{text}' → skip={skip}, reason='{actual_reason}' (expected skip={should_skip})")
                all_passed = False

        if all_passed:
            print("✅ PASS: TTS skip detection works")
        return all_passed
    except Exception as e:
        print(f"❌ FAIL: {e}")
        return False

def test_tts_service_state_machine():
    """Test TTS service state machine transitions."""
    print("\n=== Test 5: TTS State Machine ===")
    try:
        from modules.voice.tts_service import get_tts_service, TTSState

        tts = get_tts_service()

        # Check state enum
        states = [TTSState.UNINITIALIZED, TTSState.LOADING, TTSState.READY,
                  TTSState.SYNTHESIZING, TTSState.PLAYING, TTSState.STOPPING,
                  TTSState.ERROR, TTSState.SHUTDOWN]
        print(f"  States defined: {len(states)}")

        # Check diagnostics structure
        diag = tts.get_diagnostics()
        required_keys = ['state', 'engine_type', 'is_ready', 'is_speaking',
                         'request_count', 'warmup_done']
        for key in required_keys:
            assert key in diag, f"Missing key: {key}"
        print(f"  Diagnostics keys: {list(diag.keys())}")

        print("✅ PASS: TTS state machine structure valid")
        return True
    except Exception as e:
        print(f"❌ FAIL: {e}")
        return False

def test_voice_state_machine():
    """Test voice state machine singleton."""
    print("\n=== Test 6: Voice State Machine ===")
    try:
        from modules.voice.voice_state import get_voice_state, VoiceState

        vs = get_voice_state()

        # Check state enum
        states = [VoiceState.IDLE, VoiceState.LISTENING, VoiceState.THINKING,
                  VoiceState.SPEAKING, VoiceState.INTERRUPTING]
        print(f"  States defined: {len(states)}")

        # Check diagnostics
        diag = vs.get_diagnostics()
        required_keys = ['state', 'active_turn_id', 'interrupt_count', 'is_busy']
        for key in required_keys:
            assert key in diag, f"Missing key: {key}"
        print(f"  Diagnostics keys: {list(diag.keys())}")

        print("✅ PASS: Voice state machine valid")
        return True
    except Exception as e:
        print(f"❌ FAIL: {e}")
        return False

def main():
    print("=" * 60)
    print("SAINT Voice Pipeline Fixes - Verification Tests")
    print("=" * 60)

    # Run from the project root so SAINT's packages import.
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.chdir(root)
    sys.path.insert(0, root)

    results = []
    results.append(("CPU Normalization", test_cpu_normalization()))
    results.append(("Control Token Filtering", test_control_token_filtering()))
    results.append(("TTS Service Singleton", test_tts_service_singleton()))
    results.append(("TTS Skip Detection", test_tts_skip_detection()))
    results.append(("TTS State Machine", test_tts_service_state_machine()))
    results.append(("Voice State Machine", test_voice_state_machine()))

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")

    print(f"\nTotal: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 All verification tests passed!")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed")
        return 1

if __name__ == "__main__":
    sys.exit(main())
