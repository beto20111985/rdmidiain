"""
RD Mídia Indoor — Execução segura de ffprobe/ffmpeg.

- Sempre lista de argumentos (sem shell), stdin fechado, demuxer FORÇADO
  (``-f <fmt>``) e ``-protocol_whitelist file`` (nada de rede/concat/HLS embutido).
- Limites de recurso via ``prlimit`` (memória virtual, tamanho máximo de arquivo
  gravado, sem core dump) + ``nice``; timeout de parede com kill do grupo de processos.
- stderr vai para arquivo temporário (sem deadlock de pipe) e só os últimos bytes
  são lidos para diagnóstico (nunca devolvidos ao usuário).
- Progresso real do ffmpeg por ``-progress pipe:1`` (``out_time_us``).
"""
from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

import structlog

from app.modules.media.policy import media_settings

log = structlog.get_logger("rd.media.ffmpeg")


class ProbeError(Exception):
    """ffprobe não conseguiu ler o arquivo (corrompido/ilegível)."""


class ProbeUnavailable(Exception):
    """Falha do AMBIENTE ao rodar o ffprobe (binário ausente, limite de recurso, morto por sinal)
    — transitória; nunca deve virar rejeição do arquivo."""


_SYSTEM_ERROR_MARKERS = ("cannot allocate memory", "error while loading shared libraries", "out of memory",
                         "resource temporarily unavailable", "failed to allocate")


class FFmpegTimeout(Exception):
    pass


class FFmpegAborted(Exception):
    """Execução interrompida a pedido (lease perdido: outra execução assumiu a mídia)."""


def _load_prctl():
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes

        return ctypes.CDLL("libc.so.6", use_errno=True).prctl  # resolvido no PAI (nada de dlopen no filho)
    except Exception:  # pragma: no cover
        return None


_PRCTL = _load_prctl()
_PR_SET_PDEATHSIG = 1


def _die_with_parent() -> None:  # pragma: no cover - roda no filho, entre fork e exec
    """PR_SET_PDEATHSIG(SIGKILL): se o processo do worker morrer (OOM, SIGKILL, reinício),
    o ffmpeg/ffprobe morre junto — nada de transcodificação órfã consumindo CPU/disco.
    Preservado por ``prlimit``/``nice`` (exec sem setuid)."""
    _PRCTL(_PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)


_PREEXEC = _die_with_parent if _PRCTL is not None else None


class FFmpegFailed(Exception):
    def __init__(self, returncode: int, stderr_tail: str) -> None:
        super().__init__(f"ffmpeg rc={returncode}")
        self.returncode = returncode
        self.stderr_tail = stderr_tail

    @property
    def looks_corrupted(self) -> bool:
        t = self.stderr_tail.lower()
        markers = (
            "invalid data found", "moov atom not found", "corrupt", "error while decoding", "invalid nal",
            "truncat", "could not find codec parameters", "error reading header", "end of file",
            "invalid frame", "decode_slice", "missing reference", "no frame!", "header damaged",
            "partial file", "error splitting the input", "nothing was written into output file",
            "does not contain any stream", "output file is empty",
        )
        return any(m in t for m in markers)

    @property
    def killed_by_signal(self) -> bool:
        return self.returncode < 0

    @property
    def system_error(self) -> bool:
        t = self.stderr_tail.lower()
        return self.returncode < 0 or self.returncode in (126, 127) or any(m in t for m in _SYSTEM_ERROR_MARKERS)


def _limits_prefix() -> list[str]:
    ms = media_settings()
    prefix: list[str] = []
    prl = shutil.which("prlimit")
    if prl:
        prefix += [
            prl,
            f"--as={ms.PROC_MEM_LIMIT_MB * 1024 * 1024}",
            f"--fsize={ms.PROC_FSIZE_LIMIT_MB * 1024 * 1024}",
            "--core=0",
            "--",
        ]
    nice = shutil.which("nice")
    if nice:
        prefix += [nice, "-n", "10"]
    return prefix


