# NHK Radio News Player (with Real-time Japanese Transcription)

Language: [日本語](README.ja.md) | [中文](README.zh-CN.md) | [English](README.en.md)

A terminal-based (CUI) Japanese radio player that streams NHK and regional stations, with optional real-time Japanese speech-to-text transcription via Whisper.

## Overview

- **Playback:** NHK national 3 channels + regional FM live streams (mpv preferred, ffplay fallback)
- **Transcription:** Real-time Japanese STT using faster-whisper; model download progress and transcript text shown directly in the terminal UI footer
- **TUI:** curses-based interface with playback status, station list, category filtering, and transcript output (up to 3 lines, scrolling)
- **Model cache:** Whisper models stored in `.whisper_models/` next to the script; subsequent runs start instantly

## Built-in Stations

### NHK National
| Station | Description |
|---------|-------------|
| NHK Radio 1 | General news & weather |
| NHK Radio 2 | Education & language |
| NHK FM | Music & culture |

### NHK Regional (DEFAULT_AREA = "tokyo")
Supported: Sapporo, Sendai, Tokyo, Nagoya, Osaka, Hiroshima, Matsuyama, Fukuoka (use `c` to cycle categories)

## Requirements

- Python 3.8+
- **macOS:** `brew install mpv`
- **Linux:** `sudo apt install mpv`
- **STT (optional):** `pip install -r requirements.txt`

> ⚠️ **Note:** Real-time speech-to-text (STT) requires `mpv`. `ffplay` does not support audio piping, so STT is unavailable when using ffplay.

## Installation

```bash
# 1. Homebrew (if not installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. Install mpv
brew install mpv        # macOS
# sudo apt install mpv  # Linux (Ubuntu/Debian)
# sudo dnf install mpv  # Linux (Fedora)

# 3. Install STT dependencies (optional)
pip install -r requirements.txt
```

## Usage

```bash
cd radio
python radio_player.py
```

## Controls

| Key | Action |
|-----|--------|
| ↑ / ↓ or j | Select station |
| Enter | Start playback |
| p / Space | Pause / Resume |
| s | Stop and return to home |
| + / - | Volume up / down |
| c | Cycle station category (NHK National / NHK Regional / ...) |
| t | Toggle STT (speech recognition) ON / OFF |
| q | Quit |

## STT Model Management

| Operation | Location |
|-----------|----------|
| Cache directory | `.whisper_models/` (same level as script) |
| Model selection | Edit `WHISPER_MODEL` at top of script (tiny/small/medium/large-v3) |
| Remove cache | Delete the `.whisper_models/` directory |

> The model is downloaded only on first use (medium model ~1.5GB). Download progress is shown live in the UI footer.

## Main Settings (top of radio_player.py)

```python
DEFAULT_AREA    = "tokyo"    # Initial area (see AREA_MAP)
ENABLE_STT      = True       # Set False to disable STT (faster startup)
WHISPER_MODEL   = "medium"   # tiny/small/medium/large-v3
CHUNK_DURATION  = 8          # Audio chunk duration in seconds
WHISPER_LANG    = "ja"       # Recognition language
```

## Technical Notes

- NHK streams use HLS (m3u8) — mpv/ffplay handle it automatically
- STT feature requires `mpv` (ffplay cannot pipe audio)
- Model cache path controlled by `HF_HUB_CACHE` / `HF_HOME`, stored in `.whisper_models/`
- Transcription runs in a background thread and does not block the TUI
