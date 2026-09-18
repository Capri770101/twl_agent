"""流式 LLM 推送逻辑测试（2026-09-18 外部安全审计 P0-1）。

**核心断言：工具轮里模型写的"过程说明"绝不能推给用户。**

为什么：实测（探针脚本）发现模型会先自言自语一大段再调工具 ——
「我注意到工具要求必须提供 source_id 参数，但我没有这个信息。…让他们选择。」
若直接边收边推，等于把内部独白**实时直播**给用户，比现在（被 `_finalize_reasoning_leak`
拦下）更糟。所以 `_stream_llm` 设了头部缓冲 + tool_calls 检测 + 独白检测三层保护。
"""

from __future__ import annotations

from types import SimpleNamespace

import agent.agent as A


def _chunk(content: str | None = None, tool_calls: list | None = None) -> SimpleNamespace:
    """构造一个 OpenAI 兼容的流式 chunk。"""
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content, tool_calls=tool_calls)
    )])


def _tc(index: int = 0, id: str | None = None, name: str | None = None,
        args: str | None = None) -> SimpleNamespace:
    """构造一个 tool_calls delta（参数是**分片累积**的，与真实流一致）。"""
    return SimpleNamespace(index=index, id=id,
                           function=SimpleNamespace(name=name, arguments=args))


# 工具轮：先一大段自言自语（实测原文），最后才发 tool_calls
TOOL_TURN = [
    _chunk('我注意到工具要求必须提供 source_id 参数，但我没有这个信息。'),
    _chunk('我可以尝试用一些常见的值，或者直接向用户询问。既然用户的问题比较宽泛，'),
    _chunk('我应该先问清楚他们指的是哪个平台。\n\n'),
    _chunk(tool_calls=[_tc(0, id='call_1', name='platform_db_query_entity', args='{"entity": ')]),
    _chunk(tool_calls=[_tc(0, args='"plan", "source_id": "aistore"}')]),
]

# 纯回复轮：直接给用户文字
REPLY_TURN = [
    _chunk('给你挑了几款现成款，都在你的预算内：'),
    _chunk('玫瑰恋人 298 元，52 朵粉玫瑰，正好卡你的预算；'),
    _chunk('深情的爱 298 元，52 朵红玫瑰配满天星，热烈直白。'),
]

# 独白轮：模型把推理当回复（无 tool_calls）
RANT_TURN = [
    _chunk('用户说想要送妈妈的花，这是一个常见的送礼场景，我需要判断意图。'),
    _chunk('我应该用 respond_to_user 来回复一句，然后保持简洁。'),
]


def test_tool_turn_text_is_never_pushed(monkeypatch) -> None:
    """⚠️ 本文件最重要的一条：工具轮的自言自语不能出现在 text_delta 里。"""
    monkeypatch.setattr(A, 'call_llm_stream', lambda *a, **k: iter(TOOL_TURN))
    events: list[dict] = []
    msg = A.ReActAgent._stream_llm([], events.append, 30)

    deltas = [e for e in events if e.get('event') == 'text_delta']
    assert deltas == [], f'工具轮的过程说明被推给用户了：{deltas}'
    assert msg.content == '', '工具轮的构思文字应被丢弃（不能作为最终回复）'


def test_tool_calls_accumulated_across_chunks(monkeypatch) -> None:
    """工具参数是分片到达的，必须正确拼接成完整 JSON。"""
    monkeypatch.setattr(A, 'call_llm_stream', lambda *a, **k: iter(TOOL_TURN))
    msg = A.ReActAgent._stream_llm([], lambda e: None, 30)

    assert msg.tool_calls, 'tool_calls 丢了'
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].function.name == 'platform_db_query_entity'
    assert '"plan"' in msg.tool_calls[0].function.arguments
    # 构造出的对象要能被既有解析器吃下
    parsed = A.ReActAgent._parse_tool_calls(msg)
    assert parsed[0]['name'] == 'platform_db_query_entity'
    assert parsed[0]['arguments'].get('entity') == 'plan'


