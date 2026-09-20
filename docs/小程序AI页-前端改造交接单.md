# 小程序 AI 页 · 前端改造交接单

> 编写时间：2026-09-20（周日）｜面向：小程序前端同事
> 依据：**2026-09-20 对生产环境 `POST /v1/ai/flower-advisor` 的真实实测**（不是推测，字段都是从真实响应里 dump 出来的）
> 关联：`docs/小程序接入待办清单.md`（更早的完整版，架构与鉴权说明看它；**本单是它的实测修订版**）

---

## 0. 一句话结论

**后端已经把卡片数据完整带回来了，是小程序前端只读了 `reply` 才没显示出来。**
不需要改后端就能让卡片出现；**只有"流式输出"这一件事需要后端配合**（见 §3）。

---

## 1. 实测：这个接口到底返回什么

请求（生产真实调用，耗时 **18.2s**）：

```http
POST http://<你们后端>/v1/ai/flower-advisor
Authorization: Bearer <小程序登录 token>
Content-Type: application/json

{ "message": "送女朋友生日，预算300左右，她喜欢淡色系", "shop_id": "s005" }
```

响应 `data` 的**真实字段**（实测 11 个）：

```
['user_id', 'reply', 'ui', 'data', 'action', 'tool_calls',
 'session_id', 'stage', 'products', 'ai_generated', 'content_disclosure']
```

| 字段 | 实测值示例 | 前端该拿它做什么 |
|---|---|---|
| `reply` | 一段 340 字的推荐文案 | 渲染气泡文字（现在只用了这个） |
| **`ui`** | `"plan_card"` | **卡片类型** → 决定渲染哪种卡片 |
| **`data.plans`** | 5 条商品的完整数组（见 §2） | **真实的商品卡数据在这里** |
| `session_id` | `"ff247f7d7fa2458685ee6c30523b4453"` | **存下来，下一轮原样回传** → 多轮上下文 |
| `tool_calls` | `['platform_db_query_entity']` | 可显示「正在查询门店/商品…」 |
| `ai_generated` | `true` | AIGC 标识（合规要求，必须展示） |
| `content_disclosure` | AIGC 说明文案 | 同上，直接展示 |
| `stage` | `"view_plan"` | 会话阶段（可忽略） |
| `action` | `{type:'show_plan', payload:{reply,...}}` | 可选：驱动页面动作 |

### 🔴 1.1 为什么现在卡片出不来（确切原因）

顶层那个 `products` 数组**看起来像商品，其实是摘要**，字段名不一样：

```
data.products[0] 真实字段 = ['plan_id', 'name', 'price_yuan', 'image', 'stock']
                            ↑ 没有 id、没有 price！
  {"id": null, "name": "春风十里不如你", "price": null,
   "image": "https://...jpg", "shop_id": null, "stock": 99}
```

而**能下单的数据在 `data.data.plans`**：

```
data.data.plans[0] 真实字段 =
['id','name','price','original_price','subtitle','description','flower_meaning',
 'flowers','image','images','tags','rating','sales','stock','season','shelf_life']

  {"id": "f_s005_1784618232974_79ar",   ← 真实商品 id（下单要用它）
   "name": "郁见你",
   "price": 158,                        ← 数字，单位元
   "image": "https://aistore.xiangbinmeigui.com/uploads/products/s005/...jpg",
   "shop_id": "s005"}                   ← 门店 id（下单要用它）
```

> 所以：**别再对顶层 `products` 做 `extractAgentProducts()` 猜数组** —— 那里的 `id`/`price` 恒为 `null`，
> 猜出来也点不了。正确做法是读 `data.data.plans`，用 `plan_id` 关联顶层摘要（如果要显示 `price_yuan` 的话）。

---

## 2. 卡片渲染规则（与 H5 已上线的实现一致）

`data.data.plans` 是**混合数组**，用 `diy` 标志区分两类，**必须分流渲染**：

| 类型 | 判别 | 关键字段 | 能否下单 |
|---|---|---|---|
| **现成商品** | `diy` 不为 `true` | `id`=`f_xxx`、`price`（数字）、`image`、`shop_id`、`stock` | ✅ **可以**，加购/下单用 `id` + `shop_id` |
| **DIY 定制方案** | `diy === true` | `plan_id`=`DIY_xxx`、**没有 `image`**、价格是 `estimated_price`（字符串，如「约 200 元」） | ❌ **不能**（没有商品 id，结算必失败） |

🔴 **DIY 卡千万不要走商品卡逻辑** —— 这是 H5 踩过的坑：无图、价格显示成「¥到店咨询」、点结算找不到商品。