def _tail(path: str, n: int = 4000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


# ─── ffprobe ─────────────────────────────────────────────────────────────────

def ffprobe_json(path: Path, demuxer: Optional[str], *, timeout_s: Optional[int] = None) -> dict:
    ms = media_settings()
    cmd = _limits_prefix() + [ms.FFPROBE_BIN, "-v", "error", "-hide_banner", "-protocol_whitelist", "file"]
    if demuxer:
        cmd += ["-f", demuxer]
    cmd += ["-show_format", "-show_streams", "-of", "json", "-i", str(path)]
    try:
        cp = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_s or ms.PROBE_TIMEOUT_S,
            check=False,
            close_fds=True,
            start_new_session=True,
            preexec_fn=_PREEXEC,
        )
    except subprocess.TimeoutExpired:
        raise ProbeUnavailable("ffprobe excedeu o tempo limite") from None
    except OSError as exc:
        raise ProbeUnavailable(f"ffprobe indisponível: {type(exc).__name__}") from None
    err = cp.stderr.decode("utf-8", "replace")[-2000:]
    if cp.returncode < 0 or cp.returncode in (126, 127) or any(m in err.lower() for m in _SYSTEM_ERROR_MARKERS):
        raise ProbeUnavailable(f"ffprobe rc={cp.returncode}")
    if cp.returncode != 0 or len(cp.stdout) > 8 * 1024 * 1024:
        raise ProbeError(err)
    try:
        data = json.loads(cp.stdout or b"{}")
    except ValueError:
        raise ProbeError("saída do ffprobe inválida") from None
    if not data.get("streams"):
        raise ProbeError("nenhum stream")
    return data


def parse_rate(value: Optional[str]) -> tuple[int, int]:
    try:
        num, _, den = (value or "0/0").partition("/")
        n, d = int(num), int(den or 1)
        if n <= 0 or d <= 0:
            return 0, 0
        f = Fraction(n, d)
        return f.numerator, f.denominator
    except (ValueError, ZeroDivisionError):
        return 0, 0


def _rotation(stream: dict) -> int:
    rot = 0
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                pass
    if not rot:
        try:
            rot = int((stream.get("tags") or {}).get("rotate", 0))
        except (TypeError, ValueError):
            rot = 0
    return rot % 360


def _to_ms(value) -> Optional[int]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v <= 0 or v == float("inf"):
        return None
    return int(round(v * 1000))


@dataclass
class VideoProbe:
    stream_index: int
    codec: str
    profile: Optional[str]
    pix_fmt: Optional[str]
    coded_w: int
    coded_h: int
    display_w: int
    display_h: int
    rotation: int
    fps_num: int
    fps_den: int
    duration_ms: Optional[int]
    stream_duration_ms: Optional[int]
    format_duration_ms: Optional[int]
    has_audio: bool
    audio_codecs: list[str] = field(default_factory=list)
    video_stream_count: int = 1
    nb_frames: Optional[int] = None
    bit_rate: Optional[int] = None
    format_name: Optional[str] = None

    @property
    def fps_milli(self) -> Optional[int]:
        if self.fps_num <= 0:
            return None
        return int(round(self.fps_num * 1000 / self.fps_den))

    def summary(self) -> dict:
        return {
            "codec": self.codec,
            "profile": self.profile,
            "pix_fmt": self.pix_fmt,
            "coded": [self.coded_w, self.coded_h],
            "display": [self.display_w, self.display_h],
            "rotation": self.rotation,
            "fps": f"{self.fps_num}/{self.fps_den}" if self.fps_num else None,
            "duration_ms": self.duration_ms,
            "audio_codecs": self.audio_codecs,
            "video_streams": self.video_stream_count,
            "nb_frames": self.nb_frames,
            "bit_rate": self.bit_rate,
            "format": self.format_name,
        }


