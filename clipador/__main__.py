"""Clipador automático de lives (Twitch, Kick, YouTube...).

Uso rápido:
    python -m clipador ao-vivo https://twitch.tv/canal1 https://kick.com/canal2 --perfil gta6
    python -m clipador quadro https://twitch.tv/canal --layout camera --camera 0,60,25,40
    python -m clipador cortar video.mp4 --inicio 01:02:03 --fim 01:03:10
    python -m clipador chat https://twitch.tv/canal
"""
from __future__ import annotations

import argparse
import itertools
import logging
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .chat import detect_platform, make_chat_reader
from .clipper import FORMATOS, LAYOUTS, Clipper, Transcriber, render_preview
from .detector import AudioPeakWatcher, PeakDetector
from .recorder import Recorder, Segment

log = logging.getLogger("clipador")

PERFIS = {
    "gta6": {
        "titulo": "{CANAL} NO GTA 6",
        "hashtags": "#gta6 #gtavi #gta #gta6gameplay #rockstargames #games #fyp #viral",
        "prompt": "Live de GTA 6 (GTA VI) da Rockstar Games. Vice City, Leonida, Lucia, Jason, "
                  "Port Gellhorn, Grassrivers, Ambrosia, Leonida Keys, Mount Kalaga.",
    },
    "padrao": {"titulo": "", "hashtags": "#fyp #viral #live", "prompt": ""},
}


def parse_time(s: str) -> float:
    t = 0.0
    for p in s.split(":"):
        t = t * 60 + float(p)
    return t


def slug(s):
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", s).strip("_")[:40] or "live"


def credit_for(platform, channel):
    return {"twitch": f"twitch.tv/{channel}", "kick": f"kick.com/{channel}",
            "youtube": f"youtube.com/{channel}"}.get(platform, channel)


def parse_camera(s):
    if not s:
        return None
    vals = [float(v) for v in s.split(",")]
    if len(vals) != 4:
        raise SystemExit("--camera precisa de 4 números: x,y,largura,altura (em % do vídeo)")
    return vals


def _resolve_profile(a):
    perfil = PERFIS[a.perfil]
    a.titulo = perfil["titulo"] if a.titulo is None else a.titulo
    a.hashtags = perfil["hashtags"] if a.hashtags is None else a.hashtags
    a.vocabulario = perfil["prompt"] if a.vocabulario is None else a.vocabulario


def make_clipper(a, out_dir, transcriber, channel=""):
    titulo = (a.titulo or "").replace("{CANAL}", channel.upper()).replace("{canal}", channel)
    return Clipper(out_dir, transcriber, formatos=a.formatos.split(","), layout=a.layout, zoom=a.zoom,
                   camera=parse_camera(a.camera), titulo=titulo, credito=not a.sem_credito, gancho=a.gancho,
                   hashtags=a.hashtags, fps=a.fps)


