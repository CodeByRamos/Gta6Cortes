"""Detecção de picos de mensagens no chat.

Conta mensagens por segundo, compara a taxa recente (janela curta) com a taxa
"normal" do canal (mediana dos últimos minutos) e dispara um evento quando o
chat explode. Mensagens com risadas/hype ("kkkk", KEKW, POG, CLIP...) pesam mais.
"""
from __future__ import annotations

import logging
import re
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger("detector")

HYPE = re.compile(
    r"(k{4,}|(ha){3,}|(rs){3,}|kekw|lul|lmao|omegalul|icant|pog|poggers|pogchamp|clip|clipa|"
    r"monka|holy|wtf|omg|nossa|caralho|mds|meu deus|absurdo|insano|brabo|lend[aá]rio|"
    r"\?{3,}|!{3,}|😂|🤣|💀|😭|🔥|😱)",
    re.I,
)
BOTS = {"nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot", "botrix",
        "kicklet", "sery_bot", "soundalerts", "pokemoncommunitygame"}


@dataclass
class Peak:
    start: float          # quando o chat começou a explodir (epoch)
    top: float            # momento de maior intensidade
    end: float            # quando voltou ao normal
    score: float          # quantas vezes acima do normal
    msgs: int             # mensagens durante o pico
    sample: list = field(default_factory=list)  # algumas mensagens do pico


class PeakDetector:
    def __init__(self, on_peak, window=10, baseline_secs=300, threshold=2.5,
                 min_msgs=15, cooldown=45, warmup=60, max_peak_secs=40, hype_weight=0.5):
        self.on_peak = on_peak
        self.window = window
        self.baseline_secs = baseline_secs
        self.threshold = threshold
        self.min_msgs = min_msgs
        self.cooldown = cooldown
        self.warmup = warmup
        self.max_peak_secs = max_peak_secs
        self.hype_weight = hype_weight
        self.lock = threading.Lock()
        self.buckets: dict[int, float] = {}   # segundo -> peso
        self.raw: dict[int, int] = {}         # segundo -> nº de mensagens
        self.recent = deque(maxlen=400)
        self.rates = deque(maxlen=baseline_secs)  # taxa da janela, 1 por segundo
        self.t0 = time.time()
        self.active: dict | None = None
        self.last_peak_end = 0.0
        self.total = 0
        self.user_hist: dict[str, deque] = {}
        self.last_status = {}

    def add(self, ts, user, text):
        text = text or ""
        u = (user or "").lower()
        if u in BOTS or text.startswith("!"):
            return  # comandos e bots não contam
        sec = int(ts)
        w = 1.0 + (self.hype_weight if HYPE.search(text) else 0.0)
        with self.lock:
            # anti-spam: a mesma pessoa mandando muitas mensagens seguidas vale menos
            hist = self.user_hist.setdefault(u, deque(maxlen=4))
            while hist and ts - hist[0] > self.window:
                hist.popleft()
            if len(hist) >= 2:
                w *= 0.3
            hist.append(ts)
            if len(self.user_hist) > 20000:
                self.user_hist.clear()
            self.buckets[sec] = self.buckets.get(sec, 0.0) + w
            self.raw[sec] = self.raw.get(sec, 0) + 1
            self.recent.append((ts, user, text))
            self.total += 1

    def _window_sum(self, d, now_sec):
        return sum(d.get(s, 0) for s in range(now_sec - self.window + 1, now_sec + 1))

    def tick(self, now=None):
        """Chamar 1x por segundo."""
        now = now or time.time()
        sec = int(now) - 1  # último segundo completo
        with self.lock:
            weighted = self._window_sum(self.buckets, sec)
            msgs = self._window_sum(self.raw, sec)
            for s in [s for s in self.buckets if s < sec - self.baseline_secs - 60]:
                self.buckets.pop(s, None)
                self.raw.pop(s, None)
        # linha de base: mediana das janelas recentes (ignora o próprio pico)
        hist = list(self.rates)[:-self.window] if len(self.rates) > self.window else []
        base = statistics.median(hist) if hist else 0.0
        base = max(base, self.min_msgs / 4)  # evita divisão por quase zero em chats parados
        score = weighted / base if base else 0.0
        if not self.active:
            self.rates.append(weighted)
        self.last_status = {"msgs_janela": msgs, "base": round(base, 1), "score": round(score, 2)}

        warm = len(self.rates) >= self.warmup  # precisa de histórico suficiente do chat
        hot = warm and msgs >= self.min_msgs and score >= self.threshold
        if self.active is None:
            if hot and now - self.last_peak_end >= self.cooldown:
                self.active = {"start": now - self.window, "top": now - self.window / 2, "best": score,
                               "msgs": msgs, "base": base}
                log.info("🔥 pico começando (score %.1f, %d msgs/%ds)", score, msgs, self.window)
        else:
            a = self.active
            if score > a["best"]:
                a["best"], a["top"] = score, now - self.window / 2
            a["msgs"] = max(a["msgs"], msgs)
            cooled = score < self.threshold * 0.6 or msgs < self.min_msgs / 2
            too_long = now - a["start"] > self.max_peak_secs
            if cooled or too_long:
                with self.lock:
                    sample = [t for ts, u, t in self.recent if a["start"] <= ts <= now][-15:]
                peak = Peak(a["start"], a["top"], now, round(a["best"], 2), a["msgs"], sample)
                self.active = None
                self.last_peak_end = now
                log.info("✅ pico detectado: score %.1f, top às %s", peak.score, time.strftime("%H:%M:%S", time.localtime(peak.top)))
                self.on_peak(peak)


