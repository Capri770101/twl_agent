"""engine/llm.py —— LLM 封装（OpenAI 兼容，live-only，P6 生产化）。

对外入口（签名向后兼容）：
- ``call_llm(messages, tools=None, stream=False, response_format=None, user_id=None)``
- ``call_llm_stream(messages, tools=None, user_id=None)``

生产化能力（P6.1 / P6.2）：
- **多 provider 兜底**：primary（llm_base_url/key/model）+ 可选 ``llm_providers`` JSON 列表，
  primary 失败自动切下一个；配置驱动，无需改调用点。
- **重试 + 指数退避 + 抖动**：仅对可重试错误（超时 / 5xx / 限流 429）生效，
  区分于不可重试错误（4xx 鉴权 / 参数）立即失败并切下一个 provider。
- **熔断**：每个 provider 独立熔断器，连续失败达阈值进入 OPEN（快速失败 + 降级），
  半开探测恢复；避免对雪崩的下游持续打流量。
- **成本预算**：调用前按 Redis 计数器做全局 / 每用户日级 token 预算拦截（best-effort），
  调用后按真实 usage 累加；预算超限抛出 ``LLMBudgetExceeded``。
- **优雅降级**：所有 provider 不可用 / 熔断时抛出 ``LLMUnavailableError``（RuntimeError 子类），
  agent 捕获后回到规则引擎 + 知识库兜底，不抛裸异常。

LLM 未配置密钥时仍抛 RuntimeError（live-only，已弃用 Mock 引擎）。
"""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

from agent.engine import budget
from agent.engine.circuit_breaker import CircuitBreaker
from backend.config import settings

logger = logging.getLogger('llm')

class LLMUnavailableError(RuntimeError):
    """所有 provider 均不可用 / 熔断，调用方应降级到规则引擎。"""

class LLMBudgetExceeded(RuntimeError):
    """LLM token 预算超限，调用方应降级并提示用户。"""

def _providers() -> list[dict[str, str]]:
    """返回 provider 列表：primary 在前，extra（llm_providers JSON）在后。"""
    primary = {'name': 'primary', 'base_url': settings.llm_base_url, 'api_key': settings.llm_api_key, 'model': settings.llm_model}
    extras: list[dict[str, str]] = []
    if settings.llm_providers:
        try:
            data = json.loads(settings.llm_providers)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get('api_key'):
                        extras.append({'name': item.get('name', 'extra'), 'base_url': item.get('base_url', settings.llm_base_url), 'api_key': item['api_key'], 'model': item.get('model', settings.llm_model)})
        except Exception:
            logger.warning('[llm] llm_providers 解析失败，忽略额外 provider')
    return [primary, *extras]

def _llm_configured() -> bool:
    if settings.llm_api_key:
        return True
    if settings.llm_providers:
        try:
            for item in json.loads(settings.llm_providers):
                if isinstance(item, dict) and item.get('api_key'):
                    return True
        except Exception:
            pass
    return False
_CBS: dict[str, CircuitBreaker] = {}

def _cb(name: str) -> CircuitBreaker:
    cb = _CBS.get(name)
    if cb is None:
        cb = CircuitBreaker(name, failure_threshold=settings.llm_cb_failure_threshold, open_seconds=settings.llm_cb_open_seconds)
        _CBS[name] = cb
    return cb

def _is_retryable(exc: Exception) -> bool:
    """判断错误是否可重试（超时 / 5xx / 限流）。不可重试则返回 False。"""
    try:
        from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
    except Exception:
        APITimeoutError, RateLimitError, APIConnectionError, InternalServerError = (Exception, Exception, Exception, Exception)
    if isinstance(exc, (APITimeoutError, RateLimitError, APIConnectionError, InternalServerError)):
        return True
    try:
        import httpx
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
            return True
    except Exception:
        pass
    status = getattr(exc, 'status_code', None)
    if status in (429, 500, 502, 503, 504):
        return True
    if status and 400 <= status < 500:
        return False
    return False

def _backoff(attempt: int) -> None:
    """指数退避 + 抖动。"""
    delay = min(settings.llm_retry_base_delay * 2 ** attempt, settings.llm_retry_max_delay)
    time.sleep(delay * (0.5 + random.random()))

