# NHK ラジオ ニュース受信ラジオ

言語 / Language: [日本語](README.ja.md) | [中文](README.zh-CN.md) | [English](README.en.md)

NHK や各地域のラジオ局をストリーミング再生し、Whisper によるリアルタイム文字起こし（日本語対応）を 지원하는 CUI ラジオプレイヤーです。

## 概要

- **再生:** NHK 全国 3 套 + 各地域 FM ライブストリーム（mpv 優先、ffplay 备用）
- **文字起こし:** faster-whisper によるリアルタイム STT（日本語）、TUI 下部にダウンロード＆認識進捗を直接表示
- **TUI:** curses ベース、播放状態・局リスト・カテゴリ絞り込み・認識テキスト（最大 3 行スクロール）に対応
- **モデルキャッシュ:** 初回 download した Whisper モデルはスクリプト同级の `.whisper_models/` に保存され、2 回目以降は即時起動

## 対応局

### NHK 全国
| 局名 | 概要 |
|------|------|
| NHK ラジオ第1 | 総合・ニュース・天気予報 |
| NHK ラジオ第2 | 教育・語学・宗教番組 |
| NHK FM | 音楽・文化・エンタメ |

### NHK 地域（DEFAULT_AREA = "tokyo"）
札幌・仙台・東京・名古屋・大阪・広島・松山・福岡 対応（`c` キーでカテゴリ切り替え）

## 動作環境

- Python 3.8+
- **macOS:** `brew install mpv`
- **Linux:** `sudo apt install mpv`
- **STT（任意）:** `pip install -r requirements.txt`

> ⚠️ **注意:** リアルタイム文字起こし（STT）機能には `mpv` が必要です。`ffplay` はオーディオ pipe 非対応のため STT は使えません。

## インストール

```bash
# 1. Homebrew（未インストールの場合）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. mpv をインストール
brew install mpv        # macOS
# sudo apt install mpv  # Linux（Ubuntu/Debian）
# sudo dnf install mpv  # Linux（Fedora）

# 3. STT 依存ライブラリ（任意）
pip install -r requirements.txt
```

## 使い方

```bash
cd radio
python radio_player.py
```

### 操作方法

| キー | 説明 |
|------|------|
| `↑` / `↓` または `j` | 局を選択 |
| `Enter` | 再生開始 |
| `p` / `Space` | 一時停止 / 再開 |
| `s` | 停止（ホームに戻る） |
| `+` / `-` | 音量調整 |
| `c` | カテゴリ切り替え（NHK全国 / NHK地域 / ...） |
| `t` | STT（音声認識）ON / OFF |
| `q` | 終了 |

## STT 模型管理

| 操作 | 場所 |
|------|------|
| キャッシュディレクトリ | スクリプト同级 `.whisper_models/` |
| モデル選択 | スクリプト頭 `WHISPER_MODEL = "medium"` を編集（tiny/small/medium/large-v3） |
| 削除 | `.whisper_models/` ディレクトリを丸ごと削除 |

> モデルは初回使用时に만 下载されます（medium 約 1.5GB）。下载進捗は TUI 下部にリアルタイム表示されます。

## 設定（radio_player.py 冒頭）

```python
DEFAULT_AREA    = "tokyo"          # 初期選択地域（AREA_MAP 参照）
ENABLE_STT      = True             # False で STT を無効化（起動を速くする）
WHISPER_MODEL   = "medium"         # tiny/small/medium/large-v3
CHUNK_DURATION  = 8                # 秒ごとキャプチャ → 認識
WHISPER_LANG    = "ja"             # 認識言語
```

## 技術的注意

- NHK ラジオの大部分の配信は HLS (m3u8) 形式（mpv/ffplay が自動処理）
- STT 機能には `mpv` が必須（ffplay はオーディオ pipe 非対応）
- モデルキャッシュは `HF_HUB_CACHE` / `HF_HOME` 環境変数で制御され、`.whisper_models/` に保存
- 文字起こしはバックグラウンドスレッドで実行され、TUI はブロックされない