DIY 方案的真实数据在 `design.*`（`main_flowers`/`fillers`/`foliage`、`color_scheme`、`packaging`、
`meaning`、`diy_steps`、`care_tips`、`card_message`、`difficulty`、`est_time`、`shelf_life`、
`suitable_for`、`caution`、`fees`），外层预算在 `budget_breakdown.items[{item,detail,amount}]` + `total_estimate`。
**DIY 卡的出口应该是「复制用料清单 / 生成效果图」，不是「立即购买」。**

> ⚠️ 平台数据里**不存在** `materials` / `budget` / `greeting_suggestion` / `price` / `skill_level` 这几个字段，
> 别按它们解析。

### 可直接移植的纯函数（H5 已经跑通，无需改写逻辑）

H5 仓库里这几个文件是**平台无关的纯函数**，小程序侧可直接搬（改成 CommonJS/ESM 即可，不含任何 DOM）：

- `H5/src/utils/diyPlan.js` —— DIY 方案解析（`plan_card.plans` → 结构化方案）
- `H5/src/utils/agentAsset.js` —— 相对图片路径 → 绝对 URL（**幂等**，可重复调用）
- `H5/src/utils/extractDiyPlan.js` —— 兜底从正文文本提取方案

参考渲染实现（`.vue` 需改写成 wxml/wxss，但**布局与字段用法可以直接对照**）：
`H5/src/components/AdvisorCards.vue`、`H5/src/components/AdvisorDiyCard.vue`

> `agentAsset` 的坑：图片可能是相对路径（`/agent/generated/xxx.png`）→ 必须拼上平台域名，
> 且**归一化函数必须幂等**，否则会被多层调用拼成 `/agent/agent/generated/x.png` → 404。

---

## 3. 🔴 唯一需要后端配合的事：流式输出

**实测确认：当前接口不是流式。**

```
POST /v1/ai/flower-advisor  →  http=200  耗时=18.2s（一次性返回整段）
```

后端实现是 `await` 等智能体说完再 `res.json()`（`/opt/flower-shop/server.js:1685`
的 `requestAiAgentChat(...)` → `res.json({code:0, data: result.body})`）。

**所以"文字一段段往外冒"这件事，改前端做不到** —— 必须后端加一个 SSE 转发路由。
这不是小程序前端同事能单方面解决的，需要商家后端（同一台机上的 `:3456`）新增：

```
POST /v1/ai/flower-advisor/stream   →  转发到平台 POST /chat/stream，边收边推
```

平台侧**已经支持真流式**（`/chat/stream`，SSE 事件：`text_delta` / `text_rollback` / `tool_call` / `card` / `done` / `error`），
H5 已经在用（`H5/deploy/server.cjs` 里 `/agent/*` 的 SSE 反代就是现成参考实现）。
🔴 反向代理必须 **关闭响应缓冲**（nginx `proxy_buffering off`），否则流式会退化成「转圈几秒后整段弹出」。

**优先级建议**：流式是**体验**问题，卡片解析是**闭环**问题。明天前先做卡片（§1/§2），流式放第二步。

---

## 4. 城市维度：一个会导致下单失败的隐患

实测：平台的商品接口 `GET /v1/merchant/products` **无视 `city` 参数，永远返回全平台 1855 个商品**。

而商家后端下单时会硬校验：**收货城市必须等于门店服务城市**，不匹配直接拒单
（文案「该商品由 XX 门店配送…请返回首页切换至 YY 市」）。

**后果**：用户在深圳、AI 推荐了福州门店的花 → 下单被拒 → 用户困惑。

**好消息**：只要 AI 入口传**真实 `shop_id`**，智能体就会把推荐锁定在那家店 —— 实测传 `shop_id=s005`
返回的活动商品 id 全是 `f_s005_*`，城市天然一致。

所以规则是（也是 `docs/小程序接入待办清单.md` §5 的设计意图）：

| 入口 | 传什么 `shop_id` | 结果 |
|---|---|---|
| 从**店铺页**进 AI | 该店真实 id（如 `s005`） | ✅ 推荐锁定该店，城市一致 |
| 从**首页**进 AI | 无具体店 → 需按用户所选城市挑一家店传，**或**前端按城市过滤 AI 返回的商品卡 | ⚠️ 否则可能推荐到别城商品 |

> ⚠️ `shop_id` 是**必填**且必须匹配 `/^[A-Za-z0-9_-]{1,64}$/`，不传直接 400。
> 传 `default` 在语法上能过，但智能体会当成「未锁店」→ 推荐全平台商品 → 就是上面的隐患。

---

## 5. 验收清单（可逐条点）

**闭环最小集（明天前必须过）**

- [ ] 发一句需求，能显示 `reply` 文字
- [ ] **能显示商品卡**：图 + 名称 + 价格（取 `data.data.plans`，不是顶层 `products`）
- [ ] 商品卡上有「立即购买/加购」，点击后**能进到下单流程**（用 `plans[i].id` + `plans[i].shop_id`）
- [ ] **DIY 方案卡与商品卡分流渲染**，DIY 卡**不出现**「立即购买」
- [ ] 拿到 `session_id` 并本地保存，**第二轮对话原样回传** → AI 记得上一轮
- [ ] 展示 `ai_generated` / `content_disclosure`（合规，必须有）

