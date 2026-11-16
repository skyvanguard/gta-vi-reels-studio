"""Narracion por escena. Dos motores, cada uno en su propio venv:

- "chatterbox" (por defecto): Chatterbox Multilingual V3 ajustado a espanol latino (MIT). La voz sale de un wav
  de referencia de `voces_dir` (la "voz activa") o, con la voz "integrada", de la voz que trae el modelo.
- "qwen3": Qwen3-TTS (Apache-2.0); clona un wav de referencia + su transcripcion. Peor en espanol.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigVoz

log = logging.getLogger(__name__)

TEXTO_REFERENCIA = ("¡Atención, gamers! Esto es lo último de GTA seis: nuevas pistas, fechas y todo lo que "
                    "tenés que saber antes del lanzamiento.")
VOCES_DISENADAS = {  # se crean con Qwen3-TTS VoiceDesign (python -m vstudio voces); sirven como timbre de referencia
    "locutor": "Young Latin American man, about 25 years old, speaking Spanish. Energetic and enthusiastic gaming "
               "news presenter for social media, fast pace, bright and clear diction, confident.",
    "locutora": "Young Latin American woman, about 25 years old, speaking Spanish. Energetic, warm gaming news "
                "presenter for social media, upbeat pace, clear diction, friendly and confident.",
    "trailer": "Adult Latin American man, about 40 years old, speaking Spanish. Deep, powerful movie-trailer "
               "narrator voice, dramatic and cinematic, slower pace with emphasis.",
}
VOZ_INTEGRADA = "integrada"
ARCHIVO_ACTIVA = "activa.txt"
EXTENSIONES = (".wav", ".flac", ".mp3", ".ogg")
RE_NOMBRE_VOZ = re.compile(r"^[a-z0-9_]{2,20}$")
MAX_REFERENCIA_S = 20
MIN_REFERENCIA_S = 5


class VozError(RuntimeError):
    pass


@dataclass(frozen=True)
class Audio:
    archivo: Path
    segundos: float


def _correr(cmd: list[str], timeout_s: int) -> list[dict]:
    """Corre un script de TTS. Cualquier falla (timeout, venv ausente, salida rota) sale como VozError."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
        linea = next((l for l in res.stdout.splitlines() if l.startswith("RESULTADO_JSON ")), None)
        if res.returncode != 0 or linea is None:
            raise VozError(f"TTS fallo (codigo {res.returncode}): {res.stderr[-800:]}")
        resultado = json.loads(linea.removeprefix("RESULTADO_JSON "))
        if not isinstance(resultado, list) or not all("archivo" in r and "segundos" in r for r in resultado):
            raise VozError("el TTS devolvio un resultado con otra forma")
        return resultado
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise VozError(f"TTS fallo: {exc}") from exc


def _referencia(cfg: ConfigVoz, nombre: str) -> Path | None:
    return next((cfg.voces_dir / f"{nombre}{ext}" for ext in EXTENSIONES
                 if (cfg.voces_dir / f"{nombre}{ext}").exists()), None)


def preparar_voces(cfg: ConfigVoz) -> list[Path]:
    """Crea (una sola vez) las voces de referencia disenadas con Qwen3-TTS."""
    cfg.voces_dir.mkdir(parents=True, exist_ok=True)
    creadas = []
    for nombre, instruccion in VOCES_DISENADAS.items():
        destino = cfg.voces_dir / f"{nombre}.wav"
        if not destino.exists():
            _correr([str(cfg.python), str(cfg.script), "disenar", "--instruct", instruccion,
                     "--texto", TEXTO_REFERENCIA, "--salida", str(destino), "--modelos", str(cfg.modelos),
                     "--dispositivo", cfg.dispositivo], cfg.timeout_s)
            (cfg.voces_dir / f"{nombre}.txt").write_text(TEXTO_REFERENCIA, encoding="utf-8")
        creadas.append(destino)
    return creadas


def voces_disponibles(cfg: ConfigVoz) -> list[str]:
    nombres = []
    if cfg.voces_dir.is_dir():
        nombres = sorted({p.stem for p in cfg.voces_dir.iterdir() if p.suffix.lower() in EXTENSIONES})
    return ([VOZ_INTEGRADA] if cfg.motor == "chatterbox" else []) + nombres


def voz_activa(cfg: ConfigVoz) -> str | None:
    disponibles = voces_disponibles(cfg)
    marca = cfg.voces_dir / ARCHIVO_ACTIVA
    if marca.exists():
        nombre = marca.read_text(encoding="utf-8").strip()
        if nombre in disponibles:
            return nombre
    return disponibles[0] if disponibles else None


