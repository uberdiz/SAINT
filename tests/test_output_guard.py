"""Nothing internal (JSON tool calls, tool names, code, unbacked claims) reaches the user."""

from modules.agent.output import (ReplyGuard, clean_reply, extract_tool_calls, honest, pseudo_answer,
                                  HONEST_NO_ACTION)


def _stream(text, chunk=3):
    shown = []
    g = ReplyGuard(shown.append)
    for i in range(0, len(text), chunk):
        g.feed(text[i:i + chunk])
    held = g.finish()
    return "".join(shown), held


def test_prose_streams_through():
    shown, held = _stream("The Burj Khalifa is 828 meters tall.")
    assert shown == "The Burj Khalifa is 828 meters tall." and held == ""


def test_textual_tool_call_is_held_and_parsed():
    text = '{"name": "screen__context", "parameters": {"monitor": "second"}}'
    shown, held = _stream(text)
    assert shown == "" and held == text
    assert extract_tool_calls(held) == [("screen__context", {"monitor": "second"})]


def test_pseudo_call_wrapping_an_answer_is_unwrapped():
    text = '{"name":"prompt","parameters":{"result":"The Burj Khalifa is 828 meters tall."}}'
    (name, args), = extract_tool_calls(text)
    assert pseudo_answer(name, args) == "The Burj Khalifa is 828 meters tall."
    assert clean_reply(text) == "The Burj Khalifa is 828 meters tall."


def test_fenced_json_and_code_removed_unless_asked():
    assert clean_reply('```json\n{"name": "desktop__open_app", "parameters": {"name": "chrome"}}\n```') == ""
    assert "def " not in clean_reply("Here you go:\n```python\ndef f(): pass\n```")
    assert "def f" in clean_reply("```python\ndef f(): pass\n```", user_text="write me a python function")


def test_internal_names_removed():
    assert "composite" not in clean_reply("Done composite:desktop.open_app+desktop.web_search now")
    assert "screen.context" not in clean_reply("I used screen.context to look.")


def test_unbacked_action_claims_are_replaced():
    assert honest("Closed the browser.", tool_succeeded=False) == HONEST_NO_ACTION
    assert honest("Okay, I opened Spotify for you.", tool_succeeded=False) == HONEST_NO_ACTION
    assert honest("Closed the browser.", tool_succeeded=True) == "Closed the browser."
    assert honest("The Burj Khalifa is 828 meters tall.", tool_succeeded=False).startswith("The Burj")


def test_action_claim_is_held_until_checked():
    shown, held = _stream("Closed the browser.")
    assert shown == "" and held == "Closed the browser."


def test_meta_narration_is_trimmed():
    """Voice-first: 'Sure, I'll go do X. The answer is Y.' -> just 'The answer is Y.'"""
    out = clean_reply("Sure, I'll inspect your screen and determine which window is active. "
                      "You're on Chrome.")
    assert out == "You're on Chrome."


def test_think_tags_stripped():
    assert clean_reply("<think>reasoning</think>Skipped.") == "Skipped."


def test_leaked_schema_fragment_stripped():
    out = clean_reply('Playing that. {"tool":"spotify.play", "args":{}}')
    assert "{" not in out and "tool" not in out


def test_vision_tool_identifier_stripped():
    assert "vision__analyze" not in clean_reply("Done using vision__analyze on the second monitor.")


def test_tool_call_later_in_a_prose_reply_is_not_spoken():
    """Logged 2026-09-25: the call streamed through because the reply started as prose."""
    text = ("You can add that as an automation. Here's an example of how you can do this: "
            'automation__schedule_command(command="steam://open/", when="every Monday at 8") '
            "This will open Steam every Monday.")
    shown, held = _stream(text)
    assert "automation__" not in shown and "command=" not in shown
    assert shown.strip() == "You can add that as an automation."    # the run-on sentence with the call is dropped


def test_prose_across_sentences_keeps_its_spacing():
    shown, _ = _stream("Paris is the capital of France. It has about two million people.")
    assert shown == "Paris is the capital of France. It has about two million people."


def test_code_is_kept_when_asked_for():
    shown = []
    g = ReplyGuard(shown.append, user_text="write a python function")
    for tok in ["Sure thing. Here it is:\n", "def add(a, b=1):\n", "    return a + b\n"]:
        g.feed(tok)
    g.finish()
    assert "def add(a, b=1):" in "".join(shown)


def test_schema_shaped_llm_arguments_are_repaired():
    from modules.agent.llm import repair_args

    class T:
        name = "spotify.play_query"
        parameters = {"query": {}, "kind": {}, "own_only": {}}

    assert repair_args(T, {"properties": {"query": "kpop"}, "required": ["query"], "type": "object"}) == \
        {"query": "kpop"}
    assert repair_args(T, {"properties": {"query": {"type": "string", "value": "jazz"}}}) == {"query": "jazz"}
    assert repair_args(T, {"query": "jazz", "kind": "auto"}) == {"query": "jazz", "kind": "auto"}
