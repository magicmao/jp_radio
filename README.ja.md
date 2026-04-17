# NHK ラジオ ニュース受信ラジオ

言語 / Language: [日本語](README.ja.md) | [中文](README.zh-CN.md) | [English](README.en.md)

日本語のラジオ局をストリーミング再生し、Whisper によるリアルタイム文字起こし（日本語対応）を 지원하는 CUI ラジオプレイヤーです。

## 概要

- **再生:** NHK 及其他日本网络广播直播流（mpv > ffplay）
- **文字起こし:** faster-whisper によるリアルタイム STT（日本語）、TUI にダウンロード＆認識進捗を直接表示
- **TUI:** curses 终端界面，显示播放状态、电台列表、识别文字
- **モデルキャッシュ:** 初回 download した Whisper モデルはスクリプト同级の `.whisper_models/` に保存され、2 回目以降は即時起動

## 対応局

| 局名 | 概要 |
|------|------|
| NHK Radio 1 | NHK総合ラジオ（全国向けニュース・情報） |
| NHK Radio 2 | NHK第2（教育・語学番組） |
| NHK FM | NHK FM（音楽・文化） |
| TBSラジオ | TBSラジオ（民放ニュース・トーク） |
| 文化放送 | JOQR（スポーツ・ニュース） |
| Nippon Broadcasting | JRN系列 |
| J-WAVE | 81.3 FM（ミュージック・カルチャー） |
| FM Tokyo | 80.0 FM（ミュージック・情報） |

## 動作環境

- Python 3.8+
- **macOS:** `brew install mpv`
- **Linux:** `sudo apt install mpv` / `sudo apt install ffmpeg`
- **STT（任意）:** `pip install -r requirements.txt`

## インストール

```bash
cd radio

# 播放用（必須）
brew install mpv       # macOS
# sudo apt install mpv  # Linux

# STT 用（任意、初回起動時にモデルが自動 download される）
pip install -r requirements.txt
```

## 使い方

```bash
python radio_player.py
```

### 操作方法

| キー | 説明 |
|------|------|
| `↑` / `↓` | 局を選択 |
| `Enter` | 再生開始 |
| `p` / `Space` | 一時停止 / 再開 |
| `s` | 停止（ホームに戻る） |
| `+` / `-` | 音量調整 |
| `q` | 終了 |

## STT 模型管理

| 操作 | 場所 |
|------|------|
| 缓存目录 | スクリプト同级 `.whisper_models/` |
| モデル選択 | スクリプト頭 `WHISPER_MODEL = "medium"` を編集（tiny/small/medium/large-v3） |
| 削除 | `.whisper_models/` ディレクトリを丸ごと削除 |

模型下载进度会实时显示在 TUI 底部，下载完成后自动切换到"待機中..."。模型仅在首次使用时下载（约 1.5GB/medium）。

## 設定（radio_player.py 冒頭）

```python
DEFAULT_AREA    = "tokyo"          # 初期選択地域
ENABLE_STT      = True             # False で STT を無効化（起動を速くする）
WHISPER_MODEL   = "medium"         # tiny/small/medium/large-v3
CHUNK_DURATION  = 8                # 秒ごとキャプチャ → 認識
WHISPER_LANG    = "ja"             # 認識言語
```

## 技術的注意

- NHK ラジオの大部分の配信は HLS (m3u8) 形式
- 再生には内部で `mpv`（推奨）または `ffplay` を使用
- STT 用模型缓存由 `HF_HUB_CACHE` / `HF_HOME` 環境変数控制
- 文字起こしはバックグラウンドスレッドで実行され、TUI はブロックされない
