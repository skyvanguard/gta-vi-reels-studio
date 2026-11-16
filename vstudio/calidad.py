"""Si un reel automatico puede salir solo a Facebook (que se publique lo que esta limpio y
lo dudoso espere al editor).

Dos controles: lo que ya quedo anotado en la advertencia del plan (datos sin respaldo, ortografia, pronunciacion,
imagenes o encuadres repetidos) y una mirada del Qwen con vision a un cuadro de cada clip, para lo que salio
deforme. Los avisos generales de la nota ("Rockstar no lo confirmo") no frenan: son contexto.
"""
from __future__ import annotations

import logging
import re
import secrets
import subprocess
from pathlib import Path

from PIL import Image

from . import guionista, imagenes, ortografia, pronunciacion, verificador
from .llm import ClienteLlm, LlmError, parte_imagen, parte_texto
from .modelos import Proyecto

log = logging.getLogger(__name__)

MAX_REEL_S = 90.0  # Meta: los reels van de 3 a 90 s
MIN_REEL_S = 3.0
PUNTO_DEL_CLIP = 0.8  # lo deforme se acumula hacia el final del clip
MARCAS = (
    (verificador.AVISO, "la verificación encontró datos que la nota no respalda"),
    (verificador.SIN_VERIFICAR, "no se pudo verificar los datos"),
    ("Revisá la ortografía", "palabras que parecen mal escritas"),
    (ortografia.PREFIJO, "palabras que parecen mal escritas"),
    (pronunciacion.AVISO, "la voz puede decir mal alguna palabra"),
    ("Hay imágenes repetidas", "imágenes repetidas"),
    ("Escenas con casi el mismo encuadre", "encuadres repetidos"),
)

PROMPT_VISUAL = """Te paso un cuadro de cada escena de un reel hecho con IA (image-to-video) a partir de imagenes
del juego GTA VI. Marca SOLO las escenas con un defecto evidente que un espectador notaria: caras deformadas o
derretidas, manos o cuerpos fundidos, brazos o piernas de mas, personas que se mezclan entre si, autos u objetos
rotos de forma imposible. No marques el estilo, un desenfoque leve, el movimiento ni lo que es asi en el juego.
Devolve SOLO un objeto JSON: {"defectuosas": [{"escena": 2, "problema": "la cara de la mujer se deforma"}]}
(lista vacia si todas estan bien)."""


def motivos_del_plan(p: Proyecto, duracion_s: float | None = None) -> list[str]:
    """Lo que ya se sabe del reel que pide que lo mire el editor antes de publicarlo."""
    advertencia = p.plan.advertencia if p.plan else ""
    motivos = list(dict.fromkeys(motivo for marca, motivo in MARCAS if marca in advertencia))
    if guionista.AVISO_GUION in advertencia:  # solo lo grave del guion (gancho, pregunta, cebo, cifras...)
        avisos = advertencia.split(guionista.AVISO_GUION, 1)[1]
        graves = [g for g in guionista.GRAVES if g in avisos]
        if graves:
            motivos.append(f"el guion no cumple reglas importantes ({', '.join(graves)})")
    if duracion_s is not None and not MIN_REEL_S <= duracion_s <= MAX_REEL_S:
        motivos.append(f"dura {duracion_s:.0f} s (Facebook acepta reels de 3 a 90 s)")
    return motivos


def cuadros(clips: list[tuple[int, Path]], carpeta: Path) -> list[tuple[int, Path]]:
    """Un cuadro de cada clip (cerca del final), en JPEG, con el numero de su escena (si un clip falla, falta
    ese y los demas siguen con su numero)."""
    carpeta.mkdir(parents=True, exist_ok=True)
    salida = []
    for n, clip in clips:
        destino = carpeta / f"control_{n}.jpg"
        try:
            dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                        "csv=p=0", str(clip)], capture_output=True, text=True, check=True).stdout)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * PUNTO_DEL_CLIP:.2f}", "-i", str(clip),
                            "-frames:v", "1", "-q:v", "3", str(destino)], check=True, capture_output=True)
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            log.warning("no pude sacar el cuadro de %s: %s", clip, exc)
            continue
        salida.append((n, destino))
    return salida


def escenas_defectuosas(cliente: ClienteLlm, cuadros_por_escena: list[tuple[int, Path]]) -> list[str] | None:
    """Las escenas que el Qwen ve deformes ("escena 2: la cara se deforma"); None si no pudo mirar (entonces el
    reel no sale solo: un control que no corrio no es un control que dio bien)."""
    encontrados = defectos(cliente, cuadros_por_escena)
    return None if encontrados is None else [f"escena {n}: {p}" for n, p in sorted(encontrados.items())]


def defectos(cliente: ClienteLlm, cuadros_por_escena: list[tuple[int, Path]]) -> dict[int, str] | None:
    """{escena: problema} de lo que el Qwen ve deforme; None si no pudo mirar."""
    if not cuadros_por_escena:
        return None
    partes = []
    for n, ruta in cuadros_por_escena:
        try:
            img = Image.open(ruta).convert("RGB")
        except OSError:
            continue
        partes += [parte_texto(f"ESCENA {n}:"), parte_imagen(imagenes.jpeg_para_llm(img))]
    try:
        datos = cliente.chat_json(PROMPT_VISUAL, partes, max_tokens=3000, esfuerzo="low",
                                  semilla=secrets.randbelow(2**31), reintentos=1)
    except (LlmError, ValueError) as exc:
        log.warning("no se pudo revisar las imágenes: %s", exc)
        return None
    lista = datos.get("defectuosas") if isinstance(datos, dict) else None
    if not isinstance(lista, list):
        log.warning("la revisión de imágenes devolvió algo sin forma: %r", str(datos)[:200])
        return None
    validas = {n for n, _ in cuadros_por_escena}
    encontrados: dict[int, str] = {}
    for d in lista:
        try:
            n = int(d.get("escena")) if isinstance(d, dict) else 0
        except (TypeError, ValueError):
            continue
        if n in validas:
            encontrados[n] = " ".join(str(d.get("problema", "se ve deforme")).split())[:100]
    return encontrados


RE_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
RE_MENCION = re.compile(r"(?<![\w@])@\w+")


def texto_para_publicar(texto: str) -> str:
    """El post sin links ni menciones (el texto sale de una nota de afuera: que no meta nada que no se vio).
    Los hashtags quedan."""
    limpio = RE_MENCION.sub("", RE_URL.sub("", texto))
    return "\n".join(" ".join(linea.split()) for linea in limpio.splitlines()).strip()
