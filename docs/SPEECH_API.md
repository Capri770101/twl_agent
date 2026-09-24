# 语音 API（TTS / ASR）

语音输入与语音回复能力。本地地址由运行配置确定，不依赖已停用的 api.tiaowulan.com。
所有接口使用现有 Bearer JWT，`user_id` 必须与 token sub 一致；`AUTH_REQUIRED=false` 仅限本地开发。

底层为阿里云百炼原生 `multimodal-generation` 接口（复用 `LLM_API_KEY`），与对话、生图同厂商：
- TTS：`qwen3-tts-flash`
- ASR：`qwen3-asr-flash`

> OpenAI 兼容的 `/audio/speech`、`/audio/transcriptions` 对这两个模型返回 404，不要走 compatible-mode。

## 设计原则：语音只念引导，不念全文

语音回复**不朗读**完整方案、推荐列表或知识长文。`/chat` 响应会额外带一个
`speech_text` 字段，由服务端按响应类型确定性生成（零 LLM 成本、零延迟），内容只是
「结果一句话 + 下一步怎么操作」，详细内容仍由屏幕上的卡片承载。

| 响应 `ui` | `speech_text` 示例 |
|---|---|
| `plan_card`（DIY） | 定制方案做好了，一共 1 个。点保存可以存进你的方案，用料清单可以复制给花店确认报价。 |
| `plan_card`（现货） | 帮你挑了 5 款花束。点卡片可以看详情，喜欢就直接加购物车结算。 |
| `image_task` | 效果图正在生成，大概十几秒，好了会自动显示。 |
| `greeting_card` | 贺卡做好了，可以下载保存，或者直接用在订单里。 |
| `dialog_options` | 给你几个选项，点屏幕上的按钮告诉我就行。 |
| `text`（长答） | 玫瑰花期五到七天。详细内容已经显示在屏幕上，可以慢慢看。 |

前端语音模式流程：

```text
用户语音 → 录音上传 /speech/transcribe → 得到文字
        → 文字填入 /chat 的 message → 得到 reply + speech_text
        → 用 speech_text 调 /speech/tts → 播放音频
```

`speech_text` 为空串表示语音能力未启用（`SPEECH_ENABLED=false`）或该响应不适合播报，
前端应回退到「不自动播报」。

## 1. 文本转语音

`POST /speech/tts`

```json
{"user_id":"换票返回的用户ID","text":"方案做好了，点保存可以存进你的方案。"}
```

返回：

```json
{"text":"方案做好了，点保存可以存进你的方案。","voice":"Cherry","audio_url":"/generated/speech_bc9b32a528752a72.wav","cached":false,"ai_generated":true}
```

- `audio_url` 为相对路径时拼接当前 API 地址；格式 WAV（16bit / mono / 24kHz）。
- 文本按 `(model, voice, text)` 哈希缓存，同文案 `cached=true` 且不重复合成。
- 超过 `SPEECH_TTS_MAX_CHARS`（默认 300）会截断而非拒绝，保证语音模式至少能听到前半段引导。
- 建议调用方直接传 `/chat` 返回的 `speech_text`，不要传完整 `reply`。

## 2. 语音转文本

`POST /speech/transcribe?user_id=<换票返回的用户ID>`

`multipart/form-data`，字段 `file`（录音文件）：

```text
file: <binary>  content-type: audio/wav | audio/mpeg | audio/m4a | audio/aac | audio/ogg | audio/webm | audio/amr | audio/silk
```

返回：

```json
{"text":"我想送妈妈一束生日的花","ai_generated":true}
```