def _raw_call(provider: dict[str, str], messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, stream: bool, response_format: dict[str, Any] | None) -> Any:
    from openai import OpenAI
    client = OpenAI(base_url=provider['base_url'], api_key=provider['api_key'], timeout=settings.llm_timeout, max_retries=0)
    kwargs: dict[str, Any] = {'model': provider['model'], 'messages': messages, 'temperature': settings.llm_temperature, 'max_tokens': settings.llm_max_tokens, 'stream': stream}
    if tools:
        kwargs['tools'] = tools
        kwargs['tool_choice'] = 'auto'
    if response_format:
        kwargs['response_format'] = response_format
    logger.info('[llm] 请求 provider=%s model=%s tools=%s stream=%s', provider['name'], provider['model'], [t['function']['name'] for t in tools] if tools else None, stream)
    return client.chat.completions.create(**kwargs)

def _record_cost(user_id: str | None, resp: Any) -> None:
    try:
        usage = getattr(resp, 'usage', None)
        if usage is not None:
            pt = getattr(usage, 'prompt_tokens', 0)
            ct = getattr(usage, 'completion_tokens', 0)
            budget.record(user_id, pt, ct)
            try:
                from backend import observability
                observability.record_llm(pt, ct)
            except Exception:
                pass
    except Exception:
        pass

def _try_providers(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, stream: bool, response_format: dict[str, Any] | None, user_id: str | None) -> Any:
    last_exc: Exception | None = None
    for provider in _providers():
        cb = _cb(provider['name'])
        if settings.llm_circuit_breaker_enabled and (not cb.allow()):
            logger.warning('[llm] provider=%s 熔断中，跳过', provider['name'])
            continue
        for attempt in range(max(1, settings.llm_retry_max_attempts)):
            try:
                resp = _raw_call(provider, messages, tools, stream, response_format)
                cb.on_success()
                if not stream:
                    _record_cost(user_id, resp)
                return resp
            except Exception as exc:
                last_exc = exc
                cb.on_failure()
                if not _is_retryable(exc):
                    logger.warning('[llm] provider=%s 不可重试错误: %s', provider['name'], exc)
                    break
                if attempt < settings.llm_retry_max_attempts - 1:
                    logger.warning('[llm] provider=%s 第 %d 次重试（可重试错误）', provider['name'], attempt + 1)
                    _backoff(attempt)
                    continue
                logger.warning('[llm] provider=%s 重试耗尽', provider['name'])
                break
    try:
        from backend import observability
        observability.record_llm(error=True)
    except Exception:
        pass
    raise LLMUnavailableError(f'所有 LLM provider 均不可用: {last_exc}')

def call_llm(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None=None, stream: bool=False, response_format: dict[str, Any] | None=None, user_id: str | None=None) -> Any:
    """统一的 LLM 调用入口（live-only，多 provider + 重试 + 熔断 + 预算）。"""
    if not _llm_configured():
        raise RuntimeError('未配置 LLM_API_KEY，系统已切换为 live-only（已弃用 Mock 引擎）。请在 .env 配置 LLM_API_KEY（或 llm_providers）后启动。')
    if settings.llm_cost_enabled:
        allowed, reason = budget.check(user_id)
        if not allowed:
            raise LLMBudgetExceeded(f'LLM token 预算超限（{reason}），已降级')
    return _try_providers(messages, tools, stream, response_format, user_id)

def call_llm_stream(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None=None, user_id: str | None=None) -> Any:
    """流式 LLM 调用，返回 OpenAI Stream 对象（调用方自行迭代 chunk）。"""
    if not _llm_configured():
        raise RuntimeError('未配置 LLM_API_KEY，系统已切换为 live-only（已弃用 Mock 引擎）。请在 .env 配置 LLM_API_KEY（或 llm_providers）后启动。')
    if settings.llm_cost_enabled:
        allowed, reason = budget.check(user_id)
        if not allowed:
            raise LLMBudgetExceeded(f'LLM token 预算超限（{reason}），已降级')
    return _try_providers(messages, tools, True, None, user_id)