def elegir_voz(cfg: ConfigVoz, nombre: str) -> None:
    if nombre not in voces_disponibles(cfg):
        raise VozError(f"voz desconocida: {nombre}. Disponibles: {', '.join(voces_disponibles(cfg))}")
    cfg.voces_dir.mkdir(parents=True, exist_ok=True)
    (cfg.voces_dir / ARCHIVO_ACTIVA).write_text(nombre, encoding="utf-8")


def guardar_referencia(cfg: ConfigVoz, nombre: str, datos: bytes) -> float:
    """Convierte un audio subido (nota de voz, mp3, m4a...) a wav mono 24 kHz de hasta 20 s. Devuelve la duracion.

    FFmpeg solo puede leer el archivo local (-protocol_whitelist file): un audio armado a proposito no puede
    hacer que lea otras rutas ni la red.
    """
    if not RE_NOMBRE_VOZ.match(nombre) or nombre == VOZ_INTEGRADA:
        raise VozError("nombre de voz invalido: usá de 2 a 20 letras minusculas, numeros o _")
    cfg.voces_dir.mkdir(parents=True, exist_ok=True)
    crudo = cfg.voces_dir / f".{nombre}.subida"
    destino = cfg.voces_dir / f"{nombre}.wav"
    crudo.write_bytes(datos)
    try:
        res = subprocess.run(["ffmpeg", "-v", "error", "-y", "-protocol_whitelist", "file", "-i", str(crudo),
                              "-t", str(MAX_REFERENCIA_S), "-ac", "1", "-ar", "24000", "-af", "loudnorm=I=-20",
                              str(destino)], capture_output=True, text=True, timeout=120, check=False)
        if res.returncode != 0:
            raise VozError(f"no pude leer el audio: {res.stderr[-300:]}")
        dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(destino)], capture_output=True, text=True, timeout=60, check=True)
        segundos = float(dur.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        destino.unlink(missing_ok=True)
        raise VozError(f"no pude procesar el audio: {exc}") from exc
    finally:
        crudo.unlink(missing_ok=True)
    if segundos < MIN_REFERENCIA_S:
        destino.unlink(missing_ok=True)
        raise VozError(f"el audio dura {segundos:.0f} s: mandá entre 10 y 20 s hablando claro")
    return segundos


def sintetizar(cfg: ConfigVoz, textos: list[str], carpeta: Path, voz: str | None = None,
               semilla: int = 0) -> list[Audio]:
    """Un audio por texto con la voz pedida (o la activa). `semilla` != 0 da otra toma (para reintentar una
    escena mal pronunciada). Levanta VozError si falla."""
    nombre = voz or voz_activa(cfg)
    if nombre is None:
        raise VozError("no hay voces: correr `python -m vstudio voces` o subir una referencia")
    carpeta.mkdir(parents=True, exist_ok=True)
    entrada = carpeta / "narracion.json"
    entrada.write_text(json.dumps(textos, ensure_ascii=False), encoding="utf-8")
    ref = None if nombre == VOZ_INTEGRADA else _referencia(cfg, nombre)
    if nombre != VOZ_INTEGRADA and ref is None:
        raise VozError(f"no existe la referencia de la voz {nombre}")
    if cfg.motor == "chatterbox":
        cmd = [str(cfg.chatterbox_python), str(cfg.chatterbox_script), "--raiz", str(cfg.chatterbox_dir),
               "--textos-json", str(entrada), "--salida-dir", str(carpeta), "--ref", str(ref or ""),
               "--dispositivo", cfg.dispositivo, "--exageracion", str(cfg.exageracion), "--cfg", str(cfg.cfg_ritmo),
               "--temperatura", str(cfg.temperatura), "--semilla", str(semilla)]
    elif cfg.motor == "qwen3":
        ref_texto_archivo = cfg.voces_dir / f"{nombre}.txt"
        if ref is None or not ref_texto_archivo.exists():
            raise VozError("Qwen3-TTS necesita una referencia con su transcripcion (.txt)")
        cmd = [str(cfg.python), str(cfg.script), "clonar", "--ref", str(ref),
               "--ref-texto", ref_texto_archivo.read_text(encoding="utf-8"), "--textos-json", str(entrada),
               "--salida-dir", str(carpeta), "--modelos", str(cfg.modelos), "--dispositivo", cfg.dispositivo]
    else:
        raise VozError(f"motor de voz desconocido: {cfg.motor}")
    return [Audio(archivo=Path(r["archivo"]), segundos=float(r["segundos"])) for r in _correr(cmd, cfg.timeout_s)]
