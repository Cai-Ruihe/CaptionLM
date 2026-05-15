<div align="center">

<img src="assets/banner.png" alt="CaptionLM — macOS 实时双语字幕翻译" width="100%" />

# CaptionLM

**macOS 实时双语字幕翻译 — 任何应用的音频都能翻译**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![macOS](https://img.shields.io/badge/macOS-12+-black.svg)](https://www.apple.com/macos/)

[English](README.md) · [安装](docs/INSTALL.md) · [API 申请](docs/API_SETUP.md) · [架构](docs/DESIGN.md)

</div>

---

CaptionLM 可以捕捉 macOS 上**任意应用**的音频 —— 浏览器、Zoom、Netflix、视频会议 —— 实时转录并在屏幕上叠加双语字幕。你可以独立选择语音识别引擎和翻译模型，按需平衡延迟、成本和质量。

## ✨ 特性

- 🎙️ **捕捉任意应用音频** —— 基于 ScreenCaptureKit，无需虚拟声卡
- 🌐 **多引擎可选** —— 语音识别 (Google Cloud / Qwen ASR / Qwen LiveTranslate / Apple SpeechAnalyzer / Whisper) 和翻译器 (Gemini / Qwen-MT / Claude / Google 免费) 自由组合
- 💬 **双语悬浮字幕** —— 原文 + 译文，永远置顶，可拖动，透明度可调
- 📺 **自动导出 SRT** —— 每次会话自动保存双语 `.srt` 到 `~/Documents/CaptionLM/sessions/`
- 🪙 **实时成本表** —— 在设置面板里看每次会话的 token 用量和 USD 成本
- 🔄 **自动重连** —— macOS 中断音频流时自动重试
- 🇨🇳 **支持新加坡 (国际版) 和中国大陆 endpoint** —— Qwen / DashScope 两边都能用

## 🎬 演示

<div align="center">

<img src="docs/screenshots/overlay-running.png" alt="CaptionLM 双语字幕悬浮窗实时运行" width="100%" />

*双语悬浮字幕在 YouTube 视频上实时运行 — 上方是日文原文，下方是中文翻译*

<br/>

<img src="docs/screenshots/settings-panel.png" alt="CaptionLM 设置面板" width="100%" />

*设置面板 — STT 引擎和翻译器独立选择，输入你自己的 API key*

</div>

## 🚀 快速开始

### 方案一 —— 直接下载 .dmg（推荐普通用户）

1. 从 [Releases](https://github.com/Cai-Ruihe/CaptionLM/releases) 下载最新 `.dmg`
2. 拖 `CaptionLM.app` 到 `/Applications`
3. **在终端跑一次**这条命令，清除 macOS Gatekeeper 的隔离标记：
   ```bash
   xattr -cr /Applications/CaptionLM.app
   ```
   *（因为 CaptionLM 尚未做 Apple Developer ID 签名，详见 [首次启动注意事项](#%EF%B8%8F-首次启动注意事项10-之前)）*
4. 打开应用，授予 **屏幕录制** 权限（系统设置 → 隐私与安全性 → 屏幕录制 → 勾选 CaptionLM）
5. 点击菜单栏图标 → **Settings** → 输入 API key（详见 [API 申请](docs/API_SETUP.md)）
6. 点击 **Start**，然后在任何应用里播放音频

### 方案二 —— 从源码运行（开发者）

```bash
git clone https://github.com/Cai-Ruihe/CaptionLM.git
cd CaptionLM
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"
captionlm
```

详细安装步骤、Python 配置和故障排查见 [docs/INSTALL.md](docs/INSTALL.md)。

## ⚠️ 首次启动注意事项（1.0 之前）

CaptionLM 目前使用 **ad-hoc 自签名**（不是 Apple Developer ID + 公证，那个需要 $99/年），所以**macOS Gatekeeper 和 TCC 系统会把每次下载、每次升级都当成"未知 app"**。在正式签名之前需要两个一次性的小操作。**这两个步骤都不影响功能**，只是 macOS 的"我能信任这个 app 吗"流程。

### 1. 首次打开时弹 "CaptionLM is damaged and can't be opened"

这个提示**有误导性**——`.app` 实际上**没坏**。macOS 会给所有通过浏览器下载的文件加一个 `com.apple.quarantine` 扩展属性，Gatekeeper 对 ad-hoc 签名的 app 看到这个属性就直接拒绝运行。一条命令解决：

```bash
xattr -cr /Applications/CaptionLM.app
```

之后正常双击就行。**每次下载新版只需要做一次。**

### 2. 升级到新版本后，"屏幕录制"权限被重置

把 `/Applications/CaptionLM.app` 换成新版 `.dmg` 之后，macOS 可能会**再次**弹屏幕录制授权对话框（即使你给老版本授权过了）。原因是每次新构建的 ad-hoc 签名都不同，TCC（Apple 的权限数据库）把它当成了一个不同的 app。

**解决方法：**

1. **系统设置 → 隐私与安全性 → 屏幕与系统录音**
2. 在列表里找到 **CaptionLM**——把开关**关掉**再**重新打开**（或者点 `−` 号删掉旧条目，再点 `+` 号添加新的 `.app`）
3. **退出 CaptionLM 再重新打开**——新的授权下一次启动生效
4. 如果你启用了**麦克风**回退，同样的流程也适用于麦克风权限

### 为什么会有这两个临时方案

这两个摩擦点都源于同一个原因：**CaptionLM 暂时没有 Apple Developer ID + 公证**（$99/年 + Apple 的自动恶意软件扫描）。一旦有了正式签名：

- ❌ "Damaged" 警告永远不会出现——Gatekeeper 会认可签名
- ❌ TCC 不会在升级时重置——签名身份保持一致

Apple Developer ID 签名 + 公证已在 [路线图](#%EF%B8%8F-路线图) 上。在此之前，上面这两条命令就是全部的临时方案。

## 🔑 API 配置

CaptionLM 软件本身免费，但大部分语音识别和翻译引擎需要 API key。**所有 key 都存在你本地**，不经过任何第三方服务器。最常用的组合：

| 组件 | 推荐选择 | 费用估算 |
|---|---|---|
| **语音识别** | Google Cloud Speech-to-Text streaming | ~$0.024/分钟，**每月免费 60 分钟** |
| **翻译** | Gemini 2.5 Flash | ~$0.01/小时语音 |
| **替代：纯 Qwen** | Qwen LiveTranslate（端到端） | ~$0.005/分钟，约 ¥0.36/小时 |

各 provider 的详细申请流程（含 Google Cloud Console 和阿里云 Model Studio 截图）见 **[docs/API_SETUP.md](docs/API_SETUP.md)**。

## 🔄 更新

macOS 的 `.app` 是绿色软件 —— **没有安装/卸载流程**，所谓"更新"就是用新的 `.app` 替换旧的：

1. 退出正在跑的 CaptionLM（菜单栏图标 → Quit，或点 ✕）
2. 下载新的 `.dmg`
3. 把新的 `CaptionLM.app` 拖进 `/Applications` → Finder 问 "替换吗？" → **替换**
4. 在终端跑 `xattr -cr /Applications/CaptionLM.app` 清掉 Gatekeeper 隔离标记（一次性，详见 [首次启动注意事项](#%EF%B8%8F-首次启动注意事项10-之前)）
5. 重新授权屏幕录制 — 详见 [首次启动注意事项 #2](#2-升级到新版本后屏幕录制权限被重置)
6. 重新打开 CaptionLM

> 步骤 4-5 只是在 Apple Developer ID 签名做好之前需要。一旦有正式签名，这两步就自动消失。

你的设置和数据**全部保留**（它们都不在 `.app` 内部）：

| 数据 | 位置 |
|---|---|
| API key、UI 设置 | `~/Library/Application Support/CaptionLM/settings.toml` |
| Google Cloud 凭证 JSON | `~/.captionlm/*.json` |
| 双语字幕历史 SRT | `~/Documents/CaptionLM/sessions/*.srt` |

如果要卸载：把 `CaptionLM.app` 拖到废纸篓即可。彻底清理（含 API key）：

```bash
rm -rf /Applications/CaptionLM.app
rm -rf ~/Library/Application\ Support/CaptionLM ~/.captionlm ~/.cache/captionlm
```

## 🎯 工作原理

<div align="center">

<img src="assets/poster.png" alt="CaptionLM 工作原理 — 音频捕获、STT、翻译、悬浮字幕" width="100%" />

</div>

CaptionLM 通过 ScreenCaptureKit 截取任意 macOS 应用的音频流（不需要虚拟声卡），喂给你选择的语音识别引擎，把识别出的文本送到你选择的翻译器，最后把双语字幕叠加在屏幕上，永远置顶可拖动。每个环节独立可换，所以你可以按延迟、成本、质量自由组合不同的 provider。

## 🏗️ 架构（技术细节）

<details>
<summary>展开查看完整数据流</summary>

```
┌────────────────────────────────────────────────────────────────┐
│  macOS 应用（YouTube / Zoom / 任意应用）                        │
└──────────────────────┬─────────────────────────────────────────┘
                       │ 系统音频
                       ▼
            ┌──────────────────────┐
            │  capture_audio.swift │  ← ScreenCaptureKit
            │  (PCM @ 16kHz 单声道) │
            └──────────┬───────────┘
                       │ stdout: float32 PCM
                       ▼
        ┌────────────────────────────────────┐
        │  STT 引擎（可插拔）                  │
        │  • Google Cloud Speech 流式         │
        │  • Qwen ASR (WebSocket 实时)        │
        │  • Qwen LiveTranslate (端到端)      │  ← 跳过翻译器
        │  • Apple SpeechAnalyzer            │
        │  • Faster-Whisper (本地)            │
        └──────────┬─────────────────────────┘
                   │ (text, is_final)
                   ▼
        ┌────────────────────────────────────┐
        │  翻译器（可插拔）                    │
        │  • Gemini 2.5 Flash (流式)          │
        │  • Qwen-MT Turbo (流式)             │
        │  • Anthropic Claude                │
        │  • Google 翻译（免费）              │
        └──────────┬─────────────────────────┘
                   │ (原文, 译文)
                   ▼
        ┌────────────────────────────────────┐
        │  悬浮字幕（PyObjC NSPanel）         │  ← 永远置顶
        │  • 实时区（当前句子）                │
        │  • 历史区滚动（最近 50 条）          │
        └────────────────────────────────────┘
```

架构细节（utterance 缓冲、partial revision 处理、自动重连）见 [docs/DESIGN.md](docs/DESIGN.md)。

</details>

## 🛣️ 路线图

- [ ] **Apple Developer ID 签名 + 公证** —— 消除"damaged"警告以及升级时的 TCC 权限重置（见 [首次启动注意事项](#%EF%B8%8F-首次启动注意事项10-之前)）
- [ ] Windows 支持（用 WASAPI loopback 替代 ScreenCaptureKit）
- [ ] 翻译后期再润色（retranslation polish）扩展到更多 provider
- [ ] 用 GitHub Actions 一键构建 `.dmg`
- [ ] 设置面板本地化（目前只有英文）

## 🤝 贡献

欢迎 PR！开发流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 📜 协议

MIT —— 见 [LICENSE](LICENSE)。

---

<div align="center">
<sub>由 PySide6、PyObjC、和大量日译中字幕测试构建。</sub>
</div>
