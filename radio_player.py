#!/usr/bin/env python3
"""
日本ラジオ ニュース受信ラジオ + 音声認識（Whisper）
支持 NHK 全部地域电台，实时显示日文语音转文字。

播放后端：mpv（推荐） > ffplay
STT 后端：faster-whisper（CPU，推荐） > openai-whisper
"""

import subprocess
import sys
import os
import signal
import time
import threading
import queue
import io
import xml.etree.ElementTree as ET
import wave
from urllib.request import urlopen, Request
from urllib.error import URLError

# ──────────────────────────────────────────────
# 1.  Settings
# ──────────────────────────────────────────────
DEFAULT_AREA = "tokyo"
ENABLE_STT = True           # False で STT を無効化（起動を速くする）
WHISPER_MODEL = "medium"    # tiny/small/medium/large-v3   medium が精度と速度のバランス良い
CHUNK_DURATION = 8          # 秒ごとキャプチャ → 認識（長いほど精度高いがメモリも消費）
SAMPLE_RATE = 16000
WHISPER_LANG = "ja"

# ──────────────────────────────────────────────
# 2.  STT — faster-whisper
# ──────────────────────────────────────────────

def _fmt_bytes(b: int) -> str:
    """字节数 → 两位小数 K/M/G + 'B' 后缀。"""
    if b >= 1024**4: return f"{b/1024**4:.2f}TB"
    if b >= 1024**3: return f"{b/1024**3:.2f}GB"
    if b >= 1024**2: return f"{b/1024**2:.2f}MB"
    if b >= 1024:    return f"{b/1024:.2f}KB"
    return f"{b}B"

def _fmt_bits(bps: float) -> str:
    """位速 → 两位小数 K/M/G + 'b' 后缀（小写）。"""
    if bps >= 1024**4: return f"{bps/1024**4:.2f}Tb"
    if bps >= 1024**3: return f"{bps/1024**3:.2f}Gb"
    if bps >= 1024**2: return f"{bps/1024**2:.2f}Mb"
    if bps >= 1024:    return f"{bps/1024:.2f}Kb"
    return f"{bps:.0f}b"



