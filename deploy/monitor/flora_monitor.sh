#!/usr/bin/env bash
# 跳舞兰花卉智能体 · 运行状态巡检告警脚本
# 安装位置（服务器）: /home/admin/monitor/flora_monitor.sh
# webhook 配置（服务器）: /home/admin/monitor/webhook.conf  →  WEBHOOK="https://qyapi.weixin.qq.com/..."
# cron 示例: * * * * * /home/admin/monitor/flora_monitor.sh >> /home/admin/monitor/monitor.log 2>&1
#
# 检查项:
#   1) 公网 HTTPS 健康检查 (域名+nginx+证书+服务 全链路)
#   2) 三个容器运行且 healthy
#   3) 磁盘使用率 > 85%
#   4) 可用内存 < 150MB
#   5) agent 近 6 分钟错误日志 >= 3 条 (阈值设计: 已知 LLM 空 content 的偶发 traceback 属正常兜底, 少量不告警)
#
# 告警策略: 状态翻转时才发 (故障发一次告警, 恢复发一次通知), 不会每分钟轰炸

set -u

# ---------- 配置 ----------
BASE_URL="https://api.tiaowulan.com"
STATE_FILE="/home/admin/monitor/.state"
WEBHOOK_FILE="/home/admin/monitor/webhook.conf"
DISK_PCT_MAX=85          # 磁盘告警阈值 %
MEM_AVAILABLE_MIN_KB=150000  # 内存告警阈值 (KB)
ERR_LOG_WINDOW="6m"      # 日志回看窗口
ERR_LOG_THRESHOLD=3      # 错误条数阈值
HOST_TAG="$(hostname)"

[ -f "$WEBHOOK_FILE" ] && . "$WEBHOOK_FILE"   # 读取 WEBHOOK=... (可选, 为空则只写日志不推送)

# ---------- 工具函数 ----------
send() {  # send "消息内容"
    local msg="$1"
    # 压成单行并去掉双引号, 避免手工拼 JSON 出错
    msg="$(printf '%s' "$msg" | tr '\n' ' ' | tr -d '"')"
    if [ -n "${WEBHOOK:-}" ]; then
        curl -s -m 10 -H 'Content-Type: application/json' \
             -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"$msg\"}}" \
             "$WEBHOOK" > /dev/null 2>&1 || true
    fi
    echo "[$(date '+%F %T')] $msg"
}

prev_state="OK"
[ -f "$STATE_FILE" ] && prev_state="$(cat "$STATE_FILE" 2>/dev/null || echo OK)"

# ---------- 巡检 ----------
issues=""

# 1) 公网健康检查
http_code="$(curl -o /dev/null -s -m 15 -w '%{http_code}' "$BASE_URL/health" 2>/dev/null || echo 000)"
if [ "$http_code" != "200" ]; then
    issues="${issues}·公网/health异常(HTTP $http_code) "
fi

# 2) 容器状态
for c in flora-agent flora-nginx flora-postgres; do
    if ! docker ps --filter "name=$c" --filter "health=healthy" --format '{{.Names}}' 2>/dev/null | grep -q "^$c$"; then
        if docker ps --filter "name=$c" --format '{{.Names}}' 2>/dev/null | grep -q "^$c$"; then
            [ "$c" != "flora-nginx" ] && issues="${issues}·${c}运行中但不健康 "   # nginx 无 healthcheck, 运行即视为正常
        else
            issues="${issues}·${c}未运行 "
        fi
    fi
done

# 3) 磁盘
disk_pct="$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')"
if [ "${disk_pct:-0}" -ge "$DISK_PCT_MAX" ]; then
    issues="${issues}·磁盘已用${disk_pct}% "
fi

# 4) 内存
mem_avail="$(awk '/MemAvailable/ {print $2}' /proc/meminfo 2>/dev/null || echo 999999999)"
if [ "${mem_avail:-999999999}" -lt "$MEM_AVAILABLE_MIN_KB" ]; then
    issues="${issues}·可用内存不足$((mem_avail/1024))MB "
fi

# 5) agent 错误日志
err_count="$(docker logs --since "$ERR_LOG_WINDOW" flora-agent 2>&1 | grep -icE 'traceback|exception|ERROR' || true)"
if [ "${err_count:-0}" -ge "$ERR_LOG_THRESHOLD" ]; then
    issues="${issues}·${ERR_LOG_WINDOW}内错误日志${err_count}条 "
fi

# ---------- 状态翻转判定 ----------
now="$(date '+%F %T')"
if [ -n "$issues" ]; then
    echo "$now FLORA-CRITICAL [$HOST_TAG] 智能体巡检异常: $issues"
    if [ "$prev_state" != "ALERT" ]; then
        send "🔴[跳舞兰智能体] 巡检异常 @${now}
${issues}
处置: ssh $HOST_TAG 后 'docker ps' 与 'docker logs --since 10m flora-agent' 排查"
    fi
    echo "ALERT" > "$STATE_FILE"
else
    echo "$now FLORA-OK 一切正常 (公网/health=${http_code}, 磁盘=${disk_pct}%, 可用内存=$((mem_avail/1024))MB, ${ERR_LOG_WINDOW}错误日志=${err_count}条)"
    if [ "$prev_state" = "ALERT" ]; then
        send "🟢[跳舞兰智能体] 已恢复正常 @${now}
公网/health=${http_code}, 磁盘=${disk_pct}%, 错误日志已清零"
    fi
    echo "OK" > "$STATE_FILE"
fi
