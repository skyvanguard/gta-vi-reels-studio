"""Control de pronunciacion: Whisper escucha cada escena SIN el guion y se marcan las palabras que no se oyen.

La voz (Chatterbox) sale distinta en cada generacion y a veces se come la enie o el acento final: "Otono" por
"Otoño", "coligo" por "código", "moví" por "movió" (en dos reels reales). Se revisan las
palabras con tilde o enie (donde estan esos errores); la escena que falla se vuelve a generar con otra semilla y
queda la version con menos palabras dudosas.
"""
from __future__ import annotations

import re
import unicodedata

# palabras con tilde que Whisper escribe con o sin ella aunque se digan bien (tilde diacritica o interrogativos)
SIN_REVISAR = frozenset(("más", "sí", "sé", "dé", "té", "tú", "él", "mí", "aún", "qué", "cómo", "cuándo", "dónde",
                         "cuánto", "cuánta", "cuántos", "cuántas", "quién", "quiénes", "cuál", "cuáles", "también",
                         "está", "están", "esté", "allá", "acá", "ahí", "así", "después", "según", "días", "día"))
# numeros escritos con letras: Whisper los escribe en cifras ("veintiséis" -> "26")
NUMERO = re.compile(r"^(dieci|veinti)\w+|^\w+(cientos|cientas)$|^(ún|dós|trés|séis)$")
CON_TILDE_O_ENIE = re.compile(r"[áéíóúñ]")


def _palabras(texto: str) -> list[str]:
    """Minusculas, con tildes y enie (es lo que se controla), sin signos."""
    texto = unicodedata.normalize("NFC", texto.lower())
    return re.findall(r"[a-záéíóúüñ]+", texto)


def dificiles(narracion: str) -> list[str]:
    """Las palabras del guion que se controlan: con tilde o enie, sin las que Whisper escribe a su manera."""
    return [p for p in _palabras(narracion)
            if CON_TILDE_O_ENIE.search(p) and p not in SIN_REVISAR and not NUMERO.search(p)]


def dudosas(narracion: str, oido: str) -> list[str]:
    """Palabras dificiles del guion que no aparecen tal cual en lo que oyo Whisper (sin pista)."""
    oidas = set(_palabras(oido))
    return [p for p in dificiles(narracion) if p not in oidas]


AVISO = "Pronunciación dudosa: "
SEPARADOR = " | "


def con_aviso(advertencia: str, detalle: str) -> str:
    """La advertencia del plan con lo que la voz puede decir mal (reemplaza el de una toma anterior). Queda
    anotado para el control antes de publicar (calidad.py)."""
    partes = [p for p in advertencia.split(SEPARADOR) if p and not p.startswith(AVISO)]
    if detalle:
        partes.append(AVISO + detalle)
    return SEPARADOR.join(partes)
