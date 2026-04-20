# NHK ラジオ ニュース受信ラジオ（リアルタイム文字起こし対応）

言語 / Language: [日本語](README.ja.md) | [中文](README.zh-CN.md) | [English](README.en.md)

NHK や各地域のラジオ局をストリーミング再生し、Whisper によるリアルタイム文字起こし（日本語対応）を 지원하는 CUI ラジオプレイヤーです。

## 概要

- **再生:** YAML で設定可能な局 + NHK 全国 3 波 + 複数地域の NHK ライブストリーム（mpv 優先、ffplay 备用）
- **文字起こし:** faster-whisper によるリアルタイム STT（日本語）、TUI 下部にダウンロード＆認識進捗を直接表示
- **字幕同期:** 音声タイムスタンプ追跡、遅延表示（音声が해당位置に到達した時点で字幕を表示）、同期遅延は約 0.5 秒
- **TUI:** curses ベース、播放状態・局リスト・カテゴリ絞り込み・認識テキスト（最大 4 行スクロール + 実時行）に対応
- **モデルキャッシュ:** 初回 download した Whisper モデルはスクリプト同级の `.whisper_models/` に保存され、2 回目以降は即時起動
- **オーディオキャッシュ:** 再生時に WAV を `.audio_cache/` に自動キャッシュ、字幕時間同期用途

## 局設定

局リストは `stations.yaml` に外出しされ、直接メンテナンスできます。

- `stations`: 固定局の手動定義
- `nhk_areas`: 動的に読み込む NHK 地域
- `default_area`: フォールバック地域

### デフォルト局
| 局名 | 概要 |
|------|------|
| NHK ラジオ第1 | 総合・ニュース・天気予報 |
| NHK ラジオ第2 | 教育・語学・宗教番組 |
| NHK FM | 音楽・文化・エンタメ |

### NHK 地域拡張
デフォルトでは公式に利用可能な 8 地域すべて（札幌・仙台・東京・名古屋・大阪・広島・松山・福岡）を動的に読み込みます。
各地域の NHK ラジオ第1 / 第2 / FM がまとめて追加されるため、地域ごとのニュースやトーク内容を比較しやすくなります。

> 現在の構成で日本語のニュース／トーク用途に最も向いているのは、各地域の NHK ラジオ第1 です。NHK ラジオ第2 は教育・語学寄り、NHK FM は音楽・文化寄りです。
>
> TUI には **ニュース** フィルタを追加し、各地域の `NHK ラジオ第1` だけを素早く絞り込めるようにしました。

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

# 3. STT / YAML 設定用ライブラリ（任意）
pip install -r requirements.txt
```

## 使い方

```bash
cd /path/to/jp_radio
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
| `c` | フィルタ切り替え（全部 / ニュース / 各カテゴリ） |
| `C` | フィルタ解除して全部表示 |
| `t` | STT（音声認識）ON / OFF |
| `q` | 終了 |

> 起動時はデフォルトで **ニュース** フィルタが有効になり、各地域の `NHK ラジオ第1` を最初に表示します。

## 字幕同期について

音声認識後の字幕は即座には表示らず、音声再生の進行状況に合わせて遅延表示されます：

- **原理:** 各オーディオブロックはタイムスタンプを記録 → Whisper が認識 → 音声がその時刻に到達した時点で字幕を表示
- **遅延:** デフォルト 0.5 秒（SYNC_DELAY は調整可能）
- **インジケーター:** 実時行に `[x.xs behind]` を表示し、現在の字幕と再生位置の遅延を示す

> Whisper の認識には 1〜2 秒の処理時間がかかるため、字幕と音声の実際の同期精度はモデル速度と音声コンテンツの複雑さに依存します。

## STT 模型管理

| 操作 | 場所 |
|------|------|
| キャッシュディレクトリ | スクリプト同级 `.whisper_models/` |
| オーディオキャッシュ | `.audio_cache/`（自動生成、再生停止後に自動クリーン）
| モデル選択 | スクリプト頭 `WHISPER_MODEL = "small"` を編集（tiny/small/medium/large-v3） |
| 削除 | 対応ディレクトリを丸ごと削除 |

> モデルは初回使用时に만 下载されます（medium 約 1.5GB）。下载進捗は TUI 下部にリアルタイム表示されます。

## 設定（radio_player.py 冒頭）

```python
STATIONS_CONFIG_PATH = "stations.yaml"  # 局設定ファイル
DEFAULT_AREA    = "tokyo"    # 設定ファイルが無い場合のフォールバック地域
ENABLE_STT      = True       # False で STT を無効化（起動を速くする）
WHISPER_MODEL   = "medium"  # tiny/small/medium/large-v2/large-v3（日本語には large-v2 推荐、精度高い；medium 速度快）
CHUNK_DURATION  = 3.0        # 秒ごとキャプチャ（3-5秒推奨、より長い文脈で精度向上）
SAMPLE_RATE     = 16000      # サンプルレート（固定 16kHz）
WHISPER_LANG    = "ja"       # 認識言語
STT_HIST_COLOR  = "WHITE"    # 歴史字幕の色（WHITE/GREEN/CYAN/YELLOW）
STT_LIVE_COLOR  = "YELLOW"   # 実時字幕の色
```

## 技術的注意

- NHK ラジオの大部分の配信は HLS (m3u8) 形式（mpv/ffplay が自動処理）
- STT 機能には `mpv` が必須（ffplay はオーディオ pipe 非対応）
- モデルキャッシュは `HF_HUB_CACHE` / `HF_HOME` 環境変数で制御され、`.whisper_models/` に保存
- 文字起こしはバックグラウンドスレッドで実行され、TUI はブロックされない
- オーディオキャッシュは時間同期用途、再生停止後に自動クリーン