**体验（第二步）**

- [ ] 文字一段段往外冒（需后端先加 SSE 路由，见 §3）
- [ ] 过程中能看到「正在查询…」（`tool_calls` 有值时）
- [ ] 生图任务：`data` 顶层若带 `task_id`/`poll`，轮询 `GET /tasks/{id}`（**必须带 Bearer**），
      图是相对路径要用幂等函数拼域名
- [ ] 图片加载失败有兜底（不要白块）

---

## 6. 时间现实（务必对齐预期）

🔴 **"明天前上线"只能到「体验版」，正式发布做不到。**

小程序发版是：**开发者工具上传 → 设为体验版 → 提交审核 →（微信审核 1–3 天，首次可能更久）→ 发布**。
线上版本目前是 `20`，线上代码只在开发者本机。

因此明天前可行的目标：

| 目标 | 谁做 | 可行性 |
|---|---|---|
| 体验版可跑通「对话 → 卡片 → 下单」 | 前端同事（上传） | ✅ 今天/明天可以 |
| 后端加 SSE 流式路由 | 服务端（可自己改） | ✅ 可以做，但**不是闭环必需** |
| **正式发布上线** | 需过微信审核 | ❌ 明天前不可能 |

**建议**：今晚的目标定为「**体验版闭环可演示**」；正式发布按审核节奏排，别把审核时间算漏。

---

## 7. 「照着 H5 复刻一版」怎么做才不会翻车

结论：**路子对，但只给效果图不够。** 只给图，AI 会自由发挥布局细节、并**自己编一套数据结构**——
结果就是"像素像了、数据接错、点不了购买"。给 AI 的输入应该是
**「参考组件源码 + 差异清单 + 字段契约」**，让 AI 做的是 1:1 转换，而不是自由创作。

### 7.1 可视基线（活的效果图，不用截图）

**真机打开 `https://h5.tiaowulan.com/advisor`** —— 这就是已经上线的「花艺标本册」风格顾问页，
可以直接对着它逐屏确认，比看静态图准（能试错误态、能试滚动）。

🔴 注意：hero 欢迎区**只在「没有非 greeting 消息」时**渲染 → 想看初始态要点右上角「**＋ 新建对话**」。

### 7.2 要复刻的 8 种卡片（`H5/src/components/AdvisorCards.vue` 支持的全部类型）

| `ui` | 卡片 | 备注 |
|---|---|---|
| `plan_card` | 方案/商品卡 | 主卡，含商品与 DIY 两类分流 |
| `order_card` | 订单卡 | |
| `shop_card` | 店铺卡 | |
| `pay_jump` | 支付跳转 | |
| `image_task` | 生图任务 | 相对路径要拼域名 |
| `greeting_card` | 电子贺卡 | |
| `diy_plan_card` | DIY 方案卡 | 前端兜底合成的那种 |
| `dialog_options` | 选项 chips | 用户没头绪时给方向 |

另有三种**页面状态**不是卡片，但必须做：**思考中**（`streaming`）、**工具进度**（`tool_calls` 有值）、**AIGC 标识**。

### 7.3 文件分工：哪些直接搬、哪些交给 AI 转

| 处理方式 | 文件 | 原因 |
|---|---|---|
| ✅ **直接复制文件，别让 AI 重写** | `H5/src/utils/diyPlan.js`、`agentAsset.js`、`extractDiyPlan.js` | 平台无关纯函数。**重写会引入隐藏 bug**（例：`agentAsset` 的**幂等性**——被多层调用时若不幂等会拼出 `/agent/agent/generated/x.png` → 404） |
| 🤖 交给 AI 转写 | `H5/src/components/AdvisorCards.vue`、`AdvisorDiyCard.vue`、`src/pages/Advisor.vue` | 结构可 1:1 对照，但要按 §7.4 改写成 wxml/wxss/js |

### 7.4 🔴 H5 → 小程序 差异清单（AI 复刻最容易翻车的地方，必须一并给出）

**① 好消息：尺寸数值可以直接搬**
H5 的 `rpx(n)` = `n/750 rem`，而 H5 里 `1rem = min(视口, 480px)`；小程序里 `750rpx = 屏幕宽`。
**两者同尺度**（H5 的样式本来就是从小程序 1:1 移植过来的）→ **`rpx()` 里的数字原样写进小程序 rpx 即可**，不用换算。

**② 聊天页骨架必须改（照抄会不滚动）**
H5 用 `height:100dvh` + `overflow:hidden` + `flex:1; min-height:0` 撑出固定高度。
小程序要改成：`page { height: 100% }` + `<scroll-view scroll-y>` 作为消息区。
**照抄 `dvh` 会导致消息区不滚动、整页被拉长。**