# --------------------------------------------------------------------- ao vivo
class Canal:
    def __init__(self, url, a, transcriber, jobs):
        self.url = url
        self.platform, self.channel = detect_platform(url)
        self.base = Path(a.saida) / f"{self.platform}_{slug(self.channel)}"
        self.credit = credit_for(self.platform, self.channel)
        self.rec = Recorder(url, self.base / "gravacao", quality=a.qualidade, segment_time=a.segmento,
                            keep_minutes=a.manter_minutos, keep_full=a.gravar_tudo,
                            wait_live=not a.nao_esperar, streamlink_args=a.streamlink_args)
        self.clipper = make_clipper(a, self.base / "clipes", transcriber, self.channel)
        self.det = PeakDetector(self._on_peak, window=a.janela, threshold=a.sensibilidade, min_msgs=a.min_msgs,
                                cooldown=a.intervalo, warmup=a.aquecimento)
        self.chat = make_chat_reader(url, self.det.add, kick_chatroom_id=a.kick_chatroom_id)
        self.jobs = jobs
        self.clips = 0
        self.keep_minutes = a.manter_minutos
        self.audio = None
        if a.sinal in ("audio", "ambos") or (a.sinal == "auto"):
            self.audio = AudioPeakWatcher(self.rec, self._on_peak, gate=self._audio_allowed,
                                          db_over=a.audio_db, cooldown=a.intervalo)
        self.sinal = a.sinal

    def _audio_allowed(self):
        if self.sinal in ("audio", "ambos"):
            return True
        # auto: o áudio só clipa se o chat estiver sem funcionar (ex.: Kick bloqueada)
        last = self.det.recent[-1][0] if self.det.recent else 0
        return not (self.chat and self.chat.connected and time.time() - last < 120)

    def _on_peak(self, peak):
        log.info("[%s] pico score %.1f entrou na fila", self.channel, peak.score)
        self.jobs.put((-peak.score, next(_seq), self, peak))

    def start(self):
        self.rec.start()
        if self.chat and self.sinal != "audio":
            self.chat.start()
        elif not self.chat:
            log.warning("[%s] plataforma sem leitor de chat — usando só o áudio", self.channel)
        if self.audio:
            self.audio.start()

    def stop(self):
        if self.chat:
            self.chat.stop()
        if self.audio:
            self.audio.stop()
        self.rec.stop()

    def status(self):
        st = self.det.last_status
        return (f"[{self.channel}] {'GRAVANDO' if self.rec.recording.is_set() else 'esperando live'} | "
                f"chat {'on' if self.chat and self.chat.connected else 'off'} | {self.det.total} msgs | "
                f"janela={st.get('msgs_janela', 0)} base={st.get('base', 0)} score={st.get('score', 0)} | "
                f"clipes={self.clips}")


_seq = itertools.count()


def clip_worker(jobs, stop_after: threading.Event):
    while True:
        try:
            _, _, canal, peak = jobs.get(timeout=1)
        except queue.Empty:
            if stop_after.is_set():
                return
            continue
        if time.time() - peak.top > (canal.keep_minutes - 3) * 60:
            log.warning("[%s] pico das %s ficou velho demais na fila, pulando", canal.channel,
                        time.strftime("%H:%M:%S", time.localtime(peak.top)))
            continue
        _, we = canal.clipper.window_for(peak.start, peak.top)
        deadline = time.time() + 240
        while canal.rec.recorded_until() < we + 1 and time.time() < deadline and not canal.rec.ended.is_set():
            time.sleep(2)
        name = time.strftime("%Y%m%d_%H%M%S", time.localtime(peak.top)) + f"_score{peak.score:.1f}"
        try:
            outs = canal.clipper.make_clips(
                canal.rec.segments(), peak.start, peak.top, name, credit_text=canal.credit,
                subtitles=canal.clipper.tr is not None,
                meta={"canal": canal.channel, "plataforma": canal.platform, "score": peak.score,
                      "msgs_no_pico": peak.msgs, "horario": time.strftime("%H:%M:%S", time.localtime(peak.top)),
                      "chat": peak.sample})
            canal.clips += 1
            log.info("🎬 [%s] clipe #%d pronto: %s", canal.channel, canal.clips, ", ".join(o.name for o in outs))
        except Exception as e:
            log.error("[%s] falha ao gerar clipe: %s", canal.channel, e)


def cmd_live(a):
    _resolve_profile(a)
    tr = None if a.sem_legenda else Transcriber(a.modelo, a.idioma, prompt=a.vocabulario)
    jobs: queue.PriorityQueue = queue.PriorityQueue()
    canais = [Canal(u, a, tr, jobs) for u in a.urls]

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    finish = threading.Event()
    workers = [threading.Thread(target=clip_worker, args=(jobs, finish), daemon=True) for _ in range(a.workers)]
    for w in workers:
        w.start()
    for c in canais:
        c.start()
        log.info("ligado em %s → %s", c.url, c.base / "clipes")
    if tr:  # carrega o Whisper já, para o primeiro clipe não atrasar
        threading.Thread(target=tr._load, daemon=True).start()

    t0, last_status = time.time(), 0.0
    while not stop.is_set():
        for c in canais:
            if c.rec.recording.is_set():
                c.det.tick()
        if time.time() - last_status >= a.status:
            last_status = time.time()
            for c in canais:
                log.info("status: %s", c.status())
            free = shutil.disk_usage(Path(a.saida).resolve()).free / 1e9
            if free < 3:
                log.warning("pouco espaço em disco: %.1f GB livres", free)
        if all(c.rec.ended.is_set() for c in canais):
            log.info("todas as lives terminaram")
            break
        if a.duracao and time.time() - t0 >= a.duracao:
            log.info("tempo de teste (%ss) atingido", a.duracao)
            break
        stop.wait(1)

    log.info("encerrando... (terminando %d clipe(s) na fila)", jobs.qsize())
    for c in canais:
        if c.chat:
            c.chat.stop()
        if c.audio:
            c.audio.stop()
    finish.set()
    for w in workers:
        w.join(timeout=900)
    for c in canais:
        c.rec.stop()
    for c in canais:
        c.rec.join(timeout=15)
        if a.gravar_tudo:
            full = c.rec.merge_full(c.base / f"live_completa_{time.strftime('%Y%m%d_%H%M%S')}.mp4")
            if full:
                log.info("live completa salva em %s", full)
    return 0


