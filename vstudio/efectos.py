"""Efectos de sonido propios, sintetizados con FFmpeg (sin bibliotecas de sonidos de terceros ni licencias).

Un golpe grave con el gancho, un whoosh en cada cambio de escena y un tick
seco en cada corte dentro de la escena. Van bajos: acompanan la voz, no la tapan.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

MUESTREO = 48000
SONIDOS = {
    # ruido rosa que sube y baja por una banda media: aire que pasa
    "whoosh": (f"anoisesrc=d=0.5:c=pink:a=0.7:r={MUESTREO},bandpass=f=1500:width_type=o:w=2.5,"
               "afade=t=in:d=0.32:curve=qsin,afade=t=out:st=0.32:d=0.18,volume=1.6"),
    # click corto y agudo
    "tick": f"sine=f=1500:d=0.05:r={MUESTREO},afade=t=out:d=0.05:curve=exp,volume=0.5",
    # golpe grave (bombo de trailer) con una cola corta
    "golpe": (f"sine=f=55:d=0.8:r={MUESTREO},afade=t=in:d=0.005,afade=t=out:st=0.04:d=0.76:curve=exp,"
              "volume=3"),
}
ADELANTO_WHOOSH_S = 0.25  # el whoosh empieza antes del cambio: el pico cae justo en el corte


def generar(carpeta: Path) -> dict[str, Path]:
    """Los WAV de cada efecto (se generan una vez por carpeta)."""
    carpeta.mkdir(parents=True, exist_ok=True)
    rutas = {}
    for nombre, filtro in SONIDOS.items():
        destino = carpeta / f"sfx_{nombre}.wav"
        if not destino.exists():
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", filtro, "-ac", "2", str(destino)],
                           check=True, capture_output=True)
        rutas[nombre] = destino
    return rutas


def momentos(inicios: tuple[float, ...], cortes: list[float]) -> dict[str, list[float]]:
    """Cuando suena cada efecto (segundos del reel): el golpe al empezar, un whoosh en cada cambio de escena y un
    tick en cada corte dentro de una escena."""
    return {"golpe": [0.0],
            "whoosh": [max(0.0, ini - ADELANTO_WHOOSH_S) for ini in inicios[1:]],
            "tick": sorted(cortes)}
