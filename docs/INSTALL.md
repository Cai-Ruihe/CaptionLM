# Installation Guide / 安装指南

中英对照。Chinese first, English follows.

## 系统要求 / Requirements

- **macOS 12 Monterey 或更新**（推荐 macOS 14 Sonoma+，ScreenCaptureKit 最稳）
- **Python 3.11+**（3.13 实测可用）
- **Xcode Command Line Tools**（用于编译 Swift 音频捕获 helper）
- 一个或多个 API key（见 [API_SETUP.md](API_SETUP.md)）

---

## 方法一：下载 .dmg（推荐）/ Pre-built DMG (recommended)

1. 去 [Releases](https://github.com/Cai-Ruihe/CaptionLM/releases) 下载最新 `.dmg`
2. 打开 `.dmg`，拖 `CaptionLM.app` 到 `/Applications`
3. 第一次打开会被 Gatekeeper 拦：
   - 系统设置 → 隐私与安全性 → "已阻止 CaptionLM" 旁边点 **仍要打开**
   - 或者命令行：`xattr -d com.apple.quarantine /Applications/CaptionLM.app`
4. 授权 **屏幕录制** 权限：
   - 系统设置 → 隐私与安全性 → **屏幕录制** → 找到 CaptionLM → 打开开关
   - 重启 CaptionLM 让权限生效
5. 点菜单栏图标 → **Settings** → 填 API key（见 [API_SETUP.md](API_SETUP.md)）
6. 点 **Start**，开始翻译

---

## 方法二：从源码运行 / From source

### 1. 安装 Xcode Command Line Tools

```bash
xcode-select --install
```

如果已经装了 Xcode，跳过。

### 2. 安装 Python 3.11+

推荐用 [Homebrew](https://brew.sh/)：

```bash
brew install python@3.13
```

### 3. 克隆 + 安装

```bash
git clone https://github.com/Cai-Ruihe/CaptionLM.git
cd CaptionLM
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"
```

`[all]` 会装所有 provider 客户端（google-cloud-speech、google-genai、openai、anthropic、websockets）+ macOS PyObjC 框架。如果只想装一部分，可以选：

```bash
pip install -e ".[providers,macos]"      # 不要 Whisper（省 ~1GB 磁盘）
pip install -e ".[whisper,macos]"        # 只要 Whisper（本地，无 cloud）
pip install -e ".[macos]"                # 只 macOS 基础，配 Google 免费翻译可跑
```

### 4. 运行

```bash
captionlm
```

第一次启动会弹 macOS 屏幕录制权限对话框，授权后重启应用。

---

## 自己打包成 .dmg / Building the DMG yourself

如果想自己改代码 + 分发，需要 `py2app` 和 `create-dmg`：

```bash
brew install create-dmg
pip install -e ".[dev]"
cd packaging
./build_dmg.sh
```

完成后 `packaging/dist/CaptionLM.dmg` 是产物。

### 注意 / Caveats

- **未签名**：脚本输出的 .dmg 没有 Apple Developer 签名。分发给别人时他们要 `xattr -d com.apple.quarantine` 或在系统设置里允许
- **公证（Notarization）**：要给别人正式发布，需要 $99/年的 Apple Developer 账号。脚本里有占位注释告诉你怎么改

---

## Troubleshooting

### `swiftc` not found

需要 Xcode CLT：`xcode-select --install`

### 启动后 NSPanel 不出现

macOS 屏幕录制权限没给。系统设置 → 隐私与安全性 → 屏幕录制 → 勾上 CaptionLM → 完全退出 CaptionLM 再重启。

### `Failed during stream due to application connection being interrupted`

ScreenCaptureKit 错误码 `-3805`，通常是同时有别的应用在抓系统音频（Zoom 屏幕共享、OBS、QuickTime 录屏）。停掉那个应用再试。

### 启动慢（> 5 秒）

第一次冷启动比较慢（Python 解释器 + PyObjC 加载），属正常。第二次启动应该 < 1 秒。如果一直慢，可能是 macOS 后台 Spotlight 索引、磁盘 cache 冷。重启 Mac 试试。

### `Qwen authentication failed (HTTP 401)`

你的 DashScope key 和 Region 不匹配。国际版 key 必须配 `intl` region，国内版 key 必须配 `cn`。Settings → DashScope Region 切换。

---

## 更新到新版本 / Updating

macOS 的 `.app` 是绿色软件 —— **不需要"卸载"再装**。来新版本只要替换 `.app` 即可。

### 步骤

1. **先把正在跑的 CaptionLM 退出**（点字幕窗的 ✕，或菜单栏图标 → Quit）
2. 下载新的 `CaptionLM.dmg`
3. 双击 .dmg 打开
4. 把 `CaptionLM.app` 拖到右边的 `Applications` 别名 → Finder 弹"已有同名项目，要替换吗？"，选 **替换**
5. 重新打开 CaptionLM

### 你的数据全部保留

替换 `.app` **不会动**这些用户数据（它们都不在 `.app` 内部）：

| 数据 | 位置 |
|---|---|
| API keys、UI 设置 | `~/Library/Application Support/CaptionLM/settings.toml` |
| Google Cloud 凭证 JSON | `~/.captionlm/*.json` |
| 历史字幕 SRT | `~/Documents/CaptionLM/sessions/*.srt` |
| Swift binary 缓存 | `~/.cache/captionlm/`（新 .app 启动时自动覆盖更新） |

打开新版本后，所有 API key 和设置都还在，开箱即用。

---

## 卸载 / Uninstalling

### 简单卸载（只删 .app，保留你的数据）

把 `/Applications/CaptionLM.app` 拖到废纸篓即可。

### 彻底卸载（清空所有相关文件）

⚠️ 这会删除你的 API keys、设置和字幕历史。**确认你不想留再操作**。

```bash
# 1. .app 本身
rm -rf /Applications/CaptionLM.app

# 2. 配置 + API keys（注意：有 API key 在这里）
rm -rf ~/Library/Application\ Support/CaptionLM/
rm -rf ~/.captionlm/

# 3. Swift binary 缓存（无害，但可以清掉）
rm -rf ~/.cache/captionlm/

# 4. 字幕历史导出（你的劳动成果 —— 一般想保留）
# rm -rf ~/Documents/CaptionLM/sessions/   # ← 注释掉了，按需手动跑
```

---

## English (quick reference)

**Requirements**: macOS 12+, Python 3.11+, Xcode CLT, at least one provider API key.

**Quick install from source**:
```bash
xcode-select --install
brew install python@3.13
git clone https://github.com/Cai-Ruihe/CaptionLM.git
cd CaptionLM
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"
captionlm
```

**First-launch checklist**:
1. Grant Screen Recording permission (System Settings → Privacy & Security → Screen Recording)
2. Settings → paste API keys (see `API_SETUP.md`)
3. Start

**Updating**: just download the new `.dmg`, quit the running CaptionLM, drag the new `.app` into `/Applications`, accept the "Replace" prompt. All your settings (API keys at `~/Library/Application Support/CaptionLM/settings.toml`, Google credentials at `~/.captionlm/`, SRT exports at `~/Documents/CaptionLM/sessions/`) are kept — they live outside the `.app`.

**Uninstalling**: drag `CaptionLM.app` to the Trash. For a clean nuke, also `rm -rf ~/Library/Application\ Support/CaptionLM ~/.captionlm ~/.cache/captionlm`.

For `.dmg` builds and troubleshooting see the Chinese section above — same content.