# ---------------------------------------------------------------------- quadro
def cmd_frame(a):
    """Captura um quadro da live e gera a prévia do layout (para acertar --camera)."""
    _resolve_profile(a)
    platform, channel = detect_platform(a.url)
    out = Path(a.saida) / "previas"
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        ts = Path(td) / "amostra.ts"
        subprocess.run(["timeout", "40", "streamlink", "--loglevel", "error", "--hls-duration", "6", "-o", str(ts),
                        a.url, a.qualidade], check=False)
        if not ts.exists() or ts.stat().st_size == 0:
            log.error("não consegui capturar a live (está no ar?)")
            return 1
        frame = out / f"quadro_{slug(channel)}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "3", "-i", str(ts), "-frames:v", "1",
                        "-vf", "scale=1920:1080", str(frame)], check=True)
    titulo = (a.titulo or "").replace("{CANAL}", channel.upper()).replace("{canal}", channel)
    prev = render_preview(frame, out / f"previa_{slug(channel)}_{a.layout}.jpg", a.layout, a.zoom,
                          parse_camera(a.camera), titulo, credit_for(platform, channel))
    print(f"quadro: {frame}\nprévia: {prev}")
    return 0


# ---------------------------------------------------------------------- cortar
def cmd_cut(a):
    _resolve_profile(a)
    src = Path(a.arquivo)
    tr = None if a.sem_legenda else Transcriber(a.modelo, a.idioma, prompt=a.vocabulario)
    clipper = make_clipper(a, Path(a.saida) / "cortes", tr, a.canal or "")
    start, end = parse_time(a.inicio), parse_time(a.fim)
    name = f"{slug(src.stem)}_{int(start)}-{int(end)}"
    outs = clipper.make_clips([Segment(src, 0.0)], start, end, name, meta={"origem": str(src)},
                              subtitles=tr is not None, credit_text=a.canal or "", fixed=(start, end))
    for o in outs:
        print(o)
    return 0


