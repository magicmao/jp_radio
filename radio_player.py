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
from difflib import SequenceMatcher
import numpy as np
from urllib.request import urlopen, Request
from urllib.error import URLError

try:
    import yaml
except ImportError:
    yaml = None

# ──────────────────────────────────────────────
# 1.  Settings
# ──────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATIONS_CONFIG_PATH = os.path.join(SCRIPT_DIR, "stations.yaml")
DEFAULT_AREA = "tokyo"
ENABLE_STT = True           # False で STT を無効化（起動を速くする）
WHISPER_MODEL = "small"  # tiny/base/small/medium/large-v2/large-v3/large-v3-turbo
CHUNK_DURATION = 1.2        # 缩短分块，降低字幕出现延迟
STT_CONTEXT_SECONDS = 0.8   # 给下一块保留少量上下文，兼顾同步与识别稳定性
SAMPLE_RATE = 16000
WHISPER_LANG = "ja"
WHISPER_BEAM_SIZE = 1       # greedy decoding — 最速
WHISPER_BEST_OF = 1
WHISPER_TEMPERATURE = 0.0
WHISPER_CONDITION_ON_PREVIOUS_TEXT = False  # 实时字幕更稳定，减少重复与越滚越慢
WHISPER_NO_SPEECH_THRESHOLD = 0.7
SUBTITLE_EMIT_RATIO = 0.35  # 在片段前 35% 左右就显示，体感更接近实时
WHISPER_INITIAL_PROMPT = "これは日本語のラジオ音声です。ニュース、天気、交通情報、アナウンサーの会話を、英字ではなく自然な日本語で書き起こしてください。"

