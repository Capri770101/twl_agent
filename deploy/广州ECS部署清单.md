# 广州 ECS 部署清单（跳舞兰花卉智能体）

> 针对实例：`b56a5198e4d449e89d29d0101fe90731`（OpenClaw-于琴）
> 华南3（广州）· 2核2G · 40G 系统盘 · 公网 IP `8.138.203.6`
> 镜像 OpenClaw 2026.4.14 · 到期 2026-10-24

---

## 0. 现状评估（先看这个）

| 项 | 状态 | 结论 |
|---|---|---|
| 内存 | 0.96 / 2 GiB 已用 | 紧张。OpenClaw 已占一半，需加 swap（第 4 步） |
| 系统盘 | 23.33 / 40 GiB | 够用，剩 ~17G；注意定期清理 Docker 缓存 |
| 带宽 | 200 Mbps 峰值 | SSE 对话流量很小，无压力；生图走公网注意流量费 |
| 到期 | 2026-10-24 | **距到期不到 2 个月**，上线前先决定续费，避免备案/域名跟着实例走 |

**上线分水岭——域名问题（现在就定）：**

- 微信小程序**正式版**要求请求域名必须是 **HTTPS + 已 ICP 备案** 的域名，且域名需备案到你们主体。
- 没有备案域名 → 只能在微信开发者工具勾选「不校验合法域名」+ 手机预览开调试模式联调，**无法发布上线**。
- 这台是大陆服务器（广州），备案走阿里云 ICP 备案流程，通常 1-3 周。**如果还没有域名，今天就该启动这事**，它比部署本身慢得多。

---

## 1. 阿里云安全组（控制台操作）

入方向添加规则：

| 端口 | 授权对象 | 用途 |
|---|---|---|
| 443 | 0.0.0.0/0 | HTTPS（正式） |
| 80 | 0.0.0.0/0 | HTTP（跳转/签证书） |
| 22 | 你的常用 IP 段 | SSH（建议限源，不要 0.0.0.0/0 裸开） |

**8000 端口不要开公网**——agent 只走 Nginx 反代或本机访问。

---

## 2. 上传代码到服务器

本地（Git Bash / PowerShell）打包并上传：

```bash
cd /c/Users/Capri/Desktop
tar --exclude='__pycache__' --exclude='.workbuddy' --exclude='data' \
    -czf flora_agent_package.tar.gz flora_agent_package
scp flora_agent_package.tar.gz root@8.138.203.6:/opt/
```

服务器上解压：

```bash
cd /opt && tar xzf flora_agent_package.tar.gz && cd flora_agent_package
```

> Windows 下也可以用 WinSCP 图形化拖过去，效果一样。

---

## 3. 服务器现状排查（OpenClaw 镜像必做）

OpenClaw 镜像自带服务，起容器前先查端口和运行状态，**别盲目部署把人家在跑的东西顶掉**：

```bash
# 看哪些端口被占（重点 80 / 443 / 8000 / 5432）
ss -tlnp | grep -E ':(80|443|8000|5432)\b'

# 看 Docker 情况（OpenClaw 镜像大概率已装 Docker）
docker ps -a 2>/dev/null || echo "docker 未安装"

# 看内存和磁盘
free -h && df -h /
```

**分叉处理：**

- `80/443` 被 OpenClaw 的 Web 服务占用 → 要么把跳舞兰花卉智能体的 Nginx 改用别的端口（如 8443），要么复用现有 Nginx（把 `deploy/nginx.conf` 里的 `location` 块并入它的配置，`proxy_pass` 指向 `http://127.0.0.1:8000`）
- `8000` 被占 → 改 `docker-compose.yml` 端口映射（如 `"8001:8000"`）
- Docker 未装 → 第 5 步装

---

## 4. 加 swap（2G 内存必做）

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
sysctl -w vm.swappiness=10
echo 'vm.swappiness=10' >> /etc/sysctl.conf
free -h   # 确认 Swap: 2.0Gi
```

内存预算参考：Postgres ~150M + agent ~400-600M + Nginx ~20M，加上 OpenClaw 的 1G，2G 物理内存贴着上限跑，swap 是安全垫。

---

## 5. 安装 Docker（若第 3 步发现没有）

```bash
curl -fsSL https://get.docker.com | bash
systemctl enable --now docker
docker compose version   # 确认 compose 插件可用
```

---

## 6. 配置 .env

```bash
cd /opt/flora_agent_package
cp .env.example .env

# 一次性生成所有密钥（记下来，填进 .env）
python3 -c "import secrets; print('JWT_SECRET:', secrets.token_urlsafe(48))"
python3 -c "import secrets; print('PG_PASS:  ', secrets.token_urlsafe(24))"
python3 -c "import secrets; print('KEY_MINI: ', secrets.token_urlsafe(32))"
python3 -c "import secrets; print('KEY_H5:   ', secrets.token_urlsafe(32))"
```

`vim .env` 关键项：

```ini
APP_ENV=prod
DATABASE_URL=postgresql://flora:<PG_PASS>@postgres:5432/flora_agent
POSTGRES_PASSWORD=<PG_PASS>

