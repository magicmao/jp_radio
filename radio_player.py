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
import tempfile
import xml.etree.ElementTree as ET
import wave
import numpy as np
from urllib.request import urlopen, Request
from urllib.error import URLError

# ──────────────────────────────────────────────
# 1.  Settings
# ──────────────────────────────────────────────
DEFAULT_AREA = "tokyo"
ENABLE_STT = True           # False で STT を無効化（起動を速くする）
WHISPER_MODEL = "medium"     # tiny≈実時/small≈実時+精度/medium=精度重視(CPU遅い)
CHUNK_DURATION = 1.0        # 秒ごとキャプチャ（短いほど実時間性↑、精度↓）
SAMPLE_RATE = 16000
WHISPER_LANG = "ja"

# ── TUI 字幕配色（curses color 番号）──
# 黒背景で見やすい配色: 歴史行=白, 実時行=黄色太字
STT_HIST_COLOR = "WHITE"        # 历史字幕前景色 (WHITE/GREEN/CYAN/YELLOW)
STT_LIVE_COLOR = "YELLOW"       # 实时字幕前景色
STT_HIST_BG    = "DEFAULT"      # 历史字幕背景色 (DEFAULT = 终端默认)
STT_LIVE_BG    = "DEFAULT"      # 实时字幕背景色

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
        self._audio_queue: queue.Queue[tuple[float, bytes]] = queue.Queue(maxsize=16)
        self._transcript_queue: queue.Queue[tuple[float, str]] = queue.Queue(maxsize=3)
        # 直近の認識結果（滚动显示，扩大缓冲），每项为 (音频录制时间, 文本)
        self._recent: list[tuple[float, str]] = []
        self._current_partial: str = ""  # 当前正在识别的部分文本
        self._current_partial_audio_time: float = 0.0  # 对应音频时间戳
        self._prebuf: list[bytes] = []   # model 加载期间暂存音频
        self._lock = threading.Lock()
        # 逐块识别状态
        self._prev_text: str = ""   # 前一块识别文本（用于 initial_prompt 上下文）
        self._transcribing = False  # 识别进行中标志
        self._is_music = False      # 当前为音乐/非语音
        self._chunk_accum: bytes = b""  # 累积 PCM（攒够 2 块再识别，减少短片段误识别）
        self._chunk_accum_audio_time: float = 0.0  # 累积块的起始音频时间
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
        """后台线程：每 0.3s 扫描模型目录计算下载进度。"""
        self._dl_start_time = time.monotonic()
        # 记录扫描开始前目录的基线大小（排除已有模型文件的干扰）
        baseline = 0
        try:
            if self._model_cache and os.path.isdir(self._model_cache):
                baseline = sum(
                    os.path.getsize(os.path.join(root, f))
                    for root, _, files in os.walk(self._model_cache)
                    for f in files
                )
        except Exception:
            pass
        # 如果基线已 >= 模型大小，说明已缓存，走加载模式而非下载模式
        is_cached = baseline >= self._model_total_bytes * 0.8
        if is_cached:
            self._loading_msg = f"Whisper モデル読込中 ({self.model_name})..."
            while self._loading and self._dl_progress < 99.0:
                elapsed = time.monotonic() - self._dl_start_time
                # 渐近曲线：永远逼近但不到 99%，不会"卡住"
                # 公式：99 * (1 - e^(-t/8))，8秒时约到63%，16秒约86%，24秒约95%
                import math
                pct = 99.0 * (1.0 - math.exp(-elapsed / 8.0))
                self._dl_progress = pct
                self._dl_downloaded = int(self._model_total_bytes * pct / 100)
                self._loading_msg = f"Whisper モデル読込中 ({self.model_name})... {elapsed:.0f}s"
                time.sleep(0.3)
        else:
            # 首次下载：扫描文件增量
            while self._loading and self._dl_progress < 99.0:
                try:
                    if self._model_cache and os.path.isdir(self._model_cache):
                        total = sum(
                            os.path.getsize(os.path.join(root, f))
                            for root, _, files in os.walk(self._model_cache)
                            for f in files
                        )
                        new_bytes = max(0, total - baseline)
                        self._dl_downloaded = new_bytes
                        self._dl_progress = min(99.0, new_bytes / self._model_total_bytes * 100)
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
            _os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
            _os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
            # 抑制 HF Hub 的 "unauthenticated requests" 警告
            import warnings
            warnings.filterwarnings("ignore", message=".*unauthenticated.*")
            import logging
            logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
            self._model_cache = _model_cache

            # 启动进度监控线程
            self._dl_progress = 0.0
            self._dl_downloaded = 0
            self._dl_progress_thread = threading.Thread(
                target=self._dl_progress_worker, daemon=True
            )
            self._dl_progress_thread.start()

            # 模型加载期间重定向 stderr，防止警告污染 curses TUI
            _old_stderr = sys.stderr
            _devnull = open(os.devnull, "w")
            sys.stderr = _devnull
            try:
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
            finally:
                sys.stderr = _old_stderr
                _devnull.close()

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
    def feed(self, pcm_chunk: bytes, audio_time: float | None = None):
        if audio_time is None:
            audio_time = time.monotonic()
        if not self._running:
            return
        if self._model is not None:
            try:
                self._audio_queue.put_nowait((audio_time, pcm_chunk))
            except queue.Full:
                # 队列满（16s积压）才丢弃最旧的
                try:
                    self._audio_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._audio_queue.put_nowait((audio_time, pcm_chunk))
                except queue.Full:
                    pass
        else:
            pass

    def _worker(self):
        self._ensure_model()
        if not self._model:
            return

        # model 加载完成后，丢弃 prebuf（旧音频已过时，避免延迟）
        self._prebuf.clear()

        while self._running:
            try:
                audio_time, raw = self._audio_queue.get(timeout=2.0)
            except queue.Empty:
                continue
            # 解码 WAV → PCM
            if len(raw) < 44:
                continue
            try:
                with io.BytesIO(raw) as f:
                    with wave.open(f) as w:
                        pcm = w.readframes(w.getnframes())
            except Exception:
                continue

            # 累积 PCM 到缓冲，保留起始时间戳
            if self._chunk_accum_audio_time == 0.0:
                self._chunk_accum_audio_time = audio_time
            self._chunk_accum += pcm
            accum_sec = len(self._chunk_accum) / (SAMPLE_RATE * 2)

            # 每累积 >= 2s 就识别一次（但 chunk 只有 1s，所以每 2 个 chunk 触发）
            if accum_sec >= 2.0:
                self._transcribe_pcm(self._chunk_accum, self._chunk_accum_audio_time)
                self._chunk_accum = b""
                self._chunk_accum_audio_time = 0.0

    def _transcribe_pcm(self, pcm: bytes, audio_time: float):
        """PCM bytes + 音频起始时间戳 → 识别。"""
        if len(pcm) < SAMPLE_RATE * 2 // 4:
            return

        self._transcribing = True
        try:
            int16_arr = np.frombuffer(pcm, dtype=np.int16)
            audio = int16_arr.astype(np.float32) / 32768.0

            # 用前一块文本作为 initial_prompt 保持上下文连贯
            prompt = self._prev_text[-100:] if self._prev_text else None

            segments, info = self._model.transcribe(
                audio,
                language=self.language,
                beam_size=1,
                best_of=1,
                vad_filter=False,
                initial_prompt=prompt,
                condition_on_previous_text=False,
            )
            text_parts = []
            high_no_speech = True
            for seg in segments:
                t = seg.text.strip()
                if seg.no_speech_prob < 0.7:
                    high_no_speech = False
                if t:
                    text_parts.append(t)

            # 音乐/非语音检测
            if not text_parts or high_no_speech:
                rms = np.sqrt(np.mean(audio ** 2))
                if rms > 0.01:
                    self._is_music = True
                else:
                    self._is_music = False
                with self._lock:
                    self._current_partial = ""
                return

            self._is_music = False
            chunk_text = "".join(text_parts)
            self._prev_text = chunk_text  # 保存给下一块做 prompt

            with self._lock:
                self._current_partial = chunk_text
                self._current_partial_audio_time = audio_time
                self._recent.append((audio_time, chunk_text))
                if len(self._recent) > 50:
                    self._recent.pop(0)
            try:
                self._transcript_queue.put_nowait((audio_time, chunk_text))
            except queue.Full:
                pass
        except Exception:
            pass
        finally:
            self._transcribing = False

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
        self._prev_text = ""
        self._is_music = False
        self._transcribing = False
        self._chunk_accum = b""
        self._chunk_accum_audio_time = 0.0
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        with self._lock:
            self._recent.clear()
            self._current_partial = ""
            self._current_partial_audio_time = 0.0

    def get_recent(self) -> list[tuple[float, str]]:
        """返回最近识别结果列表，每项 (音频时间戳, 文本)。"""
        with self._lock:
            return list(self._recent)

    def get_recent_texts(self, max_items: int = 4) -> list[str]:
        """返回最近文本（不含时间戳），用于显示。"""
        with self._lock:
            return [text for _, text in self._recent[-max_items:]]

    def get_partial(self) -> str:
        """当前正在识别的实时文本。"""
        with self._lock:
            return self._current_partial

    def get_partial_audio_time(self) -> float:
        """当前实时文本对应的音频时间戳。"""
        with self._lock:
            return self._current_partial_audio_time

    def is_music(self) -> bool:
        """当前是否检测到音乐/非语音。"""
        return self._is_music

    def get_latest(self) -> str:
        with self._lock:
            return self._recent[-1][1] if self._recent else ""

    def get_latest_with_time(self) -> tuple[float, str]:
        """返回最新字幕 (音频时间, 文本)。"""
        with self._lock:
            return self._recent[-1] if self._recent else (0.0, "")

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
        self._fifo_path: str | None = None                  # stream-record FIFO
        self._fifo_dir: str | None = None
        self.current_station: dict | None = None
        self.is_playing = False
        self.is_paused = False
        self.volume = 80
        self._curses_mode = False   # curses 模式下禁止 print

        # ── 音频缓存（用于字幕同步）──
        # 缓存目录：脚本同级 .audio_cache/
        self._cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".audio_cache")
        os.makedirs(self._cache_dir, exist_ok=True)
        self._cache_index: list[tuple[float, float]] = []  # (block_audio_start_time, block_file_offset)
        self._cache_wav_path: str | None = None            # 当前缓存的 WAV 文件
        self._cache_wav: wave.Wave_write | None = None     # 当前打开的 WAV 写入器
        self._cache_total_bytes: int = 0                   # 已写入字节数
        self._cache_start_audio_time: float = 0.0          # 当前缓存文件的音频起始时间
        self._cache_lock = threading.Lock()
        # 播放开始的墙上时间（用于计算当前音频播放位置）
        self._play_start_wall_time: float = 0.0
        self._play_start_audio_offset: float = 0.0        # 播放开始时的音频偏移

    # ── mpv の場合は audio-output で Raw PCM を別プロセスに吐出 ──
    def _build_mpv_play_args(self, url: str) -> list[str]:
        """mpv 播放参数（输出到 coreaudio）。"""
        return [
            self.backend,
            url,
            "--no-video",
            "--quiet",
            f"--volume={self.volume}",
            "--no-resume-playback",
            "--force-seekable=yes",
            "--demuxer-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=10",
        ]

    def _build_ffmpeg_stt_args(self, source: str) -> list[str]:
        """ffmpeg STT capture: source → 16kHz mono s16le PCM → stdout。"""
        return [
            "ffmpeg",
            "-i", source,
            "-map", "0:a:0",
            "-ar", str(SAMPLE_RATE),
            "-ac", "1",
            "-f", "s16le",
            "pipe:1",
        ]

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

    def _init_audio_cache(self, audio_start_time: float):
        """初始化/重置音频缓存，创建新的 WAV 文件。"""
        import uuid
        cache_path = os.path.join(self._cache_dir, f"stream_{uuid.uuid4().hex[:8]}.wav")
        # 清理旧缓存文件
        if self._cache_wav:
            try:
                self._cache_wav.close()
            except Exception:
                pass
        self._cache_wav_path = cache_path
        self._cache_wav = wave.open(cache_path, "wb")
        self._cache_wav.setnchannels(1)
        self._cache_wav.setsampwidth(2)
        self._cache_wav.setframerate(SAMPLE_RATE)
        self._cache_total_bytes = 0
        self._cache_start_audio_time = audio_start_time
        self._cache_index.clear()
        # 重置播放时间基准
        self._play_start_wall_time = time.monotonic()
        self._play_start_audio_offset = audio_start_time

    def _write_audio_cache(self, pcm_chunk: bytes, audio_time: float):
        """写入一段音频到缓存（线程安全）。"""
        with self._cache_lock:
            if self._cache_wav is None:
                return
            # 记录这个块的起始偏移
            self._cache_index.append((audio_time, self._cache_total_bytes))
            self._cache_wav.writeframes(pcm_chunk)
            self._cache_total_bytes += len(pcm_chunk)

    def _flush_audio_cache(self):
        """关闭缓存文件。"""
        with self._cache_lock:
            if self._cache_wav:
                try:
                    self._cache_wav.close()
                except Exception:
                    pass
                self._cache_wav = None

    def get_current_audio_time(self) -> float:
        """计算当前正在播放的音频时间（wall-clock 推算）。"""
        if not self.is_playing or self._play_start_wall_time == 0:
            return 0.0
        elapsed = time.monotonic() - self._play_start_wall_time
        # 暂停时停止推算
        if self.is_paused:
            return self._play_start_audio_offset
        return self._play_start_audio_offset + elapsed

    def play(self, station: dict):
        # 先关闭旧缓存
        self._flush_audio_cache()

        self.stop()
        self.current_station = station

        try:
            if self.stt and self.backend == "mpv":
                # ── FIFO 方式：mpv 播放 + stream-record → FIFO → ffmpeg → STT ──
                # 同一ストリームデータを使うので字幕と音声が完全同期
                self._fifo_dir = tempfile.mkdtemp()
                self._fifo_path = os.path.join(self._fifo_dir, "stream.ts")
                os.mkfifo(self._fifo_path)

                # 先启动 ffmpeg（读 FIFO，会阻塞直到 mpv 开始写入）
                ffmpeg_args = self._build_ffmpeg_stt_args(self._fifo_path)
                self.capture_proc = subprocess.Popen(
                    ffmpeg_args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )

                # 启动 mpv：播放 + 将流录制到 FIFO
                mpv_args = self._build_mpv_play_args(station["url"])
                mpv_args.append(f"--stream-record={self._fifo_path}")
                self.proc = subprocess.Popen(
                    mpv_args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

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
            if not self._curses_mode:
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
        """ffmpeg stdout（s16le PCM）を読み取って Whisper + 缓存发送。"""
        # 用 time.monotonic() 记录音频流开始时间
        first_chunk_time = None
        bytes_written = 0  # 已写入缓存的总字节数（用于推算音频时间）
        try:
            with self.capture_proc.stdout as stdout:
                chunk_size = int(SAMPLE_RATE * 2 * CHUNK_DURATION)  # 2 bytes/sample
                while True:
                    chunk = stdout.read(chunk_size)
                    if not chunk:
                        break
                    if first_chunk_time is None:
                        first_chunk_time = time.monotonic()
                        # 初始化音频缓存
                        self._init_audio_cache(0.0)  # 音频起始时间从 0 开始
                        self._play_start_wall_time = first_chunk_time
                        self._play_start_audio_offset = 0.0

                    # 推算这段音频的时间（从流起始点算起）
                    audio_time = bytes_written / (SAMPLE_RATE * 2)

                    # 写入缓存文件
                    self._write_audio_cache(chunk, audio_time)
                    bytes_written += len(chunk)

                    # 转换成 WAV 发送 STT
                    wav_buf = io.BytesIO()
                    with wave.open(wav_buf, "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(SAMPLE_RATE)
                        w.writeframes(chunk)
                    if self.stt:
                        self.stt.feed(wav_buf.getvalue(), audio_time)
        except Exception:
            pass
        finally:
            self._flush_audio_cache()

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
        # FIFO クリーンアップ
        if self._fifo_path:
            try:
                os.unlink(self._fifo_path)
            except OSError:
                pass
            self._fifo_path = None
        if self._fifo_dir:
            try:
                os.rmdir(self._fifo_dir)
            except OSError:
                pass
            self._fifo_dir = None
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
        # 暂停/恢复后重新校准播放时间
        if self.is_playing and not self.is_paused:
            self._play_start_wall_time = time.monotonic()

        state = "一時停止中" if self.is_paused else "再生中"
        if not self._curses_mode:
            print(f"\n{'⏸' if self.is_paused else '▶'}  {state}")

    def set_volume(self, delta: int):
        self.volume = max(0, min(100, self.volume + delta))
        if not self._curses_mode:
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
    player._curses_mode = True
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(80)

    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)      # 選択行
    curses.init_pair(2, curses.COLOR_WHITE, -1)     # 通常
    curses.init_pair(3, curses.COLOR_YELLOW, -1)    # 見出し
    curses.init_pair(4, curses.COLOR_GREEN, -1)     # 再生中
    curses.init_pair(5, curses.COLOR_RED, -1)       # 停止
    curses.init_pair(6, curses.COLOR_MAGENTA, -1)  # カテゴリ
    # 字幕配色（从顶部配置读取）
    _color_map = {
        "WHITE": curses.COLOR_WHITE, "GREEN": curses.COLOR_GREEN,
        "CYAN": curses.COLOR_CYAN, "YELLOW": curses.COLOR_YELLOW,
        "RED": curses.COLOR_RED, "MAGENTA": curses.COLOR_MAGENTA,
        "BLUE": curses.COLOR_BLUE, "DEFAULT": -1,
    }
    _stt_hist_fg = _color_map.get(STT_HIST_COLOR, curses.COLOR_WHITE)
    _stt_hist_bg = _color_map.get(STT_HIST_BG, -1)
    _stt_live_fg = _color_map.get(STT_LIVE_COLOR, curses.COLOR_YELLOW)
    _stt_live_bg = _color_map.get(STT_LIVE_BG, -1)
    curses.init_pair(7, _stt_hist_fg, _stt_hist_bg)   # 歴史字幕
    curses.init_pair(8, _stt_live_fg, _stt_live_bg)   # 実時字幕

    current_idx = 0
    categories = sorted(set(s.get("category", "Other") for s in all_stations))
    current_cat_idx = 0
    filter_cat: str | None = None

    def get_visible(fc: str | None) -> list:
        return all_stations if fc is None else [s for s in all_stations if s.get("category") == fc]

    def draw():
        nonlocal current_idx, stt_anim_frame, _stt_was_loading
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        # ── STT 下位 5 行提前定义（供全函数使用）──
        stt_rows = 5

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
            stt_lines = ["", "", f"{stt_label}─── 認識OFF（按 T 开启）───", "", ""]
        elif stt_err:
            stt_lines = ["", f"{stt_label}エラー: {stt_err}", "", "", ""]
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
                "", "", "",
            ]
        elif not stt_ready:
            stt_lines = ["", f"{stt_label}待機中...", "", "", ""]
        else:
            recent = stt.get_recent() if stt else []
            partial = stt.get_partial() if stt else ""
            # 当前音频播放时间（用于字幕同步）
            cur_audio_time = player.get_current_audio_time() if player else 0.0
            # 字幕延迟阈值：音频已播放超过此时间才显示字幕
            SYNC_DELAY = 0.5  # 秒，音频播放 0.5s 后显示对应字幕
            # 前4行=历史，最后1行=当前实时
            hist_rows = stt_rows - 1
            stt_lines = [""] * stt_rows

            # 从历史中选择"已到时间"的字幕（带延迟同步）
            # 字幕按时间顺序排列，取最新的几条
            display_items = []  # 提前初始化，避免 NameError
            if recent:
                # 过滤出已到时间的字幕
                due_captions = [(at, text) for at, text in recent if (cur_audio_time - at) >= SYNC_DELAY]
                # 取最近 hist_rows 条
                display_items = due_captions[-hist_rows:] if len(due_captions) >= hist_rows else recent[-hist_rows:]
                for j, (audio_ts, text) in enumerate(display_items):
                    row_idx = hist_rows - len(display_items) + j
                    if 0 <= row_idx < hist_rows:
                        stt_lines[row_idx] = text

            # 最后一行：当前正在识别（闪烁光标）/ 音乐检测
            if stt and stt.is_music():
                music_anim = ["♪", "♫", "♪♫", "♫♪"]
                sym = music_anim[(stt_anim_frame // 4) % len(music_anim)]
                # 显示当前音频时间和字幕延迟差
                if recent and cur_audio_time > 0:
                    last_audio_ts, _ = recent[-1]
                    lag = cur_audio_time - last_audio_ts
                    lag_str = f" [{lag:.1f}s behind]"
                else:
                    lag_str = ""
                stt_lines[stt_rows - 1] = f"  {sym} 【MUSIC...】{sym}{lag_str}"
            elif partial:
                # 实时字幕显示，带延迟指示
                blink = "▍" if (stt_anim_frame // 3) % 2 == 0 else " "
                # 如果有上条字幕的时间，计算延迟
                if recent and cur_audio_time > 0:
                    last_audio_ts, last_text = recent[-1]
                    lag = cur_audio_time - last_audio_ts
                    lag_str = f" [{lag:.1f}s behind]" if lag > 0.1 else ""
                else:
                    lag_str = ""
                stt_lines[stt_rows - 1] = f"▸ {partial}{blink}{lag_str}"
            elif display_items:
                stt_lines[stt_rows - 1] = "▸ ..."
            else:
                stt_lines[stt_rows - 1] = ""

        for i, line in enumerate(stt_lines):
            y = stt_sep_y + 1 + i
            if y >= h - 1:
                break
            if i == stt_rows - 1 and stt_ready and not stt_err:
                # 实时行：亮色粗体（pair 8）
                attr = curses.color_pair(8) | curses.A_BOLD
            else:
                # 历史行（pair 7）
                attr = curses.color_pair(7)
            try:
                stdscr.addstr(y, 2, line[:w-4], attr)
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

        time.sleep(0.02)


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
                            rc = player.proc.poll()
                            print(f"\n⚠  ストリーム切断 (exit={rc})。")
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
            import traceback
            print(f"\nTUIエラー ({e})、简单モードで起動します。\n")
            traceback.print_exc()
            run_simple(player, all_stations, stt)
    else:
        print("curses がないため、简单モードで起動します。")
        run_simple(player, all_stations, stt)

    if stt:
        stt.stop()
    print("\n終了。")


if __name__ == "__main__":
    main()
