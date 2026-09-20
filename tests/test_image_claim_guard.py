"""生图声明护栏回归门（2026-09-16 线上缺陷：谎称已出图）。

线上实测（演示实例，连续两轮）：
  · 用户「出个效果图」→ 模型**零工具调用**、2.6 秒回「效果图任务已提交（AI 生成中…）」；
  · 用户追问「图呢？」→ 又回「这张是按「星河长明」的花材构成生成的参考图」。

**全程没有任何生图任务**，用户在等一张根本不存在的图。与「未查证即作答」同类：
光靠 prompt 不够，必须有确定性拦截。

保护 `_image_task_created` / `_claims_image_done` / `_needs_image_nudge` /
`_finalize_image_claim`（纯函数，不连 LLM / DB / 网络）。
"""
from __future__ import annotations

import json

from agent.agent import (
    _claims_image_done,
    _finalize_image_claim,
    _image_task_created,
    _needs_image_nudge,
)


class _TC:
    """最小工具调用记录替身（只需 name/status/result）。"""

    def __init__(self, name: str, status: str = 'ok', result: object = None) -> None:
        self.name = name
        self.status = status
        self.result = json.dumps(result) if isinstance(result, dict) else (result if result is not None else '{}')
        self.arguments = {}


def _img_ok(task_id: str = 't1') -> _TC:
    return _TC('generate_effect_image', result={'task_id': task_id, 'poll': '/tasks/t1'})


# 线上那两句原话
CLAIM_SUBMITTED = '效果图任务已提交（AI 生成中，通常几十秒到几分钟）。'
CLAIM_REFERENCE = '这张是按「星河长明」的花材构成生成的参考图，整体走清冷星空感的蓝色调。'


# ── _image_task_created ──────────────────────────────────────────────

def test_task_created_requires_task_id():
    assert _image_task_created([_img_ok()]) is True


def test_task_created_false_without_task_id():
    """调用但返回 error（没有 task_id）不算提交成功。"""
    assert _image_task_created([_TC('generate_effect_image', result={'error': '未找到可生图的方案'})]) is False


def test_task_created_false_on_failure_or_other_tools():
    assert _image_task_created([_TC('generate_effect_image', status='error', result={'task_id': 'x'})]) is False
    assert _image_task_created([_TC('generate_diy_plan', result={'plan_id': 'p1'})]) is False
    assert _image_task_created([]) is False
    assert _image_task_created(None) is False


# ── _claims_image_done ───────────────────────────────────────────────

def test_claims_detected_on_real_transcript():
    assert _claims_image_done(CLAIM_SUBMITTED)
    assert _claims_image_done(CLAIM_REFERENCE)
    assert _claims_image_done('效果图已生成，请看上方卡片。')


def test_claims_ignored_when_honestly_reporting_failure():
    """如实说明失败不能被当成「声称已出图」，否则会把正确行为拦掉。"""
    assert not _claims_image_done('抱歉，这次效果图没有生成成功，我还没找到可生图的方案。')
    assert not _claims_image_done('效果图暂时无法生成，需要你先定一个方案。')


def test_claims_ignored_without_image_topic():
    assert not _claims_image_done('这个方案还没有生成，我先帮你调整配色。')
    assert not _claims_image_done('')
    assert not _claims_image_done(None)


def test_claims_ignored_for_plain_generation_words():
    """⚠️ 重要回归：早期版本用「提到图」+「提到生成」的宽松共现，把普通回复也误伤成
    「谎称已出图」——线上实测用户问「目前这个数据库的信息包括哪些？」被白拦一轮、
    多烧 30 秒重答。这里锁住「只认图词与完成态的**邻近**组合」。"""
    assert not _claims_image_done('我已经生成了方案，共 11 枝玫瑰。')
    assert not _claims_image_done('正在生成你的专属方案，稍等一下。')
    assert not _claims_image_done('方案已生成，明细在卡片里。')
    assert not _claims_image_done('这个方案是我根据你的预算生成的，可以再调。')
    assert not _claims_image_done('数据库里包括商品、店铺、价格、库存这些字段。')


def test_claims_detected_on_colloquial_phrasings():
    """⚠️ 回归（2026-09-20 **生产**实测漏判）：原词表只认书面说法（已提交/已生成/生成中…），
    用户说「就这款了，帮我生成效果图」，模型**零生图任务**却回
    「效果图我也同步在跑了」——护栏放行，用户在等一张根本不存在的图。

    补齐两类：①「已经 + 动词」（原表只认「已提交」，不认「已经提交」）；
    ② 口语进行态（在跑 / 跑起来 / 已安排 / 正在处理）。"""
    # 生产那两句原话
    assert _claims_image_done('就它了！效果图我也同步在跑了。')
    assert _claims_image_done('效果图已经提交，稍等就好。')
    # 同类口语变体
    assert _claims_image_done('效果图跑起来了，稍等片刻。')
    assert _claims_image_done('效果图已安排上了。')
    assert _claims_image_done('效果图正在处理，马上就好。')


def test_claims_ignored_for_explanatory_in_progress():
    """⚠️ 反例（放宽词表时最容易踩的误伤）：**解释句**里的「在生成」不能算声称。
    ——「在生成」能出现在「效果图在生成时会用到方案里的花材」这类解释里，
    所以口语进行态只收「在跑 / 跑起来」这类不可能用于解释的动词。"""
    assert not _claims_image_done('效果图在生成时会用到方案里的花材，所以要先定方案。')
    assert not _claims_image_done('效果图生成需要先确认配色，我们再调一版？')


# ── _needs_image_nudge ───────────────────────────────────────────────

def test_nudge_fires_when_claiming_without_task():
    assert _needs_image_nudge([], CLAIM_SUBMITTED) is True
    assert _needs_image_nudge([], CLAIM_REFERENCE) is True


def test_nudge_silent_with_real_task():
    """真的提交了任务 → 说「生成中」完全正常。"""
    assert _needs_image_nudge([_img_ok()], CLAIM_SUBMITTED) is False


def test_nudge_silent_when_honest():
    assert _needs_image_nudge([], '效果图还没生成成功，抱歉～') is False
    assert _needs_image_nudge([], '好的，你想送谁呢？') is False


# ── _finalize_image_claim（确定性兜底）────────────────────────────────

def test_finalize_replaces_false_claim():
    out = _finalize_image_claim(CLAIM_REFERENCE, [])
    assert out != CLAIM_REFERENCE
    assert '没有真的生成成功' in out
    assert '参考图' not in out


def test_finalize_keeps_reply_with_real_task():
    assert _finalize_image_claim(CLAIM_SUBMITTED, [_img_ok()]) == CLAIM_SUBMITTED


def test_finalize_keeps_honest_reply():
    honest = '效果图还没生成成功，我再试一次。'
    assert _finalize_image_claim(honest, []) == honest
