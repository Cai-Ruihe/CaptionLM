# API 申请与配置指南 / API Setup Guide

> 中文在前，英文在后。Chinese first, English follows.

CaptionLM 不附带任何 API key —— 所有 key 都需要你自己向各 provider 申请，然后在 Settings 面板里粘贴进去。这份文档手把手讲清楚每个 provider 怎么申请、怎么填、怎么验证。

CaptionLM ships with no API keys — every credential is something you obtain yourself from the provider, then paste into the Settings panel. This document walks through each provider step by step.

---

## 目录 / Contents

- [推荐组合](#推荐组合--recommended-combos)
- [① Google Cloud Speech-to-Text（STT）](#-google-cloud-speech-to-textstt)
- [② Gemini API（翻译）](#-gemini-api翻译)
- [③ Qwen / DashScope（STT + 翻译）](#-qwen--dashscopestt--翻译)
- [④ Anthropic Claude（翻译）](#-anthropic-claude翻译)
- [Google 免费翻译（无需申请）](#google-免费翻译--google-free-translate)
- [常见问题](#常见问题--troubleshooting)

---

## 推荐组合 / Recommended combos

| 场景 | STT | 翻译 | 月预算 |
|---|---|---|---|
| **零成本试用** | Apple SpeechAnalyzer (macOS 26+) 或 Whisper（本地）| Google 免费翻译 | $0 |
| **质量优先** | Google Cloud Speech-to-Text | Gemini 2.5 Flash | ~$5（每天 1-2 小时使用）|
| **延迟优先 / 国内** | Qwen ASR | Qwen-MT Turbo | ~¥10/月 |
| **最简单** | Qwen LiveTranslate（一个 API 同时做两件事）| 不需要 | ~¥20/月 |

---

## ① Google Cloud Speech-to-Text（STT）

**为什么用 Google Cloud STT**：质量最稳，流式 partial 延迟低，**每月免费 60 分钟**。代价：注册流程比较繁琐（需要 GCP 账号 + 信用卡 + 服务账号 JSON）。

**Why Google Cloud STT**: best ASR quality, low-latency streaming partials, and the **first 60 minutes per month are free**. Trade-off: signup is more involved (GCP account + credit card + service-account JSON).

### 步骤 / Steps

1. **创建 Google Cloud 账号**
   - 打开 [console.cloud.google.com](https://console.cloud.google.com/)，用 Google 账号登录
   - 第一次会引导你创建 GCP 项目 + 加信用卡。**新用户 $300 试用额度**，期间用完 60 分钟免费额度不会真正扣费。

2. **启用 Speech-to-Text API**
   - 进入 [Speech-to-Text API 页面](https://console.cloud.google.com/apis/library/speech.googleapis.com)
   - 点 **ENABLE** 按钮
   - 等几秒钟启用完成

3. **创建服务账号（Service Account）**
   - 进入 [IAM → Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
   - 点 **CREATE SERVICE ACCOUNT**
   - 名字随便（比如 `captionlm-stt`），点 **CREATE AND CONTINUE**
   - 角色选 **`Cloud Speech Client`**（最低权限），点 **CONTINUE** → **DONE**

4. **生成 JSON key**
   - 在 Service Accounts 列表里点你刚建的那个
   - 选 **KEYS** 标签 → **ADD KEY** → **Create new key** → **JSON** → **CREATE**
   - 浏览器会自动下载一个 `.json` 文件，**这就是你的 STT 凭证**

5. **在 CaptionLM 里加载**

   方法 A（推荐）：把 JSON 文件放到 `~/.captionlm/`：
   ```bash
   mkdir -p ~/.captionlm
   mv ~/Downloads/your-service-account-*.json ~/.captionlm/
   chmod 600 ~/.captionlm/*.json
   ```
   CaptionLM 启动时会自动扫描这个目录，认出 service-account 类型的 JSON 并加载。

   方法 B：通过 Settings 面板：
   - 打开 CaptionLM → 菜单栏图标 → **Settings**
   - **API Keys** 区域里找 **Google Cloud Credentials**
   - 点 **Choose JSON...** 选你刚下载的文件
   - 状态会变成绿色 `✓ ~/.captionlm/your-file.json`

6. **验证**：在 STT 下拉框选 **Google Cloud Speech**，点 **Start**，播放任意视频。如果两秒内字幕开始出现 = 配置成功。

### 费用 / Cost

- 流式 STT：**$0.024/分钟**
- 免费额度：**每月 60 分钟**
- 一小时视频 = $1.44 - $0.024×60 = $0（前 60 分钟）/ $1.44（之后每小时）

⚠️ **保管好 JSON 文件**。它等于服务账号的密码。**不要上传到 GitHub 或公开分享**。

---

## ② Gemini API（翻译）

**为什么用 Gemini**：当下性价比最高的 LLM 翻译，**Gemini 2.5 Flash** 翻译质量好、延迟低（~1 秒）、便宜。

**Why Gemini**: best value LLM for translation right now. 2.5 Flash gives good quality, low latency (~1s), and is cheap.

### 步骤 / Steps

1. **打开 [Google AI Studio](https://aistudio.google.com/apikey)**（**注意**：这不是 Google Cloud，是另一个站点）

2. 用 Google 账号登录

3. 点 **Create API key**
   - 选已有的 GCP 项目（推荐复用上面 STT 那个项目，账单和 quota 统一）
   - **或者**直接选 "Create API key in new project"（不需要绑定信用卡，但 quota 有限）

4. 复制生成的 `AIza...` 开头的 key

5. **在 CaptionLM 里填入**
   - 菜单栏 → **Settings**
   - **API Keys** 区域 → **Gemini** 输入框 → 粘贴 key
   - 点 **Save**

6. **验证**：Translator 下拉框选 **Gemini 2.5 Flash**，点 Start 测试。日志里 grep `Gemini stream: TTFT=` 应该能看到 < 1 秒的延迟。

### 费用 / Cost

- Gemini 2.5 Flash：$0.15/M input tokens, $0.60/M output tokens
- 一小时讲话 ≈ 5000 字 ≈ 10K tokens ≈ **每小时 < $0.01**
- 免费额度：**每天 200 个请求**（远超日常使用）

---

## ③ Qwen / DashScope（STT + 翻译）

阿里云的 DashScope 平台同时提供：
- **qwen3-asr-flash-realtime** — 流式语音识别（替代 Google STT）
- **qwen3-livetranslate-flash-realtime** — 端到端语音 → 翻译（一个 API 搞定）
- **qwen-mt-turbo** — 专门的翻译模型（替代 Gemini）

**核心优势**：国内访问稳定、单价便宜（约为 Google 1/4）、国际版有新加坡 endpoint 不需要梯子。

⚠️ **关键**：国际版和国内版是**两套独立账号**，key 不通用。在国外/海外华人 → 用 **国际版（intl）**；在中国大陆 → 用 **国内版（cn）**。

### 步骤（国际版 / intl）/ Steps (international)

1. **打开 [Alibaba Cloud Model Studio (Singapore)](https://modelstudio.console.alibabacloud.com/?serviceSite=international)**

2. 注册 / 登录 —— 用邮箱注册即可，**不需要中国身份证**

3. 进 **API-KEY 管理** → **Create API key**

4. 复制 `sk-...` 开头的 key

5. **在 CaptionLM 里填**：
   - Settings → API Keys → **DashScope (Qwen)** 输入框 → 粘贴
   - DashScope 区域旁边有 **Region** 下拉框 → 选 **intl (Singapore)**
   - 点 **Save**

### 步骤（国内版 / cn）/ Steps (China mainland)

1. 打开 [百炼平台 (中国大陆)](https://bailian.console.aliyun.com/)
2. 用阿里云账号登录（需要实名认证）
3. 进 **API-KEY 管理** → 创建 key
4. 复制 `sk-...` key
5. CaptionLM Settings：DashScope key 粘贴 + Region 选 **cn (Mainland)**

### 模型选择 / Model picks

CaptionLM 提供三种用 DashScope 的方式，按你的取舍选：

| 模式 | STT 选 | Translator 选 | 优势 |
|---|---|---|---|
| **纯 Qwen 端到端** | `Qwen LiveTranslate` | (none — 内置) | 最快、最便宜 |
| **Qwen ASR + Qwen 翻译** | `Qwen ASR` | `Qwen-MT Turbo` | 灵活 + 便宜 |
| **Qwen ASR + Gemini** | `Qwen ASR` | `Gemini 2.5 Flash` | Qwen 识别中文/日文准 + Gemini 翻译自然 |

### 费用 / Cost

- Qwen ASR：$0.00009/sec ≈ **$0.0054/分钟**（约 Google STT 的 1/4）
- Qwen-MT Turbo：**$0.5 / 1M output tokens**（约 Gemini 的 1/1）
- Qwen LiveTranslate：每分钟约 ¥0.06

### 验证

启动后 grep log：
- `Qwen WS connecting (..., region=intl)` 或 `region=cn`
- `Qwen FINAL push` —— 表示 ASR 工作
- `Qwen-MT stream: TTFT=...ms` —— 表示翻译工作

如果出现 `Qwen authentication failed (HTTP 401)` → 你用错了 region 的 key。intl key 不能给 cn endpoint 用，反之亦然。

---

## ④ Anthropic Claude（翻译）

可选的备用翻译器。

1. 打开 [console.anthropic.com](https://console.anthropic.com/)
2. 注册 / 登录，加信用卡
3. 进 **Settings → API Keys** → **Create Key**
4. 复制 `sk-ant-...` 开头的 key
5. CaptionLM Settings → API Keys → **Claude** → 粘贴 → Save

### 费用

- Claude 3.5 Haiku：$0.80/M input tokens, $4.00/M output tokens
- 比 Gemini 2.5 Flash 贵 ~6 倍，质量略好。一般推荐用 Gemini。

---

## Google 免费翻译 / Google Free Translate

**不需要任何 API key**。Settings 里 Translator 选 **Google Translate (Free)** 即可。

⚠️ 局限：
- 没有 LLM 那种语境理解，短句、上下文敏感的句子翻译质量差
- 经常被限速（间歇性返回 429）
- 不要用于商业用途（违反 Google 服务条款）

适合纯试用，认真使用建议至少配 Gemini。

---

## 常见问题 / Troubleshooting

### Google STT 一直显示 "No Google Cloud credentials found"

- 检查 `~/.captionlm/` 目录是否存在且至少有一个 `.json` 文件
- 检查 JSON 文件内容是不是 service-account 类型（`"type": "service_account"`）。用户账号导出的 OAuth JSON **不行**
- 文件权限：`chmod 600 ~/.captionlm/*.json`
- 路径里不能有空格或特殊字符

### Gemini 返回 "[Error] You exceeded your current quota"

- 免费 tier 每天 200 个请求，超了就 429
- 等 24 小时，或者升级到付费 tier
- 或者切到 Qwen-MT / Claude

### Qwen 一直 "Authentication failed (HTTP 401)"

- 90% 的情况：region 选错了。国际版 key 不能用在 cn endpoint
- 重新打开 Settings 检查 DashScope Region 下拉框

### 设置面板 API key 输入框被挤压

- 已修复（v0.1.0 起），默认窗口高度 940px。如果你看到挤压请把窗口拉到至少 900px 高，并报告 issue 附上你的 macOS 版本

### 我的 API key 会被上传到任何服务器吗？

**不会**。Key 保存在本地：
```
~/Library/Application Support/CaptionLM/settings.toml
```
所有 API 请求直接从你的电脑发到 Google / Anthropic / 阿里云。CaptionLM 没有任何后端服务器。

### 我有 .pyc / .log 文件想分享给 issue，但里面有 API key 怎么办？

CaptionLM 的日志默认会 redact `Bearer sk-...` 和 `Bearer AIza...` 这类 token。但保险起见，发 issue 前还是 `grep -i "sk-\|AIza\|key" your-log.txt` 自己检查一遍。

---

## English version

The Chinese version above is comprehensive. For an English-only quick reference:

- **Google Cloud STT**: console.cloud.google.com → enable Speech-to-Text API → create Service Account with `Cloud Speech Client` role → download JSON key → drop into `~/.captionlm/`
- **Gemini**: aistudio.google.com/apikey → Create API key → paste into Settings → API Keys → Gemini
- **Qwen / DashScope (intl)**: modelstudio.console.alibabacloud.com → API-KEY → create → paste, set Region = intl
- **Qwen / DashScope (cn)**: bailian.console.aliyun.com → similar, set Region = cn
- **Claude**: console.anthropic.com → Settings → API Keys → paste

All keys are stored locally in `~/Library/Application Support/CaptionLM/settings.toml`. No backend, nothing routes through our servers.