class AudioSTT:
    """faster-whisper でリアルタイム文字起こし。別スレッドで走る。"""

    def __init__(self, model_name: str = WHISPER_MODEL, language: str = WHISPER_LANG):
        self.language = language
        self.model_name = model_name
        self._model = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=8)
        self._transcript_queue: queue.Queue[str] = queue.Queue(maxsize=3)
        # 直近の認識結果（滚动显示，扩大缓冲）
        self._recent: list[str] = []
        self._prebuf: list[bytes] = []   # model 加载期间暂存音频
        self._lock = threading.Lock()
        self._load_error: str | None = None
        self._loading = True        # モデル加载中フラグ
        self._loading_msg: str = f"Whisper モデル下载中 ({model_name})..."
        # 下载进度（0-100，由后台线程更新）
        self._dl_progress: float = 0.0
        self._dl_downloaded: int = 0       # 已下载字节数
        self._dl_start_time: float = time.monotonic()   # 下载开始时间
        self._model_cache: str = ""
        self._model_total_bytes: int = 0   # 预计总字节数
        # 模型大小估算（byte）
        _sizes = {
            "tiny":75*1024*1024,"base":140*1024*1024,"small":480*1024*1024,
            "medium":1500*1024*1024,"large-v2":3100*1024*1024,"large-v3":3100*1024*1024,
            "large":3100*1024*1024,
        }
        for k in list(_sizes):  # list() prevents dict size change during iteration
            _sizes[k+"+.en"] = _sizes[k]
        self._model_total_bytes = _sizes.get(model_name, 1500*1024*1024)
        self._dl_progress_thread: threading.Thread | None = None

    # ── 进度监控线程 ──
    def _dl_progress_worker(self):
        """后台线程：每 0.3s 扫描 .whisper_models/ 目录计算下载进度。"""
        self._dl_start_time = time.monotonic()
        while self._loading and self._dl_progress < 99.0:
            try:
                if self._model_cache and os.path.isdir(self._model_cache):
                    total = sum(
                        os.path.getsize(os.path.join(root, f))
                        for root, _, files in os.walk(self._model_cache)
                        for f in files
                    )
                    self._dl_downloaded = total
                    self._dl_progress = min(99.0, total / self._model_total_bytes * 100)
            except Exception:
                pass
            time.sleep(0.3)
        self._dl_downloaded = self._model_total_bytes
        self._dl_progress = 100.0

    # ── lazy load（起動を速くするため最初の一回だけ） ──
    def _ensure_model(self):
        if self._load_error:
            return
        if self._model is not None:
            return
        try:
            import os as _os

            # ── 模型缓存目录设为脚本同级 .whisper_models/ ──
            _script_dir = os.path.dirname(os.path.abspath(__file__))
            _model_cache = os.path.join(_script_dir, ".whisper_models")
            _os.makedirs(_model_cache, exist_ok=True)
            _os.environ["HF_HUB_CACHE"] = _model_cache
            _os.environ["HF_HOME"] = _model_cache
            _os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
            self._model_cache = _model_cache

            # 启动进度监控线程
            self._dl_progress = 0.0
            self._dl_downloaded = 0
            self._dl_progress_thread = threading.Thread(
                target=self._dl_progress_worker, daemon=True
            )
            self._dl_progress_thread.start()

            from faster_whisper import WhisperModel

            self._loading_msg = f"Whisper モデル下载中 ({self.model_name})..."

            # GPU があれば cuda、なければ CPU
            try:
                self._model = WhisperModel(
                    self.model_name,
                    device="cuda",
                    compute_type="float16",
                )
                self._loading_msg = "[Whisper] GPU モードで動作中"
            except Exception:
                self._model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type="int8",
                )
                self._loading_msg = "[Whisper] CPU モードで動作中"

            self._loading = False
            self._dl_progress = 100.0
            self._loading_msg = ""

        except ImportError:
            self._load_error = "faster-whisper 未安装。请运行: pip install faster-whisper"
            self._loading = False
            self._loading_msg = self._load_error
        except Exception as e:
            self._load_error = f"モデル加载失敗: {e}"
            self._loading = False
            self._loading_msg = self._load_error


    # ── WAV ヘッダー除去 → PCM bytes を返す ──
    @staticmethod
    def _wav_to_pcm(raw: bytes) -> bytes:
        with io.BytesIO(raw) as f:
            with wave.open(f) as w:
                if w.getnchannels() != 1:
                    raise ValueError("ステレオには未対応")
                if w.getsampwidth() != 2:
                    raise ValueError("16bit のみ対応")
                return w.readframes(w.getnframes())

    # ── キューに音声を追加（mpv/ffplay から呼ばれる） ──
    def feed(self, pcm_chunk: bytes):
        if not self._running:
            return
        if self._model is not None:
            try:
                self._audio_queue.put_nowait(pcm_chunk)
            except queue.Full:
                pass
        else:
            # model 加载中 → 暂存到 prebuf
            self._prebuf.append(pcm_chunk)
            # prebuf 最多保留 60 秒音频（约 60 * 16000 * 2 = 1.92MB）
            while len(self._prebuf) > 750:   # ~60s @ 8s chunks
                self._prebuf.pop(0)

    def _worker(self):
        self._ensure_model()
        if not self._model:
            return

        # model 加载完成后，先处理 prebuf（缓冲期间积累的音频）
        while self._prebuf and self._running:
            raw = self._prebuf.pop(0)
            self._transcribe_chunk(raw)

        while self._running:
            try:
                raw = self._audio_queue.get(timeout=2.0)
            except queue.Empty:
                continue
            self._transcribe_chunk(raw)

    def _transcribe_chunk(self, raw: bytes):
        """对一个 WAV chunk 进行识别，结果写入 _recent。"""
        try:
            pcm = self._wav_to_pcm(raw)
        except Exception:
            return
        if len(pcm) < SAMPLE_RATE // 8:  # 0.125秒以下はスキップ
            return
        try:
            segments, info = self._model.transcribe(
                io.BytesIO(pcm),
                language=self.language,
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=300),
            )
            text_parts = []
            for seg in segments:
                if seg.text.strip():
                    text_parts.append(seg.text.strip())
            if text_parts:
                text = " ".join(text_parts)
                with self._lock:
                    self._recent.append(text)
                    if len(self._recent) > 20:
                        self._recent.pop(0)
                try:
                    self._transcript_queue.put_nowait(text)
                except queue.Full:
                    pass
        except Exception:
            pass

    # ── 制御 ──
    def start(self):
        if not ENABLE_STT:
            return
        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._prebuf.clear()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        with self._lock:
            self._recent.clear()

    def get_recent(self) -> list[str]:
        with self._lock:
            return list(self._recent)

    def get_latest(self) -> str:
        with self._lock:
            return self._recent[-1] if self._recent else ""

    def is_ready(self) -> bool:
        return self._model is not None and self._load_error is None

    def download_progress(self) -> tuple[float, int, int]:
        """返回 (progress_0_100, downloaded_bytes, total_bytes)。"""
        return (self._dl_progress, self._dl_downloaded, self._model_total_bytes)

    def is_loading(self) -> bool:
        return self._loading

    def loading_msg(self) -> str:
        return self._loading_msg

    def error(self) -> str | None:
        return self._load_error


