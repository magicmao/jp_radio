# NHK 新闻广播播放器（带日语实时转写）

语言版本： [日本語](README.ja.md) | [English](README.en.md)

这是一个终端（CUI）日语广播播放器，支持日本网络电台的流式播放，并可选开启 Whisper 实时语音转文字（日语）。

## 功能概览

- 播放：支持 NHK 及部分日本网络电台直播流（mpv 优先，ffplay 备用）
- 实时转写：使用 faster-whisper 进行日语 STT，下载与识别进度直接显示在终端界面
- 终端界面：基于 curses，显示播放状态、电台列表、识别文本
- 模型缓存：Whisper 模型下载后缓存到项目目录下的 .whisper_models，后续启动无需重复下载

## 当前内置电台

| 电台 | 说明 |
|------|------|
| NHK Radio 1 | 综合新闻与资讯 |
| NHK Radio 2 | 教育与语言节目 |
| NHK FM | 音乐与文化节目 |

## 运行环境

- Python 3.8+
- macOS：安装 mpv（brew install mpv）
- Linux：安装 mpv 或 ffmpeg（sudo apt install mpv / sudo apt install ffmpeg）
- 可选 STT 依赖：pip install -r requirements.txt

## 安装

```bash
cd radio

# 播放依赖（必需）
brew install mpv        # macOS
# sudo apt install mpv  # Linux

# 语音转写依赖（可选）
pip install -r requirements.txt
```

## 使用方法

```bash
python radio_player.py
```

## 键位操作

| 按键 | 作用 |
|------|------|
| ↑ / ↓ | 选择电台 |
| Enter | 开始播放 |
| p / Space | 暂停 / 继续 |
| s | 停止并回到首页 |
| + / - | 调节音量 |
| q | 退出 |

## STT 模型管理

| 操作 | 位置 |
|------|------|
| 缓存目录 | .whisper_models（与脚本同级） |
| 模型选择 | 修改脚本顶部 WHISPER_MODEL（tiny/small/medium/large-v3） |
| 删除缓存 | 直接删除 .whisper_models 目录 |

说明：模型仅在首次使用时下载。以 medium 为例，大小约 1.5GB。

## 主要配置（radio_player.py 顶部）

```python
DEFAULT_AREA    = "tokyo"
ENABLE_STT      = True
WHISPER_MODEL   = "medium"
CHUNK_DURATION  = 8
WHISPER_LANG    = "ja"
```

## 技术说明

- 大部分 NHK 音频流为 HLS（m3u8）
- 播放后端优先使用 mpv，不可用时回退到 ffplay
- STT 模型缓存由 HF_HUB_CACHE / HF_HOME 环境变量控制
- 语音识别在后台线程执行，不阻塞终端 UI