PCM_BYTES_PER_SECOND = SAMPLE_RATE * 2
MAX_PROMPT_CHARS = 80
MIN_OVERLAP_CHARS = 4
MAX_OVERLAP_CHARS = 24
MIN_EMIT_INTERVAL = 0.45
SIMILAR_EMIT_INTERVAL = 0.8
MIN_SEGMENT_DURATION = 0.05
MIN_NEW_SEGMENT_END = 0.05

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
        self._last_emitted_text: str = ""
        self._last_emitted_audio_time: float = 0.0
        self._pcm_tail: bytes = b""   # 给下一块保留一点上下文，避免短块切断句子
        self._transcribing = False  # 识别进行中标志
        self._is_music = False      # 当前为音乐/非语音
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
            "large":3100*1024*1024,"large-v3-turbo":1600*1024*1024,
        }
        for k in list(_sizes):  # list() prevents dict size change during iteration
            _sizes[k+"+.en"] = _sizes[k]
        self._model_total_bytes = _sizes.get(model_name, 1500*1024*1024)
        self._dl_progress_thread: threading.Thread | None = None

    # ── 进度监控线程 ──
    def _dl_progress_worker(self):
        """后台线程：每 0.3s 扫描模型目录计算下载进度。"""
        self._dl_start_time = time.monotonic()
        # 模型目录路径
        model_dir = os.path.join(self._model_cache, f"models--Systran--faster-whisper-{self.model_name}")

        def _scan_dir_size(path: str, exclude_incomplete=True) -> int:
            """扫描目录总大小（可排除 .incomplete 文件）。"""
            try:
                return sum(
                    os.path.getsize(os.path.join(root, f))
                    for root, _, files in os.walk(path)
                    for f in files
                    if not (exclude_incomplete and f.endswith(".incomplete"))
                )
            except Exception:
                return 0

        # 记录扫描开始前已完成文件的基线大小（不含 .incomplete）
        baseline = _scan_dir_size(model_dir, exclude_incomplete=True)
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
            # 首次下载：直接用已完成文件大小 / 总大小显示进度
            # 注意：.incomplete 文件会被 huggingface_hub 逐步转为正式文件，
            # 所以 exclude_incomplete 后每次扫描只会看到正式文件的增量进度，
            # 无法实时反映 .incomplete 的下载进度。
            # 为解决这个问题：进度 = (baseline + .incomplete 大小) / total
            # 这样能看到下载中的文件增长
            while self._loading and self._dl_progress < 99.0:
                # 已完成文件（不含 .incomplete）
                completed = _scan_dir_size(model_dir, exclude_incomplete=True)
                # 下载中的文件（含 .incomplete）
                downloading = sum(
                    os.path.getsize(os.path.join(root, f))
                    for root, _, files in os.walk(model_dir)
                    for f in files
                    if f.endswith(".incomplete")
                )
                total_downloaded = completed + downloading
                self._dl_downloaded = total_downloaded
                self._dl_progress = min(99.0, total_downloaded / self._model_total_bytes * 100)
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

    def _is_similar_text(self, new_text: str, old_text: str) -> bool:
        if not new_text or not old_text:
            return False
        if new_text == old_text:
            return True
        if new_text in old_text or old_text in new_text:
            shorter = min(len(new_text), len(old_text))
            longer = max(len(new_text), len(old_text))
            if shorter >= 6 or shorter / max(1, longer) >= 0.8:
                return True
        return SequenceMatcher(None, new_text, old_text).ratio() >= 0.88

    def _trim_repeated_prefix(self, new_text: str, old_text: str) -> str:
        if not new_text or not old_text:
            return new_text
        max_overlap = min(len(new_text), len(old_text), MAX_OVERLAP_CHARS)
        for overlap in range(max_overlap, MIN_OVERLAP_CHARS - 1, -1):
            if old_text.endswith(new_text[:overlap]):
                trimmed = new_text[overlap:].lstrip(" 　、。,.!！?？")
                return trimmed or ""
        return new_text

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

            # ── 跳过积压：如果队列里还有更新的音频，丢弃旧的 ──
            skipped = 0
            while not self._audio_queue.empty():
                try:
                    audio_time, raw = self._audio_queue.get_nowait()
                    skipped += 1
                except queue.Empty:
                    break
            # 解码 WAV → PCM
            if len(raw) < 44:
                continue
            try:
                with io.BytesIO(raw) as f:
                    with wave.open(f) as w:
                        pcm = w.readframes(w.getnframes())
            except Exception:
                continue

            self._transcribe_pcm(pcm, audio_time)

    def _transcribe_pcm(self, pcm: bytes, audio_time: float):
        """PCM bytes + 音频起始时间戳 → 识别。"""
        if len(pcm) < PCM_BYTES_PER_SECOND // 4:
            return

        tail_bytes = int(PCM_BYTES_PER_SECOND * STT_CONTEXT_SECONDS)
        context_pcm = self._pcm_tail[-tail_bytes:] if tail_bytes > 0 else b""
        merged_pcm = context_pcm + pcm if context_pcm else pcm
        context_seconds = len(context_pcm) / PCM_BYTES_PER_SECOND
        merged_audio_time = max(0.0, audio_time - context_seconds)
        self._pcm_tail = merged_pcm[-tail_bytes:] if tail_bytes > 0 else b""

        self._transcribing = True
        try:
            int16_arr = np.frombuffer(merged_pcm, dtype=np.int16)
            audio = int16_arr.astype(np.float32) / 32768.0

            # 用日文广播提示词 + 前一块尾部上下文，尽量稳定输出日文
            prompt_parts = [WHISPER_INITIAL_PROMPT]
            if self._prev_text:
                prompt_parts.append(self._prev_text[-MAX_PROMPT_CHARS:])
            prompt = " ".join(part for part in prompt_parts if part)

            segments, info = self._model.transcribe(
                audio,
                language=self.language,
                beam_size=WHISPER_BEAM_SIZE,
                best_of=WHISPER_BEST_OF,
                temperature=WHISPER_TEMPERATURE,
                vad_filter=True,
                initial_prompt=prompt,
                condition_on_previous_text=WHISPER_CONDITION_ON_PREVIOUS_TEXT,
                word_timestamps=False,
            )
            emitted_segments: list[tuple[float, str]] = []
            high_no_speech = True
            default_emit_time = audio_time + max(0.0, CHUNK_DURATION * SUBTITLE_EMIT_RATIO)
            chunk_text_parts: list[str] = []

            for seg in segments:
                t = seg.text.strip()
                if getattr(seg, "no_speech_prob", 1.0) < WHISPER_NO_SPEECH_THRESHOLD:
                    high_no_speech = False
                if not t:
                    continue

                seg_start = float(getattr(seg, "start", 0.0) or 0.0)
                seg_end = float(getattr(seg, "end", seg_start) or seg_start)
                if context_seconds > 0 and seg_end <= context_seconds + MIN_NEW_SEGMENT_END:
                    continue

                t = self._trim_repeated_prefix(t, self._last_emitted_text)
                if not t:
                    continue

                effective_start = max(seg_start, context_seconds)
                effective_end = max(seg_end, effective_start + MIN_SEGMENT_DURATION)
                emit_time = merged_audio_time + effective_start + (effective_end - effective_start) * SUBTITLE_EMIT_RATIO
                emit_time = max(audio_time, emit_time)
                emitted_segments.append((emit_time, t))
                chunk_text_parts.append(t)

            # 音乐/非语音检测
            if not emitted_segments and high_no_speech:
                rms = np.sqrt(np.mean(audio ** 2))
                self._is_music = rms > 0.01
                with self._lock:
                    self._current_partial = ""
                return

            self._is_music = False
            chunk_text = "".join(chunk_text_parts)
            if chunk_text:
                self._prev_text = chunk_text  # 保存给下一块做 prompt

            with self._lock:
                self._current_partial = chunk_text
                self._current_partial_audio_time = emitted_segments[-1][0] if emitted_segments else default_emit_time

            for emit_time, seg_text in emitted_segments:
                if (emit_time - self._last_emitted_audio_time) < MIN_EMIT_INTERVAL and self._is_similar_text(seg_text, self._last_emitted_text):
                    continue
                if self._is_similar_text(seg_text, self._last_emitted_text):
                    time_gap = emit_time - self._last_emitted_audio_time
                    if time_gap < SIMILAR_EMIT_INTERVAL:
                        continue
                with self._lock:
                    self._recent.append((emit_time, seg_text))
                    if len(self._recent) > 50:
                        self._recent.pop(0)
                    self._last_emitted_text = seg_text
                    self._last_emitted_audio_time = emit_time
                try:
                    self._transcript_queue.put_nowait((emit_time, seg_text))
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

    def reset(self):
        """清空当前识别状态，供切换电台时复用已加载的模型线程。"""
        self._prebuf.clear()
        self._prev_text = ""
        self._last_emitted_text = ""
        self._last_emitted_audio_time = 0.0
        self._pcm_tail = b""
        self._is_music = False
        self._transcribing = False

        while True:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break

        while True:
            try:
                self._transcript_queue.get_nowait()
            except queue.Empty:
                break

        with self._lock:
            self._recent.clear()
            self._current_partial = ""
            self._current_partial_audio_time = 0.0

    def stop(self):
        self._running = False
        self.reset()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

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
DEFAULT_STATIONS = [
    {
        "id": "nhk_r1",
        "name": "NHK ラジオ第1",
        "name_zh": "NHK 广播第1（综合·新闻）",
        "desc": "NHK総合 — ニュース・情報・天気予報",
        "url": "https://simul.drdi.st.nhk/live/3/joined/master.m3u8",
        "logo": "NHK1",
        "category": "NHK",
        "area": "全国",
    },
    {
        "id": "nhk_r2",
        "name": "NHK ラジオ第2",
        "name_zh": "NHK 广播第2（教育·外语）",
        "desc": "NHK第2 — 教育・語学・宗教番組",
        "url": "https://simul.drdi.st.nhk/live/4/joined/master.m3u8",
        "logo": "NHK2",
        "category": "NHK",
        "area": "全国",
    },
    {
        "id": "nhk_fm",
        "name": "NHK FM",
        "name_zh": "NHK FM（音乐·文化）",
        "desc": "NHK FM — 音楽・文化・エンタメ",
        "url": "https://simul.drdi.st.nhk/live/5/joined/master.m3u8",
        "logo": "NHKFM",
        "category": "NHK",
        "area": "全国",
    },
]