# ──────────────────────────────────────────────
# 3.  放送局データ
# ──────────────────────────────────────────────
STATIONS = [
    {
        "id": "nhk_r1",
        "name": "NHK ラジオ第1",
        "name_zh": "NHK 广播第1（综合·新闻）",
        "desc": "NHK総合 — ニュース・情報・天気予報",
        "url": "https://simul.drdi.st.nhk/live/3/joined/master.m3u8",
        "logo": "NHK1",
        "category": "NHK",
    },
    {
        "id": "nhk_r2",
        "name": "NHK ラジオ第2",
        "name_zh": "NHK 广播第2（教育·外语）",
        "desc": "NHK第2 — 教育・語学・宗教番組",
        "url": "https://simul.drdi.st.nhk/live/4/joined/master.m3u8",
        "logo": "NHK2",
        "category": "NHK",
    },
    {
        "id": "nhk_fm",
        "name": "NHK FM",
        "name_zh": "NHK FM（音乐·文化）",
        "desc": "NHK FM — 音楽・文化・エンタメ",
        "url": "https://simul.drdi.st.nhk/live/5/joined/master.m3u8",
        "logo": "NHKFM",
        "category": "NHK",
    },
]

NHK_CONFIG_URL = "https://www.nhk.or.jp/radio/config/config_web.xml"

AREA_MAP = {
    "sapporo":   "札幌",
    "sendai":    "仙台",
    "tokyo":     "東京",
    "nagoya":    "名古屋",
    "osaka":     "大阪",
    "hiroshima": "広島",
    "matsuyama": "松山",
    "fukuoka":   "福岡",
}


