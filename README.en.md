# NHK Radio News Player (with Real-time Japanese Transcription)

Language: [日本語](README.ja.md) | [中文](README.zh-CN.md) | [English](README.en.md)

A terminal-based (CUI) Japanese radio player that streams NHK and regional stations, with optional real-time Japanese speech-to-text transcription via Whisper.

## Overview

- **Playback:** YAML-configurable stations + NHK national 3 channels + optional multi-area NHK regional streams (mpv preferred, ffplay fallback)
- **Transcription:** Real-time Japanese STT using faster-whisper; model download progress and transcript text shown directly in the terminal UI footer
- **Subtitle Sync:** Audio timestamp tracking with delayed display — captions appear only when audio reaches their corresponding moment, with ~0.5s sync latency
- **TUI:** curses-based interface with playback status, station list, category filtering, and transcript output (up to 4 scrolling history lines + live line)
- **Model cache:** Whisper models stored in `.whisper_models/` next to the script; subsequent runs start instantly
- **Audio cache:** WAV files cached to `.audio_cache/` during playback for subtitle time synchronization

## Station Configuration

The station list is now maintained in `stations.yaml`:

- `stations`: manually maintained fixed stations
- `nhk_areas`: NHK regional areas to load dynamically
- `default_area`: fallback default area

### Default Stations
| Station | Description |
|---------|-------------|
| NHK Radio 1 | General news & weather |
| NHK Radio 2 | Education & language |
| NHK FM | Music & culture |

### NHK Regional Expansion
By default, the app dynamically loads all 8 officially available NHK areas: Sapporo, Sendai, Tokyo, Nagoya, Osaka, Hiroshima, Matsuyama, and Fukuoka.
This brings in each area's NHK Radio 1 / Radio 2 / FM streams, making it easier to compare regional news and talk coverage.

> For Japanese news / talk listening, NHK Radio 1 regional variants are the best fit in the current set. NHK Radio 2 is more education/language focused, and NHK FM is mostly music/culture.
>
> The TUI now includes a **News** filter that shows only regional `NHK Radio 1` stations for faster news/talk browsing.

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

# 3. Install STT / YAML config dependencies (optional)
pip install -r requirements.txt
```

## Usage

```bash
cd /path/to/jp_radio
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
| c | Cycle filters (All / News / each category) |
| C | Clear filter back to All |
| t | Toggle STT (speech recognition) ON / OFF |
| q | Quit |

> On startup, the TUI now opens in the **News** filter by default, showing regional `NHK Radio 1` stations first.

## Subtitle Synchronization

Transcribed subtitles are not displayed immediately — they appear based on audio playback progress:

- **Mechanism:** Each audio chunk records its timestamp → Whisper recognizes → Caption displays when audio reaches that moment
- **Latency:** Default 0.5 seconds (SYNC_DELAY adjustable)
- **Indicator:** Live line shows `[x.xs behind]` to indicate the delay between current caption and playback position

> Since Whisper recognition takes 1-2 seconds of processing time, actual sync accuracy depends on model speed and audio content complexity.

## STT Model Management

| Operation | Location |
|-----------|----------|
| Cache directory | `.whisper_models/` (same level as script) |
| Audio cache | `.audio_cache/` (auto-generated, auto-cleaned after playback stops) |
| Model selection | Edit `WHISPER_MODEL` at top of script (tiny/small/medium/large-v3) |
| Remove cache | Delete the corresponding directory |

> The model is downloaded only on first use (medium model ~1.5GB). Download progress is shown live in the UI footer.

## Main Settings (top of radio_player.py)

```python
STATIONS_CONFIG_PATH = "stations.yaml"  # station config file
DEFAULT_AREA    = "tokyo"    # fallback area when config is missing
ENABLE_STT      = True       # Set False to disable STT (faster startup)
WHISPER_MODEL   = "medium"  # tiny/small/medium/large-v2/large-v3 (recommended: large-v2 for Japanese, high accuracy; medium is faster)
CHUNK_DURATION  = 3.0        # Audio chunk duration in seconds (3-5s recommended, longer = more context = better accuracy)
SAMPLE_RATE     = 16000      # Sample rate (fixed 16kHz)
WHISPER_LANG    = "ja"       # Recognition language
STT_HIST_COLOR  = "WHITE"    # History subtitle color (WHITE/GREEN/CYAN/YELLOW)
STT_LIVE_COLOR  = "YELLOW"   # Live subtitle color
```

## Technical Notes

- NHK streams use HLS (m3u8) — mpv/ffplay handle it automatically
- STT feature requires `mpv` (ffplay cannot pipe audio)
- Model cache path controlled by `HF_HUB_CACHE` / `HF_HOME`, stored in `.whisper_models/`
- Transcription runs in a background thread and does not block the TUI
- Audio cache used for time synchronization, auto-cleaned after playback stops