def analyze_video(data: dict) -> VideoProbe:
    streams = data.get("streams") or []
    videos = [
        s for s in streams
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    if not videos:
        raise ProbeError("sem stream de vídeo")
    v = videos[0]
    codec = (v.get("codec_name") or "").strip()
    if not codec or codec in ("none", "unknown"):
        raise ProbeError("codec de vídeo desconhecido")
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if w <= 0 or h <= 0:
        raise ProbeError("dimensões inválidas")
    rot = _rotation(v)
    # SAR não quadrado (anamórfico) → largura de exibição
    sar_n, sar_d = parse_rate((v.get("sample_aspect_ratio") or "1:1").replace(":", "/"))
    dw = int(round(w * sar_n / sar_d)) if sar_n and sar_d and sar_n != sar_d else w
    dh = h
    if rot in (90, 270):
        dw, dh = dh, dw
    fn, fd = parse_rate(v.get("avg_frame_rate"))
    if not fn:
        fn, fd = parse_rate(v.get("r_frame_rate"))
    fmt = data.get("format") or {}
    sdur = _to_ms(v.get("duration"))
    if sdur is None:
        tags = v.get("tags") or {}
        d = tags.get("DURATION") or tags.get("duration")
        if d and ":" in str(d):
            try:
                hh, mm, ss = str(d).split(":")
                sdur = int(round((int(hh) * 3600 + int(mm) * 60 + float(ss)) * 1000))
            except ValueError:
                sdur = None
    fdur = _to_ms(fmt.get("duration"))
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    try:
        nb = int(v.get("nb_frames")) if v.get("nb_frames") not in (None, "N/A") else None
    except (TypeError, ValueError):
        nb = None
    try:
        br = int(fmt.get("bit_rate")) if fmt.get("bit_rate") not in (None, "N/A") else None
    except (TypeError, ValueError):
        br = None
    return VideoProbe(
        stream_index=int(v.get("index", 0)),
        codec=codec[:32],
        profile=(v.get("profile") or None),
        pix_fmt=(v.get("pix_fmt") or None),
        coded_w=w,
        coded_h=h,
        display_w=dw,
        display_h=dh,
        rotation=rot,
        fps_num=fn,
        fps_den=fd if fn else 0,
        duration_ms=sdur or fdur,
        stream_duration_ms=sdur,
        format_duration_ms=fdur,
        has_audio=bool(audios),
        audio_codecs=[str(a.get("codec_name")) for a in audios][:8],
        video_stream_count=len(videos),
        nb_frames=nb,
        bit_rate=br,
        format_name=fmt.get("format_name"),
    )


# ─── ffmpeg ──────────────────────────────────────────────────────────────────

ProgressCb = Callable[[float], None]


def run_ffmpeg(
    args: list[str],
    *,
    timeout_s: float,
    duration_ms: Optional[int] = None,
    on_progress: Optional[ProgressCb] = None,
    workdir: Optional[Path] = None,
    should_abort: Optional[Callable[[], bool]] = None,
) -> None:
    """Executa ffmpeg com limites; ``on_progress(fração 0..1)``; ``should_abort()`` consultado
    a cada ≤ 1 s (mata o ffmpeg). Lança FFmpegTimeout/FFmpegFailed/FFmpegAborted."""
    ms = media_settings()
    cmd = _limits_prefix() + [ms.FFMPEG_BIN, "-hide_banner", "-nostdin", "-v", "error", "-nostats",
                              "-progress", "pipe:1"] + args
    fd, err_path = tempfile.mkstemp(prefix="ffmpeg-", suffix=".log", dir=str(workdir) if workdir else None)
    os.close(fd)
    deadline = time.monotonic() + timeout_s
    try:
        with open(err_path, "wb") as err:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=err,
                close_fds=True,
                start_new_session=True,
                preexec_fn=_PREEXEC,
            )
            sel = selectors.DefaultSelector()
            assert proc.stdout is not None
            sel.register(proc.stdout, selectors.EVENT_READ)
            buf = b""
            try:
                while True:
                    if should_abort is not None and should_abort():
                        raise FFmpegAborted()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise FFmpegTimeout()
                    events = sel.select(timeout=min(remaining, 1.0))
                    if events:
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            if on_progress and duration_ms and line.startswith(b"out_time_us="):
                                try:
                                    us = int(line.split(b"=", 1)[1])
                                except ValueError:
                                    continue
                                if us > 0:
                                    on_progress(min(1.0, us / 1000.0 / duration_ms))
                    elif proc.poll() is not None:
                        break
                rc = proc.wait(timeout=max(1.0, deadline - time.monotonic()))
            except (FFmpegTimeout, subprocess.TimeoutExpired):
                _kill(proc)
                raise FFmpegTimeout() from None
            except FFmpegAborted:
                _kill(proc)
                raise
            except BaseException:
                _kill(proc)
                raise
            finally:
                sel.close()
                if proc.stdout:
                    proc.stdout.close()
        if rc != 0:
            raise FFmpegFailed(rc, _tail(err_path))
    finally:
        try:
            os.unlink(err_path)
        except OSError:
            pass


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        pass