# LLM 二选一填（hy 推荐 / OpenAI 兼容）
HY_API_KEY=sk-xxx
# 或
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://... 
LLM_MODEL=...

JWT_SECRET=<JWT_SECRET>
ALLOWED_ORIGINS=https://api.你的域名.com     # H5 接入时用；小程序请求不受 CORS 限制
PLATFORM_API_KEYS=wxmini=<KEY_MINI>,h5app=<KEY_H5>

# 小程序走内置微信登录时才需要
WECHAT_APPID=
WECHAT_SECRET=
```

> 生图如果走本机 `/generated`，建议后续把 `IMAGE_PUBLIC_BASE_URL` 指向 OSS/CDN——按量计费的 200M 峰值带宽，图片流量直接走这台会烧钱。

---

## 7. 启动

```bash
docker compose up -d --build
docker compose ps          # 三个服务应 healthy / running
docker compose logs -f agent   # 看到 Uvicorn running 即就绪，Ctrl+C 退出日志
```

自带数据库模式（postgres 容器）不需要额外建库；agent 会等 postgres 健康检查通过才启动。

---

## 8. 部署验证（服务器上执行）

```bash
# 健康检查
curl -s http://localhost:8000/health

# 平台换 token（换成 .env 里真实的 wxmini Key）
curl -s -X POST http://localhost:8000/auth/token \
  -H "X-API-Key: <KEY_MINI>" \
  -H "Content-Type: application/json" \
  -d '{"external_user_id": "smoke-test-001"}'
# 预期返回 JSON：platform_id=wxmini、user_id=wxmini_开头、带 access_token

# 用拿到的 token 验证鉴权闭环
curl -s http://localhost:8000/auth/me -H "Authorization: Bearer <上一步的access_token>"

# 反向验证：匿名登录在生产已被关闭
curl -s -X POST http://localhost:8000/auth/anonymous
# 预期 403，说明额度防线生效
```

---

## 9. HTTPS + 域名（小程序上线必经）

前提：已备案域名，DNS A 记录指到 `8.138.203.6`。

```bash
# 证书放进来（阿里云免费证书下载 Nginx 版，或 certbot 签 Let's Encrypt）
mkdir -p deploy/certs
# 放入 fullchain.pem 和 privkey.pem

# 改 deploy/nginx.conf 里两处 server_name 为实际域名
vim deploy/nginx.conf

# 启用 Nginx 容器（80/443）
docker compose --profile nginx up -d
curl -s https://api.你的域名.com/health   # 通了就是全链路就绪
```

然后到**微信公众平台** → 开发管理 → 开发设置 → 服务器域名 → request 合法域名 加 `https://api.你的域名.com`。

小程序端接入方式（给同事的口径）：
- 走平台通道（推荐）：你们后端持 `X-API-Key` 调 `POST /auth/token` 换 token → 小程序带 Bearer 调 `/chat` 系列
- 或走内置微信登录：直接调 `POST /auth/wx-login`（code 换 token），需在 .env 配小程序的 APPID/SECRET

---

## 10. 日常运维备忘

```bash
# 数据库备份（建议 crontab 每天一次）
docker exec flora-postgres pg_dump -U flora flora_agent | gzip > /opt/backup/flora_$(date +%F).sql.gz

# 磁盘清理（盘只剩 ~17G，每月跑一次）
docker system prune -f

# 更新代码重新部署
cd /opt/flora_agent_package && docker compose up -d --build
```

- **续费**：2026-10-24 到期，提前决定；换实例的话备案域名可以重新解析，数据用上面的备份迁移
- **Key 轮换**：`PLATFORM_API_KEYS` 换新 Key 后 `docker compose restart agent` 生效，用户身份不受影响（user_id 派生自平台标识+用户标识，不含 Key）
- **看日志**：`docker compose logs -f --tail=100 agent`

---

## 排障速查

| 症状 | 排查 |
|---|---|
| agent 起不来 | `docker compose logs agent`；大概率 .env 缺项（生产校验会直接报错） |
| postgres 一直 unhealthy | `docker compose logs postgres`；换 POSTGRES_PASSWORD 需先 `docker volume rm` 旧卷 |
| 外网访问不通 | 安全组没开 / 域名解析没生效 / Nginx profile 没启用 |
| SSE 流式断流 | Nginx 配置已带 `proxy_buffering off`，确认没被宿主机上其他 Nginx 套一层 |
| 内存报警 | `free -h` 看 swap；长期 OOM 就考虑升配到 2核4G |
