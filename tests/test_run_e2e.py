"""端到端 `run()` 集成测试（2026-09-20 新增，外部 code review 第 12 条）。

## 为什么需要这个文件

review 指出的最严重问题不是「函数长」，而是**组合爆炸没有测试兜底**：
`run()` 362 行 + `_post_process()` 276 行 + 16 个护栏函数 + 10+ 个 session flag，
任何一处改动都可能引发连锁反应，而当时**没有任何测试真正跑过完整 `run()`**
（`test_stream_hang.py` 把 `run` 本身 mock 掉了，其余测试都是纯函数单测）。

本文件是**后续所有重构的安全网**：mock 掉 LLM 与存储，但**让主循环真跑**，
从用户消息一路走到 `ChatResponse`，断言最终 `ui / data / reply`。

## 与其它测试的分工

- `test_stream_llm.py`：只测 `_stream_llm` 这一个环节（工具轮独白不推送）
- `test_card_nudge.py` 等：只测单个护栏**纯函数**
- **本文件**：测「编排」—— 多轮 LLM → 工具 → 护栏 → 清理链 → 出参 的完整链路

## mock 边界

- `mem_store`（15 个方法）→ 内存实现，不连 DB
- `call_llm_stream` → 按调用次数返回预置轮次，不调 LLM
- `execute_tool` → 固定返回，不连平台/不发网络
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent.agent as A  # noqa: E402
from agent.agent import ReActAgent  # noqa: E402


# ── chunk / tool_call 构造（与 test_stream_llm 同款，保持独立不互相依赖）──

def _chunk(content: str | None = None, tool_calls: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content, tool_calls=tool_calls)
    )])


def _tc(index: int = 0, id: str | None = None, name: str | None = None,
        args: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(index=index, id=id,
                           function=SimpleNamespace(name=name, arguments=args))


# ── mem_store 的内存替身 ──

class FakeMemory:
    """`run()` / `_post_process()` 用到的 mem_store 方法的**内存实现**。

    ⚠️ 用 `(*a, **k)` 宽松签名：本测试关心的是**编排行为**（轮次 / 护栏 / 清理 / 出参），
    不是存储细节。只实现被真正调用的方法（数量见 grep `mem_store.` 的结果）。
    """

    def __init__(self) -> None:
        self.ctx: dict[str, dict] = {}
        self.stage: dict[str, str] = {}
        self.flags: dict[tuple[str, str], str] = {}
        self.req: dict[str, object] = {}
        self.history: list[dict] = []
        self.saved: list[dict] = []

    async def get_or_create_session(self, user_id, session_id=None, **kw):
        sid = session_id or 'sid_test'
        self.ctx.setdefault(sid, {k: v for k, v in kw.items() if v})
        return sid

    async def get_session_context(self, sid):
        return dict(self.ctx.get(sid, {}))

    async def get_stage(self, sid):
        return self.stage.get(sid, 'analyze')

    async def update_stage(self, sid, stage):
        self.stage[sid] = getattr(stage, 'value', str(stage))

    async def create_conversation(self, user_id, title='', **kw):
        sid = 'sid_new'
        self.ctx[sid] = {k: v for k, v in kw.items() if v}
        return sid

    async def get_requirement(self, sid):
        return self.req.get(sid)

    async def set_requirement(self, sid, requirement):
        self.req[sid] = requirement

    async def get_long_term(self, user_id):
        return {}

    async def load_history(self, sid, limit=20):
        return list(self.history)

    async def load_display_messages(self, *a, **k):
        return []

    async def save_messages(self, *a, **k):
        self.saved.append({'args': a, 'kwargs': k})

    async def set_session_flag(self, user_id, sid, key, value='1'):
        self.flags[(sid, key)] = value

    async def get_session_flag(self, user_id, sid, key):
        return self.flags.get((sid, key))

    async def clear_session_flags(self, user_id, sid, prefix=''):
        for sk in [k for k in self.flags if k[0] == sid and k[1].startswith(prefix)]:
            self.flags.pop(sk, None)

    async def get_session_json(self, uid, sid, key):
        return {}


# ── 测试脚手架 ──

def _install(monkeypatch, turns: list[list], *, tool_results: dict | None = None,
             facts: str = '') -> FakeMemory:
    """装好 mock：内存存储 / 轮次化 LLM / 固定工具结果。返回 FakeMemory 供断言。"""
    fake = FakeMemory()
    monkeypatch.setattr(A, 'mem_store', fake, raising=False)
    monkeypatch.setattr(A, '_find_pending_image_task',
                        lambda *a, **k: _async_none(), raising=False)
    monkeypatch.setattr(A, '_platform_fact_hint', lambda m: facts, raising=False)
    monkeypatch.setattr(A, '_platform_source_ids', lambda: (['aistore'] if facts else []), raising=False)

    calls = {'n': 0}

    def fake_stream(*a, **k):
        i = calls['n']
        calls['n'] += 1
        return iter(turns[min(i, len(turns) - 1)])

    monkeypatch.setattr(A, 'call_llm_stream', fake_stream, raising=False)

    async def fake_execute(name, arguments, ctx=None):
        # ⚠️ execute_tool 的契约是返回 (result, status) 二元组（run() 里直接解包），
        # 不是裸的结果字符串 —— 集成测试第一次就因为这里少了一层而报
        # "too many values to unpack"，正好说明这类测试能逼出真实的接口契约。
        val = (tool_results or {}).get(name)
        if callable(val):
            # 同一个工具有多轮不同参数时用回调（如 platform_db_query_entity 换关键词查两次）
            out = val(arguments)
            return out if isinstance(out, tuple) else (out, 'ok')
        if val is not None:
            return val, 'ok'
        return '{}', 'ok'

    monkeypatch.setattr(A, 'execute_tool', fake_execute, raising=False)
    fake.llm_calls = calls  # type: ignore[attr-defined]
    return fake


async def _async_none():
    return None


def _run(message: str, **kw) -> tuple[object, list[dict]]:
    """跑一轮完整 `run()`，返回 `(ChatResponse, sse_events)`。

    ⚠️ **必须传 `on_event`**：`run()` 有两条 LLM 路径 ——
    有 `on_event` 时走流式 `_stream_llm`（**生产 SSE 的真实路径**），
    没有时走非流式 `call_llm`（`/chat` 同步接口）。
    本文件要覆盖生产主路径，故显式传 `on_event`；否则会绕开被测代码、
    还得额外 mock 第二个 LLM 入口（`call_llm`）。
    """
    agent = ReActAgent()
    events: list[dict] = []
    resp = asyncio.run(asyncio.wait_for(
        agent.run('u_test', message, 'sid_test', None, on_event=events.append, **kw),
        timeout=20))
    return resp, events


def _terminal(name: str, payload: str, call_id: str = 'c1') -> list:
    """构造一个「终结工具」轮次（终结工具不执行，参数即本轮结论）。"""
    return [_chunk(tool_calls=[_tc(0, id=call_id, name=name, args=payload)])]


# ── 场景 1：纯文字回复（happy path）──

def test_plain_text_reply(monkeypatch):
    """LLM 调 respond_to_user → 出参 ui=text、reply 原样保留、session_id 有值。"""
    turns = [_terminal('respond_to_user',
                       '{"reply": "你好呀，我是你的专属花艺小助手～", "ui": "text", "data": {}, '
                       '"stage": "analyze", "intent": "chitchat"}')]
    _install(monkeypatch, turns)
    resp, events = _run('你好')
    assert resp.session_id, 'run() 必须回填 session_id'
    assert getattr(resp.ui, 'value', resp.ui) == 'text'
    assert '花艺小助手' in (resp.reply or '')


# ── 场景 2：DIY 方案卡（多轮 + 工具执行）──

def test_diy_plan_card_flow(monkeypatch):
    """第 1 轮调 generate_diy_plan → 第 2 轮 show_plan_card → 出参 ui=plan_card 且 plans 非空。"""
    plan = {
        'plan_id': 'DIY_test1', 'name': '静默初心', 'price': 178,
        'desc': '洋桔梗×4 配香槟玫瑰',
        'design': {'main_flowers': [{'name': '洋桔梗', 'qty': 4}], 'color_scheme': ['香槟']},
    }
    turns = [
        _terminal('generate_diy_plan', '{"requirements": "送妈妈的花，预算300"}', 'c1'),
        _terminal('show_plan_card',
                  '{"plans": [' + __import__('json').dumps(plan, ensure_ascii=False) + '], '
                  '"reply": "给你配好了这束～", "stage": "view_plan"}', 'c2'),
    ]
    import json as _json
    _install(monkeypatch, turns, tool_results={'generate_diy_plan': _json.dumps(plan, ensure_ascii=False)})
    resp, events = _run('送妈妈的花，预算300')
    assert getattr(resp.ui, 'value', resp.ui) == 'plan_card', f'期望出卡，实得 {resp.ui}'
    plans = (resp.data or {}).get('plans') or []
    assert plans, 'plan_card 的 data.plans 不能为空'
    assert plans[0].get('name') == '静默初心'


# ── 场景 3：护栏拦截真的生效（零工具调用 + 平台事实）──

def test_platform_guard_triggers_retry(monkeypatch):
    """模型没查平台就答「平台有哪些店」→ 必须注入纠正并要求重答（LLM 被调 ≥ 2 次）。

    对应 review 第 4 条关注的「护栏与 LLM 拉锯」：这里断言护栏**确实触发过一次**，
    且第二轮收到纠正后能正常收口（不能无限拉锯）。
    """
    turns = [
        [_chunk('平台上有三家店：A、B、C。')],                      # 违规：未查证即答
        _terminal('respond_to_user',
                  '{"reply": "我查了一下平台在售店铺，结果如下。", "ui": "text", "data": {}, '
                  '"stage": "analyze", "intent": "qa"}', 'c2'),
    ]
    fake = _install(monkeypatch, turns, facts='shop')
    resp, events = _run('平台上有哪些店铺？')
    assert fake.llm_calls['n'] >= 2, '护栏应注入纠正并要求重答（LLM 被调 ≥ 2 次）'
    assert resp.reply, '重答后必须有回复'


# ── 场景 4：清理链端到端生效 ──

def test_cleanup_chain_scrubs_reply(monkeypatch):
    """最终 reply 必须已过清理链：危险标签转义、内部术语/工具名不泄漏、独立分隔线删除。

    对应 review 第 5 条（流式推的是原始输出，**done 才是清理后的最终版**）——
    这里锁住「最终版确实被清理过」这一契约。
    """
    dirty = ('你好<script>alert(1)</script>，我用 platform_db_query_entity 查了一下。\\n'
             '---\\n结果如上。')
    turns = [_terminal('respond_to_user',
                       '{"reply": ' + __import__('json').dumps(dirty, ensure_ascii=False) +
                       ', "ui": "text", "data": {}, "stage": "analyze", "intent": "qa"}')]
    _install(monkeypatch, turns)
    resp, events = _run('随便问问')
    reply = resp.reply or ''
    assert '<script>' not in reply, 'XSS 兜底失效'
    assert 'platform_db_query_entity' not in reply, '内部工具名泄漏'
    assert '\n---\n' not in reply and not reply.startswith('---'), '独立分隔线未清理'
    assert reply, '清理后不能为空（_ensure_non_empty_reply 应兜住）'


# ── 场景 5：卡片永远与文案对齐（取数并集 + 数量回写）──

def _query_rows(*names: str) -> str:
    import json as _json
    return _json.dumps({'ok': True, 'data': [{'name': n, 'price': 100} for n in names]},
                       ensure_ascii=False)


def test_product_card_matches_reply_across_query_rounds(monkeypatch):
    """⚠️ 2026-09-20 生产回归（端到端）：模型分两轮换关键词查商品，文案把两轮结果都点了名。

    线上原症状：「我挑了 4 款…（月光漫步 / 热恋代码 / 郁见你 / 网红Kitty）」却只渲染 **2 张**卡，
    且其中一张还是文案没提过的款 —— 因为卡片只取**最后一次**查询的结果。

    这里断言完整契约的两半：
      ① 卡片必须包含**两轮查询**里被点名的商品（不能只取最后一轮）；
      ② 文案自报的数量必须等于卡片实际条数（模型那个数字是猜的）。
    """
    table = {'第一轮': ('月光漫步', '热恋代码'), '第二轮': ('网红Kitty', '花漾岁月')}

    def _q(arguments):
        return _query_rows(*table.get(str((arguments or {}).get('keyword') or ''), ()))

    turns = [
        [_chunk(tool_calls=[_tc(0, id='c1', name='platform_db_query_entity',
                                args='{"source_id": "aistore", "entity": "plan", "keyword": "第一轮"}')])],
        [_chunk(tool_calls=[_tc(0, id='c2', name='platform_db_query_entity',
                                args='{"source_id": "aistore", "entity": "plan", "keyword": "第二轮"}')])],
        _terminal('respond_to_user',
                  '{"reply": "我挑了 4 款都在预算内：月光漫步、网红Kitty", '
                  '"stage": "view_plan", "intent": "buying"}', 'c3'),
    ]
    _install(monkeypatch, turns, tool_results={'platform_db_query_entity': _q})
    resp, events = _run('送女朋友一束花，预算200左右')
    assert getattr(resp.ui, 'value', resp.ui) == 'plan_card', f'期望出商品卡，实得 {resp.ui}'
    names = [p.get('name') for p in (resp.data or {}).get('plans') or []]
    assert names == ['月光漫步', '网红Kitty'], f'卡片必须含两轮里被点名的款，实得 {names}'
    reply = resp.reply or ''
    assert '4 款' not in reply, '文案自报数量必须与卡片条数一致'
    assert '2 款' in reply, f'数字应被回写为 2 款，实得：{reply}'


# ── 场景 6：方案卡 + 待出图（卡片不能被生图任务顶掉）──

def test_diy_card_survives_pending_image_task(monkeypatch):
    """⚠️ 2026-09-20 生产回归：**方案卡 + 待出图**（生图任务还没出图）时，卡片不能被丢掉。

    线上原症状：这一轮 `ui` 被降级成 `text`、`data` 只剩 `task_id`，已推导出的方案卡
    **整个消失**，而文案仍在说「方案留在卡片上」「点卡片可以直接下单」——
    用户看不到任何卡片（呈现与文案不一致）。

    根因：`_post_process` 里那段「生图任务接管 UI」的兜底**无条件**执行，
    覆盖掉了 `_derive_ui` 已经做对的「卡片优先」（09-16 修过一次同类问题，这里是更后一步）。
    """
    import json as _json
    plan = {'plan_id': 'DIY_x', 'name': '温柔回响', 'price': 299,
            'design': {'main_flowers': [{'name': '康乃馨', 'qty': 60}]}}
    turns = [
        _terminal('generate_diy_plan', '{"requirements": "送妈妈，粉色系，预算300"}', 'c1'),
        [_chunk(tool_calls=[_tc(0, id='c2', name='generate_effect_image',
                                args='{"plan": "latest_diy"}')])],
        _terminal('respond_to_user',
                  '{"reply": "「温柔回响」这版方案留在卡片上，效果图任务已提交。", '
                  '"stage": "view_plan", "intent": "design"}', 'c3'),
    ]
    _install(monkeypatch, turns, tool_results={
        'generate_diy_plan': _json.dumps(plan, ensure_ascii=False),
        'generate_effect_image': _json.dumps({'task_id': 't1', 'poll': '/tasks/t1'}),
    })
    resp, _ = _run('就这个，帮我出一张效果图')
    assert getattr(resp.ui, 'value', resp.ui) == 'plan_card', f'方案卡不能被待出图顶掉，实得 {resp.ui}'
    data = resp.data or {}
    assert (data.get('plans') or []), '方案卡内容不能为空'
    assert data.get('task_id') == 't1', '生图任务信息应挂到卡片 data 上（前端据此轮询补图）'
