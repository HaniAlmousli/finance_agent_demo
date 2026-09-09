"""
Unit tests for the session-memory trim policy (agent/langgraph_bedrock.py).

The checkpointer itself is LangGraph's (nothing of ours to test); what IS
ours is the replay-bounding call: history must be capped, the system message
kept, and a trim must NEVER split an assistant tool_use from its tool_result
(Bedrock rejects orphaned pairs). These tests exercise the exact trim_messages
configuration the agent uses.

Run (langchain-core is not an infra dependency — pull it for the run):
    uv run --with pytest --with langchain-core python -m pytest tests/ -q
"""

import pytest

lc = pytest.importorskip("langchain_core.messages",
                         reason="trim tests need langchain-core "
                                "(run with --with langchain-core)")
from langchain_core.messages import (AIMessage, HumanMessage,  # noqa: E402
                                     SystemMessage, ToolMessage, trim_messages)

MAX_HISTORY_MESSAGES = 24   # keep in sync with agent/langgraph_bedrock.py


def agent_trim(messages):
    """The EXACT call the agent's chatbot node makes."""
    return trim_messages(messages, strategy="last", token_counter=len,
                         max_tokens=MAX_HISTORY_MESSAGES,
                         start_on="human", include_system=True)


def turn(i, with_tool=False):
    """One conversation turn: human -> (tool round ->) final answer."""
    msgs = [HumanMessage(content=f"question {i}")]
    if with_tool:
        msgs += [AIMessage(content="", tool_calls=[
                     {"name": "get_weather", "args": {"country": "Jordan"},
                      "id": f"call-{i}"}]),
                 ToolMessage(content="sunny", tool_call_id=f"call-{i}")]
    msgs.append(AIMessage(content=f"answer {i}"))
    return msgs


def build_history(n_turns, with_tools=True):
    msgs = [SystemMessage(content="system prompt")]
    for i in range(n_turns):
        msgs += turn(i, with_tool=with_tools and i % 2 == 0)
    return msgs


def test_short_history_untouched():
    h = build_history(2)
    assert agent_trim(h) == h


def test_long_history_is_capped():
    trimmed = agent_trim(build_history(20))
    assert len(trimmed) <= MAX_HISTORY_MESSAGES + 1   # +1: the system message


def test_system_message_survives_trim():
    trimmed = agent_trim(build_history(20))
    assert isinstance(trimmed[0], SystemMessage)


def test_newest_turns_kept_oldest_dropped():
    trimmed = agent_trim(build_history(20))
    texts = " ".join(m.content for m in trimmed
                     if isinstance(m, (HumanMessage, AIMessage)) and m.content)
    assert "question 19" in texts and "answer 19" in texts
    assert "question 0" not in texts


def test_trim_never_orphans_a_tool_result():
    """The failure this guards: a trim boundary landing between a tool_use
    and its tool_result. After trimming, every ToolMessage must be preceded
    by the AIMessage carrying the matching tool_call id, and the first
    non-system message must be a HumanMessage (start_on='human')."""
    for n in range(5, 25):   # sweep boundaries across many history lengths
        trimmed = agent_trim(build_history(n))
        body = trimmed[1:] if isinstance(trimmed[0], SystemMessage) else trimmed
        assert body, f"n={n}: trim returned no conversation"
        assert isinstance(body[0], HumanMessage), f"n={n}: must start on human"
        for j, m in enumerate(body):
            if isinstance(m, ToolMessage):
                prev = body[j - 1]
                assert isinstance(prev, AIMessage) and any(
                    tc["id"] == m.tool_call_id for tc in prev.tool_calls), \
                    f"n={n}: orphaned tool_result at position {j}"