DEFAULT_NHK_AREAS = ["tokyo", "osaka", "fukuoka"]
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


def load_station_settings() -> tuple[list[dict], list[str], str]:
    if not os.path.exists(STATIONS_CONFIG_PATH):
        return DEFAULT_STATIONS[:], DEFAULT_NHK_AREAS[:], DEFAULT_AREA
    if yaml is None:
        raise RuntimeError("stations.yaml を使うには PyYAML が必要です。pip install -r requirements.txt を実行してください。")

    with open(STATIONS_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    stations = data.get("stations") or DEFAULT_STATIONS
    nhk_areas = data.get("nhk_areas") or DEFAULT_NHK_AREAS
    default_area = data.get("default_area") or (nhk_areas[0] if nhk_areas else DEFAULT_AREA)

    valid_stations = []
    for s in stations:
        if not isinstance(s, dict):
            continue
        if not all(s.get(k) for k in ("id", "name", "name_zh", "desc", "url")):
            continue
        station = dict(s)
        station.setdefault("logo", station["id"].upper())
        station.setdefault("category", "Custom")
        station.setdefault("area", "全国")
        valid_stations.append(station)

    valid_areas = [a for a in nhk_areas if a in AREA_MAP]
    if not valid_areas:
        valid_areas = DEFAULT_NHK_AREAS[:]
    if default_area not in AREA_MAP:
        default_area = valid_areas[0]

    return valid_stations, valid_areas, default_area


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
                    desc = f"{label} {city_name}"
                    if key == "r1hls":
                        desc += " — 地域ニュース・情報"
                    elif key == "r2hls":
                        desc += " — 教育・語学・トーク"
                    else:
                        desc += " — 音楽・文化"
                    result.append({
                        "id": f"nhk_{base_id}_{area_key}",
                        "name": f"{label}（{city_name}）",
                        "name_zh": f"NHK {zh_label}（{city_name}）",
                        "desc": desc,
                        "url": el.text.strip(),
                        "logo": f"NHK{base_id.upper()}",
                        "category": "NHK",
                        "area": city_name,
                    })
            return result
    except Exception as e:
        print(f"[警告] NHK XML取得失敗 ({e})、デフォルトURLを使用", file=sys.stderr)
    return []


def build_station_list(stations: list[dict], nhk_areas: list[str]) -> list[dict]:
    all_stations = list(stations)
    for area in nhk_areas:
        all_stations.extend(fetch_nhk_regional(area))

    seen = {}
    for s in all_stations:
        seen[s["id"]] = s

    result = list(seen.values())
    result.sort(key=lambda s: (s.get("category", ""), s.get("area", ""), s["id"]))
    return result


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

        # 切换电台时重置 STT（旧电台音频已无效）
        if self.stt:
            self.stt.reset()

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
        """ffmpeg stdout（s16le PCM）を 1 秒ごとに読み取り、Whisper + 缓存发送。"""
        # 用 time.monotonic() 记录音频流开始时间
        first_chunk_time = None
        bytes_written = 0  # 已写入缓存的总字节数（用于推算音频时间）
        try:
            with self.capture_proc.stdout as stdout:
                chunk_size = int(PCM_BYTES_PER_SECOND * CHUNK_DURATION)

                while True:
                    chunk = stdout.read(chunk_size)
                    if not chunk:
                        break
                    if first_chunk_time is None:
                        first_chunk_time = time.monotonic()
                        # 切换电台后音频时间从 0 重新计数
                        bytes_written = 0
                        # 初始化音频缓存
                        self._init_audio_cache(0.0)
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

    # ── 按分类 > 地区构建分组列表 ──
    from collections import defaultdict
    grouped: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for s in all_stations:
        cat = s.get('category', 'Other')
        area = s.get('area', '')
        grouped[cat][area].append(s)

    # 扁平化导航列表
    flat_items: list[tuple[str, dict | None]] = []
    for cat in sorted(grouped.keys()):
        flat_items.append((f'━━ {cat} ━━', None))
        for area in sorted(grouped[cat].keys()):
            area_label = area if area else '全国'
            flat_items.append((f'  ▸ {area_label}', None))
            for station in grouped[cat][area]:
                flat_items.append((station['name'], station))

    categories = sorted(grouped.keys())
    filter_modes = [
        ("全部", lambda s: True),
        ("ニュース", lambda s: s.get("id", "").startswith("nhk_r1_") and s.get("area") != "全国"),
    ] + [(c, lambda s, cat=c: s.get("category") == cat) for c in categories]
    news_filter_idx = next((i for i, (label, _) in enumerate(filter_modes) if label == "ニュース"), 0)
    filter_mode_idx = news_filter_idx
    selectable_indices = [idx for idx, (txt, st) in enumerate(flat_items) if st is not None]
    current_flat_idx = selectable_indices[0] if selectable_indices else 0

    def get_filter_predicate():
        return filter_modes[filter_mode_idx][1]

    def get_visible() -> list:
        pred = get_filter_predicate()
        return [item[1] for item in flat_items if item[1] is not None and pred(item[1])]

    def get_filtered_flat_indices() -> list[int]:
        pred = get_filter_predicate()
        return [idx for idx, (_, st) in enumerate(flat_items) if st is not None and pred(st)]

    def draw():
        nonlocal stt_anim_frame, _stt_was_loading, current_flat_idx
        nonlocal last_live_text, last_live_audio_time, last_live_wall_time
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
        current_filter_label = filter_modes[filter_mode_idx][0]
        mode_badge = "【ニュース優先】" if current_filter_label == "ニュース" else f"【{current_filter_label}】"
        title = f"📻  日本ラジオ ニュース受信ラジオ {mode_badge}"
        stdscr.addstr(0, max(0, (w - len(title)) // 2),
                      title[:max(0, w - 1)], curses.A_BOLD | curses.color_pair(3))
        engine_str = f"[{player.backend} | vol:{player.volume}%]"
        stdscr.addstr(0, w - len(engine_str) - 1, engine_str, curses.A_DIM)

        # ── カテゴリバー（分类标签）──
        try:
            x = 2
            for idx, (label, _) in enumerate(filter_modes):
                is_current = (label == current_filter_label)
                chip = f" {label} "
                attr = curses.color_pair(1) | curses.A_BOLD if is_current else (curses.color_pair(6) | curses.A_DIM)
                if x < w - 1:
                    stdscr.addstr(1, x, chip[:max(0, w - x - 1)], attr)
                x += len(chip)
                if idx < len(filter_modes) - 1 and x < w - 1:
                    sep = "|"
                    stdscr.addstr(1, x, sep[:max(0, w - x - 1)], curses.A_DIM)
                    x += len(sep)
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

        # ── 局リスト（分组显示，STT 下位预留）──
        start_y = 3
        list_end_y = h - stt_rows - 2
        max_list_rows = list_end_y - start_y
        if max_list_rows < 3:
            max_list_rows = 5

        current_flat_idx = max(0, min(current_flat_idx, len(flat_items) - 1))
        filtered_flat_indices = get_filtered_flat_indices()
        filtered_indices = set(filtered_flat_indices)
        if filtered_flat_indices and current_flat_idx not in filtered_indices:
            current_flat_idx = filtered_flat_indices[0]

        render_items: list[tuple[int, str, dict | None]] = []
        for i, (txt, station) in enumerate(flat_items):
            if station is None:
                next_station_idx = i + 1
                while next_station_idx < len(flat_items) and flat_items[next_station_idx][1] is None:
                    next_station_idx += 1
                if next_station_idx >= len(flat_items) or next_station_idx not in filtered_indices:
                    continue
            else:
                if i not in filtered_indices:
                    continue
            render_items.append((i, txt, station))

        row_heights = [1 if station is None else 3 for _, _, station in render_items]
        selected_render_idx = next((idx for idx, (flat_idx, _, station) in enumerate(render_items)
                                    if station is not None and flat_idx == current_flat_idx), 0)

        rows_before_selected = sum(row_heights[:selected_render_idx])
        selected_height = row_heights[selected_render_idx] if render_items else 0
        target_top_row = max(0, rows_before_selected - max(0, (max_list_rows - selected_height) // 2))

        scroll_render_idx = 0
        rows_consumed = 0
        while scroll_render_idx < len(render_items):
            next_rows = rows_consumed + row_heights[scroll_render_idx]
            if next_rows > target_top_row:
                break
            rows_consumed = next_rows
            scroll_render_idx += 1

        # 渲染
        y = start_y
        rendered_stations = 0
        for _, txt, station in render_items[:scroll_render_idx]:
            if station is not None:
                rendered_stations += 1

        for i in range(scroll_render_idx, len(render_items)):
            flat_idx, txt, station = render_items[i]
            if station is None:
                if y >= list_end_y - 1:
                    break
                try:
                    stdscr.addstr(y, 0, " ", curses.A_DIM)
                    stdscr.addstr(y, 2, txt, curses.A_BOLD | curses.color_pair(3))
                except curses.error:
                    pass
                y += 1
            else:
                if y + 2 >= list_end_y:
                    break
                is_sel = (flat_idx == current_flat_idx)
                is_playing = (player.is_playing and player.current_station
                              and player.current_station["id"] == station["id"])

                marker = "▶" if is_playing else " "
                item_num = rendered_stations + 1
                sel_attr = curses.color_pair(1) | curses.A_BOLD
                norm_attr = curses.color_pair(2)

                line1 = f"{marker} {item_num:2d}. {station['name']:<30}"
                line2 = f"      {station['name_zh']}"
                line3 = f"      {station['desc']}"

                try:
                    sel_a = sel_attr if is_sel else norm_attr
                    dim_a = curses.A_DIM
                    stdscr.addstr(y,   2, line1[:w-4], sel_a)
                    stdscr.addstr(y+1, 4, line2[:w-6], sel_a if is_sel else dim_a)
                    stdscr.addstr(y+2, 4, line3[:w-6],
                                  sel_a if is_sel else (dim_a | (curses.color_pair(4) if is_playing else dim_a)))
                except curses.error:
                    pass
                rendered_stations += 1
                y += 3

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
            partial_audio_time = stt.get_partial_audio_time() if stt else 0.0
            # 当前音频播放时间（用于字幕同步）
            cur_audio_time = player.get_current_audio_time() if player else 0.0
            now_wall_time = time.monotonic()
            # 体感同步：尽量贴近音频，同时减少占位状态反复出现
            DISPLAY_LEAD = 0.30   # 秒，最多提前约 300ms 显示
            MAX_SUBTITLE_LAG = 15.0  # 放宽滞后容忍，避免频繁掉到 lag dropped
            LIVE_FUTURE_TOLERANCE = 1.5  # 实时行允许更大的超前空间，减少 listening
            LIVE_HOLD_SECONDS = 2.0  # live 行短暂保留上一条字幕，减少空窗和抖动
            # 前4行=历史，最后1行=当前实时
            hist_rows = stt_rows - 1
            stt_lines = [""] * stt_rows

            fresh_recent = []
            if cur_audio_time > 0:
                fresh_recent = [
                    (at, text) for at, text in recent
                    if -DISPLAY_LEAD <= (cur_audio_time - at) <= MAX_SUBTITLE_LAG
                ]
            else:
                fresh_recent = list(recent)

            # 从历史中选择"已到时间（允许轻微提前）且未过时"的字幕
            display_items = fresh_recent[-hist_rows:]
            for j, (_, text) in enumerate(display_items):
                row_idx = hist_rows - len(display_items) + j
                if 0 <= row_idx < hist_rows:
                    stt_lines[row_idx] = text

            latest_lag = None
            visible_latest_lag = None
            if recent and cur_audio_time > 0:
                last_audio_ts, _ = recent[-1]
                latest_lag = cur_audio_time - last_audio_ts
            if display_items and cur_audio_time > 0:
                visible_latest_lag = cur_audio_time - display_items[-1][0]

            live_text = ""
            live_lag = None
            if partial:
                live_text = partial
                live_lag = (cur_audio_time - partial_audio_time) if (partial_audio_time > 0 and cur_audio_time > 0) else None
                last_live_text = partial
                last_live_audio_time = partial_audio_time
                last_live_wall_time = now_wall_time
            elif last_live_text and (now_wall_time - last_live_wall_time) <= LIVE_HOLD_SECONDS:
                live_text = last_live_text
                live_lag = (cur_audio_time - last_live_audio_time) if (last_live_audio_time > 0 and cur_audio_time > 0) else None
            elif display_items:
                live_text = display_items[-1][1]
                live_lag = (cur_audio_time - display_items[-1][0]) if cur_audio_time > 0 else None

            # 最后一行：当前正在识别（闪烁光标）/ 音乐检测
            if stt and stt.is_music():
                music_anim = ["♪", "♫", "♪♫", "♫♪"]
                sym = music_anim[(stt_anim_frame // 4) % len(music_anim)]
                if visible_latest_lag is not None and visible_latest_lag > 0.6:
                    lag_str = f" [{visible_latest_lag:.1f}s behind]"
                else:
                    lag_str = ""
                stt_lines[stt_rows - 1] = f"  {sym} 【MUSIC...】{sym}{lag_str}"
            elif live_text:
                blink = "▍" if (stt_anim_frame // 3) % 2 == 0 else " "
                if live_lag is not None and live_lag > MAX_SUBTITLE_LAG and display_items:
                    stt_lines[stt_rows - 1] = f"▸ {display_items[-1][1]}{blink}"
                elif live_lag is not None and live_lag < -LIVE_FUTURE_TOLERANCE and display_items:
                    stt_lines[stt_rows - 1] = f"▸ {display_items[-1][1]}{blink}"
                else:
                    if live_lag is not None and live_lag > 1.2:
                        lag_str = f" [{live_lag:.1f}s behind]"
                    elif live_lag is not None and live_lag < -0.8:
                        lag_str = f" [{-live_lag:.1f}s early]"
                    else:
                        lag_str = ""
                    stt_lines[stt_rows - 1] = f"▸ {live_text}{blink}{lag_str}"
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
    last_live_text = ""
    last_live_audio_time = 0.0
    last_live_wall_time = 0.0

    while True:
        # ── 动画帧递增（在 draw 之前，让动画实时生效）──
        stt_anim_frame += 1

        draw()
        try:
            key = stdscr.getch()
        except curses.error:
            key = -1

        if key != -1:
            visible = get_visible()
            filtered_flat_indices = get_filtered_flat_indices()

            if filtered_flat_indices and current_flat_idx not in filtered_flat_indices:
                current_flat_idx = filtered_flat_indices[0]

            if key in (curses.KEY_UP, ord("k")):
                if filtered_flat_indices:
                    cur_pos = filtered_flat_indices.index(current_flat_idx)
                    if cur_pos > 0:
                        current_flat_idx = filtered_flat_indices[cur_pos - 1]
            elif key in (curses.KEY_DOWN, ord("j")):
                if filtered_flat_indices:
                    cur_pos = filtered_flat_indices.index(current_flat_idx)
                    if cur_pos < len(filtered_flat_indices) - 1:
                        current_flat_idx = filtered_flat_indices[cur_pos + 1]
            elif key in (curses.KEY_ENTER, 10, 13):
                if visible and current_flat_idx in filtered_flat_indices:
                    player.play(flat_items[current_flat_idx][1])
            elif key in (ord(" "), ord("p")):
                if player.is_playing:
                    player.pause_resume()
                elif visible and current_flat_idx in filtered_flat_indices:
                    player.play(flat_items[current_flat_idx][1])
            elif key in (ord("s"), ord("S")):
                player.stop()
            elif key in (ord("+"), ord("=")):
                player.set_volume(10)
            elif key in (ord("-"), ord("_")):
                player.set_volume(-10)
            elif key == ord("c"):
                filter_mode_idx = (filter_mode_idx + 1) % len(filter_modes)
                filtered_flat_indices = get_filtered_flat_indices()
                current_flat_idx = filtered_flat_indices[0] if filtered_flat_indices else 0
            elif key == ord("C"):
                filter_mode_idx = 0
                filtered_flat_indices = get_filtered_flat_indices()
                current_flat_idx = filtered_flat_indices[0] if filtered_flat_indices else 0
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
                                print(f"\r🎤 {recent[-1][1][:80]}      ", end="", flush=True)

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
    stations_config, nhk_areas, default_area = load_station_settings()
    all_stations = build_station_list(stations_config, nhk_areas)

    print(f"登録局数: {len(all_stations)}")
    print(f"NHK地域ロード: {', '.join(AREA_MAP[a] for a in nhk_areas)}")
    print(f"初期地域: {AREA_MAP.get(default_area, default_area)}")
    print(f"設定ファイル: {STATIONS_CONFIG_PATH}")

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
