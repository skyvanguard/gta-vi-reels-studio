"""Ortografia del titular, los textos, la narracion y el post (una vez un titular salio "DESCARGAS
ANTECIDAS").

Diccionario hunspell es_ES (paquete hunspell-es de Ubuntu; MPL-1.1/GPL-3+/LGPL-3+), leido con spylls. Una palabra
se marca si no esta en el diccionario, ni en el material de la nota, ni entre las palabras del juego. Sin
diccionario (o sin spylls) el control no corre: no frena ningun plan.
"""
from __future__ import annotations

import functools
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

MIN_LETRAS = 4  # las cortas casi nunca son errores y llenan de falsos avisos
MAX_DUDOSAS = 8
RE_PALABRA = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+")
RE_HASHTAG = re.compile(r"#\w+")
RE_CAMEL = re.compile(r"[a-záéíóúñ][A-ZÁÉÍÓÚÑ]")  # "joinSession": nombres de funciones, marcas
# lo que el diccionario no trae y en una pagina de GTA es normal
PALABRAS_DEL_JUEGO = frozenset("""
gta rockstar leonida vice city lucía lucia jason duval caminos precarga precargar precargas precargado precargada
multijugador online offline tráiler trailer tráileres trailers gameplay gamer gamers streamer streamers youtuber
reel reels hashtag hashtags likes playstation xbox series nintendo steam grassrivers ambrosia kalaga gellhorn keys
port mount park national boobie drequan priest dimez heder raul bautista vinewood vespucci ocean beach stunt
ultimate edition vintage pack grotti cheetah vapid ganado buggy squalo stanier spoiler spoilers leak leaks leaker
filtración filtraciones remaster remake roleplay mods modders speedrun crossover fans fan tráilers canis kayak
imprimible""".split())


_DICCIONARIO: Path | None = None


def usar(diccionario: Path | None) -> None:
    """Ruta base del diccionario (sin extension: ".../es_ES"). La fija el servicio al arrancar."""
    global _DICCIONARIO
    _DICCIONARIO = diccionario


@functools.lru_cache(maxsize=4)
def _cargar(base: str):
    try:
        from spylls.hunspell import Dictionary
    except ImportError:
        log.warning("sin spylls: no hay control de ortografía")
        return None
    if not (Path(f"{base}.dic").exists() and Path(f"{base}.aff").exists()):
        log.warning("no está el diccionario %s.dic/.aff: no hay control de ortografía", base)
        return None
    return Dictionary.from_files(base)


def disponible() -> bool:
    return _DICCIONARIO is not None and _cargar(str(_DICCIONARIO)) is not None


def dudosas(textos: list[str], conocidas: str = "") -> list[str]:
    """Palabras que parecen mal escritas, en el orden en que aparecen (sin repetir). `conocidas`: el material de
    la nota (lo que la nota nombra no es un error aunque el diccionario no lo traiga)."""
    if _DICCIONARIO is None:
        return []
    diccionario = _cargar(str(_DICCIONARIO))
    if diccionario is None:
        return []
    material = {w.lower() for w in RE_PALABRA.findall(conocidas)}
    vistas: set[str] = set()
    malas: list[str] = []
    for texto in textos:
        for palabra in RE_PALABRA.findall(RE_HASHTAG.sub(" ", texto or "")):
            clave = palabra.lower()
            if len(palabra) < MIN_LETRAS or clave in vistas or RE_CAMEL.search(palabra):
                continue
            vistas.add(clave)
            if clave in PALABRAS_DEL_JUEGO or clave in material:
                continue
            if not (diccionario.lookup(palabra) or diccionario.lookup(clave) or diccionario.lookup(clave.capitalize())
                    or _es_voseo(clave, diccionario)):
                malas.append(palabra)
    return malas[:MAX_DUDOSAS]


_SIN_TILDE = str.maketrans("áéí", "aei")
_PRONOMBRES = ("nos", "los", "las", "les", "me", "te", "lo", "la", "le")


def _es_voseo(palabra: str, diccionario) -> bool:
    """El diccionario es_ES no trae el voseo, que en los textos y el post es normal: "comentá", "contás",
    "entendés" (infinitivo sin la r, con tilde) y "contanos", "seguinos" (con pronombre)."""
    def infinitivo(base: str) -> bool:  # el voseo es el infinitivo sin la r: termina en a, e o i ("segu" no)
        base = base.translate(_SIN_TILDE)
        return len(base) >= 3 and base[-1] in "aei" and bool(diccionario.lookup(base + "r"))
    tilde = re.match(r"^(.*[áéí])s?$", palabra)
    if tilde and infinitivo(tilde.group(1)):
        return True
    return any(palabra.endswith(p) and infinitivo(palabra[:-len(p)]) for p in _PRONOMBRES)


PREFIJO = "palabras que no existen o están mal escritas: "


def problema(malas: list[str]) -> list[str]:
    return [f"{PREFIJO}{', '.join(malas)} (corregí la ortografía)"] if malas else []


def solo_ortografia(problemas: tuple[str, ...]) -> bool:
    return bool(problemas) and all(p.startswith(PREFIJO) for p in problemas)
