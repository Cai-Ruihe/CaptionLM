# Build Checklist — `.dmg` 打包前 + 测试

按这个清单跑一遍，确保最终的 `.dmg` 对**零编程背景的用户**是开箱即用的。

---

## 1. Build machine 环境（一次性）

```bash
# Xcode CLT — 提供 swiftc（预编译 capture_audio）+ codesign + iconutil
xcode-select --install

# create-dmg — 把 .app 打包成 .dmg
brew install create-dmg

# Python 3.11+，建议用 pyenv 或 brew
brew install python@3.13

# 在 CaptionLM_release/ 目录下创建虚拟环境 + 装所有依赖
cd CaptionLM_release
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[all,dev]"
pip install py2app
```

✅ 验证：
```bash
which swiftc        # /usr/bin/swiftc
which create-dmg    # /opt/homebrew/bin/create-dmg (Apple Silicon) 或 /usr/local/bin/create-dmg
python3 -c "import py2app; print(py2app.__version__)"
python3 -c "import captionlm"
python3 -c "import PySide6, numpy, sounddevice"
python3 -c "from google.cloud import speech; from google import genai; import openai, anthropic, websockets"
```

全部不报错才能进下一步。

---

## 2. 跑 build_dmg.sh

```bash
./packaging/build_dmg.sh
```

脚本会自动做 5 件事：

| 步骤 | 检查项 |
|---|---|
| 0 | sanity check：swiftc / create-dmg / py2app / captionlm 都能 import |
| 1 | 用 swiftc 预编译 `capture_audio.swift` → binary |
| 2 | 清理 `packaging/build/` 和 `packaging/dist/` |
| 3 | py2app 打包 `CaptionLM.app`（3-8 分钟） |
| 4 | smoke test：验证 `capture_audio`、`logo-overlay.png`、`icon.icns` 都在 .app 里 |
| 5 | `create-dmg` 包成 `CaptionLM.dmg` |

如果任何一步红色报错，**停下来排查**，不要直接发布。

---

## 3. 本地测试 .app（**必做**）

在打包机器上先跑：

```bash
open packaging/dist/CaptionLM.app
```

逐项检查：

- [ ] 没有任何 ImportError / 红框报错
- [ ] 菜单栏图标出现
- [ ] 字幕窗（NSPanel）出现，左下角有 logo
- [ ] 点 logo → 浏览器打开 GitHub 页面
- [ ] 打开 Settings → 5 行 API key 输入框第一次显示就在同一行（不挤压）
- [ ] 填入 API key → 保存 → 切换 STT 引擎 → 点击 Start → 播放任意视频 → 字幕出现

---

## 4. 在干净机器上测试 .dmg（**强烈建议**）

最关键的一步。打包机器上**所有依赖都装齐**，但目标用户机器可能什么都没有。

理想测试：找一台**从来没装过 Python、Xcode、brew** 的 macOS，复制 `.dmg` 过去：

1. 双击 `.dmg` → 拖 `CaptionLM.app` 到 `/Applications`
2. 第一次双击 .app：会被 Gatekeeper 拦截
   - 右键 .app → **打开** → 系统弹窗 → 选择"打开"
3. 系统弹屏幕录制权限：**系统设置 → 隐私与安全性 → 屏幕录制 → 勾选 CaptionLM**
4. 重启 .app
5. 按 §3 的逐项检查

如果干净机器跑不起来 → 必有依赖没打包好 → 回头改 `setup_app.py` 的 `packages` / `includes` 列表。

---

## 5. 常见打包问题排查

### `ImportError: No module named google.cloud.speech` 在干净机器上

py2app 的静态分析器漏了 google-cloud-speech 的某些子模块（gRPC 经常有这问题）。

修复：编辑 `packaging/setup_app.py`，在 `packages` 列表里加：
```python
"google",
"google_cloud_speech",
"grpc",
"grpc._cython",
```

### `dyld: Library not loaded: ...PySide6/Qt/...`

py2app 没正确处理 Qt 的动态库引用。重新跑 build_dmg.sh，如果还有问题，考虑用 `--include-frameworks` 选项。

### `.app` 打开秒退

打开 `Console.app` → 搜 `CaptionLM` → 看 Python traceback。99% 是某个 import 没打包进去。

---

## 6. 发布到 GitHub Releases

```bash
# 给 .dmg 加版本号（可选）
mv packaging/dist/CaptionLM.dmg packaging/dist/CaptionLM-0.1.0.dmg

# 在 GitHub 上创建 Release（手动或用 gh CLI）
gh release create v0.1.0 \
    packaging/dist/CaptionLM-0.1.0.dmg \
    --title "CaptionLM v0.1.0" \
    --notes-file RELEASE_NOTES.md
```

**Release 描述必须包含**：

> ⚠️ Unsigned build — 首次打开会被 macOS Gatekeeper 拦截，请：
>
> 1. 右键 `CaptionLM.app` → **打开** → 在弹窗里再选一次"打开"
> 2. 或者命令行：`xattr -d com.apple.quarantine /Applications/CaptionLM.app`
>
> 然后到系统设置 → 隐私与安全性 → 屏幕录制，勾选 CaptionLM。

---

## 可选：notarization（消除 Gatekeeper 警告）

要让用户开箱就能开（不需要右键 Open），需要 Apple Developer 账号（$99/年）。

1. 拿到 `Developer ID Application` 证书
2. 编辑 `build_dmg.sh`，取消 `codesign` 和 `xcrun notarytool` 那两块注释
3. 重跑 `build_dmg.sh`

notarized 的 .dmg 用户双击就能开，不需要任何特殊操作。