**③ `<img>` → `<image>`，且必须显式给宽高**
小程序 `<image>` 不给宽高**不渲染**（H5 会自动撑开）。用 `mode="widthFix"` + `binderror` 兜底
（H5 有 `FlowerImage.vue` 做兜底，小程序要自己写）。

**④ 输入框与键盘（H5 完全没有这个概念）**
`confirm-type="send"` + `bindconfirm` 发消息；`adjust-position` 与 `cursor-spacing` 处理键盘顶起；
否则键盘会遮住输入框或整页跳。

**⑤ 事件与滚动到底**
`@click.stop` → `catchtap`；滚到底**不要**用 `scrollTop = scrollHeight`，
改用 `<scroll-view scroll-into-view="{{lastMsgId}}">` —— 更稳，也不受渲染时机影响。

**⑥ 没有 `EventSource`，流式只能靠 `wx.request` 的 chunk**
小程序不支持 SSE。**当前后端也不是流式**，所以第一版按非流式做。
将来接流式唯一可行路径是 `wx.request({ enableChunked: true })` + `onChunkReceived` 自己解析 SSE 报文。

**⑦ CSS 支持差异**
`dvh` / `gap`（旧基础库）/ 部分 `transition` 行为不一致 → 布局间距用 `margin` 兜底，先保证不重叠。

**⑧ 换行与富文本**
`<text>` 里换行需要 `\n` + `white-space: pre-wrap`（小程序默认不会保留换行）。

### 7.5 给 AI 的输入应该是什么（照着这个顺序喂）

1. **字段契约**（本单 §1/§2）→ 明确告诉它 `data.data.plans` 才是商品、`diy` 要分流
2. **参考组件源码**（§7.3 里"交给 AI 转写"那三个文件）
3. **本节 §7.4 的差异清单** → 明确告诉它哪些必须改
4. **验收清单**（§5）→ 让它按条目自检

缺了第 1 项，出来的是"好看的假界面"；缺了第 3 项，出来的是"在手机上不滚动/不显示图"的界面。

---

## 8. 状态触发清单（给同事：照着发这几何话，每种卡片都能复现）

跑基线页面时**别只是随便聊**——下面每种状态都有触发条件，有些还要等。建议每条都**截图存档**，
一起发给前端，她复刻时才有边界态可对照。

| 想看的状态 | 怎么触发 | 会出现什么 |
|---|---|---|
| **初始态 / hero** | 点右上角「**新建对话**」（hero 只在「无非 greeting 消息」时渲染） | 欢迎区（hero） |
| **4 个预设快捷问题** | 顶部 chips **常驻**（不受 hero 影响），随时可点 | 直接发起一轮：生日祝福 / 道歉和好 / 表白心意 / 感谢帮助 |
| **商品卡** `plan_card` | 点预设「生日祝福」，或发：`送朋友生日，预算200元左右，希望温暖明亮一点` | `reply` 文字 + 若干商品（`data.data.plans`，`id=f_xxx`） |
| **DIY 方案卡** `diy_plan_card` | 发：`我想用香槟玫瑰和洋桔梗自己搭配一束，预算200` | 结构化方案：主花/配材/步骤/养护/预算明细（**无商品 id，不能下单**） |
| **效果图** `image_task` | DIY 之后说：`给我看看效果图`（智能体会主动调 `generate_effect_image`），或点卡上「生成效果图」 | 生成中骨架 + 计时 → 出图（**很慢，>40s，要等**） |
| **选项 chips** `dialog_options` | 发模糊需求：`我想要一束花，不知道选什么好` | 2–4 个可点按钮（风格/色系方向） |
| **店铺卡** `shop_card` | 问：`这家店几点关门？配送费多少？` | 店铺卡（营业时间/配送费/起送价/评分） |
| **电子贺卡** `greeting_card` | 说：`帮我写一张贺卡，送给妈妈` | 贺卡卡（可换模板/改文案） |
| **跨会话历史** | **先开一段新对话**再说：`上次你给我推荐的那家店` | 结合历史会话做答（不是本轮上下文） |
| **多轮记忆** | 第二轮说：`预算再降一点` | AI 应记得上一轮的预算与色系，而不是重新问一遍 |
| **订单卡 / 支付跳转** `order_card` `pay_jump` | 走完一次加购 → 下单 | 订单卡 / 拉起支付 |

> 建议至少截这 6 张给我方对齐：**初始态、商品卡、DIY 卡、效果图（生成中+已出图）、选项 chips、思维/工具进度**。
> `tool_calls` 有值时页面会显示「正在查询…」，这个中间态也要截（小程序没做的话用户会对着空屏等十几秒）。
