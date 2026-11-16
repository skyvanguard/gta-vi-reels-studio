"""Lo que el editor pide en el mensaje, ademas del material: cuanto dura el reel y si va sin voz ("solo video").

Solo se lee en mensajes cortos (una instruccion): en una noticia pegada, "el trailer dura 2 minutos" o "solo
video" son parte de la nota, no un pedido.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from . import formatos

MAX_LARGO_INSTRUCCION = 280  # caracteres (sin el link); un texto mas largo es una nota pegada

_NUMEROS = {"un": 1.0, "uno": 1.0, "una": 1.0, "medio": 0.5, "dos": 2.0}
RE_MINUTOS = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?|un|uno|una|medio|dos)\s*(?:minutos?|mins?)(?!\w)", re.I)
RE_SEGUNDOS = re.compile(r"(?<![\w.,])(\d+)\s*(?:segundos?|segs?|s)(?!\w)", re.I)
RE_SOLO_ANIMACION = re.compile(r"(?<!\w)(?:sin\s+(?:voz|narraci[oó]n|locutor|locuci[oó]n)|"
                               r"s[oó]lo\s+(?:el\s+|la\s+|un\s+|una\s+)?(?:video|vídeo|animaci[oó]n))(?!\w)", re.I)


@dataclass(frozen=True)
class Pedido:
    segundos: int | None = None  # lo que pidio, sin ajustar a los largos que se ofrecen
    solo_animacion: bool = False  # sin voz ni textos: la imagen animada y nada mas
    formato: str = ""  # "sabias", "ranking", "comparacion" ("" = no pidio ninguno: noticia)


def leer(texto: str) -> Pedido:
    """`texto` sin el link (ver fuentes.sin_url)."""
    texto = texto.strip()
    if not texto or len(texto) > MAX_LARGO_INSTRUCCION:
        return Pedido()
    segundos = None
    if m := RE_MINUTOS.search(texto):
        valor = m.group(1).lower()
        minutos = _NUMEROS[valor] if valor in _NUMEROS else float(valor.replace(",", "."))
        segundos = round(minutos * 60)
    elif m := RE_SEGUNDOS.search(texto):
        segundos = int(m.group(1))
    plano = "".join(c for c in unicodedata.normalize("NFKD", texto.lower()) if not unicodedata.combining(c))
    return Pedido(segundos=segundos or None, solo_animacion=bool(RE_SOLO_ANIMACION.search(texto)),
                  formato=formatos.pedido_en(plano))


def largo_para(pedido_s: int, largos: tuple[int, ...]) -> int:
    """El largo ofrecido mas corto que alcanza lo pedido ("al menos 1 min" -> 75); si pide mas que todos, el mayor."""
    return next((largo for largo in sorted(largos) if largo >= pedido_s), max(largos))
