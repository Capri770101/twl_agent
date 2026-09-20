# 开发与发布流程

> **一条总原则：服务器上的代码永远来自 Git，不在线上手改。**

## 一、为什么要写这份文档

现状（2026-09-20 在服务器上实测）：

- 服务器代码目录 `~/flora_agent` **不是 git 仓库**（`git rev-parse` 直接报 fatal）；
- 目录里**没有任何版本标识文件** → 回答不了「线上跑的是哪个版本」，也没有回滚点；
- 部署靠手工 `scp` 覆盖 + `docker compose up -d --build`；
- 后果：改坏一行只能靠记忆还原，说不清某次变更何时上线。

好的一面（这让迁移很便宜）：

- `.gitignore` 已把**代码**与**运行时文件**分得很干净 ——
  `.env*` / `data/` / `data_demo/` / `deploy/certs/` 都不入库；
- 服务器能直连 GitHub：`git ls-remote` 实测成功，返回的正是最新提交。

## 二、三个环境（不用多买机器）

| 环境 | 位置 | 用途 | 数据 |
|---|---|---|---|
| **开发** | 本地 Windows | 写代码 + 跑单测（674 项、秒级、**不烧 LLM**） | 无 |
| **预发布** | 服务器 `demo` 栈 | 真 LLM 跑真实对话验证；同时是客户体验页 | `postgres-demo` 独立库 |
| **生产** | 服务器生产栈 | 对外 API | `postgres` |

> 关键认知：**预发布环境已经存在**了（`demo` profile 的独立栈，真 LLM + 独立库，
> 与生产完全隔离）。缺的不是环境，是「代码只从 Git 来」这条规矩。

## 三、日常流程（上线后就照这个走）

### 1. 本地改代码 + 跑测试

```bash
git checkout -b feat/xxx        # 有分支更清楚，单人开发直接改 main 也行
# ... 改代码 ...
pytest -q                       # 期望 674 passed，秒级完成、不烧 token
```

### 2. 上预发布验证（真实 LLM）

```bash
ssh -i "$PEM" "ubuntu@$HOST" "cd ~/flora_agent \
  && git pull \
  && docker compose --profile demo up -d --build agent-demo"
```

打开演示页跑 2~3 条真实对话，确认没有退化（尤其出卡、报价、错别字）。

### 3. 上生产

```bash
ssh -i "$PEM" "ubuntu@$HOST" "cd ~/flora_agent \
  && git pull \
  && docker compose up -d --build agent \
  && sleep 5 && curl -s localhost:8000/health"
```

### 4. 打 tag（没有 tag 就没有回滚点）

```bash
git tag -a v2026.09.20 -m "延迟优化上线"
git push origin --tags
```

### 5. 回滚

```bash
ssh -i "$PEM" "ubuntu@$HOST" "cd ~/flora_agent \
  && git checkout v2026.09.19 \
  && docker compose up -d --build agent"
```

## 四、一次性改造（约 20 分钟，做完上面流程才成立）

### ① 服务器代码目录 git 化（可做到零停机）

容器已经在跑，**在新目录构建期间线上不受影响**：

```bash
# 服务器上执行
cd ~
git clone https://github.com/Capri770101/twl_agent.git flora_agent_new

# 迁运行时文件（这些不在 git 里，必须手工搬过去）
cp ~/flora_agent/.env ~/flora_agent/.env.demo ~/flora_agent_new/ 2>/dev/null
cp -r ~/flora_agent/data ~/flora_agent/data_demo ~/flora_agent_new/ 2>/dev/null
mkdir -p ~/flora_agent_new/deploy
cp -r ~/flora_agent/deploy/certs ~/flora_agent/deploy/webroot ~/flora_agent_new/deploy/ 2>/dev/null

# 先只重建演示栈，验证新目录可用
cd ~/flora_agent_new
docker compose --profile demo up -d --build agent-demo
# 演示页确认正常后，再切生产
docker compose --profile nginx --profile demo up -d --build
```

⚠️ **切换注意**：compose 的项目名默认取自目录名（`flora_agent` → `flora_agent_new`），
直接起新栈会出现新旧容器并存、抢 8000 端口。稳妥顺序：
先停旧栈（`cd ~/flora_agent && docker compose --profile nginx --profile demo down`）
→ 起新栈 → 验证 → 确认后删掉旧目录。

### ② 版本可见性（一眼知道线上是哪个 commit）

```dockerfile
# Dockerfile：接住构建参数
ARG GIT_SHA=dev
ENV GIT_SHA=$GIT_SHA
```

```yaml
# docker-compose.yml：agent 与 agent-demo 两处都要加
build:
  context: .
  args:
    GIT_SHA: ${GIT_SHA:-dev}
```

```python
# main.py
import os
_VERSION = os.getenv('GIT_SHA', 'dev')

@app.get('/health')
async def health():
    return {'status': 'ok', 'version': _VERSION}
```

部署时带上 SHA：

```bash
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
```

⚠️ `/health` 目前**故意只返回 `{"status":"ok"}`**（2026-09-18 安全审计结论：不泄露内部信息）。
加上 `version` 属于**有意放开** —— 版本号本身不敏感，但需确认可接受。

### ③ 发布脚本（固化流程，别再靠记忆敲命令）

建议新增 `deploy/release.sh`，把这几步焊死：

```
拉取 → 记录版本 → 构建 → 健康检查 → 失败自动回滚到上一个 tag
```

历史上漏过的一步：本地文件是 **CRLF**，传到服务器必须 `sed -i 's/\r$//'`
（用 git 后不再需要，或加 `.gitattributes` 让 Git 自动转 LF —— 更彻底）。

## 五、三条铁律

1. **永远不在服务器上改代码**（`vim` / `sed` / 手工 cp 都不行）——只在本地改，走 Git。
2. **紧急修 bug 也走同一流程**（本地改 → 测试 → push → pull + rebuild，约 3 分钟）。
   「先上服务器救火」省下的 3 分钟，会在下次排查时加倍还回来。
3. **上线必打 tag**，回滚才有落点。

## 六、另需处理的遗留项

- `~/flora_agent` 里堆着 `.env.bak.20260917-*` —— 说明配置变更靠「手改 + 备份」。
  git 化后，配置项以 `.env.example` 为准，**改配置也要同步模板**。
- 生产环境目前跑的是**旧代码**：2026-09-18 之后的几轮优化（定价改造、延迟优化）
  只上了演示栈，还没上生产。建议把「git 化」与「正式上线」合并成一个动作，
  给生产一个干净的起点。
- 服务器资源余量：内存 3.7G（可用约 2.5G）、磁盘 39G 用 11G ——
  当前双栈够用，不必再加第三套环境。