# ------------------------------------------------------------------------ chat
def cmd_chat(a):
    det = PeakDetector(lambda p: print(f"  >>> PICO score={p.score} msgs={p.msgs} amostra={p.sample[-5:]}"),
                       window=a.janela, threshold=a.sensibilidade, min_msgs=a.min_msgs, warmup=a.aquecimento)
    chat = make_chat_reader(a.url, det.add, kick_chatroom_id=a.kick_chatroom_id)
    chat.start()
    t0 = time.time()
    try:
        while not a.duracao or time.time() - t0 < a.duracao:
            det.tick()
            if int(time.time() - t0) % 5 == 0:
                print(f"{time.strftime('%H:%M:%S')} total={det.total} {det.last_status}", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    chat.stop()
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="clipador", description="Robô clipador de lives")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def detector_opts(sp):
        sp.add_argument("--sensibilidade", type=float, default=2.5,
                        help="quantas vezes acima do normal o chat precisa ficar (menor = mais clipes)")
        sp.add_argument("--min-msgs", type=int, default=15, help="mínimo de mensagens na janela p/ ser pico")
        sp.add_argument("--janela", type=int, default=10, help="janela de contagem (s)")
        sp.add_argument("--aquecimento", type=int, default=90, help="segundos medindo o chat antes de clipar")
        sp.add_argument("--kick-chatroom-id", type=int, default=None)
        sp.add_argument("--duracao", type=int, default=0, help="parar depois de N segundos (0 = infinito)")

    def video_opts(sp):
        sp.add_argument("--perfil", default="padrao", choices=sorted(PERFIS), help="gta6 = título/hashtags/vocabulário")
        sp.add_argument("--formatos", default="tiktok", help=f"um ou mais de: {','.join(FORMATOS)} (ex: tiktok,shorts)")
        sp.add_argument("--layout", default="desfoque", choices=LAYOUTS)
        sp.add_argument("--camera", default=None, help="layout camera: x,y,largura,altura da webcam em %% do vídeo")
        sp.add_argument("--zoom", type=float, default=1.25, help="zoom do jogo no layout desfoque (1.0-1.6)")
        sp.add_argument("--titulo", default=None, help="texto fixo no topo; {CANAL} vira o nome do canal")
        sp.add_argument("--hashtags", default=None)
        sp.add_argument("--sem-credito", action="store_true", help="não escreve o @ do canal no vídeo")
        sp.add_argument("--gancho", action="store_true",
                        help="abre o vídeo com 3s do momento principal (cuidado: Shorts com violência nos 7s iniciais)")
        sp.add_argument("--modelo", default="large-v3-turbo", help="whisper: small, medium, large-v3-turbo...")
        sp.add_argument("--idioma", default="pt", help="pt, en, es... ou auto")
        sp.add_argument("--vocabulario", default=None, help="nomes próprios p/ o Whisper acertar")
        sp.add_argument("--sem-legenda", action="store_true")
        sp.add_argument("--fps", type=int, default=30, choices=[30, 60], help="30 = processa 2x mais rápido")
        sp.add_argument("--saida", default="saida")

    lv = sub.add_parser("ao-vivo", help="grava uma ou mais lives e clipa os picos do chat automaticamente")
    lv.add_argument("urls", nargs="+")
    detector_opts(lv)
    video_opts(lv)
    lv.add_argument("--qualidade", default="1080p60,1080p,936p60,720p60,720p,best")
    lv.add_argument("--intervalo", type=float, default=60, help="espera mínima entre dois clipes do mesmo canal (s)")
    lv.add_argument("--segmento", type=int, default=10)
    lv.add_argument("--manter-minutos", type=int, default=45, help="apaga gravação mais velha que isso")
    lv.add_argument("--gravar-tudo", action="store_true", help="guarda a live inteira e junta num mp4 no final")
    lv.add_argument("--nao-esperar", action="store_true", help="não fica esperando a live começar/voltar")
    lv.add_argument("--sinal", default="auto", choices=["auto", "chat", "audio", "ambos"],
                    help="o que detecta os picos; auto = chat, e áudio se o chat cair")
    lv.add_argument("--audio-db", type=float, default=10, help="quantos dB acima do normal é um grito/reação")
    lv.add_argument("--workers", type=int, default=1, help="clipes processados ao mesmo tempo")
    lv.add_argument("--status", type=int, default=60, help="intervalo do log de status (s)")
    lv.add_argument("--streamlink-args", default="", help="args extras p/ o streamlink")
    lv.set_defaults(func=cmd_live)

    qd = sub.add_parser("quadro", help="tira um print da live e mostra a prévia do layout")
    qd.add_argument("url")
    qd.add_argument("--qualidade", default="1080p60,1080p,720p60,720p,best")
    video_opts(qd)
    qd.set_defaults(func=cmd_frame)

    ct = sub.add_parser("cortar", help="corta e legenda um trecho exato de um vídeo já gravado")
    ct.add_argument("arquivo")
    ct.add_argument("--inicio", required=True, help="hh:mm:ss")
    ct.add_argument("--fim", required=True, help="hh:mm:ss")
    ct.add_argument("--canal", default="", help="crédito escrito no vídeo (ex: twitch.tv/canal)")
    video_opts(ct)
    ct.set_defaults(func=cmd_cut)

    ch = sub.add_parser("chat", help="só mede o chat e mostra os picos (calibrar sensibilidade)")
    ch.add_argument("url")
    detector_opts(ch)
    ch.set_defaults(func=cmd_chat)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    for noisy in ("websocket", "httpx", "httpx2", "faster_whisper", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return a.func(a) or 0


if __name__ == "__main__":
    sys.exit(main())
