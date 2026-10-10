# AI 接手指南

> 本文件面向 AI 编码助手（Claude Code / Cursor / Copencode 等），自动读取本文件即可了解项目约定。人类请从根目录 [README.md](README.md) 开始。

## 从哪里读信息（优先级从高到低）

1. **根 [README.md](README.md)** — 项目定位、能力边界、快速开始
2. **[DELIVERY.md](DELIVERY.md)** — 当前交付范围与验证结果
3. **[VERSION](VERSION) / [CHANGELOG.md](CHANGELOG.md)** — 当前版本与变更
4. **[docs/README.md](docs/README.md)** — 文档地图（docs 下唯一索引入口）
5. **源码本身** — 文档与代码冲突时，**以源码为准**

## ⚠️ 禁止把 docs/archive/ 当现行规范

- `docs/archive/` 内全部是历史快照、过程稿、旧版本记录；每个文件顶部有归档横幅
- 回答架构 / API / 部署 / 能力问题时，只引用这些**现行文档**：
  - `docs/03-系统架构设计.md`、`docs/04-API接口文档.md`、`docs/05-前端对接契约.md`
  - `docs/06-数据库设计.md`、`docs/07-部署手册.md`、`docs/10-安全与合规.md`
  - `docs/INTEGRATION-v1.4.0.md`、`docs/CONFIGURATION.md`
- 明确**未接通**的能力（取消订单 / 退款 / 外部推送 / 真实 DIY 下单等）以根 README「能力与边界」表为准，不要引用归档文档的乐观描述

## 代码结构速览

| 目录 | 内容 |
|------|------|
| `agent/` | ReAct 循环、工具注册、提示词、花艺知识库、编排策略 |
| `backend/` | FastAPI 路由、鉴权、存储、限流、监控 |
| `domain/` | 领域模型（花艺需求等） |
| `tests/` | pytest 回归（运行 `python -m pytest tests/ -q`） |
| `evals/` | 场景评测集（`scripts/validate_eval_set.py` 校验） |
| `scripts/` | 验证与运维脚本 |

## 修改纪律

- 改 API / 环境变量 / 版本 / 开关时，同步根 README、DELIVERY、CHANGELOG
- 保持"诚实边界"：不可用的操作返回明确错误码（如 `CHANNEL_UNAVAILABLE`），**不伪称成功**
- 危险操作（对外发消息、改配置、不可逆写入）保持人工审批或默认关闭
- 文档与配置示例中**不得**出现真实密钥、令牌、密码、用户 ID
- 新增文档遵循：每主题只留一份现行文档；被替代的移入 `docs/archive/`（不物理删除），并更新 `docs/README.md` 索引

## 常用验证命令

```bash
python -m pytest tests/ -q
python scripts/validate_eval_set.py
python scripts/validate_knowledge.py
git diff --check
```
