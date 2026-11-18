"""Gravação contínua da live com streamlink -> ffmpeg em segmentos.

Cada segmento recebe no nome o horário real (relógio de parede) em que começou,
assim conseguimos casar o horário das mensagens do chat com o vídeo mesmo que
a live caia e volte (a gravação reinicia sozinha).
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("gravador")

SEG_PREFIX = "seg_"
SEG_FMT = "%Y%m%d-%H%M%S"


@dataclass
class Segment:
    path: Path
    start: float  # epoch (s)


class Recorder(threading.Thread):
    def __init__(self, url, out_dir: Path, quality="best", segment_time=10,
                 keep_minutes=30, keep_full=False, wait_live=True, streamlink_args=""):
        super().__init__(daemon=True, name="gravador")
        self.url = url
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.quality = quality
        self.segment_time = segment_time
        self.keep_minutes = keep_minutes
        self.keep_full = keep_full
        self.wait_live = wait_live
        self.streamlink_args = streamlink_args
        self.stop_event = threading.Event()
        self.recording = threading.Event()
        self.ended = threading.Event()
        self._procs = []

    # ------------------------------------------------------------------ util
    def segments(self) -> list[Segment]:
        segs = []
        for p in self.dir.glob(f"{SEG_PREFIX}*.ts"):
            try:
                t = datetime.strptime(p.stem[len(SEG_PREFIX):len(SEG_PREFIX) + 15], SEG_FMT).timestamp()
            except ValueError:
                continue
            segs.append(Segment(p, t))
        segs.sort(key=lambda s: (s.start, s.path.name))
        return segs

    def recorded_until(self) -> float:
        """Horário (epoch) até onde já existe vídeo FECHADO em disco."""
        segs = self.segments()
        if len(segs) < 2:
            return 0.0
        # o último segmento ainda está sendo escrito; o penúltimo está completo
        return segs[-1].start

    # --------------------------------------------------------------- gravação
    def _record_once(self):
        sl = ["streamlink", "--stdout", "--loglevel", "warning",
              "--stream-segment-threads", "2", "--hls-live-edge", "3"]
        if self.wait_live:
            # tenta a cada 20s até a live começar -> grava desde o 1º segundo
            sl += ["--retry-streams", "20", "--retry-max", "0"]
        sl += shlex.split(self.streamlink_args)
        sl += [self.url, self.quality]
        ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
              "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
              "-f", "segment", "-segment_time", str(self.segment_time),
              "-segment_format", "mpegts", "-reset_timestamps", "1", "-strftime", "1",
              str(self.dir / f"{SEG_PREFIX}{SEG_FMT}.ts")]
        log.info("aguardando/gravando %s (%s)", self.url, self.quality)
        p1 = subprocess.Popen(sl, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        p2 = subprocess.Popen(ff, stdin=p1.stdout, stdout=subprocess.DEVNULL)
        p1.stdout.close()
        self._procs = [p1, p2]
        while p2.poll() is None and not self.stop_event.is_set():
            if not self.recording.is_set() and self.segments():
                log.info("live no ar — gravação iniciada")
                self.recording.set()
            self._cleanup()
            time.sleep(1)
        self._shutdown()

    def _shutdown(self):
        # para o streamlink primeiro: o ffmpeg recebe fim de arquivo e fecha o último segmento direito
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()

    def run(self):
        misses = 0
        while not self.stop_event.is_set():
            before = len(self.segments())
            self._record_once()
            if self.stop_event.is_set():
                break
            got_data = len(self.segments()) > before
            misses = 0 if got_data else misses + 1
            if not self.wait_live and misses >= 3:
                log.warning("live terminou / indisponível")
                break
            log.info("stream caiu, tentando reconectar...")
            time.sleep(5)
        self.ended.set()

    def _cleanup(self):
        if self.keep_full or self.keep_minutes <= 0:
            return
        limit = time.time() - self.keep_minutes * 60
        for s in self.segments()[:-2]:
            if s.start < limit:
                try:
                    s.path.unlink()
                except OSError:
                    pass

    def stop(self):
        self.stop_event.set()

    # ------------------------------------------------------- vídeo completo
    def merge_full(self, out_path: Path):
        segs = self.segments()
        if not segs:
            return None
        lst = self.dir / "full_list.txt"
        lst.write_text("".join(f"file '{s.path.resolve()}'\n" for s in segs))
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out_path)], check=False)
        return out_path if out_path.exists() else None
