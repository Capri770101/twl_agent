# DIY 成交通道方案

> 状态：**讨论稿**（2026-09-21，Capri 提出「DIY 方案怎么生成订单」）
> 一句话结论：**后端已经做到头了**（方案 + 可复制的需求单都在下发），真正缺的是
> 「**平台认账的入口**」。这不是后端没做好，而是**两端各缺一块** —— 平台缺认账入口、接入方缺复制按钮。

---

## 1. 现状核实

每一条都有代码坐标，可直接复核。

| 环节 | 状态 | 位置 |
|---|---|---|
| DIY 方案生成 | ✅ 已就绪 | `agent/tools.py::design_diy_plan`、`agent/diy_tools.py::revise_diy_plan` |
| 锁店缺料标注 | ✅ 已就绪 | `annotate_shop_materials`（标「该店暂无」，**保留花材不替换**）|
| **可复制的需求单** | ✅ **后端一直在下发** | `agent/tools.py::build_plan_copy_text` → `plan['copy_text']`；改版路径 `agent/diy_tools.py:68` 同步重算 |
| 对外契约声明 | ✅ 已写明 | `GET /ui-contract` 的 `plan_card` 条目已注明「建议在卡片上加一个『复制用料清单』按钮」 |
| **接入方渲染** | ❌ **一个都没接** | 只有 `demo/index.html` 的 `diyCard()` 接了。H5 `AdvisorDiyCard.vue` **已经有 `copy()` / `copyHint`**（现用于贺卡文案），但没接用料清单；`H5/src/utils/diyPlan.js` 也**未透传** `copy_text` |
| 线上下单 | ❌ 不可用 | `agent/skills/skill_order.py::create_order` 代码完整（payload 组装 / 幂等 ID / 响应解析 / DIY 落库）但**已移出注册表**，且无 `PLATFORM_ORDER_API_URL`（按既有约定**不得配置**）|

### 「没有 SKU」到底卡住了什么

SKU（货号）= 平台认账的商品记录。平台的 `products` 每条就是一个 SKU，带 `id`(f_xxx) / `price` / `stock` / `image`。
用户在卡片点「下单」，本质是拿 `id` + `shop_id` 去结算接口 —— 平台查得到这条记录才开得了单。

DIY 方案是模型当场算出来的，`plan_id`（`DIY_xxx`）是**我方 `backend/storage/diy.py` 自己生成的**，
平台后台从来没有这条记录 → 拿它去结算接口只会得到「查无此商品」。

所以「没有 SKU」的完整含义是：**平台没法给 DIY 开单、定价、扣库存、分账**。
H5 之前踩过的坑（无图、价格显示「¥到店咨询」、点结算找不到商品）就是这件事的直接后果。

---

## 2. 三条候选路线

### 路线 A｜平台开放「定制需求单」写入接口 —— 唯一能产生**真订单**的路

**我方已备好的请求体**（`skill_order.py::_build_payload`，无需重写）：

| 字段 | 含义 | 备注 |
|---|---|---|
| `request_id` | `AG{YYYYMMDDHHMMSS}{6位hex}` | 我方幂等键，平台可用于去重 |
| `channel` | 固定 `flora_agent` | 便于平台区分来源 |
| `external_user_id` / `agent_session_id` | 用户身份 / 会话 ID | |
| `shop_id` | 承接店铺 | 锁店会话下由后端强制锁定 |
| `plan` | `{plan_id, name, type(existing\|diy), price, desc, image, recipient, occasion, card_message}` | 定制单快照 |
| `items` | DIY 时 = `[{kind:'flower', name, qty}]` + `{kind:'design_note', text}` | **结构化花材清单**，平台可直接换算/核库存 |
| `estimated_total` | 估算金额 | 明确标注「最终以平台计算为准」 |
| `remark` | 来源说明 | |

**期望响应**（`_parse_platform_order` 已能兼容多种命名）：
`order_id|order_no|id|trade_id`、`status`、`total_price|total|amount|pay_amount`、
`pay{page_path, params, url}`。

**我方改造清单**
1. 与环境变量 `PLATFORM_ORDER_API_URL`（可选 `PLATFORM_ORDER_API_KEY`）对齐；
2. 重新注册 `create_order`（`skill_order.py:199` 注释段取消注释）+ prompt 里恢复「用户要买 → create_order」的指引；
3. 按平台实际字段微调 `_build_payload` / `_parse_platform_order`（两个函数就是为此预留的对齐点）。

**卡点：全部在平台方** —— 要新增写接口 + 处理鉴权、库存校验、履约、支付分账。
对应待办 `P-2`，已挂了较长时间。

> ⚠️ **一个能大幅缩短路径的可能性**：`twl/` 里存在 `merchant.tiaowulan.com` 与 `admin.tiaowulan.com`
> 的证书，说明**商家后台是真实存在的**。若它由本方/可推进的人维护，则不必等「公开 API」——
> 只要商家后台能收一张定制单，甚至可以直接把 DIY 需求**落成平台订单表里的一种特殊订单**
> （金额取 DIY 报价、items 放花材清单），订单列表 / 微信支付 / 分账全部免费复用。
> **这一条取决于「商家后台在谁手里」，是全局变量。**

