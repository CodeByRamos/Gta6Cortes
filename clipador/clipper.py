"""Corte inteligente + Whisper + layout vertical + legenda queimada.

Fluxo de cada clipe:
 1. pega uma janela larga em volta do pico do chat;
 2. transcreve essa janela inteira com o Whisper (uma vez só);
 3. acha o "momento" (pico de volume do áudio — o streamer gritando) perto do pico do chat;
 4. para cada formato (tiktok, shorts...) escolhe início/fim em PAUSAS da fala,
    respeitando a duração mínima/máxima do formato;
 5. monta o vídeo vertical (layout), legenda estilo shorts, título, crédito e
    normaliza o volume (-14 LUFS, padrão das redes).
"""
from __future__ import annotations

import csv
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("cortador")

FONT = "DejaVu Sans"


# ------------------------------------------------------------------ formatos
@dataclass(frozen=True)
class Formato:
    nome: str
    min: float      # duração mínima (s)
    max: float      # duração máxima (s)
    antes: float    # contexto desejado antes do momento
    depois: float   # reação desejada depois do momento
    desc: str


FORMATOS = {
    # TikTok Creator Rewards só paga vídeos com MAIS de 1 minuto. 63s dá margem.
    # O mesmo arquivo serve pro Reels (até 3 min é recomendado para não seguidores).
    "tiktok": Formato("tiktok", 63, 90, 45, 12, "TikTok + Reels (monetizável, >60s)"),
    # Shorts com mais de 1 min e conteúdo reivindicado (Content ID) fica bloqueado: ficamos < 60s.
    "shorts": Formato("shorts", 25, 58, 25, 8, "YouTube Shorts (<60s)"),
    "curto": Formato("curto", 15, 35, 14, 6, "clipe curto / teaser"),
}

LAYOUTS = ("desfoque", "camera", "cheio", "horizontal")


def _run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou: {r.stderr[-800:]}")
    return r