- `user_id` 走查询参数（multipart body 不便再塞 JSON）。
- 前端应限制录音 ≤ 60 秒；音频上限 `SPEECH_ASR_MAX_BYTES`（默认 10MB）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SPEECH_ENABLED` | `true` | 关闭时 `/speech/*` 返回 503，`/chat` 不带 `speech_text` |
| `SPEECH_API_KEY` | 空 | 留空回退 `LLM_API_KEY`（同厂商复用） |
| `SPEECH_TTS_MODEL` | `qwen3-tts-flash` | TTS 模型 |
| `SPEECH_TTS_VOICE` | `Cherry` | 音色 |
| `SPEECH_ASR_MODEL` | `qwen3-asr-flash` | ASR 模型 |
| `SPEECH_TTS_MAX_CHARS` | `300` | 播报文本上限 |
| `SPEECH_ASR_MAX_BYTES` | `10485760` | 上传音频上限 |
| `SPEECH_ASR_MAX_SECONDS` | `60` | 录音时长建议上限 |

## 错误与限制

- 401 缺少/无效 Bearer；403 身份不一致；413 音频过大；415 音频格式不支持；
  422 参数不合规；429 限流；503 语音未启用或实例繁忙；504 超时；502 上游合成/识别失败。
- 进程内 2 个并发槽位 + 按用户限流（`speech-tts:<uid>` / `speech-asr:<uid>`）；多进程不共享限流。
- TTS 结果落 `data/generated/speech_*.wav`，与生图共用静态托管；清理策略见运维手册。
- 用量落 `speech_logs` 表（kind/model/units/cached/latency/status），供监控面板统计；音频不绑定订单、不自动播放。

## 试用（无需前端）

`scripts/try_speech.py` 可在不写任何 UI 的情况下验证链路、试听音色、产生真实用量：

```bash
# 本机容器，演示栈匿名登录
python scripts/try_speech.py --text "方案做好了，点保存可以存进你的方案。"

# 生产（走 H5 反代），需要平台 API Key
python scripts/try_speech.py --base-url http://<H5-IP>/agent --api-key <PLATFORM_KEY> \
    --text "帮你挑了3款花束，点卡片看详情。" --roundtrip

# 转写一段已有录音
python scripts/try_speech.py --base-url ... --asr-file my_voice.wav
```

- TTS 音频默认存 `./speech_preview.wav`，可直接播放试听。
- `--roundtrip`：TTS 合成后把音频送回 ASR，对比识别文本与原文（验证两个方向都通）。
- key 优先从环境变量 `PLATFORM_API_KEY` 读取；`--api-key` 明文参数可能进入 shell history。

## 监控

语音用量并入现有调用监控面板（`/dashboard/`），不另起炉灶：

- 埋点：`backend/observability.py::record_speech_call`，best-effort（失败只记 warning，不影响语音主链路）。
- 端点：`GET /api/metrics/speech?hours=24`（受 `DASHBOARD_API_KEY` 保护），返回：

```json
{
  "hours": 24,
  "tts": {"calls": 1, "errors": 0, "cached_hits": 1, "chars": 18, "avg_latency_ms": 1, "last_at": "..."},
  "asr": {"calls": 1, "errors": 0, "seconds": 3, "avg_latency_ms": 477, "last_at": "..."}
}
```

- 面板「语音用量」卡片展示：TTS 合成次数/字符数、缓存命中（=省掉的上游合成）、ASR 次数/秒数、错误数、平均延迟。
- 计费口径：TTS `chars`=字符数，ASR `seconds`=音频秒数，与百炼计费维度一致，可据此估算成本。
- TTS `units` 为请求文本字符数（包含缓存命中），不是实际账单；ASR 优先取上游 `usage.seconds`，缺失时由 WAV 头估算，其他格式无法估算时为 0。

## 前端接入前提（重要）

**麦克风权限需要安全上下文（HTTPS）**：微信内置浏览器与现代手机浏览器在 HTTP 页面
不授予 `getUserMedia`。当前 H5 经 `http://<ip>/` 访问，语音输入在手机端不可用（桌面 Chrome 除外）。
智能体端点本身与传输层无关，已可用；前端录音 UI 待 HTTPS 域名就绪后启用。
