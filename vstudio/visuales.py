"""Los clips que no necesitan GPU: el recorte vertical de una toma de video real, el paneo sobre una foto de la
noticia y la union de varios clips dentro de una misma escena (la imagen cambia cada ~3 s).

FFmpeg siempre se invoca con lista de argumentos (sin shell).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .imagenes import Caja
from .tomas import MAX_SEGMENTO_S

ANCHO, ALTO, FPS = 1080, 1920, 30  # el reel
MAX_LENTO = 1.35  # cuanto se puede estirar un clip para que llene su lugar
MIN_SEGMENTO_S = 1.2  # un clip que se ve menos que esto es un parpadeo
MAX_CLIPS_ESCENA = 6
PANEO_ZOOM = 0.12  # cuanto se acerca (o se aleja) la camara sobre una foto
SOBREMUESTREO = 2  # zoompan tiembla si no parte de una imagen mas grande
PLAZO_S = 600


class VisualError(RuntimeError):
    pass


def duracion(ruta: Path) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(ruta)], capture_output=True, text=True, check=True, timeout=60)
        segundos = float(out.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise VisualError(f"no pude medir {ruta.name}") from exc
    if not segundos > 0:
        raise VisualError(f"{ruta.name} no tiene duracion")
    return segundos


def _ffmpeg(args: list[str], que: str) -> None:
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True, timeout=PLAZO_S)
    except (subprocess.SubprocessError, OSError) as exc:  # OSError: no esta ffmpeg
        detalle = getattr(exc, "stderr", b"") or b""
        raise VisualError(f"{que}: {detalle.decode('utf-8', 'replace')[-300:] or exc}") from exc


def _codificar(destino: Path) -> list[str]:
    return ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "15", "-pix_fmt", "yuv420p", str(destino)]


def recortar_toma(origen: Path, destino: Path, caja: Caja) -> Path:
    """La toma en vertical (1080x1920): `caja` = (izq, arriba, der, abajo) en fracciones del cuadro."""
    izq, arriba, der, abajo = caja
    filtro = (f"crop=iw*{der - izq:.5f}:ih*{abajo - arriba:.5f}:iw*{izq:.5f}:ih*{arriba:.5f},"
              f"scale={ANCHO}:{ALTO}:flags=lanczos,setsar=1,fps={FPS}")
    _ffmpeg(["-i", str(origen), "-vf", filtro, *_codificar(destino)], f"no pude recortar {origen.name}")
    return destino


def foto_animada(imagen: Path, destino: Path, segundos: float, acercar: bool = True) -> Path:
    """Un paneo lento sobre una foto (ya recortada en vertical): se acerca o se aleja, sin inventar movimiento."""
    cuadros = max(2, round(segundos * FPS))
    zoom = f"1+{PANEO_ZOOM}*on/{cuadros}" if acercar else f"{1 + PANEO_ZOOM}-{PANEO_ZOOM}*on/{cuadros}"
    filtro = (f"scale={ANCHO * SOBREMUESTREO}:{ALTO * SOBREMUESTREO}:flags=lanczos,"
              f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={cuadros}:s={ANCHO}x{ALTO}:fps={FPS},"
              "setsar=1")
    _ffmpeg(["-i", str(imagen), "-vf", filtro, "-frames:v", str(cuadros), *_codificar(destino)],
            f"no pude animar {imagen.name}")
    return destino


def repartir(duraciones: list[float], total_s: float) -> list[float]:
    """Cuanto se ve cada clip de una escena de `total_s`: en proporcion a lo que dura, con tope (un clip largo no
    se queda con toda la escena)."""
    if not duraciones or min(duraciones) <= 0:
        raise VisualError("clips sin duracion")
    pesos = [min(d, MAX_SEGMENTO_S) for d in duraciones]
    falta = total_s - sum(pesos)
    sobra = sum(duraciones) - sum(pesos)
    if falta > 0 and sobra > 0:  # la escena es mas larga: antes de pasar a camara lenta, cada clip muestra mas de si
        usar = min(falta, sobra)
        pesos = [p + (d - p) / sobra * usar for p, d in zip(pesos, duraciones)]
    suma = sum(pesos)
    return [total_s * p / suma for p in pesos]


def secuencia(clips: list[Path], duraciones: list[float], total_s: float) -> list[Path]:
    """Los clips que se muestran en una escena de `total_s`: se sacan del final los que dejarian parpadeos (la
    escena salio mas corta de lo previsto) y, si no alcanzan ni en camara lenta (tomas cortas en una escena larga),
    se repite la serie en vez de congelar el ultimo cuadro."""
    clips, duraciones = list(clips), list(duraciones)
    while len(clips) > 1 and min(repartir(duraciones, total_s)) < MIN_SEGMENTO_S:
        clips, duraciones = clips[:-1], duraciones[:-1]
    serie, serie_s = list(clips), list(duraciones)
    while sum(duraciones) * MAX_LENTO < total_s and len(clips) + len(serie) <= MAX_CLIPS_ESCENA:
        clips, duraciones = clips + serie, duraciones + serie_s
    return clips


def componer(clips: list[Path], total_s: float, destino: Path) -> list[float]:
    """Une los clips de una escena en uno solo de `total_s` segundos, con cortes secos. Devuelve los segundos
    (desde que empieza la escena) donde cambia la imagen. Si un clip no llega a llenar su lugar se pasa mas lento
    (hasta MAX_LENTO) y, si aun asi falta, se queda en el ultimo cuadro."""
    if not clips:
        raise VisualError("no hay clips para unir")
    clips = secuencia(clips, [duracion(c) for c in clips], total_s)
    duraciones = [duracion(c) for c in clips]
    partes = repartir(duraciones, total_s)
    entradas, filtros = [], []
    for k, (clip, dura, parte) in enumerate(zip(clips, duraciones, partes)):
        factor = min(max(parte / dura, 1.0), MAX_LENTO)
        relleno = max(0.0, parte - dura * factor) + 0.05
        entradas += ["-i", str(clip)]
        filtros.append(f"[{k}:v]setpts={factor:.4f}*(PTS-STARTPTS),fps={FPS},scale={ANCHO}:{ALTO}:flags=lanczos,"
                       f"setsar=1,format=yuv420p,tpad=stop_mode=clone:stop_duration={relleno:.3f},"
                       f"trim=duration={parte:.3f},setpts=PTS-STARTPTS[s{k}]")
    filtros.append("".join(f"[s{k}]" for k in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[v]")
    _ffmpeg([*entradas, "-filter_complex", ";".join(filtros), "-map", "[v]", "-t", f"{total_s:.3f}",
             *_codificar(destino)], "no pude unir los clips de la escena")
    cambios, acumulado = [], 0.0
    for parte in partes[:-1]:
        acumulado += parte
        cambios.append(round(acumulado, 3))
    return cambios
