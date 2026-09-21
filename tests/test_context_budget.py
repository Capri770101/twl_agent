from agent.agent import _trim_history_for_llm
from agent.toolkit import to_openai_tools


def test_history_budget_keeps_recent_messages_in_order():
    history = [{'role': 'user', 'content': '旧' * 20}, {'role': 'assistant', 'content': '中' * 20}, {'role': 'user', 'content': '新' * 20}]
    result = _trim_history_for_llm(history, 25)
    assert result == [{'role': 'user', 'content': '新' * 20}]


def test_history_budget_does_not_split_a_message():
    result = _trim_history_for_llm([{'role': 'user', 'content': 'a' * 12}, {'role': 'assistant', 'content': 'b' * 12}], 24)
    assert result[-1]['content'] == 'b' * 12
    assert sum(len(m['content']) for m in result) <= 24


def test_compact_tools_preserve_schema_and_reduce_long_descriptions():
    full = to_openai_tools()
    compact = to_openai_tools(compact=True)
    assert [x['function']['name'] for x in full] == [x['function']['name'] for x in compact]
    assert all(x['function']['parameters'] == y['function']['parameters'] for x, y in zip(full, compact))
    assert sum(len(str(x)) for x in compact) < sum(len(str(x)) for x in full)