def _ts(t):
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _srt_ts(t):
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_text(s: str) -> str:
    s = re.sub(r"[\U00010000-\U0010FFFF]", "", s)  # emojis não renderizam no libass
    return s.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def load_audio(media_path) -> np.ndarray:
    """Áudio 16 kHz mono float32 via ffmpeg (sem depender do PyAV)."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(media_path),
                        "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg (áudio) falhou: {r.stderr[-400:]!r}")
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


# ---------------------------------------------------------------- transcrição
class Transcriber:
    """Carrega o modelo Whisper (faster-whisper) uma vez só."""

    def __init__(self, model="large-v3-turbo", language="pt", device="auto", compute_type="int8", prompt=None):
        self.model_name = model
        self.language = None if language in ("auto", "", None) else language
        self.device = device
        self.compute_type = compute_type
        self.prompt = prompt or None
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("carregando Whisper '%s'...", self.model_name)
            self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        return self._model

    def words(self, audio: np.ndarray):
        with self._lock:
            model = self._load()
            segments, info = model.transcribe(audio, language=self.language, word_timestamps=True,
                                              vad_filter=True, beam_size=5, initial_prompt=self.prompt,
                                              condition_on_previous_text=False)
            words = []
            for seg in segments:
                for w in seg.words or []:
                    if w.word.strip():
                        words.append({"start": float(w.start), "end": float(w.end), "word": w.word.strip()})
            return words, info.language


# ------------------------------------------------------- escolha dos cortes
def loudest_moment(audio: np.ndarray, lo: float, hi: float, sr=16000) -> float | None:
    """Instante (s) de maior volume entre lo e hi — normalmente o grito/reação."""
    a, b = max(0, int(lo * sr)), min(len(audio), int(hi * sr))
    if b - a < sr:
        return None
    hop = sr // 4
    seg = audio[a:b]
    n = len(seg) // hop
    rms = np.sqrt(np.mean(seg[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    if len(rms) >= 4:
        rms = np.convolve(rms, np.ones(4) / 4, mode="same")
    return max(lo, 0) + float(np.argmax(rms)) * hop / sr


def _cut_points(words):
    """Candidatos a início/fim de corte: pausas entre palavras."""
    starts, ends = [], []
    for i, w in enumerate(words):
        prev = words[i - 1] if i else None
        gap = w["start"] - prev["end"] if prev else 2.0
        strong = 0.6 if prev and re.search(r"[.!?…]$", prev["word"]) else 0.0
        if gap >= 0.18 or strong:
            starts.append((w["start"] - min(0.2, gap / 2), min(gap, 2.0) + strong))
            if prev:
                ends.append((prev["end"] + min(0.3, gap / 2), min(gap, 2.0) + strong))
    if words:
        ends.append((words[-1]["end"] + 0.3, 1.5))
    return starts, ends


def _snap(cands, target, lo, hi):
    best, best_score = None, -1e9
    for t, strength in cands:
        if target + lo <= t <= target + hi:
            sc = strength - abs(t - target) * 0.12
            if sc > best_score:
                best, best_score = t, sc
    return target if best is None else best


def choose_cut(fmt: Formato, words, moment: float, chat_top: float, total: float, teaser_len=0.0):
    """Devolve (inicio, fim) em segundos dentro da janela larga."""
    mn, mx = fmt.min - teaser_len, fmt.max - teaser_len
    starts, ends = _cut_points(words)
    end_t = min(max(moment + fmt.depois, chat_top + 2), total)
    start_t = moment - fmt.antes
    if end_t - start_t < mn:
        start_t = end_t - mn - 1.0
    if end_t - start_t > mx:
        start_t = end_t - mx + 1.0
    ce = min(_snap(ends, end_t, -2, 6), total)
    cs = max(_snap(starts, start_t, -6, 3), 0.0)
    if ce - cs < mn:  # garante o mínimo (ex.: >60s do TikTok) puxando o início pra trás
        cs = max(_snap(starts, ce - mn - 0.8, -6, 0), 0.0)
        if ce - cs < mn:
            cs = max(ce - mn - 0.3, 0.0)
        if ce - cs < mn:  # começo da gravação: estica o fim
            ce = min(cs + mn + 0.3, total)
    if ce - cs > mx:
        cs = ce - mx
    return cs, ce


# ------------------------------------------------------------------ legendas
def chunk_words(words, max_words=4, max_chars=18, max_secs=1.8):
    chunks, cur = [], []
    for w in words:
        chars = sum(len(x["word"]) + 1 for x in cur) + len(w["word"])
        if cur and (len(cur) >= max_words or chars > max_chars or w["end"] - cur[0]["start"] > max_secs
                    or w["start"] - cur[-1]["end"] > 0.6):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def write_srt(chunks, path: Path):
    lines = []
    for i, c in enumerate(chunks, 1):
        lines += [str(i), f"{_srt_ts(c[0]['start'])} --> {_srt_ts(c[-1]['end'])}",
                  " ".join(w["word"] for w in c), ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _geometry(layout, zoom):
    """(resx, resy, tamanho legenda, margem inferior da legenda, margem superior do título)."""
    if layout == "horizontal":
        return 1920, 1080, 66, 70, 40
    if layout == "camera":
        return 1080, 1920, 74, 1920 - 790, 90  # legenda na divisa câmera/jogo
    if layout == "cheio":
        return 1080, 1920, 78, 480, 230
    video_h = 1080 * zoom * 9 / 16
    top = (1920 - video_h) / 2
    return 1080, 1920, 78, int(max(420, top - 130)), int(max(200, top - 260))


def write_ass(chunks, path: Path, layout="desfoque", zoom=1.25, titulo="", credito="", dur=0.0):
    resx, resy, size, margin_v, title_v = _geometry(layout, zoom)
    if layout == "camera":
        titulo = ""  # no layout câmera o título cobriria o rosto e as abas do TikTok
        title_v = 170
    side = 60 if layout == "horizontal" else 110  # botões laterais do TikTok/Reels
    title_lines = 0
    if titulo:  # estimativa de quebra de linha do título (fonte em negrito, caixa alta)
        per_line = max(8, int((resx - 2 * side) / (size * 0.85 * 0.68)))
        title_lines = -(-len(titulo) // per_line)
    credit_v = title_v + int(size * 0.85 * 1.25 * title_lines) + (int(size * 0.3) if titulo else 0)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {resx}
PlayResY: {resy}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Clip,{FONT},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,1,0,1,5,2,2,{side},{side},{margin_v},1
Style: Titulo,{FONT},{int(size * 0.85)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,6,3,8,{side},{side},{title_v},1
Style: Credito,{FONT},{int(size * 0.5)},&H00E0E0E0,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,1,8,{side},{side},{credit_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if titulo:
        ev.append(f"Dialogue: 1,{_ts(0)},{_ts(dur)},Titulo,,0,0,0,,{_ass_text(titulo).upper()}")
    if credito:
        ev.append(f"Dialogue: 1,{_ts(0)},{_ts(dur)},Credito,,0,0,0,,{_ass_text(credito)}")
    for k, c in enumerate(chunks):
        toks = [_ass_text(w["word"]).upper() for w in c]
        next_start = chunks[k + 1][0]["start"] if k + 1 < len(chunks) else float("inf")
        for i, w in enumerate(c):
            start = w["start"]
            end = c[i + 1]["start"] if i + 1 < len(c) else min(c[-1]["end"] + 0.15, next_start)
            end = min(end, next_start)
            if end <= start:
                continue
            text = " ".join(
                ("{\\c&H00E5FF&}" + t + "{\\c&HFFFFFF&}") if j == i else t for j, t in enumerate(toks)
            )
            ev.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Clip,,0,0,0,,{text}")
    path.write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


# ------------------------------------------------------------------- layouts
def layout_filter(layout, zoom=1.25, camera=None, src="vin"):
    """Cadeia de filtros que transforma [src] em [vl] no tamanho final."""
    if layout == "horizontal":
        return f"[{src}]scale=-2:1080,setsar=1[vl]"
    if layout == "cheio":
        return f"[{src}]crop=ih*9/16:ih,scale=1080:1920,setsar=1[vl]"
    if layout == "camera":
        if not camera:
            raise ValueError("layout 'camera' precisa de --camera x,y,largura,altura (em % do vídeo)")
        x, y, w, h = [v / 100 for v in camera]
        return (f"[{src}]split=2[c][g];"
                f"[c]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale=1080:700:force_original_aspect_ratio=increase,"
                f"crop=1080:700[cam];"
                f"[g]crop=ih*1080/1220:ih,scale=1080:1220[game];"
                f"[cam][game]vstack,setsar=1[vl]")
    # desfoque: vídeo inteiro (com zoom leve) sobre fundo desfocado
    return (f"[{src}]split=2[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,"
            f"scale=1080:1920,eq=brightness=-0.08[bg];"
            f"[b]crop=iw/{zoom}:ih,scale=1080:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[vl]")


# -------------------------------------------------------------------- cortador
class Clipper:
    def __init__(self, out_dir: Path, transcriber: Transcriber | None, formatos=("tiktok",),
                 layout="desfoque", zoom=1.25, camera=None, titulo="", credito=True, gancho=False,
                 hashtags="", crf=20, preset="veryfast", fps=30):
        self.out = Path(out_dir).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.tr = transcriber
        self.formatos = [FORMATOS[f] if isinstance(f, str) else f for f in formatos]
        self.layout = layout
        self.zoom = zoom
        self.camera = camera
        self.titulo = titulo
        self.credito = credito
        self.gancho = gancho
        self.hashtags = hashtags
        self.crf = crf
        self.preset = preset
        self.fps = int(fps)

    def window_for(self, chat_start, chat_top):
        """Janela larga necessária em volta do pico."""
        mx = max(f.max for f in self.formatos)
        dp = max(f.depois for f in self.formatos)
        return chat_start - mx - 15, chat_top + dp + 12

    @staticmethod
    def _concat_list(segments, start, end, work: Path):
        chosen = []
        for i, s in enumerate(segments):
            nxt = segments[i + 1].start if i + 1 < len(segments) else float("inf")
            if s.start <= end and nxt > start:
                chosen.append(s)
        if not chosen:
            raise RuntimeError("nenhum segmento gravado cobre esse intervalo")
        lst = work / "lista.txt"
        lst.write_text("".join(f"file '{s.path.resolve()}'\n" for s in chosen))
        return lst, chosen[0].start

    def make_clips(self, segments, chat_start, chat_top, name, meta=None, subtitles=True, credit_text="",
                   fixed=None):
        """Gera um arquivo por formato. Tempos em epoch (ou em s do arquivo, no corte manual).
        fixed=(inicio, fim) faz um corte exato, sem escolha automática."""
        work = self.out / f".tmp_{name}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            return self._make(segments, chat_start, chat_top, name, meta or {}, subtitles, credit_text, work,
                              fixed)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _make(self, segments, chat_start, chat_top, name, meta, subtitles, credit_text, work, fixed=None):
        t0 = time.time()
        ws, we = (fixed[0] - 1, fixed[1] + 1) if fixed else self.window_for(chat_start, chat_top)
        lst, first = self._concat_list(segments, ws, we, work)
        # 1) janela larga = segmentos inteiros copiados (sem reencode); o corte preciso é feito depois
        wide = work / "janela.mkv"
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
              "-i", str(lst), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", str(wide)])
        ws = first
        total = float(_run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                            str(wide)]).stdout.strip() or (we - ws))
        audio = load_audio(wide)
        rel_start, rel_top = chat_start - ws, chat_top - ws

        # 2) momento = pico de volume perto do começo da reação do chat
        moment = loudest_moment(audio, rel_start - 12, rel_start + 6)
        moment = rel_start if moment is None else moment
        # 3) transcrição da janela toda
        words, lang = [], None
        if subtitles and self.tr:
            words, lang = self.tr.words(audio)
            log.info("transcrição: %d palavras (%s) em %.0fs", len(words), lang, time.time() - t0)

        results = []
        formatos = [Formato("corte", 0, 1e9, 0, 0, "corte manual")] if fixed else self.formatos
        for fmt in formatos:
            teaser = (max(0.0, moment - 1.5), min(total, moment + 2.0)) if self.gancho and not fixed else None
            tlen = (teaser[1] - teaser[0]) if teaser else 0.0
            if fixed:
                cs, ce = max(0.0, fixed[0] - ws), min(total, fixed[1] - ws)
            else:
                cs, ce = choose_cut(fmt, words, moment, rel_top, total, tlen)
            out = self._render(fmt, wide, words, cs, ce, teaser, name, credit_text, work)
            dur = ce - cs + tlen
            transcript = " ".join(w["word"] for w in words if cs <= w["start"] < ce)
            transcript = re.sub(r"(\d) ([.,]\d)", r"\1\2", transcript)  # "1 .800" -> "1.800"
            info = dict(meta)
            info.update({
                "arquivo": out.name, "formato": fmt.nome, "formato_desc": fmt.desc,
                "duracao_s": round(dur, 1), "momento_no_clipe_s": round(moment - cs + tlen, 1),
                "idioma": lang, "transcricao": transcript,
                "tempo_processamento_s": round(time.time() - t0, 1),
            })
            out.with_suffix(".json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
            self._post_caption(out.with_suffix(".txt"), transcript, credit_text)
            self._ranking(info)
            results.append(out)
            log.info("   %s: %.1fs -> %s", fmt.nome, dur, out.name)
        return results

    def _render(self, fmt, wide, words, cs, ce, teaser, name, credit_text, work):
        tlen = (teaser[1] - teaser[0]) if teaser else 0.0
        dur = ce - cs + tlen
        sub_words = []
        if teaser:
            sub_words += [dict(w, start=w["start"] - teaser[0], end=min(w["end"], teaser[1]) - teaser[0])
                          for w in words if teaser[0] <= w["start"] < teaser[1] - 0.15]
        sub_words += [dict(w, start=w["start"] - cs + tlen, end=min(w["end"], ce) - cs + tlen)
                      for w in words if cs <= w["start"] < ce - 0.1]
        chunks = chunk_words(sub_words)
        final = self.out / f"{name}_{fmt.nome}.mp4"
        ass = work / f"legenda_{fmt.nome}.ass"
        write_ass(chunks, ass, self.layout, self.zoom, self.titulo, credit_text if self.credito else "", dur)
        if chunks:
            write_srt(chunks, final.with_suffix(".srt"))

        # pula direto para perto do trecho (seek preciso na entrada) e corta com trim
        seek = max(0.0, min(cs, teaser[0] if teaser else cs) - 0.5)
        a, b = cs - seek, ce - seek
        if teaser:
            ta, tb = teaser[0] - seek, teaser[1] - seek
            pre = (f"[0:v]split=2[v0][v1];[0:a]asplit=2[a0][a1];"
                   f"[v0]trim={ta:.3f}:{tb:.3f},setpts=PTS-STARTPTS[tv];"
                   f"[a0]atrim={ta:.3f}:{tb:.3f},asetpts=PTS-STARTPTS[ta];"
                   f"[v1]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[mv];"
                   f"[a1]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[ma];"
                   f"[tv][ta][mv][ma]concat=n=2:v=1:a=1[vin][ain];")
        else:
            pre = (f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[vin];"
                   f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[ain];")
        fc = (pre + layout_filter(self.layout, self.zoom, self.camera) + ";"
              f"[vl]ass={ass.name},fps=fps={self.fps}:round=near,format=yuv420p[v];"
              f"[ain]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]")
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{seek:.3f}", "-i", str(wide),
              "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
              "-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf), "-profile:v", "high",
              "-maxrate", "14M", "-bufsize", "28M", "-g", str(self.fps * 2),
              "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(final)], cwd=work)
        return final

    def _post_caption(self, path: Path, transcript: str, credit_text: str):
        frase = transcript.strip()
        if len(frase) > 140:
            frase = frase[:140].rsplit(" ", 1)[0] + "..."
        partes = []
        if self.titulo:
            partes.append(self.titulo)
        if frase:
            partes.append(f"\"{frase}\"")
        if credit_text:
            partes.append(f"🎥 Créditos: {credit_text}")
        if self.hashtags:
            partes.append(self.hashtags)
        path.write_text("\n\n".join(partes) + "\n", encoding="utf-8")

    def _ranking(self, info):
        f = self.out / "ranking.csv"
        new = not f.exists()
        with f.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["arquivo", "formato", "score", "msgs_no_pico", "duracao_s", "horario", "canal",
                            "inicio_da_fala"])
            w.writerow([info.get("arquivo"), info.get("formato"), info.get("score", ""),
                        info.get("msgs_no_pico", ""), info.get("duracao_s"), info.get("horario", ""),
                        info.get("canal", ""), (info.get("transcricao") or "")[:80]])


def render_preview(image_in: Path, image_out: Path, layout, zoom=1.25, camera=None, titulo="", credito=""):
    """Gera um quadro de exemplo do layout (para conferir câmera, título e legenda)."""
    image_out = image_out.resolve()
    work = image_out.parent
    ass = work / "_preview.ass"
    write_ass([[{"start": 0, "end": 5, "word": w} for w in ["EXEMPLO", "DE", "LEGENDA"]]], ass, layout, zoom,
              titulo, credito, 5)
    fc = layout_filter(layout, zoom, camera, src="0:v") + f";[vl]ass={ass.name}[v]"
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(Path(image_in).resolve()),
          "-filter_complex", fc, "-map", "[v]", "-frames:v", "1", str(image_out)], cwd=work)
    ass.unlink(missing_ok=True)
    return image_out