def test_reply_turn_is_streamed(monkeypatch) -> None:
    """纯回复轮要把文字逐段推出去（没有它，流式就没意义）。"""
    monkeypatch.setattr(A, 'call_llm_stream', lambda *a, **k: iter(REPLY_TURN))
    events: list[dict] = []
    msg = A.ReActAgent._stream_llm([], events.append, 30)

    deltas = [e['content'] for e in events if e.get('event') == 'text_delta']
    assert deltas, '纯回复轮的逐字推送丢了'
    assert ''.join(deltas) == msg.content
    assert msg.tool_calls is None


def test_head_buffer_defers_tiny_reply(monkeypatch) -> None:
    """头部缓冲：不足阈值的极短回复不推（避免工具轮开头就漏出去）。"""
    monkeypatch.setattr(A, 'call_llm_stream', lambda *a, **k: iter([_chunk('好的～')]))
    events: list[dict] = []
    A.ReActAgent._stream_llm([], events.append, 30)
    assert [e for e in events if e.get('event') == 'text_delta'] == []


def test_process_narration_blocked_before_push(monkeypatch) -> None:
    """独白轮：**推送之前**就该拦住 —— 等推出去再回滚，用户已经看到了。"""
    monkeypatch.setattr(A, 'call_llm_stream', lambda *a, **k: iter(RANT_TURN))
    events: list[dict] = []
    A.ReActAgent._stream_llm([], events.append, 30)
    assert [e for e in events if e.get('event') == 'text_delta'] == [], \
        '独白在推送前就应被粗筛拦住'


# 前半段是正常推荐（须超过头部缓冲阈值，否则本来就不会推），后半段突然变成"过程说明"
MIXED_TURN = [
    _chunk('给你挑了几款现成款，都在你的预算范围内，价格从六十八到一百五十八都有，你看看哪款更合适，'),
    _chunk('我应该先确认一下你的偏好再继续推荐。'),
]


def test_already_pushed_text_is_rolled_back(monkeypatch) -> None:
    """兜底路径：已经推出去一段（前半段正常），之后才命中过程说明 → 必须回滚。"""
    monkeypatch.setattr(A, 'call_llm_stream', lambda *a, **k: iter(MIXED_TURN))
    events: list[dict] = []
    A.ReActAgent._stream_llm([], events.append, 30)

    kinds = [e.get('event') for e in events]
    assert 'text_delta' in kinds, '前半段是正常文字，应该推出去'
    assert 'text_rollback' in kinds, '命中过程说明后必须回滚'
    assert kinds.index('text_rollback') > kinds.index('text_delta'), '回滚必须在推送之后'


def test_empty_choices_chunk_is_safe(monkeypatch) -> None:
    """真实流里会出现没有 choices 的 chunk（实测踩到 IndexError），必须跳过而不崩。"""
    chunks = [
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices=None),
        _chunk('正常文字一二三四五六七八九十'),
        _chunk('继续补齐到超过头部缓冲阈值以上一点点'),
    ]
    monkeypatch.setattr(A, 'call_llm_stream', lambda *a, **k: iter(chunks))
    msg = A.ReActAgent._stream_llm([], lambda e: None, 30)
    assert msg.content
    assert msg.tool_calls is None


# ── 防回归：主循环的流式分支／非流式分支不能交叉引用变量 ────────────────────────
#
# 真实事故（2026-09-18 部署后第一次实测就炸）：把 `resp = call_llm(...)` 改成
# `if on_event: msg = _stream_llm(...) else: resp = call_llm(...)` 之后，
# **紧随其后残留了旧代码 `msg = resp.choices[0].message`** —— 流式分支下 `resp`
# 未赋值，抛 UnboundLocalError，SSE 直接回 error 事件、用户什么都看不到。
# 这类"分支变量交叉引用"单元测试很难覆盖（要跑整个 run()），但静态可查。

def test_run_has_no_stale_resp_assignment() -> None:
    """`msg = resp...` 只应出现 **1 处**（else 非流式分支里）。

    事故复盘：改成双分支后，紧随其后残留了一行同名的 `msg = resp.choices[0].message`
    —— 它在 else 分支之外，缩进与 if 同级，**流式路径下必然 UnboundLocalError**。
    这里用"出现次数"作判据（残留会变成 2 处），比缩进判断稳健。
    """
    import inspect
    import re

    src = inspect.getsource(A.ReActAgent.run)
    hits = re.findall(r'^\s*msg\s*=\s*resp\b', src, re.M)
    assert len(hits) <= 1, f'出现 {len(hits)} 处 `msg = resp...`，流式路径下会 UnboundLocalError'