def fetch_nhk_regional(area_key: str = DEFAULT_AREA) -> list[dict]:
    try:
        req = Request(NHK_CONFIG_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as resp:
            xml_text = resp.read().decode("utf-8")
        root = ET.fromstring(xml_text)
        for data in root.findall(".//data"):
            area = data.find("area")
            if area is None or area.text != area_key:
                continue
            city = data.find("areajp")
            city_name = city.text if city is not None else AREA_MAP.get(area_key, area_key)

            result = []
            for key, label, zh_label in [
                ("r1hls", "NHK ラジオ第1", "广播第1"),
                ("r2hls", "NHK ラジオ第2", "广播第2"),
                ("fmhls",  "NHK FM",         "FM"),
            ]:
                el = data.find(key)
                if el is not None and el.text:
                    base_id = key.replace("hls", "")
                    result.append({
                        "id": f"nhk_{base_id}_{area_key}",
                        "name": f"{label}（{city_name}）",
                        "name_zh": f"NHK {zh_label}（{city_name}）",
                        "desc": f"{label} {city_name} フィラー",
                        "url": el.text.strip(),
                        "logo": f"NHK{base_id.upper()}",
                        "category": f"NHK-{city_name}",
                    })
            return result
    except Exception as e:
        print(f"[警告] NHK XML取得失敗 ({e})、デフォルトURLを使用", file=sys.stderr)
    return []


# ──────────────────────────────────────────────
# 4.  バックエンド検出
# ──────────────────────────────────────────────
def detect_backend() -> str | None:
    for cmd in ["mpv", "ffplay"]:
        r = subprocess.run(["which", cmd], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return cmd
    return None


# ──────────────────────────────────────────────
# 5.  プレイヤー（ffmpeg キャプチャ対応）
# ──────────────────────────────────────────────
class RadioPlayer:
    def __init__(self, stt: AudioSTT | None):
        self.stt = stt
        self.backend = detect_backend()
        if not self.backend:
            print(
                "エラー：mpv または ffplay が見つかりません。\n"
                "  macOS: brew install mpv\n"
                "  Linux: sudo apt install mpv\n",
                file=sys.stderr,
            )
            sys.exit(1)

        self.proc: subprocess.Popen | None = None
        self.capture_proc: subprocess.Popen | None = None   # ffmpeg キャプチャ
        self.current_station: dict | None = None
        self.is_playing = False
        self.is_paused = False
        self.volume = 80

    # ── mpv の場合は audio-output で Raw PCM を別プロセスに吐出 ──
    def _build_mpv_args(self, url: str) -> tuple[list[str], list[str]]:
        """(mpv_args, ffmpeg_args) を返す。"""
        mpv_args = [
            self.backend,
            url,
            "--no-video",
            "--quiet",
            f"--volume={self.volume}",
            "--no-resume-playback",
            "--force-seekable=yes",
            "--demuxer-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=10",
            # Raw PCM 出力（ffmpeg が受信）
            "--audio-display=no",
            "--audio-channels=mono",
            "--audio-samplerate=16000",
            f"--audio-file=pipe:1",
            "--ao=pcm:file=-",
        ]
        # ffmpeg: PCM → 16kHz mono WAV → stdout（Whisper が消費）
        ffmpeg_args = [
            "ffmpeg",
            "-f", "s16le",          # 入力: signed 16bit little-endian PCM
            "-ar", str(SAMPLE_RATE),
            "-ac", "1",
            "-i", "pipe:0",
            "-f", "wav",
            "pipe:1",
        ]
        return mpv_args, ffmpeg_args

    def _build_ffplay_args(self, url: str) -> list[str]:
        return [
            self.backend,
            "-v", "error",
            "-nodisp",
            "-autoexit",
            "-volume", str(self.volume),
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            url,
        ]

    def play(self, station: dict):
        self.stop()
        self.current_station = station

        try:
            if self.backend == "mpv" and self.stt:
                # mpv → pipe → ffmpeg → WAV PCM → Whisper
                mpv_args, ffmpeg_args = self._build_mpv_args(station["url"])

                # ffmpeg を先に起動（stdin=PIPE, stdout=PIPE）
                self.capture_proc = subprocess.Popen(
                    ffmpeg_args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )

                # mpv の出力を ffmpeg の stdin に接続
                self.proc = subprocess.Popen(
                    mpv_args,
                    stdout=self.capture_proc.stdin,
                    stderr=subprocess.DEVNULL,
                )
                # mpv stdout = ffmpeg stdin（閉じるのは proc 終了時）
                if self.capture_proc.stdin:
                    self.capture_proc.stdin.close()

                # ffmpeg stdout を読み取るスレッド → Whisper に渡す
                t = threading.Thread(target=self._pcm_reader, daemon=True)
                t.start()

            else:
                args = self._build_ffplay_args(station["url"])
                self.proc = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            self.is_playing = True
            self.is_paused = False
            print(f"\n▶  再生中: {station['name']} ({station['name_zh']})")
            print(f"   {station['desc']}")
            if self.stt and self.stt.is_ready():
                print(f"   🎤 音声認識: 動作中")
            elif self.stt:
                print(f"   🎤 音声認識: {self.stt.error() or '初期化中'}")
        except Exception as e:
            print(f"再生エラー: {e}", file=sys.stderr)
            self.is_playing = False

    def _pcm_reader(self):
        """ffmpeg stdout（WAV PCM）を読み取って Whisper に送る。"""
        try:
            with self.capture_proc.stdout as stdout:
                buf = io.BytesIO()
                chunk_size = SAMPLE_RATE * 2 * CHUNK_DURATION  # 2 bytes/sample
                while True:
                    chunk = stdout.read(chunk_size)
                    if not chunk:
                        break
                    # WAV バイナリを構築
                    wav_buf = io.BytesIO()
                    with wave.open(wav_buf, "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(SAMPLE_RATE)
                        w.writeframes(chunk)
                    if self.stt:
                        self.stt.feed(wav_buf.getvalue())
        except Exception:
            pass

    def stop(self):
        def safe_terminate(p, timeout=2.0):
            if p is None:
                return
            try:
                p.terminate()
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                except Exception:
                    pass
            except Exception:
                pass

        safe_terminate(self.proc)
        safe_terminate(self.capture_proc)
        self.proc = None
        self.capture_proc = None
        self.is_playing = False
        self.is_paused = False
        self.current_station = None

    def pause_resume(self):
        if not self.current_station:
            return
        if self.backend == "mpv" and self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGSTOP if not self.is_paused else signal.SIGCONT)
            self.is_paused = not self.is_paused
        else:
            was = self.is_playing
            self.stop()
            if was and self.current_station:
                self.play(self.current_station)

        state = "一時停止中" if self.is_paused else "再生中"
        print(f"\n{'⏸' if self.is_paused else '▶'}  {state}")

    def set_volume(self, delta: int):
        self.volume = max(0, min(100, self.volume + delta))
        print(f"\n🔊  音量: {self.volume}%")

    def status(self) -> str:
        if not self.is_playing:
            return "停止中"
        if self.is_paused:
            return "一時停止"
        return "再生中"

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


# ──────────────────────────────────────────────
# 6.  TUI — curses
# ──────────────────────────────────────────────
try:
    import curses
except ImportError:
    curses = None


def run_curses(stdscr, player: RadioPlayer, all_stations: list, stt: AudioSTT | None):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(150)

    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)      # 選択行
    curses.init_pair(2, curses.COLOR_WHITE, -1)     # 通常
    curses.init_pair(3, curses.COLOR_YELLOW, -1)    # 見出し
    curses.init_pair(4, curses.COLOR_GREEN, -1)     # 再生中
    curses.init_pair(5, curses.COLOR_RED, -1)       # 停止
    curses.init_pair(6, curses.COLOR_MAGENTA, -1)  # カテゴリ
    curses.init_pair(7, curses.COLOR_GREEN, -1)    # 認識テキスト
    curses.init_pair(8, curses.COLOR_WHITE, -1)     # 認識テキスト背景

    current_idx = 0
    categories = sorted(set(s.get("category", "Other") for s in all_stations))
    current_cat_idx = 0
    filter_cat: str | None = None

    def get_visible(fc: str | None) -> list:
        return all_stations if fc is None else [s for s in all_stations if s.get("category") == fc]

    def draw():
        nonlocal current_idx, stt_anim_frame, _stt_was_loading
        h, w = stdscr.getmaxyx()
        stdscr.clear()

        # ── STT 下位 3 行提前定义（供全函数使用）──
        stt_rows = 3

        # ── Whisper 加载完成通知 ──
        if stt:
            was_loading = _stt_was_loading
            if was_loading and not stt.is_loading() and stt.is_ready():
                _stt_was_loading = False
                # 加载完成，在 STT 区分隔线上方显示一行提示
                try:
                    stt_sep_y = h - stt_rows - 2
                    progress = stt.download_progress() if hasattr(stt, "download_progress") else 100.0
                    total_mb = (stt._model_total_bytes / (1024*1024)) if hasattr(stt, "_model_total_bytes") else 1500
                    mb_str = f"{total_mb:.0f}MB"
                    msg = f"✅ Whisper モデル加载完了！({mb_str})"
                    stdscr.addstr(stt_sep_y - 1, 2,
                        msg, curses.color_pair(4) | curses.A_BOLD)
                except curses.error:
                    pass

        # ── タイトル ──
        title = "📻  日本ラジオ ニュース受信ラジオ"
        stdscr.addstr(0, max(0, (w - len(title)) // 2),
                      title, curses.A_BOLD | curses.color_pair(3))
        engine_str = f"[{player.backend} | vol:{player.volume}%]"
        stdscr.addstr(0, w - len(engine_str) - 1, engine_str, curses.A_DIM)

        # ── カテゴリバー ──
        cat_bar = " | ".join((">" + c if c == filter_cat else c) for c in categories)
        try:
            stdscr.addstr(1, 2, cat_bar[:w-4], curses.color_pair(6) | curses.A_DIM)
        except curses.error:
            pass

        # ── ステータス ──
        status = player.status()
        sta_name = player.current_station["name"] if player.current_station else "---"
        st_color = curses.color_pair(4) if player.is_playing else curses.color_pair(5)
        st_line = f"{'▶' if player.is_playing else '■'} {status}  |  {sta_name}"
        try:
            stdscr.addstr(1, max(0, w - len(st_line) - 2), st_line[:w-2], st_color)
        except curses.error:
            pass

        # ── 区切り線 ──
        try:
            stdscr.addstr(2, 0, "─" * w)
        except curses.error:
            pass

        # ── 局リスト（STT 下位 3 行を確保）──
        visible = get_visible(filter_cat)
        start_y = 3
        max_list_rows = h - start_y - stt_rows - 2   # 区切り＋操作説明
        if max_list_rows < 3:
            max_list_rows = 5

        current_idx = max(0, min(current_idx, len(visible) - 1))
        scroll_off = 0
        if current_idx >= scroll_off + max_list_rows:
            scroll_off = current_idx - max_list_rows + 1
        if current_idx < scroll_off:
            scroll_off = current_idx

        for i in range(scroll_off, min(len(visible), scroll_off + max_list_rows)):
            y = start_y + (i - scroll_off)
            if y >= h - stt_rows - 2:
                break
            station = visible[i]
            is_sel = (i == current_idx)
            is_playing = (player.is_playing and player.current_station
                          and player.current_station["id"] == station["id"])

            marker = "▶" if is_playing else " "
            sel_attr = curses.color_pair(1) | curses.A_BOLD
            norm_attr = curses.color_pair(2)

            line1 = f"{marker} {i+1:2d}. {station['name']:<30}"
            line2 = f"      {station['name_zh']}"
            line3 = f"      {station['desc']}"

            try:
                stdscr.addstr(y,   2, line1[:w-4], sel_attr if is_sel else (norm_attr | curses.A_BOLD))
                stdscr.addstr(y+1, 4, line2[:w-6], norm_attr if is_sel else curses.A_DIM)
                stdscr.addstr(y+2, 4, line3[:w-6],
                              curses.A_DIM | (curses.color_pair(4) if is_playing else curses.A_DIM))
            except curses.error:
                pass

        # ── STT 区切り線 ──
        stt_sep_y = h - stt_rows - 2
        try:
            stdscr.addstr(stt_sep_y, 0, "─" * w)
        except curses.error:
            pass

        # ── 音声認識テキスト（最下行から3行）──
        stt_label = "🎤 認識: "
        stt_ready = stt and stt.is_ready()
        stt_err   = stt.error() if stt else None

        # STT 总开关状态：区分「关闭」「加载中/待机」「运行中」
        if not stt_active:
            stt_lines = ["", f"{stt_label}─── 認識OFF（按 T 开启）───", ""]
        elif stt_err:
            stt_lines = [f"{stt_label}エラー: {stt_err}", "", ""]
        elif stt and stt.is_loading():
            _pct, _dl_bytes, _total_bytes = (
                stt.download_progress() if hasattr(stt, "download_progress")
                else (0.0, 0, 1500*1024*1024)
            )
            # 文件大小（两位小数 K/M/G/T + B 后缀）
            dl_str  = _fmt_bytes(_dl_bytes)
            tot_str = _fmt_bytes(_total_bytes)
            # 预计剩余时间 + 速度
            _elapsed = time.monotonic() - stt._dl_start_time if hasattr(stt, "_dl_start_time") else 0.0
            if _pct > 0.1 and _elapsed > 0:
                speed_bps = _dl_bytes / _elapsed       # bytes/sec
                speed_bits = speed_bps * 8             # bits/sec
                remain_bytes = _total_bytes - _dl_bytes
                remain_sec = int(remain_bytes / speed_bps) if speed_bps > 0 else 0
                if remain_sec >= 3600:
                    eta_str = f"ETA {remain_sec//3600}h{(remain_sec%3600)//60}m"
                elif remain_sec >= 60:
                    eta_str = f"ETA {remain_sec//60}m{remain_sec%60}s"
                elif remain_sec > 0:
                    eta_str = f"ETA {remain_sec}s"
                else:
                    eta_str = "        "
                speed_str = _fmt_bits(speed_bits) + "/s"
            else:
                eta_str = "        "
                speed_str = ""
            if speed_str:
                eta_str = f"{eta_str}  {speed_str}"
            bar_width = max(10, w - 70)
            filled = int(bar_width * _pct / 100)
            bar = "█" * filled + "░" * (bar_width - filled)
            pct_str = f"{_pct:5.1f}%"
            size_str = f"{dl_str}/{tot_str}"
            stt_lines = [
                f"  {eta_str}  {stt.loading_msg()}",
                f"  ▓{bar}▓  {pct_str}  {size_str}",
                "",
            ]
        elif not stt_ready:
            stt_lines = [f"{stt_label}待機中...", "", ""]
        else:
            recent = stt.get_recent() if stt else []
            if recent:
                # 最新 → 下から2行目、1つ前 → 下から3行目、最旧 → 下から1行目
                display = recent[-3:] if len(recent) >= 3 else recent
                stt_lines = ["", "", ""]
                for j, text in enumerate(display):
                    row_idx = stt_rows - len(display) + j   # 0..2
                    if row_idx >= 0:
                        stt_lines[row_idx] = text
            else:
                stt_lines = [f"{stt_label}待機中...", "", ""]

        for i, line in enumerate(stt_lines):
            y = stt_sep_y + 1 + i
            if y >= h - 1:
                break
            # 認識テキストは緑色、薄い文字で
            attr = curses.A_DIM | curses.color_pair(7)
            label = stt_label if (i == 0 and not stt_ready and not stt_err) else ""
            try:
                stdscr.addstr(y, 2, (label + line)[:w-4], attr)
            except curses.error:
                pass

        # ── 操作説明（最下行）──
        help_items = [
            "↑↓:移動", "Enter:再生", "SPACE/p:再生/一時停止",
            "s:停止", "+/-:音量", "c:カテゴリ", "t:STT ON/OFF", "q:終了",
        ]
        try:
            stdscr.addstr(h-1, 2, "  |  ".join(help_items)[:w-4], curses.A_DIM)
        except curses.error:
            pass

        stdscr.refresh()

    # STT ON/OFF トグル用フラグ（draw 内から参照）
    stt_active = True

    stt_anim_frame = 0
    _stt_was_loading = True   # 前回も加载中だったか（完了通知用）

    while True:
        # ── 动画帧递增（在 draw 之前，让动画实时生效）──
        stt_anim_frame += 1

        draw()
        try:
            key = stdscr.getch()
        except curses.error:
            key = -1

        if key != -1:
            visible = get_visible(filter_cat)

            if key in (curses.KEY_UP, ord("k")):
                current_idx = max(0, current_idx - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                current_idx = min(len(visible) - 1, current_idx + 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                if visible:
                    player.play(visible[current_idx])
            elif key in (ord(" "), ord("p")):
                if player.is_playing:
                    player.pause_resume()
                elif visible:
                    player.play(visible[current_idx])
            elif key in (ord("s"), ord("S")):
                player.stop()
            elif key in (ord("+"), ord("=")):
                player.set_volume(10)
            elif key in (ord("-"), ord("_")):
                player.set_volume(-10)
            elif key in (ord("c"), ord("C")):
                current_cat_idx = (current_cat_idx + 1) % (len(categories) + 1)
                filter_cat = None if current_cat_idx == len(categories) else categories[current_cat_idx]
                current_idx = 0
            elif key in (ord("t"), ord("T")):
                # STT ON/OFF
                stt_active = not stt_active
                if stt_active and stt:
                    stt.start()
                elif stt:
                    stt.stop()
                player.stop()
                player.stt = stt if stt_active else None
            elif key in (ord("q"), ord("Q"), 27):
                player.stop()
                if stt:
                    stt.stop()
                break

        if player.is_playing and not player.is_alive():
            try:
                h2, _ = stdscr.getmaxyx()
                stdscr.addstr(h2 - 2, 2, "⚠ ストリーム切断、局を選択してください。",
                              curses.color_pair(5) | curses.A_BOLD)
            except curses.error:
                pass
            player.is_playing = False

        time.sleep(0.05)


def run_simple(player: RadioPlayer, all_stations: list, stt: AudioSTT | None):
    print("\n" + "=" * 60)
    print("📻  日本ラジオ ニュース受信ラジオ")
    print("=" * 60)
    print(f"再生エンジン: {player.backend}  |  音量: {player.volume}%")
    if stt and stt.is_ready():
        print("🎤 音声認識: 動作中")
    elif stt:
        print(f"🎤 音声認識: {stt.error() or 'モデル加载中...'}")
    print()

    while True:
        print("\n【局リスト】")
        for i, s in enumerate(all_stations):
            print(f"  {i+1:2d}. {s['name']} [{s.get('category','')}]")
            print(f"       {s['name_zh']} — {s['desc']}")
        print()

        try:
            choice = input("番号 (1-{}) / q終了: ".format(len(all_stations))).strip()
        except (KeyboardInterrupt, EOFError):
            print("\n終了します。")
            break

        if choice.lower() == "q":
            player.stop()
            break
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(all_stations):
                station = all_stations[idx]
                print(f"\n▶  {station['name']} を再生中...")
                print(f"   操作: p/space=一時停止  s=停止  +/-=音量  q=終了\n")
                player.play(station)

                try:
                    while True:
                        if player.proc and player.proc.poll() is not None:
                            print("\n⚠  ストリーム接続切れ。")
                            break

                        # 認識結果表示（5秒ごと）
                        if stt and stt.is_ready():
                            recent = stt.get_recent()
                            if recent:
                                print(f"\r🎤 {recent[-1][:80]}      ", end="", flush=True)

                        try:
                            cmd = input().strip()
                        except (KeyboardInterrupt, EOFError):
                            print()
                            break
                        if not cmd:
                            continue
                        if cmd == "q":
                            player.stop()
                            break
                        elif cmd in ("p", " "):
                            player.pause_resume()
                        elif cmd in ("+", "="):
                            player.set_volume(10)
                        elif cmd in ("-", "_"):
                            player.set_volume(-10)
                        elif cmd in ("s", "stop"):
                            player.stop()
                            print("停止しました。")
                            break
                except (KeyboardInterrupt, EOFError):
                    player.stop()
                    print("\n中断しました。")
            else:
                print("範囲外の番号です。")
        except ValueError:
            print("数字を入力してください。")


# ──────────────────────────────────────────────
# 7.  エントリーポイント
# ──────────────────────────────────────────────
def main():
    print("📻  日本ラジオ ニュース受信ラジオ 起動中...")

    # STT 初期化
    stt: AudioSTT | None = None
    if ENABLE_STT:
        print("🎤 音声認識モデル加载中（初回のみ、 Downloading ...）...")
        stt = AudioSTT(model_name=WHISPER_MODEL, language=WHISPER_LANG)
        stt.start()   # バックグラウンドでモデル加载 + 待機

    # 局リスト構築
    base_stations = STATIONS[:]
    regional = fetch_nhk_regional(DEFAULT_AREA)
    all_stations = base_stations + regional
    seen = {}
    for s in all_stations:
        seen[s["id"]] = s
    all_stations = list(seen.values())
    all_stations.sort(key=lambda s: (s.get("category", ""), s["id"]))

    print(f"登録局数: {len(all_stations)}")

    player = RadioPlayer(stt)

    if curses is not None:
        try:
            curses.wrapper(lambda sc: run_curses(sc, player, all_stations, stt))
        except Exception as e:
            print(f"\nTUIエラー ({e})、简单モードで起動します。\n")
            run_simple(player, all_stations, stt)
    else:
        print("curses がないため、简单モードで起動します。")
        run_simple(player, all_stations, stt)

    if stt:
        stt.stop()
    print("\n終了。")


if __name__ == "__main__":
    main()