class AudioPeakWatcher(threading.Thread):
    """Plano B sem chat: detecta gritos/reações pelo volume do áudio gravado.

    Lê cada segmento fechado da gravação, mede o volume a cada 0,5s e dispara
    quando o volume fica bem acima do normal do canal (mediana dos últimos minutos).
    `gate()` decide se o áudio pode disparar (ex.: só quando o chat está fora do ar).
    """

    def __init__(self, recorder, on_peak, gate=lambda: True, db_over=10.0, min_frames=3,
                 cooldown=60, baseline_frames=600):
        super().__init__(daemon=True, name="audio-picos")
        self.rec = recorder
        self.on_peak = on_peak
        self.gate = gate
        self.db_over = db_over
        self.min_frames = min_frames
        self.cooldown = cooldown
        self.hist = deque(maxlen=baseline_frames)
        self.stop_event = threading.Event()
        self.last_peak = 0.0
        self.done = set()

    def stop(self):
        self.stop_event.set()

    def _frames(self, path):
        from .clipper import load_audio  # import tardio (numpy/ffmpeg)
        import numpy as np

        a = load_audio(path)
        hop = 8000  # 0,5s a 16 kHz
        n = len(a) // hop
        if n == 0:
            return []
        rms = np.sqrt(np.mean(a[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-10)
        return list(20 * np.log10(rms))

    def run(self):
        while not self.stop_event.wait(3):
            for seg in self.rec.segments()[:-1]:
                if seg.path.name in self.done:
                    continue
                self.done.add(seg.path.name)
                try:
                    frames = self._frames(seg.path)
                except Exception:
                    continue
                run = 0
                for i, db in enumerate(frames):
                    t = seg.start + i * 0.5
                    base = statistics.median(self.hist) if len(self.hist) >= 120 else None
                    self.hist.append(db)
                    if base is None or db < -35:
                        run = 0
                        continue
                    run = run + 1 if db >= base + self.db_over else 0
                    if run >= self.min_frames and t - self.last_peak >= self.cooldown and self.gate():
                        self.last_peak = t
                        score = round(float(1 + (db - base) / 5), 2)
                        log.info("🔊 pico de áudio (+%.0f dB) às %s", db - base, time.strftime("%H:%M:%S", time.localtime(t)))
                        self.on_peak(Peak(t - 4, t, t + 2, score, 0, ["(pico de áudio)"]))
                        run = 0
