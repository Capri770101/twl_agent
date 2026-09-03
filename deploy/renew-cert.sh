#!/bin/sh
# 证书到期检查 / 续期
# 由 root cron 每周一 03:00 调用，结果写入 deploy/renew-cert.log
#
# 两种证书来源：
#   1) 阿里云免费证书（当前使用，本服务器连不上 Let's Encrypt）
#      有效期 3 个月，无法自动续期 —— 脚本只在剩余 <21 天时报警，需人工去控制台重新申请下载
#   2) Let's Encrypt（备用，需服务器能出网到 acme-v02；本服务器实测被墙）
#      若 deploy/letsencrypt 下存在证书，则尝试 certbot renew
set -e
BASE=/opt/flora_agent_package
CERT="$BASE/deploy/certs/fullchain.pem"
LOG="$BASE/deploy/renew-cert.log"
TS=$(date '+%F %T')

if [ -f "$CERT" ]; then
  # 取证书到期时间并计算剩余天数（openssl + date，POSIX 兼容）
  END=$(openssl x509 -enddate -noout -in "$CERT" | sed 's/notAfter=//')
  END_EPOCH=$(date -d "$END" +%s 2>/dev/null || python3 -c "import calendar,time,sys;print(int(calendar.timegm(time.strptime('$END','%b %d %H:%M:%S %Y %Z'))))")
  NOW_EPOCH=$(date +%s)
  DAYS=$(( (END_EPOCH - NOW_EPOCH) / 86400 ))
  echo "$TS cert expires in $DAYS days ($END)" >> "$LOG"
  if [ "$DAYS" -lt 21 ]; then
    echo "$TS [WARN] 证书剩余不足 21 天：阿里云免费证书需人工重新申请下载，覆盖 deploy/certs/{fullchain,privkey}.pem 后 docker restart flora-nginx" >> "$LOG"
  fi
else
  echo "$TS [WARN] 未找到 $CERT" >> "$LOG"
fi

# 备用路径：若使用 Let's Encrypt 签发过，则尝试续期（本服务器通常连不上，失败不影响主流程）
if [ -d "$BASE/deploy/letsencrypt/live" ]; then
  sudo docker run --rm \
    -v "$BASE/deploy/letsencrypt:/etc/letsencrypt" \
    -v "$BASE/deploy/webroot:/var/www/certbot" \
    certbot/certbot renew --webroot -w /var/www/certbot --quiet >> "$LOG" 2>&1 || \
    echo "$TS [INFO] Let's Encrypt 续期不可用（本服务器出站被墙），请走阿里云免费证书" >> "$LOG"
fi

echo "$TS done" >> "$LOG"