def test_run_assigns_msg_in_both_branches() -> None:
    """两个分支都必须给 msg 赋值，否则后续解析必然出错。"""
    import inspect
    import re

    src = inspect.getsource(A.ReActAgent.run)
    assert re.search(r'msg\s*=\s*self\._stream_llm\(', src), '流式分支没给 msg 赋值'
    assert re.search(r'resp\s*=\s*call_llm\(', src), '非流式分支没调 call_llm'
    assert re.search(r'msg\s*=\s*resp\.choices\[0\]\.message', src), '非流式分支没给 msg 赋值'


# ── 终结工具 reply 参数的流式解析 ─────────────────────────────────────────
#
# 关键认知（部署后实测才发现，值得单独记一笔）：flora 的最终回复**不走 LLM 的
# `content`**，而是通过终结工具 `respond_to_user(reply="...")` 的 **JSON 参数**传回。
# 实测三轮 LLM 调用：`content` 全是 0 字、`tool_calls` 全是 1 个。
# 所以真流式的**主战场是解析工具参数**，不是读 content —— 这是第一版实现没生效的原因。

def test_extract_complete_string() -> None:
    assert A._extract_partial_json_string('{"reply": "你好呀", "ui": "text"}', 'reply') == '你好呀'


def test_extract_unterminated_string() -> None:
    """流式中途 JSON 尚未闭合，也要能拿到已确定的部分。"""
    assert A._extract_partial_json_string('{"reply": "你好呀，我是花', 'reply') == '你好呀，我是花'


def test_extract_handles_escapes() -> None:
    assert A._extract_partial_json_string(r'{"reply": "第一行\n第二行"}', 'reply') == '第一行\n第二行'
    assert A._extract_partial_json_string(r'{"reply": "他说\"好的\""}', 'reply') == '他说"好的"'


def test_extract_missing_or_wrong_type() -> None:
    ex = A._extract_partial_json_string
    assert ex('{"ui": "text"}', 'reply') == ''      # 没这个字段
    assert ex('{"reply": 123}', 'reply') == ''      # 不是字符串
    assert ex('', 'reply') == ''
    assert ex('{"reply": ', 'reply') == ''          # 值还没开始


def test_extract_trailing_backslash_not_emitted() -> None:
    """转义符只收到一半（以 \\ 结尾）→ 不能把它当字符吐出去。"""
    assert A._extract_partial_json_string('{"reply": "abc\\', 'reply') == 'abc'


# 终结工具 reply 参数的增量推送（模拟真实的分片到达）
TERMINAL_TURN = [
    _chunk(tool_calls=[_tc(0, id='c1', name='respond_to_user', args='{"reply": "送妈')]),
    _chunk(tool_calls=[_tc(0, args='妈的话，粉色康乃馨')]),
    _chunk(tool_calls=[_tc(0, args='最合适。", "ui": "text"}')]),
]


def test_terminal_reply_is_streamed(monkeypatch) -> None:
    """⚠️ 核心断言：最终回复写在工具参数里，也必须能逐字推出去。"""
    monkeypatch.setattr(A, 'call_llm_stream', lambda *a, **k: iter(TERMINAL_TURN))
    events: list[dict] = []
    msg = A.ReActAgent._stream_llm([], events.append, 30)

    pushed = ''.join(e['content'] for e in events if e.get('event') == 'text_delta')
    assert pushed, '终结工具的 reply 没有被流式推送（这正是第一版失效的原因）'
    assert pushed == '送妈妈的话，粉色康乃馨最合适。'
    assert getattr(msg, '_pushed', False) is True, '推过之后要标记，避免末尾再整段重复推'
    # 工具调用本身仍要能被解析出来
    assert A.ReActAgent._parse_tool_calls(msg)[0]['name'] == 'respond_to_user'
