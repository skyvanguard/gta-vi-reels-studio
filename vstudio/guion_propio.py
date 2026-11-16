"""El guion que escribe el editor, usado tal cual.

Una vez el editor pego un guion con secciones y tiempos ("[INTRO – 0:00 a 0:10]" ... "[CIERRE – 0:55 a
1:15]") y el bot lo reescribio en uno de 30 s. Si el texto tiene esa forma, la narracion es la suya (repartida
en las escenas por oraciones) y el largo, el de sus tiempos.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import guion

RE_SECCION = re.compile(r"^\s*\[[^\]\n]{2,60}\]\s*$", re.M)
RE_TIEMPO = re.compile(r"(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)")
RE_RANGO = re.compile(r"(?<!\d)\d{1,2}:[0-5]\d\s*(?:a|-|–|—|al|hasta)\s*\d{1,2}:[0-5]\d(?!\d)")
# "0:00 a 0:10", "(0:10-0:35)" o "[0:35 – 0:55]" al principio de una linea: se quita (si no queda nada, es
# como una seccion)
RE_RANGO_INICIAL = re.compile(r"^\s*[\[(]?\s*\d{1,2}:[0-5]\d\s*(?:a|-|–|—|al|hasta)\s*\d{1,2}:[0-5]\d\s*[\])]?"
                              r"\s*[:.\-–—]*\s*")
RE_VIÑETA = re.compile(r"^\s*(?:[-•*]|\d+[.)])\s+")
RE_ORACION = re.compile(r"(?<=[.!?…])\s+")
MIN_SECCIONES = 2
REEMPLAZOS_VOZ = ((re.compile(r"\bGTA\s*(?:VI|6)\b"), "GTA seis"), (re.compile(r"\bGTA\s*(?:V|5)\b"), "GTA cinco"))


@dataclass(frozen=True)
class GuionPropio:
    oraciones: tuple[str, ...]
    segundos: int | None  # el final del ultimo tiempo ("1:15" -> 75); None si no puso tiempos


def es_guion(texto: str) -> bool:
    return len(RE_SECCION.findall(texto)) >= MIN_SECCIONES or len(RE_RANGO.findall(texto)) >= MIN_SECCIONES


def para_la_voz(texto: str) -> str:
    """Lo que el locutor no puede leer bien: "GTA VI" se dice "GTA seis"."""
    for patron, reemplazo in REEMPLAZOS_VOZ:
        texto = patron.sub(reemplazo, texto)
    return guion.corregir_palabras(texto)


def _cerrar_lista(lineas: list[str], abierta: bool) -> bool:
    if abierta and lineas:
        lineas[-1] += "."
    return False


def _lineas_de_narracion(texto: str) -> list[str]:
    """Las lineas que se dicen: sin secciones ni tiempos; las viñetas que siguen a "...:" van en la misma
    oracion, separadas por comas. Una primera linea sin puntuacion antes de la primera seccion es un titulo
    ("GUION REEL GTA ONLINE") y no se dice."""
    lineas: list[str] = []
    lista, antes_de_secciones = False, True
    for linea in texto.splitlines():
        sin_tiempo = RE_RANGO_INICIAL.sub("", linea)
        if RE_SECCION.match(linea) or (sin_tiempo != linea and not sin_tiempo.strip()):
            lista, antes_de_secciones = _cerrar_lista(lineas, lista), False
            continue
        es_vineta = bool(RE_VIÑETA.match(sin_tiempo))
        limpia = RE_VIÑETA.sub("", sin_tiempo).strip()
        if not limpia:
            continue
        if antes_de_secciones and not lineas and limpia[-1] not in ".!?…:;,":
            continue  # titulo
        if es_vineta and lineas and (lista or lineas[-1].endswith(":")):
            separador = " " if lineas[-1].endswith(":") else ", "
            lineas[-1] = lineas[-1].rstrip(".") + separador + limpia.rstrip(".;,")
            lista = True
            continue
        lista = _cerrar_lista(lineas, lista)
        lineas.append(limpia if limpia[-1] in ".!?…:;," else f"{limpia}.")
    _cerrar_lista(lineas, lista)
    return lineas


def leer(texto: str) -> GuionPropio | None:
    if not es_guion(texto):
        return None
    tiempos = [int(m) * 60 + int(s) for m, s in RE_TIEMPO.findall(texto)]
    cuerpo = " ".join(_lineas_de_narracion(texto))
    oraciones = tuple(para_la_voz(o.strip()) for o in RE_ORACION.split(cuerpo) if o.strip())
    if not oraciones:
        return None
    return GuionPropio(oraciones=oraciones, segundos=max(tiempos) if tiempos else None)


def repartir(oraciones: tuple[str, ...], n: int) -> list[str]:
    """Reparte las oraciones en n escenas seguidas con cantidades de palabras parecidas (sin cortar oraciones)."""
    n = max(1, min(n, len(oraciones)))
    total = sum(guion.palabras(o) for o in oraciones)
    escenas, actual, acumulado = [], [], 0
    for i, oracion in enumerate(oraciones):
        actual.append(oracion)
        acumulado += guion.palabras(oracion)
        quedan_escenas = n - len(escenas) - 1
        quedan_oraciones = len(oraciones) - i - 1
        if quedan_escenas and (acumulado >= total * (len(escenas) + 1) / n or quedan_oraciones == quedan_escenas):
            escenas.append(" ".join(actual))
            actual = []
    if actual:
        escenas.append(" ".join(actual))
    return escenas
