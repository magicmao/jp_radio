# NHK 新闻广播播放器（带日语实时转写）

语言版本： [日本語](README.ja.md) | [English](README.en.md)

终端（CUI）日语广播播放器，支持 NHK 及各地域电台流式播放，可选开启 Whisper 实时语音转文字（日语）。

## 功能概览

- **播放：** NHK 全国 3 套 + 各地域电台直播流（mpv 优先，ffplay 备用）
- **实时转写：** faster-whisper 日语 STT，模型下载进度与识别文字直接显示在终端界面底部
- **终端界面：** curses，显示播放状态、电台列表、分类筛选、识别文本（最多 3 行滚动）
- **模型缓存：** Whisper 模型缓存到脚本同级 `.whisper_models/`，后续启动无需重复下载

## 内置电台

### NHK 全国
| 电台 | 说明 |
|------|------|
| NHK 广播第1 | 综合 · 新闻 · 天气预报 |
| NHK 广播第2 | 教育 · 外语 · 宗教节目 |
| NHK FM | 音乐 · 文化 · 娱乐 |

### NHK 地域（DEFAULT_AREA = "tokyo"）
支持：札幌、仙台、东京、大阪、福岡 等地域 FM（`c` 键循环切换分类查看）

## 运行环境

- Python 3.8+
- **macOS：** `brew install mpv`
- **Linux：** `sudo apt install mpv`（STT 功能需要 mpv）
- **STT（可选）：** `pip install -r requirements.txt`

> ⚠️ **注意：** 实时语音转写（STT）功能依赖 `mpv`，`ffplay` 不支持音频 pipe，无法启用 STT。

## 安装

```bash
# 1. Homebrew（如果没有）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. 安装 mpv
brew install mpv        # macOS
# sudo apt install mpv  # Linux（Ubuntu/Debian）
# sudo dnf install mpv  # Linux（Fedora）

# 3. 安装 STT 依赖（可选）
pip install -r requirements.txt
```

## 使用方法

```bash
cd radio
python radio_player.py
```

## 键位操作

| 按键 | 作用 |
|------|------|
| ↑ / ↓ 或 j | 选择电台 |
| Enter | 开始播放 |
| p / Space | 暂停 / 继续 |
| s | 停止并回到首页 |
| + / - | 调节音量 |
| c | 循环切换电台分类（NHK全国 / NHK地域 / ...） |
| t | 开启 / 关闭语音转写 |
| q | 退出 |

## STT 模型管理

| 操作 | 位置 |
|------|------|
| 缓存目录 | `.whisper_models/`（与脚本同级） |
| 模型选择 | 修改脚本顶部 `WHISPER_MODEL`（tiny/small/medium/large-v3） |
| 删除缓存 | 直接删除 `.whisper_models/` 目录 |

> 模型仅在首次使用时下载。以 medium 为例，大小约 1.5GB。下载进度实时显示在界面底部。

## 主要配置（radio_player.py 顶部）

```python
DEFAULT_AREA    = "tokyo"    # 初始地域（见 AREA_MAP）
ENABLE_STT      = True       # False 禁用 STT（加快启动速度）
WHISPER_MODEL   = "medium"   # tiny/small/medium/large-v3
CHUNK_DURATION  = 8          # 每块音频秒数（越长精度越高，但延迟越大）
WHISPER_LANG    = "ja"       # 识别语言
```

## 技术说明

- NHK 广播采用 HLS（m3u8）流，mpv/ffplay 自动处理
- STT 功能需要 `mpv`（ffplay 无法 pipe 音频）
- 模型缓存由 `HF_HUB_CACHE` / `HF_HOME` 环境变量控制，指向 `.whisper_models/`
- 语音识别在后台线程执行，不阻塞终端 UI
