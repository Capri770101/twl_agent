"""订阅提醒：写入 reminders 表，由调度器到点生成 notification。

设计（2026-10-10）：
- 提醒是 agent 自己的能力（不依赖 H5）——用户说「每月8号提醒我订花」，
  智能体登记周期规则；调度线程每分钟扫 due 项，写入已有的 notifications
  表（inbox 通道）。接入方读通知即可展示/推送。
- 一次性提醒 first_run；周期 cron（5 字段，标准语法）。next_run 由本模块
  计算，调度器只比对 next_run <= now，不重复触发（触发后周期项推进
  next_run，一次性项置 done）。
- cron 解析用保守子集：数字、*、*/n、范围 a-b、列表 a,b。字段含其他
  表达式一律拒绝（INVALID_ARGUMENT），不给用户含糊的「下次什么时候」。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import re
import threading

from backend.config import settings
from backend.storage.db import transaction

_CRON_RE = re.compile(r'^[0-9*/,-]{1,60}$')  # 整串参考（含空格校验在 parse_cron 字段级做）
_FIRST_RUN_RE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')

_FLD_RANGE = {
    'minute': (0, 59), 'hour': (0, 23), 'day': (1, 31),
    'month': (1, 12), 'weekday': (0, 6),
}

#: cron 的 weekday 语义：0=周日…6=周六（POSIX）；Python weekday()：0=周一…6=周日。
#: 匹配前把 cron 值换算到 Python 语义（(cron_w + 6) % 7 → python_w）。
def _cron_weekday_to_python(values: list[int]) -> set[int]:
    return {(v + 6) % 7 for v in values}


def _parse_field(expr: str, lo: int, hi: int) -> list[int] | None:
    """解析单个 cron 字段 → 允许值列表；非法返回 None。"""
    values: set[int] = set()
    for part in str(expr or '').split(','):
        part = part.strip()
        if not part:
            return None
        step = 1
        if '/' in part:
            part, _, s = part.partition('/')
            if not s.isdigit() or int(s) < 1:
                return None
            step = int(s)
        if part == '*':
            start, end = lo, hi
        elif '-' in part:
            a, _, b = part.partition('-')
            if not (a.isdigit() and b.isdigit()):
                return None
            start, end = int(a), int(b)
            if start < lo or end > hi or start > end:
                return None
        elif part.isdigit():
            start = end = int(part)
            if start < lo or start > hi:
                return None
        else:
            return None
        values.update(range(start, end + 1, step))
    return sorted(values)


def parse_cron(cron: str) -> dict[str, list[int]] | None:
    """5 字段 cron → {minute,hour,day,month,weekday} 允许值；非法 None。

    字段级校验（整串含空格，不能用单一正则 fullmatch）：每字段只允许
    数字、*、/、,、- 的组合；语法语义在 _parse_field 里判。
    """
    parts = str(cron or '').split()
    if len(parts) != 5 or not all(re.fullmatch(r'[0-9*/,\-]+', p) for p in parts):
        return None
    bounds = [_FLD_RANGE['minute'], _FLD_RANGE['hour'], _FLD_RANGE['day'],
              _FLD_RANGE['month'], _FLD_RANGE['weekday']]
    out: dict[str, list[int]] = {}
    for name, expr, (lo, hi) in zip(('minute', 'hour', 'day', 'month', 'weekday'), parts, bounds):
        vals = _parse_field(expr, lo, hi)
        if vals is None:
            return None
        out[name] = vals
    return out


def _next_after(parsed: dict[str, list[int]], after: _dt.datetime) -> _dt.datetime | None:
    """从 after 之后找下一个满足 cron 的时刻（最多向前搜 2 年）。

    标准 cron 语义：day-of-month 与 day-of-week 同时受限（均非 *）时取
    **OR**（任一命中即可）；其中一个是 * 时用另一个。与 POSIX crontab 一致。
    """
    day_restricted = len(parsed['day']) != 31
    py_weekday = _cron_weekday_to_python(parsed['weekday'])
    weekday_restricted = len(py_weekday) != 7
    t = after.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
    limit = after + _dt.timedelta(days=730)
    while t <= limit:
        if (t.minute in parsed['minute'] and t.hour in parsed['hour']
                and t.month in parsed['month']):
            day_hit = t.day in parsed['day']
            weekday_hit = t.weekday() in py_weekday
            if day_restricted and weekday_restricted:
                matches = day_hit or weekday_hit
            else:
                matches = day_hit and weekday_hit
            if matches:
                return t
        t += _dt.timedelta(minutes=1)
    return None


async def create_reminder(user_id: str, content: str, *, cron: str = '', first_run: str = '', repeat: bool = False) -> dict:
    if not settings.CUSTOMER_SERVICE_ENABLED:
        return {'ok': False, 'code': 'UNSUPPORTED'}
    user_id = str(user_id or '').strip()[:80]
    if not user_id:
        return {'ok': False, 'code': 'AUTH_REQUIRED'}
    content = str(content or '').strip()[:200]
    if not content:
        return {'ok': False, 'code': 'INVALID_ARGUMENT'}
    if repeat:
        parsed = parse_cron(cron)
        if parsed is None:
            return {'ok': False, 'code': 'INVALID_ARGUMENT', 'reason': 'cron 格式不合法'}
        next_run = _next_after(parsed, _dt.datetime.now())
        if next_run is None:
            return {'ok': False, 'code': 'INVALID_ARGUMENT', 'reason': '两年内无可触发时间'}
        cron_val = cron
        first_val = None
    else:
        if not _FIRST_RUN_RE.fullmatch(first_run or ''):
            return {'ok': False, 'code': 'INVALID_ARGUMENT', 'reason': 'first_run 需为 YYYY-MM-DD HH:MM'}
        try:
            next_run = _dt.datetime.strptime(first_run, '%Y-%m-%d %H:%M')
        except ValueError:
            return {'ok': False, 'code': 'INVALID_ARGUMENT', 'reason': 'first_run 日期不合法'}
        if next_run <= _dt.datetime.now():
            return {'ok': False, 'code': 'INVALID_ARGUMENT', 'reason': '提醒时间须晚于当前时间'}
        cron_val = ''
        first_val = first_run
    try:
        with transaction() as conn:
            cur = conn.execute(
                """INSERT INTO reminders (user_id, content, cron, first_run, repeat, next_run, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'active') RETURNING id""",
                (user_id, content, cron_val or None, first_val, repeat, next_run))
            row = cur.fetchone()
            rid = row['id'] if isinstance(row, dict) else row[0]
    except Exception:  # noqa: BLE001
        return {'ok': False, 'code': 'INTERNAL_ERROR'}
    return {'ok': True, 'data': {'reminder_id': int(rid), 'content': content,
                                 'repeat': repeat, 'cron': cron_val or None,
                                 'first_run': first_val, 'next_run': next_run.isoformat(),
                                 'note': '提醒已登记，到点会通过系统通知提醒你'}}


def _fire_due() -> int:
    """扫 due 项 → 写 notifications + 推进/关闭。返回触发条数（调度器用）。"""
    now = _dt.datetime.now()
    fired = 0
    with transaction() as conn:
        rows = conn.execute(
            'SELECT id, user_id, content, cron, repeat FROM reminders '
            "WHERE status='active' AND next_run <= ? LIMIT 50", (now,)).fetchall()
        for r in rows:
            conn.execute(
                "INSERT INTO notifications (id, user_id, type, title, body) VALUES (?, ?, 'reminder', ?, ?)",
                (_uuid(), r['user_id'], '提醒：' + str(r['content'])[:40], str(r['content'])))
            if r['repeat'] and r['cron']:
                parsed = parse_cron(r['cron'])
                nxt = _next_after(parsed, now) if parsed else None
                if nxt:
                    conn.execute('UPDATE reminders SET next_run=?, updated_at=NOW() WHERE id=?', (nxt, r['id']))
                else:
                    conn.execute("UPDATE reminders SET status='done' WHERE id=?", (r['id'],))
            else:
                conn.execute("UPDATE reminders SET status='done' WHERE id=?", (r['id'],))
            fired += 1
    return fired


def _uuid() -> str:
    import uuid
    return uuid.uuid4().hex


# ── 调度线程：每 60s 扫一次（进程内单例，随服务启停）──
_SCHED_INTERVAL = 60.0
_sched_thread: threading.Thread | None = None
_sched_lock = threading.Lock()
_sched_stop = threading.Event()


def start_scheduler() -> None:
    global _sched_thread
    with _sched_lock:
        if _sched_thread and _sched_thread.is_alive():
            return
        _sched_stop.clear()

        def _loop() -> None:
            while not _sched_stop.wait(_SCHED_INTERVAL):
                try:
                    _fire_due()
                except Exception:  # noqa: BLE001 - 调度永不冒泡
                    import logging
                    logging.getLogger('reminders').exception('提醒调度轮询失败')

        _sched_thread = threading.Thread(target=_loop, name='flora-reminders', daemon=True)
        _sched_thread.start()


def stop_scheduler() -> None:
    _sched_stop.set()
