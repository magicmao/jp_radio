# NHK Radio News Player (with Real-time Japanese Transcription)

Language: [日本語](README.ja.md) | [中文](README.zh-CN.md)

This is a terminal-based (CUI) Japanese radio player that streams online stations and optionally provides real-time Japanese speech-to-text transcription via Whisper.

## Overview

- Playback: Streams NHK and selected Japanese internet radio stations (mpv first, ffplay fallback)
- Transcription: Real-time Japanese STT using faster-whisper, with download/recognition progress shown in the terminal UI
- TUI: curses-based interface for playback status, station list, and transcript output
- Model cache: Whisper models are stored in .whisper_models next to the script, so later runs start quickly

## Built-in Stations

| Station | Description |
|---------|-------------|
| NHK Radio 1 | General news and information |
| NHK Radio 2 | Education and language programs |
| NHK FM | Music and culture |

## Requirements

- Python 3.8+
- macOS: install mpv (brew install mpv)
- Linux: install mpv or ffmpeg (sudo apt install mpv / sudo apt install ffmpeg)
- Optional STT dependency: pip install -r requirements.txt

## Installation

```bash
cd radio

# Playback dependency (required)
brew install mpv        # macOS
# sudo apt install mpv  # Linux

# Speech-to-text dependency (optional)
pip install -r requirements.txt
```

## Usage

```bash
python radio_player.py
```

## Controls

| Key | Action |
|-----|--------|
| ↑ / ↓ | Select station |
| Enter | Start playback |
| p / Space | Pause / Resume |
| s | Stop and return to home |
| + / - | Volume up/down |
| q | Quit |

## STT Model Management

| Operation | Location |
|-----------|----------|
| Cache directory | .whisper_models (same level as script) |
| Model selection | Edit WHISPER_MODEL at top of script (tiny/small/medium/large-v3) |
| Remove cache | Delete .whisper_models |

Note: The model is downloaded only on first use. The medium model is about 1.5GB.

## Main Settings (top of radio_player.py)

```python
DEFAULT_AREA    = "tokyo"
ENABLE_STT      = True
WHISPER_MODEL   = "medium"
CHUNK_DURATION  = 8
WHISPER_LANG    = "ja"
```

## Technical Notes

- Most NHK streams use HLS (m3u8)
- Playback uses mpv first and falls back to ffplay if needed
- STT model cache path is controlled by HF_HUB_CACHE / HF_HOME
- Transcription runs in a background thread and does not block the TUI