### 路线 B｜复制用料清单，发给店家 —— 唯一**零依赖、可立即执行**的动作

后端已就绪，**只差客户端加一个按钮**。

**H5 的精确改动（2 处）**
1. `H5/src/utils/diyPlan.js` 的 `normalizeDiyPlan()` 返回对象里补一行：
   `copyText: String(p.copy_text || '').trim()`
2. `H5/src/components/AdvisorDiyCard.vue` 在 `.card-actions`（约 129 行）附近加一个按钮 + 一句说明，
   **直接复用文件里已有的 `copy()` / `copyHint` / `showHint()`（194–209 行）**：
   - `v-if="d.copyText"` 才渲染（**缺字段绝不自行拼接**，否则口径会和卡片漂移）
   - 文案示例：「复制用料清单」+「平台暂不支持定制款线上下单 —— 复制发给花店即可沟通报价」

小程序等效做法：`wx.setClipboardData({ data: copyText })`。

**它能换来什么**：需求真的到达商家。
**它换不来什么**：订单、GMV、转化率、可追踪、可分账 —— 用户跳出线上闭环后就断线了。

> ⚠️ **硬前提**：接入方必须存在「联系店家」的入口（店家微信 / 客服 / 电话）。
> 若复制完用户无处可发，这个按钮就白加了。**这一点需要接入方确认。**

### 路线 C｜挂在最接近的在售成品上（载体 SKU）—— 降级兜底

思路：复用**现有商品卡结算链路**（微信支付 / 分账 / 配送范围全部现成），
挑一个价格与色系最接近的在售成品作为「载体」，把 DIY 用料需求写进订单备注 / `card_message`。

- **优点**：零平台新接口、当场闭环、复用全部既有履约能力。
- **风险（必须正视）**：**履约一致性差** —— 用户看到的是成品图，商家很可能照成品做；
  除非平台愿意在订单里展示定制备注，否则用户收到的很可能不是方案里那束花（差评风险）。
- **定位**：只作「用户今天就要拿到花」时的兜底，**不建议作为主线**。

### 明确不建议

接入方自建「定制单」走自己的微信支付收款 —— 平台没有 SKU 就**无法分账**，
资金与履约责任全部压到接入方，合规风险不可控。

---

## 3. 决策矩阵

| | A 平台定制需求单 | B 复制用料清单 | C 载体 SKU |
|---|---|---|---|
| 产生真订单 | ✅ | ❌ | ✅（但非 DIY 那束）|
| 依赖平台方改接口 | ✅ 强依赖 | ❌ 无 | ❌ 无 |
| 我方工作量 | 小（对齐字段 + 注册）| **极小（H5 两处）** | 中（选品逻辑 + 备注透传）|
| 可立即执行 | ❌ | ✅ | ✅ |
| 成交可追踪 / 可分账 | ✅ | ❌ | ✅ |
| 履约一致性 | ✅ | ✅ | ⚠️ 差 |
| 主要风险 | 排期不可控 | 跳出闭环、易流失 | 做错花 |

---

## 4. 建议与推进顺序

1. **B 立刻做** —— 成本极低（H5 两处、复用已有复制函数），是当前唯一能让 DIY 产生商业动作的出口。
   顺带把 `demo/index.html` 已验证的剪贴板兜底一起搬过去。
2. **A 同步推** —— 把上面的字段契约直接甩给平台方评估；同时**先查清商家后台归属**，
   若在自家人手里，路径会比想象中短得多。
3. **C 只作兜底** —— 用户明确「今天就要」且 A 未就绪时启用。
4. **不建议** 自建定制单收款。

---

## 5. 待确认（只有 Capri 能答）

1. **考核口径**：DIY 要的是**真订单**（进 GMV）还是**需求到达商家**？—— 这决定 B 算不算成功。
2. **商家后台归属**：`merchant.tiaowulan.com` 由谁维护、能否改？—— 这决定 A 是「一周」还是「无限期」。
3. **接入方有没有「联系店家」入口**？—— 这决定 B 会不会白做。

---

## 附：相关代码与文档索引

| 用途 | 位置 |
|---|---|
| 需求单文本生成 | `agent/tools.py::build_plan_copy_text`（`_COPY_TEXT_MAX = 2000`，超长截断留痕）|
| 方案产出（两条通路）| `agent/tools.py::design_diy_plan`、`agent/diy_tools.py::revise_diy_plan` |
| 下单 payload / 响应解析 | `agent/skills/skill_order.py::_build_payload` / `_parse_platform_order` |
| 契约声明 | `backend/routers/chat.py` 的 `GET /ui-contract`（`plan_card` 条目）|
| 演示页参考实现 | `demo/index.html::diyCard()` + `copy()` 兜底（约 983–1014 行）|
| H5 待改点 | `H5/src/utils/diyPlan.js`（透传）、`H5/src/components/AdvisorDiyCard.vue`（按钮）|
| 小程序交接单 | `docs/小程序AI页-前端改造交接单.md`（§2 卡片渲染规则）|
| 待办登记 | `docs/11-变更记录.md` 的 `P-2`（阻塞成交）、`P-3`（花材库接口）|
