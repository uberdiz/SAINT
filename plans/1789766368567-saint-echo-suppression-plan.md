# SAINT Echo Suppression Fix

## Problem
SAINT responds to its own voice. The microphone picks up SAINT's TTS output, STT transcribes it, and SAINT treats it as a genuine user input, generating a new response. Current echo suppression fails because:
1. Time window is too short (2 seconds after TTS ends)
2. Matching is too strict (exact substring only; misses fragments and rephrased echoes)
3. `_current_ai_response` may be stale or empty when echo check runs

## Root Cause Analysis

**From logs**: SAINT says "Don't interrupt. I'm here to help. What's the next topic you'd like to discuss?" → Mic picks up SAINT's voice → STT captures "I can't stop talking." → SAINT treats as new prompt → responds "You're not focused."

**Why echo check misses this**: The echo check at `core/conversation.py:185` requires state in (THINKING, SPEAKING) OR within 2 seconds of `_last_tts_end_time`. When SAINT finishes and the STT result comes in after 2 seconds (or state is IDLE), the check doesn't run at all. And even when it does run, exact substring matching catches only near-exact echoes.

## Changes

### File: `core/conversation.py`

#### 1. Increase echo time window (line 185)
Change `2.0` to `5.0` seconds. SAINT's TTS audio often has residual playback + STT processing latency >2 seconds.

#### 2. Add word-overlap echo detection (after line 197)
Add a new function `_word_overlap_ratio(text1, text2)` that:
- Splits both texts into word sets
- Computes Jaccard similarity: `intersection / union`
- Returns ratio 0.0–1.0

Then add echo check after the existing substring check:
```python
overlap = self._word_overlap(t_clean, ai_clean)
if t_clean and ai_clean and overlap > 0.5:
    # suppress as echo
```
This catches fragments and rephrased echoes that substring matching misses.

#### 3. Preserve last AI response for echo checking
`_current_ai_response` is cleared by `_start_thinking` before `_handle_user_speech` processes the next input. Add `_last_ai_response` field that persists the last completed AI response for echo checking even after `_current_ai_response` is cleared.

#### 4. Prioritize stop commands over echo (already implemented)
Stop command check runs before echo check — already done in current code. No change needed.

### No changes to:
- `modules/voice/tts.py` — KokoroTTS turn tracking already fixed
- `modules/voice/tts_service.py` — `speak(turn_id=...)` already fixed
- `modules/voice/module.py` — no changes needed

## Echo Check Flow (after fix)

```
_handle_user_speech(text):
  1. If stop command detected → interrupt TTS, go LISTENING
  2. If within 5s of TTS end:
     a. Exact substring match → suppress (existing)
     b. Word overlap > 50% → suppress (new)
  3. If SPEAKING/THINKING (not echo) → interrupt and start new turn
  4. Otherwise → start new turn normally
```

## Verification

1. Run existing tests: `python -m pytest tests/test_conversation.py -v`
2. Simulate echo: SAINT says "Hello, how can I help?" → mock STT returns "Hello how can I" → should be suppressed
3. Simulate near-echo: SAINT says "Python dictionaries store data" → STT returns "dictionaries that store data" → should be suppressed (overlap > 50%)
4. Simulate genuine: SAINT says "Hello" → STT returns "Tell me about Lua" → should NOT be suppressed
5. Verify stop commands still work within suppression window: "stop" during AI response → interrupts (not suppressed as echo)
