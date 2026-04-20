# NHK 新闻广播播放器（带日语实时转写）

语言版本： [日本語](README.ja.md) | [English](README.en.md)

终端（CUI）日语广播播放器，支持 NHK 及各地域电台流式播放，可选开启 Whisper 实时语音转文字（日语）。

## 功能概览

- **播放：** NHK 全国 3 套 + 各地域电台直播流（mpv 优先，ffplay 备用）
- **实时转写：** faster-whisper 日语 STT，模型下载进度与识别文字直接显示在终端界面底部
- **字幕同步：** 音频时间戳跟踪，字幕延迟显示（等音频播放到对应时刻才展示），同步延迟约 0.5 秒
- **终端界面：** curses，显示播放状态、电台列表、分类筛选、识别文本（最多 4 行滚动 + 实时行）
- **模型缓存：** Whisper 模型缓存到脚本同级 `.whisper_models/`，后续启动无需重复下载
- **音频缓存：** 播放时自动缓存 WAV 到 `.audio_cache/`，用于字幕时间同步

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
cd /path/to/jp_radio
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

## 字幕同步说明

语音识别后的字幕不会立即显示，而是根据音频播放进度延迟展示：

- **原理：** 每块音频记录时间戳 → Whisper 识别 → 等音频实际播放到该时刻才显示字幕
- **延迟：** 默认 0.5 秒（SYNC_DELAY 可调整）
- **指示器：** 实时行显示 `[x.xs behind]` 表示当前字幕与播放位置的延迟

> 由于 Whisper 识别需要 1-2 秒处理时间，字幕与音频之间的实际同步取决于模型速度和当前音频内容复杂程度。

## STT 模型管理

| 操作 | 位置 |
|------|------|
| 缓存目录 | `.whisper_models/`（与脚本同级） |
| 音频缓存 | `.audio_cache/`（自动生成，关闭播放后自动清理） |
| 模型选择 | 修改脚本顶部 `WHISPER_MODEL`（tiny/small/medium/large-v3） |
| 删除缓存 | 直接删除对应目录 |

> 模型仅在首次使用时下载。以 medium 为例，大小约 1.5GB。下载进度实时显示在界面底部。

## 主要配置（radio_player.py 顶部）

```python
DEFAULT_AREA    = "tokyo"    # 初始地域（见 AREA_MAP）
ENABLE_STT      = True       # False 禁用 STT（加快启动速度）
WHISPER_MODEL   = "small"    # tiny/small/medium/large-v3（推荐 small，平衡速度与精度）
CHUNK_DURATION  = 1.0        # 每块音频秒数（1-2s 推荐，越短延迟越低）
SAMPLE_RATE     = 16000      # 采样率（固定 16kHz）
WHISPER_LANG    = "ja"       # 识别语言
STT_HIST_COLOR  = "WHITE"    # 历史字幕颜色（WHITE/GREEN/CYAN/YELLOW）
STT_LIVE_COLOR  = "YELLOW"   # 实时字幕颜色
```

## 技术说明

- NHK 广播采用 HLS（m3u8）流，mpv/ffplay 自动处理
- STT 功能需要 `mpv`（ffplay 无法 pipe 音频）
- 模型缓存由 `HF_HUB_CACHE` / `HF_HOME` 环境变量控制，指向 `.whisper_models/`
- 语音识别在后台线程执行，不阻塞终端 UI
- 音频缓存用于时间同步，自动清理旧文件