"""agent.py —— 智能体主类：ReAct 主循环 + 会话状态机驱动。

核心职责：
1. 载入短期记忆（历史消息）+ 长期记忆（用户偏好），拼成 system prompt。
2. 进入「思考-行动-观察」循环：call_llm → 解析工具调用 → 执行 → 回填 → 再思考，
   直到模型给出最终回复或达到 max_iterations。
3. 根据本轮工具产出推导 UI 焦点（focus，仅前端高亮）并产出结构化 UI（plan_card / shop_card / pay_jump ...）。
   流程不再由状态机硬锁，用户可随时调用任一 skill（设计/改设计/生图/看店/下单）。
4. 最终根据本轮工具产出结构化 UI（plan_card / shop_card / pay_jump ...）。

说明：
- call_llm 为 OpenAI 兼容真实接口（live-only），必须配置 LLM_API_KEY，已弃用 Mock 引擎。
- 同步存储操作通过 asyncio.to_thread 调用，避免阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable
from typing import Any

from agent.engine.llm import call_llm
from agent.engine.state import SessionStage
from agent.engine.ui_protocol import AgentAction, AgentActionType, ChatResponse, ToolCallRecord, UIType
from agent.toolkit import execute_tool, generate_tool_manual, to_openai_tools
from backend.config import settings, setup_logging
from backend.storage import memory as mem_store

_CHITCHAT_WORDS = ('你好', '您好', '在吗', '在么', '嗨', '哈喽', '谢谢', '感谢', '再见', '拜拜', '哈哈', '辛苦了', '赞', '呵呵')
_BUY_INTENT = ('买', '送', '下单', '购买', '付款', '支付', '选一束', '挑一束', '想要', '需要', '来一束', '订一束')

def _clean_reply(text: str) -> str:
    """清理智能体回复里的 markdown 噪声，让前端纯文本渲染更整洁。

    前端不渲染 markdown，因此 ``**加粗**`` 会原样显示成 ``**``；这里统一去除
    ``**`` 与行首 ``#`` 标题符，并把连续空行折叠为单空行，保留有序列表等可读结构。
    """
    if not text:
        return text
    text = text.replace('**', '')
    text = re.sub('(?m)^#{1,6}\\s*', '', text)
    text = re.sub('\\n{3,}', '\n\n', text)
    return text.strip()

def _is_chitchat(text: str) -> bool:
    """判断消息是否与花卉导购无关（纯寒暄/感谢）。用于 DONE 后判断是否开启新会话。"""
    t = text.strip().lower()
    if not t:
        return True
    if any(k in t for k in ('买', '送', '花', '束', '预算', '方案', 'diy', '自己', '店铺', '下单', '订单', '确认', '选', '要', '想要', '需要', '推荐', '生图', '效果', '图')):
        return False
    return any(w in t for w in _CHITCHAT_WORDS)
logger = logging.getLogger('agent')
_AFFIRMATIVE = ('好', '可以', '确认', '同意', '生成', '要', '行', '是', '看看')
_NEGATIVE = ('不用', '不要', '不需要', '不必', '算了', '跳过', '无需', '别', '放弃')


def _platform_source_ids() -> list[str]:
    """扫描进程环境变量，返回已配置的平台数据源 source_id（小写去重排序）。

    与 backend/data_gateway/external.py 一致：连接串从
    ``PLATFORM_DB_<SOURCE_ID>_URL`` 读取（部署方注入容器环境变量）。
    未配置任何平台库时返回空列表，供 system prompt 如实告知 LLM。
    """
    ids: set[str] = set()
    for key in os.environ:
        if key.startswith('PLATFORM_DB_') and key.endswith('_URL'):
            mid = key[len('PLATFORM_DB_'):-len('_URL')]
            if mid:
                ids.add(mid.lower())
    return sorted(ids)


def _entity_query_ok(tool_log: list[ToolCallRecord], entity: str) -> bool:
    """本轮是否有成功的 platform_db_query_entity 调用且 arguments.entity 匹配。

    entity 取值：plan / shop / order / user（对应平台只读查询的标准业务实体）。
    """
    return any(
        tc.status == 'ok'
        and tc.name == 'platform_db_query_entity'
        and (tc.arguments or {}).get('entity') == entity
        for tc in tool_log
    )


def is_allowed(role: str, action: str) -> bool:
    """角色权限检查（兼容接口）。"""
    return True

def is_affirmative(text: str) -> bool:
    """判断用户消息是否为明确肯定意图（用于生图确认等关卡）。"""
    t = (text or '').strip()
    if not t:
        return False
    if any(k in t for k in _NEGATIVE):
        return False
    return any(k in t for k in _AFFIRMATIVE)

class ReActAgent:
    """基于 ReAct + 状态机的导购智能体。"""

    async def arun(self, user_id: str, message: str, session_id: str | None=None, location: dict[str, float] | None=None, shop_id: str | None=None) -> ChatResponse:
        """异步入口：用线程池跑同步主循环。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: asyncio.run(self.run(user_id, message, session_id, location, shop_id=shop_id)))

    async def arun_stream(self, user_id: str, message: str, session_id: str | None=None, location: dict[str, float] | None=None, shop_id: str | None=None):
        """流式异步入口：yield SSE 事件字典，供 /chat/stream 消费。

        事件类型：
        - {"event": "tool_call", "name": "...", "status": "ok/error"}
        - {"event": "text", "content": "..."}  — 逐句输出最终回复
        - {"event": "card", "ui": "...", "data": {...}}  — 结构化卡片
        - {"event": "done", "session_id": "..."}
        - {"event": "error", "message": "..."}
        """
        try:
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[dict | None] = asyncio.Queue()

            def _on_event(evt: dict) -> None:
                """run() 线程中调用，线程安全地把事件推入 async Queue。"""
                loop.call_soon_threadsafe(queue.put_nowait, evt)

            async def _run():
                result = await loop.run_in_executor(None, lambda: asyncio.run(self.run(user_id, message, session_id, location, on_event=_on_event, shop_id=shop_id)))
                await queue.put({'event': 'done', 'session_id': result.session_id})
                await queue.put(None)
            task = loop.create_task(_run())
            try:
                while True:
                    evt = await queue.get()
                    if evt is None:
                        break
                    yield evt
            except asyncio.CancelledError:
                task.cancel()
                raise
            finally:
                if not task.done():
                    task.cancel()
        except Exception as exc:
            logger.exception('[agent] arun_stream 异常')
            yield {'event': 'error', 'message': f'智能体执行失败: {type(exc).__name__}'}

    async def run(self, user_id: str, message: str, session_id: str | None, location: dict[str, float] | None, on_event: Callable[[dict], None] | None=None, shop_id: str | None=None) -> ChatResponse:
        t0 = time.perf_counter()
        sid = await mem_store.get_or_create_session(user_id, session_id, shop_id=shop_id)
        # shop_id 绑定在会话上，以会话存储的为准（创建时写入，整个会话不变）
        session_shop = await mem_store.get_session_shop_id(sid)
        shop_id = session_shop or shop_id
        stage = SessionStage(await mem_store.get_stage(sid))
        if stage == SessionStage.DONE and (not _is_chitchat(message)):
            sid = await mem_store.create_conversation(user_id, title=message[:20], shop_id=shop_id)
            stage = SessionStage.ANALYZE
        try:
            existing_req = await mem_store.get_requirement(sid)
            if existing_req and location and not existing_req.location:
                existing_req.location = location
                await mem_store.set_requirement(sid, existing_req)
        except Exception:
            pass
        stage = SessionStage(await mem_store.get_stage(sid))
        incoming = stage
        if stage == SessionStage.IMAGE_GEN and is_affirmative(message):
            await mem_store.set_session_flag(user_id, sid, 'image_confirmed', '1')
        long_term = await mem_store.get_long_term(user_id)
        history = await mem_store.load_history(sid, settings.history_limit)
        system = self._build_system(stage, long_term, shop_id=shop_id)
        messages: list[dict[str, Any]] = [{'role': 'system', 'content': system}]
        messages += history
        messages.append({'role': 'user', 'content': message})
        tool_log: list[ToolCallRecord] = []
        respond_args: dict[str, Any] | None = None
        final_reply = ''
        new_msgs: list[dict[str, Any]] = [{'role': 'user', 'content': message}]
        for turn in range(1, settings.max_iterations + 1):
            logger.info('[agent] ReAct 第 %d/%d 轮 阶段=%s', turn, settings.max_iterations, stage.value)
            try:
                resp = call_llm(messages, tools=to_openai_tools())
            except Exception as exc:
                logger.exception('[agent] LLM 调用失败')
                final_reply = f'抱歉，模型调用出错：{exc}'
                break
            msg = resp.choices[0].message
            tool_calls = self._parse_tool_calls(msg)
            if tool_calls:
                assistant_msg = {'role': 'assistant', 'content': getattr(msg, 'content', '') or '', 'tool_calls': [{'id': tc['id'], 'type': 'function', 'function': {'name': tc['name'], 'arguments': json.dumps(tc['arguments'], ensure_ascii=False)}} for tc in tool_calls]}
                messages.append(assistant_msg)
                new_msgs.append({**assistant_msg, 'content': ''})
                for tc in tool_calls:
                    if tc['name'] in ('respond_to_user', 'show_plan_card'):
                        respond_args = tc['arguments']
                        obs = json.dumps(respond_args, ensure_ascii=False)
                        messages.append({'role': 'tool', 'content': obs, 'tool_call_id': tc.get('id', '')})
                        new_msgs.append({'role': 'tool', 'content': obs, 'tool_call_id': tc.get('id', '')})
                        continue
                    result, status = await execute_tool(tc['name'], tc['arguments'], {'user_id': user_id, 'session_id': sid, 'location': location, 'shop_id': shop_id})
                    record = ToolCallRecord(name=tc['name'], arguments=tc['arguments'], result=result, status=status)
                    tool_log.append(record)
                    if on_event:
                        on_event({'event': 'tool_call', 'name': tc['name'], 'status': status})
                    messages.append({'role': 'tool', 'content': result, 'tool_call_id': tc.get('id', '')})
                    new_msgs.append({'role': 'tool', 'content': result, 'tool_call_id': tc.get('id', '')})
                if respond_args is not None:
                    break
                continue
            else:
                final_reply = getattr(msg, 'content', '') or ''
                messages.append({'role': 'assistant', 'content': final_reply})
                break
        else:
            if any(tc.status == 'ok' for tc in tool_log):
                final_reply = final_reply or '我已经为你整理好相关结果啦，请查看下方卡片～'
            else:
                final_reply = final_reply or '抱歉，我思考得太久啦，请简化需求或分步骤再问我～'
        new_stage, ui, data, final_reply, llm_intent = await self._post_process(
            respond_args, tool_log, incoming, message, final_reply, user_id, sid, location, new_msgs,
        )
        _img_intent = any(w in message for w in ('效果图', '生图', '生成'))
        new_msgs.append({'role': 'assistant', 'content': final_reply, 'ui': ui.value, 'data': data})
        await mem_store.save_messages(sid, new_msgs)
        await mem_store.update_stage(sid, new_stage.value)
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info('[agent] 完成 阶段=%s ui=%s 耗时=%.0fms', new_stage.value, ui.value, elapsed)
        if on_event:
            import re as _re
            parts = _re.split('([。！？\\n])', final_reply or '')
            buf = ''
            for seg in parts:
                buf += seg
                if seg in ('。', '！', '？', '\n') or len(buf) > 20:
                    on_event({'event': 'text', 'content': buf})
                    buf = ''
                    time.sleep(0.03)
            if buf:
                on_event({'event': 'text', 'content': buf})
            if ui and ui.value != 'text':
                on_event({'event': 'card', 'ui': ui.value, 'data': data})
        action_type = {
            UIType.PLAN_CARD: AgentActionType.SHOW_PLAN,
            UIType.SHOP_CARD: AgentActionType.SHOW_SHOP,
            UIType.ORDER_CARD: AgentActionType.CREATE_ORDER,
            UIType.PAY_JUMP: AgentActionType.OPEN_PAYMENT,
            UIType.IMAGE_TASK: AgentActionType.START_IMAGE_TASK,
            UIType.DIALOG_OPTIONS: AgentActionType.SHOW_OPTIONS,
            UIType.TEXT: AgentActionType.SHOW_TEXT,
        }.get(ui, AgentActionType.SHOW_TEXT)
        capability = {
            AgentActionType.SHOW_PLAN: 'show_plan_page',
            AgentActionType.SHOW_SHOP: 'show_shop_page',
            AgentActionType.CREATE_ORDER: 'create_order',
            AgentActionType.OPEN_PAYMENT: 'open_payment',
            AgentActionType.START_IMAGE_TASK: 'start_image_task',
        }.get(action_type)
        action = AgentAction(
            type=action_type,
            payload={'reply': final_reply, 'ui': ui.value, 'data': data, 'stage': new_stage.value},
            required_capabilities=[capability] if capability else [],
            fallback=final_reply or '当前平台暂未实现对应能力，请使用文本方式继续引导。',
        )
        return ChatResponse(user_id=user_id, reply=final_reply, ui=ui, data=data, action=action, tool_calls=tool_log, session_id=sid, stage=new_stage.value)

    async def _post_process(
        self, respond_args, tool_log, incoming, message, final_reply,
        user_id, sid, location, new_msgs,
    ):
        """run() 的后处理：UI 推导、业务补调（生图/推方案/QA 过滤）、回复清理。"""
        # ── 1. 推导 new_stage / ui / data ──
        if respond_args is not None:
            new_stage = self._derive_focus(tool_log, incoming, message)
            if new_stage == SessionStage.DONE:
                ordered = [tc.name for tc in tool_log if tc.status == 'ok']
                if 'create_order' not in ordered:
                    new_stage = SessionStage.SHOP_RECOMMEND if _entity_query_ok(tool_log, 'shop') else incoming
            ui_arg = str(respond_args.get('ui', ''))
            try:
                ui = UIType(ui_arg)
            except ValueError:
                ui = UIType.TEXT
            data_arg = respond_args.get('data') or {}
            data = data_arg if isinstance(data_arg, dict) else {}
            if self._validate_respond_data(ui, data) is None:
                data = {}
            inferred_ui, inferred_data = self._derive_ui(tool_log, new_stage, final_reply)
            _card_types = {UIType.DIALOG_OPTIONS, UIType.PLAN_CARD, UIType.SHOP_CARD, UIType.ORDER_CARD, UIType.PAY_JUMP}
            _data_effective = bool(data) and (not (ui == UIType.IMAGE_TASK and (not (data.get('task_id') or data.get('result_url')))))
            if not _data_effective:
                if inferred_ui in _card_types and inferred_data:
                    ui = inferred_ui
                    data = inferred_data
            elif ui == UIType.PLAN_CARD and inferred_ui == UIType.PLAN_CARD and inferred_data:
                ui = inferred_ui
                data = inferred_data
            if inferred_ui in (UIType.ORDER_CARD, UIType.PAY_JUMP) and inferred_data.get('pay_jump'):
                ui = UIType.PAY_JUMP
                data = inferred_data
            if inferred_data.get('task_id'):
                if inferred_data.get('result_url'):
                    ui = UIType.IMAGE_TASK
                    data = {'task_id': inferred_data['task_id'], 'poll': inferred_data.get('poll'), 'result_url': inferred_data['result_url']}
                else:
                    ui = UIType.TEXT
                    data = {'task_id': inferred_data['task_id'], 'poll': inferred_data.get('poll')}
            final_reply = str(respond_args.get('reply', final_reply) or final_reply)
            if not final_reply.strip():
                final_reply = '我已经为你整理好相关结果啦，请查看下方卡片～' if tool_log else '好的，收到你的想法啦，请稍等～'
            if ui == UIType.DIALOG_OPTIONS and isinstance(data.get('options'), list):
                data['options'] = [o if isinstance(o, dict) and o.get('label') else {'label': str(o), 'value': str(o)} for o in data['options']]
            if ui == UIType.IMAGE_TASK:
                if inferred_data.get('task_id'):
                    data = {'task_id': inferred_data['task_id'], 'poll': inferred_data.get('poll')}
                    if inferred_data.get('result_url'):
                        data['result_url'] = inferred_data['result_url']
                else:
                    ui = UIType.TEXT
                    data = {}
            if ui == UIType.SHOP_CARD:
                if inferred_ui == UIType.SHOP_CARD and inferred_data.get('shops'):
                    ui = inferred_ui
                    data = inferred_data
                else:
                    ui = UIType.TEXT
                    data = {}
        else:
            new_stage = self._derive_focus(tool_log, incoming, message)
            if new_stage == SessionStage.DONE:
                ordered = [tc.name for tc in tool_log if tc.status == 'ok']
                if 'create_order' not in ordered:
                    new_stage = SessionStage.SHOP_RECOMMEND if _entity_query_ok(tool_log, 'shop') else incoming
            ui, data = self._derive_ui(tool_log, new_stage, final_reply)

        llm_intent = str(respond_args.get('intent', '') or '') if respond_args else ''
        _img_intent = any(w in message for w in ('效果图', '生图', '生成'))

        # ── 2. 图片确认标记 ──
        if new_stage == SessionStage.IMAGE_GEN and new_stage != incoming:
            await mem_store.clear_session_flags(user_id, sid, prefix='image_')
            await mem_store.set_session_flag(user_id, sid, 'image_confirmed', '1')
        elif _img_intent and incoming in (SessionStage.DIY_DESIGN, SessionStage.IMAGE_GEN) and (await mem_store.get_session_flag(user_id, sid, 'image_confirmed') != '1'):
            await mem_store.set_session_flag(user_id, sid, 'image_confirmed', '1')

        # ── 3. 方案确认入库 ──
        if any(w in message for w in ('确认方案', '确认这个方案', '就这个', '定这个', '就它', '这个方案', '方案可以')) or (is_affirmative(message) and '方案' in message):
            try:
                from backend.storage.diy import save_diy_plan
                _diy = await mem_store.get_session_json(user_id, sid, 'latest_diy_plan')
                if _diy and _diy.get('diy'):
                    if not (_diy.get('result_url') or _diy.get('effect_image_url')):
                        try:
                            from backend.storage.tasks import get_image_task
                            for _m in reversed(await mem_store.load_display_messages(sid)):
                                _d = _m.get('data') if isinstance(_m.get('data'), dict) else {}
                                if _d.get('task_id'):
                                    _t = await get_image_task(str(_d['task_id']))
                                    if _t.get('result_url'):
                                        _diy['result_url'] = _t['result_url']
                                    break
                        except Exception:
                            logger.exception('[agent] DIY 方案效果图回填失败')
                    _diy['requirement'] = message
                    _res = await save_diy_plan(_diy, user_id)
                    logger.info('[agent] DIY 方案入库 saved=%s duplicate=%s id=%s', _res['saved'], _res['duplicate'], _res['plan_id'])
            except Exception:
                logger.exception('[agent] DIY 方案入库失败')

        # ── 4. 兜底推方案 ──
        _had_card = ui in (UIType.PLAN_CARD, UIType.SHOP_CARD, UIType.ORDER_CARD, UIType.PAY_JUMP) or (ui == UIType.TEXT and bool(data.get('task_id')))
        _plan_pushed = await mem_store.get_session_flag(user_id, sid, 'plan_pushed') == '1'
        if any(w in message for w in ('再', '换', '别的', '预算', '有没有', '其他', '看看')):
            await mem_store.clear_session_flags(user_id, sid, prefix='plan_')
            _plan_pushed = False

        # ── 5. QA 意图过滤 ──
        if llm_intent:
            _qa_intent = llm_intent == 'qa'
        else:
            _qa_intent = bool(re.search('什么|怎么|为什么|多久|花期|养护|寓意|百科|介绍|季节', message)) and (not any(w in message for w in ('买', '送', '预算', '下单', 'diy', '方案', '推荐', '想要', '需要', '束')))
        _has_plan_evidence = any(tc.name in ('generate_diy_plan', 'revise_diy_plan', 'generate_effect_image', 'create_order') and tc.status == 'ok' for tc in tool_log) or _entity_query_ok(tool_log, 'plan') or _entity_query_ok(tool_log, 'shop')
        if _qa_intent and ui == UIType.PLAN_CARD and (not _has_plan_evidence):
            ui = UIType.TEXT
            data = {}
            logger.info('[agent] 知识问答轮次，丢弃 LLM 擅自推送的方案卡')

        # ── 6. 方案即生图 ──
        diy_done = any(tc.name in ('generate_diy_plan', 'revise_diy_plan') and tc.status == 'ok' for tc in tool_log)
        eff_done = any(tc.name == 'generate_effect_image' and tc.status == 'ok' for tc in tool_log)
        if diy_done and (not eff_done) and (ui == UIType.PLAN_CARD) and (new_stage not in (SessionStage.DONE, SessionStage.ORDER_CONFIRM)):
            try:
                from agent.tools import generate_effect_image as _gei
                await mem_store.set_session_flag(user_id, sid, 'image_confirmed', '1')
                raw = await _gei('latest_diy', {'user_id': user_id, 'session_id': sid, 'location': location})
                eff = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                if 'task_id' in eff:
                    data = {**data, 'task_id': eff['task_id'], 'poll': eff.get('poll', True)}
                    if eff.get('result_url'):
                        data['result_url'] = eff['result_url']
                    tool_log.append(ToolCallRecord(name='generate_effect_image', arguments={'plan': 'latest_diy'}, result=json.dumps(eff, ensure_ascii=False), status='ok'))
                    logger.info('[agent] 方案即生图 task_id=%s', eff['task_id'])
            except Exception:
                logger.exception('[agent] 方案即生图失败')

        # ── 7. 生图补调 ──
        eff_confirmed = await mem_store.get_session_flag(user_id, sid, 'image_confirmed') == '1'
        eff_done = any(tc.name == 'generate_effect_image' and tc.status == 'ok' for tc in tool_log)
        if eff_confirmed and (not eff_done) and (ui != UIType.PLAN_CARD) and (new_stage not in (SessionStage.DONE, SessionStage.ORDER_CONFIRM)):
            try:
                from agent.tools import generate_effect_image as _gei
                await mem_store.update_stage(sid, SessionStage.IMAGE_GEN.value)
                raw = await _gei('latest_diy', {'user_id': user_id, 'session_id': sid, 'location': location})
                eff = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                if 'task_id' in eff:
                    ui = UIType.TEXT
                    data = {'task_id': eff['task_id'], 'poll': eff.get('poll', True)}
                    final_reply = '正在为您生成效果图预览，请稍候～ 🎨'
                    tool_log.append(ToolCallRecord(name='generate_effect_image', arguments={'plan': 'latest_diy'}, result=json.dumps(eff, ensure_ascii=False), status='ok'))
                    new_msgs.append({'role': 'tool', 'content': json.dumps(eff, ensure_ascii=False), 'tool_call_id': 'forced_effect_image'})
                    logger.info('[agent] 生图补调成功 task_id=%s', eff['task_id'])
            except Exception:
                logger.exception('[agent] 生图补调失败')

        # ── 8. 回复清理 ──
        final_reply = _clean_reply(final_reply)
        if ui in (UIType.ORDER_CARD, UIType.PAY_JUMP):
            final_reply = re.sub('[，,]?共\\s*\\d+[\\d.]*\\s*元', '', final_reply)
            final_reply = re.sub('[，,]?\\d+[\\d.]*\\s*元[。.?]', '', final_reply)
            final_reply = final_reply.strip() or '订单已生成，请确认信息后去支付～'
        if ui.value != UIType.TEXT and (not final_reply or final_reply == '好的，收到你的想法啦，请稍等～'):
            final_reply = ''
        elif not final_reply:
            final_reply = '我已经为你整理好相关结果啦，请查看下方卡片～' if tool_log else '好的，收到你的想法啦，请稍等～'

        return new_stage, ui, data, final_reply, llm_intent

    def _build_system(self, stage: SessionStage, long_term: dict[str, str], shop_id: str | None=None) -> str:
        """构造 system prompt：身份 + 能力 + 工具，鼓励自主推理。"""
        parts = [
            '你是「花卉 DIY 设计智能体」，帮助用户设计花艺方案、生成效果图、推荐店铺并下单。用简洁中文回复。',
            '',
            '## 核心原则',
            '- 先理解用户意图，再决定做什么。不要套用固定流程。',
            '- 每轮对话都独立思考：这个用户现在需要什么？我该调什么工具？结果够不够？要不要再查？',
            '- 不要预设用户需求。用户说「买花」不代表要下单，可能是咨询。',
            '',
            '## 工具调用指南（什么时候调什么）',
            '',
            '### 场景1：用户要买「平台在售花束」/ 看推荐',
            '用户说「给妈妈买束花」「有什么玫瑰推荐」→',
            '  1. 调 platform_db_query_entity(source_id="<平台数据源>", entity="plan", keyword="母亲" 或 "玫瑰") 只读查平台在售方案（keyword 用用户原话；不传则取最近/全部，limit 控制条数）',
            '  2. 从返回行（id / name / price / desc / image / merchant / tags / shop_id）挑选合适的推荐给用户',
            '  3. 用户选定并要买：先 platform_db_query_entity(entity="shop") 查能做/配送的店铺，再 create_order(shop_id=店铺真实ID, plan_id=方案ID, plan_type="existing", source_id=同一数据源)',
            '  4. 用 respond_to_user 结束：给方案卡 ui="plan_card" data={plans:[...]}；只咨询/比价也可 ui="text"',
            '',
            '### 场景2：用户要 DIY 定制',
            '用户说「帮我设计一束花」「想要独一无二的」→',
            '  1. 先问清楚：送给谁？什么场合？预算多少？喜欢什么颜色？（问1-2个关键问题）',
            '  2. 用户回答后，调 generate_diy_plan(requirements="送给妈妈的生日花束，预算200，喜欢粉色")',
            '  3. 方案生成后展示给用户（plan_card），问「方案满意吗？」',
            '  4. 用户确认后要买：platform_db_query_entity(entity="shop") 找店铺 → create_order(shop_id=店铺ID, plan_id="latest", plan_type="diy")',
            '  5. 调 show_plan_card(plans=[...], reply="方案已设计好...")',
            '',
            '### 场景3：用户问花艺知识',
            '用户说「百合花什么季节开花」「玫瑰的花语是什么」→',
            '  1. 调 retrieve_knowledge(domain="flower", query="百合")',
            '  2. 根据知识库回答，不要推荐方案',
            '  3. 调 respond_to_user(reply="百合花...", ui="text", intent="qa")',
            '',
            '### 场景4：用户要修改方案',
            '用户说「换个颜色」「不要百合」「预算降低一点」→',
            '  1. 调 revise_diy_plan(plan="当前方案JSON", feedback="换粉色，不要百合")',
            '  2. 展示修改后的方案',
            '',
            '### 场景5：用户查订单',
            '用户说「我上次订的花发货了吗」→',
            '  1. 调 platform_db_query_entity(source_id="<平台数据源>", entity="order", keyword="订单号/用户ID") 只读查询',
            '  2. 告知订单状态；订单/配送以下单平台侧为准（order 实体需该 source 的 active 映射已配置，否则如实说明）',
            '',
            '### 场景6：平台数据未接入 / 查询报错',
            'platform_db_query_entity 报「未配置外部数据源连接」或「没有该来源的 active mapping」时：',
            '  - 如实告知用户：该平台暂未接入商品/店铺/订单数据，无法查询与下单；',
            '  - 绝不编造商品或订单，绝不拿别家数据冒充；',
            '  - 接入由部署方配置 PLATFORM_DB_<SOURCE_ID>_URL 与 active 映射后生效，对话内不会自动完成。',
            '',
            '### 场景7：为订单配电子贺卡（下单成功后可主动引导）',
            '用户刚下单成功，或明确说「配张贺卡」「写句祝福」「做张卡片」→',
            '  1. 先调 suggest_greetings(recipient="收卡人", occasion="场合") 取候选祝福语（优先用方案里已确认的送花对象/场合；不确定可留空让词库出通用候选）',
            '  2. 把候选展示给用户挑（每条带序号与适用备注）；用户选定、或直接给了自定义文案后',
            '  3. 调 render_greeting_card(text="祝福语", recipient="亲爱的妈妈", sender="落款", template="warm/blush/green/letter/night") 渲染成贺卡图（秒级出图，无需轮询）',
            '  4. 贺卡会以 greeting_card 卡片自动展示；可追问用户要不要换模板 / 改文案重做',
            '  注意：贺卡渲染是模板合成（文字清晰），不要为「做贺卡」调用 generate_effect_image（那是给花束效果图用的异步生图）。',
            '',
            '## 下单契约（重要）',
            '- 下单一律走平台自有下单接口 create_order：需部署方已配置 PLATFORM_ORDER_API_URL（可选 KEY），未配置时工具会明确报错并引导去平台下单，绝不写本地订单表。',
            '- shop_id 必须是 platform_db_query_entity(entity="shop") 返回的平台真实店铺 ID；plan_id 用 "latest"（会话内 DIY/最近方案）或平台在售方案 ID（配 source_id 只读回读）。',
            '- 订单金额/状态以平台下单接口返回为准；智能体只组装信息并提交，不直写平台库。',
            '',
            '## 核心工具速览（完整工具说明书自动附在文末）',
            '- platform_db_query_entity(source_id, entity, keyword, limit)：只读查平台 plan/shop/order/user（实时数据，展示商品/店铺优先用它）',
            '- generate_diy_plan(requirements)：设计 DIY 花艺方案',
            '- revise_diy_plan(plan, feedback)：按反馈改方案',
            '- generate_effect_image(plan)：为方案生成效果图（方案完成后系统常自动触发）',
            '- retrieve_knowledge(domain, query)：查花艺知识库（花材/风格/搭配/预算/包装/商家智库）',
            '- create_order(shop_id, plan_id, plan_type, source_id)：提交平台下单并返回支付跳转',
            '- suggest_greetings(recipient, occasion, style)：按收卡人×场合×语气返回预设祝福语候选（内置情景词库）',
            '- render_greeting_card(text, recipient, sender, template)：把祝福语模板合成电子贺卡图，返回 image_url（同步出图）',
            '- save_memory / save_user_profile：记住用户偏好',
            '- respond_to_user(reply, ui, data, stage, intent)：通用终结工具',
            '- show_plan_card(plans, reply, stage, intent)：标准方案卡片输出工具',
            '- platform_db_discover / platform_db_sample_table / platform_mapping_*：部署接入期由配置方使用；日常对话不需要',
            '',
            '## respond_to_user 参数说明',
            '- reply：给用户的文字回复（1-2句话）',
            '- ui：UI 类型。plan_card(方案卡)/shop_card(店铺卡)/text(纯文字)/order_card(订单卡)',
            '- data：卡片数据。如 {plans: [...]} 或 {shops: [...]}',
            '- intent：用户意图。buying(要买)/qa(问知识)/chitchat(闲聊)/design(要DIY)/other',
            '',
            '## 回复格式',
            '- 简短亲切，像专业花艺师在聊天。',
            '- 结构化内容用卡片展示，文字只给结论。',
            '- 不要用 **markdown** 加粗，不要用 # 标题。',
        ]
        sources = _platform_source_ids()
        if sources:
            parts.append('## 已接入平台数据源（source_id）')
            parts.append('platform_db_query_entity / create_order 只能使用以下已配置数据源：' + '、'.join(sources) + '。用户提到的平台不在其中时，说明尚未接入，不要猜测或套用其他 source_id。')
        else:
            parts.append('## 平台接入状态')
            parts.append('当前未配置任何平台数据库（无 PLATFORM_DB_<SOURCE_ID>_URL）：平台在售方案/店铺/订单均不可查，涉及下单会明确报错。此时可正常做 DIY 设计与效果图；用户要买平台花束时，如实告知平台暂未接入。')
        if long_term:
            mem = '；'.join((f'{k}={v}' for k, v in long_term.items()))
            parts.append('## 用户偏好记忆：' + mem)
        if shop_id:
            parts.append(f'## 当前店铺锁定：{shop_id}（仅搜索/推荐该店铺）')
        parts.append('## 工具说明书\n' + generate_tool_manual())
        return '\n\n'.join(parts)

    @staticmethod
    def _parse_tool_calls(msg: Any) -> list[dict[str, Any]]:
        """兼容 OpenAI（msg.tool_calls[i].function）与 Mock（_MockToolCall）。"""
        raw = getattr(msg, 'tool_calls', None)
        if not raw:
            return []
        calls: list[dict[str, Any]] = []
        for tc in raw:
            name = tc.function.name
            args = json.loads(tc.function.arguments or '{}')
            tid = getattr(tc, 'id', '')
            calls.append({'id': tid, 'name': name, 'arguments': args})
        return calls

    @staticmethod
    def _derive_focus(tool_log: list[ToolCallRecord], incoming: SessionStage, message: str) -> SessionStage:
        """基于本轮工具产出推导 UI 焦点（focus），不再做状态机拦截。

        skill 编排模式下，focus 仅用于前端高亮「用户当前在做什么」，不限制流程：
        - 有订单 → done；电子贺卡渲染 → greeting_card；
        - 店铺查询(platform_db_query_entity entity=shop) → shop_recommend；
        - 有生图 → image_gen；DIY 方案生成/改版 → diy_design；
        - 平台在售方案浏览(entity=plan) → view_plan；
        - 否则保持进入时的焦点（incoming），避免无工具轮次焦点乱跳。
        """
        ordered = [tc.name for tc in tool_log if tc.status == 'ok']
        if 'create_order' in ordered:
            return SessionStage.DONE
        if 'render_greeting_card' in ordered:
            return SessionStage.GREETING_CARD
        if _entity_query_ok(tool_log, 'shop'):
            return SessionStage.SHOP_RECOMMEND
        if 'generate_effect_image' in ordered:
            return SessionStage.IMAGE_GEN
        if 'generate_diy_plan' in ordered or 'revise_diy_plan' in ordered:
            return SessionStage.DIY_DESIGN
        if _entity_query_ok(tool_log, 'plan'):
            return SessionStage.VIEW_PLAN
        return incoming

    @staticmethod
    def _validate_respond_data(ui: UIType, data: dict) -> dict | None:
        """按 ui 契约校验 respond_to_user 携带的 data 形状；无效返回 None。

        卡片类 ui 必须有核心字段，否则视为 LLM 幻觉（如 plan_card 无 plans、
        pay_jump 无 order_id、dialog_options 无 options），调用方据此把 data 置空，
        交给 _derive_ui 依据真实工具成果重建卡片。
        """
        if ui == UIType.TEXT:
            return data
        if ui == UIType.DIALOG_OPTIONS:
            return data if isinstance(data.get('options'), list) and data['options'] else None
        if ui == UIType.PLAN_CARD:
            return data if isinstance(data.get('plans'), list) and data['plans'] else None
        if ui == UIType.SHOP_CARD:
            return data if isinstance(data.get('shops'), list) and data['shops'] else None
        if ui in (UIType.ORDER_CARD, UIType.PAY_JUMP):
            return data if data.get('order_id') or data.get('page_path') else None
        if ui == UIType.IMAGE_TASK:
            return data if data.get('task_id') or data.get('result_url') else None
        if ui == UIType.GREETING_CARD:
            return data if data.get('image_url') else None
        return data

    def _derive_ui(self, tool_log: list[ToolCallRecord], stage: SessionStage, reply: str) -> tuple[UIType, dict[str, Any]]:
        """根据本轮工具产出决定 ui 类型与 data。

        注意：不能只看「最后一个成功工具」——live LLM 常在设计/生图之后追加
        save_memory / retrieve_knowledge 落库偏好，若只取 last 会漏掉方案卡/生图卡/
        店铺卡（已复现：generate_diy_plan > save_memory 时 ui 退化为 text）。
        这里从最近一次成功工具回溯，跳过不产出卡片的辅助工具，命中即返回。
        """
        renderers: dict[str, Callable[[dict[str, Any]], tuple[UIType, dict[str, Any]]]] = {
            'generate_diy_plan': lambda r: (UIType.PLAN_CARD, {'plans': [r]}),
            'revise_diy_plan': lambda r: (UIType.PLAN_CARD, {'plans': [r]}),
            'generate_effect_image': lambda r: (UIType.IMAGE_TASK, {'task_id': r.get('task_id'), 'poll': r.get('poll'), **({'result_url': r['result_url']} if r.get('result_url') else {})}),
            'create_order': lambda r: (UIType.ORDER_CARD, r),
            'render_greeting_card': lambda r: (UIType.GREETING_CARD, r),
        }
        for tc in reversed(tool_log):
            if tc.status != 'ok':
                continue
            try:
                result = json.loads(tc.result) if isinstance(tc.result, str) else tc.result or {}
            except (json.JSONDecodeError, TypeError):
                result = {}
            if isinstance(result, dict) and result.get('error'):
                # 工具自身返回 {error}（如下单/渲染失败）：不产卡片，回退 text 如实播报
                continue
            # platform_db_query_entity 返回 tool_result 封装 {ok, data:[规范行], error}，
            # 按调用参数的 entity 分流：plan → plan_card、shop → shop_card；失败/空结果不产卡片。
            if tc.name == 'platform_db_query_entity':
                if not isinstance(result, dict) or result.get('ok') is not True:
                    continue
                rows = result.get('data')
                if not isinstance(rows, list) or not rows:
                    continue
                entity = (tc.arguments or {}).get('entity')
                if entity == 'plan':
                    return (UIType.PLAN_CARD, {'plans': rows})
                if entity == 'shop':
                    return (UIType.SHOP_CARD, {'shops': rows})
                continue
            render = renderers.get(tc.name)
            if not render:
                continue
            if isinstance(result, list) and (not result):
                continue
            return render(result)
        return (UIType.TEXT, {})
if __name__ == '__main__':
    setup_logging()
    from backend.storage.db import init_db
    init_db()
    agent = ReActAgent()
    user_msg = '想给母亲买一束花，预算 200 元左右'
    result = agent.run('cli_user', user_msg)
    print(result.model_dump_json(indent=2))
