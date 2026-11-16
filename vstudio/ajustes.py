"""Cambios que el editor pide con sus palabras sobre un plan o un reel ya hecho: "sin subtítulos", "usa otras
imágenes", "sin voz", "que dure 1 min", "otro guion".

Un mensaje corto, sin link ni fotos, que pide alguno de estos cambios va al ultimo proyecto del chat (o al del
mensaje que responde): no arma un reel nuevo. Una vez "Quiero uno sin subtitulos" y "Usa otras imágenes"
armaron reels nuevos con imagenes oficiales en vez de cambiar el que el editor estaba mirando.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import pedido

MAX_LARGO_AJUSTE = 160  # caracteres: un pedido de cambio es una frase, no una nota

_ART = r"(?:(?:el|la|los|las|le|les)\s+)?"
_QUITAR = r"(?:sin|ni|quit\w*|sac\w*|elimin\w*|borr\w*|no\s+(?:pongas|quiero|lleve|uses|tenga))"
_PONER = r"(?:con|pon\w*|agreg\w*|añad\w*|sum\w*|volv\w*\s+a\s+poner)"
_SUBS = r"(?:subt[ií]tulos?|subs|textos?|letras?|leyendas?)"
_VOZ = r"(?:voz|audio|narraci[oó]n|locutor|locuci[oó]n|sonido)"
_TIT = r"(?:titular|t[ií]tulo)"
# lo pedido termina ahi: "sin voz", "sin voz y ...", "sin voz, ..."; "con voz grave" o "con la voz de Jason" no son
# un si/no de la voz
_FIN = (r"(?=\s*(?:$|[,.;:!?)]|y\b|e\b|ni\b|pero\b|tambi[eé]n\b|por\s+favor|porfa\b|ahora\b|mejor\b|en\s+(?:el|este|"
        r"ese)\b|al\s+video\b|del\s+video\b))")


def _patron(verbo: str, cosa: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){verbo}\s+{_ART}{cosa}(?!\w){_FIN}", re.I)


RE = {
    ("subtitulos", False): _patron(_QUITAR, _SUBS), ("subtitulos", True): _patron(_PONER, _SUBS),
    ("voz", False): _patron(_QUITAR, _VOZ), ("voz", True): _patron(_PONER, _VOZ),
    ("titular", False): _patron(_QUITAR, _TIT), ("titular", True): _patron(_PONER, _TIT),
}
# un pedido nuevo, no un cambio al anterior: "un video nuevo sin voz", "haceme un reel de 1 minuto sobre el mapa",
# "quiero un video de 30 s", "reel sin voz sobre la fecha"
RE_NUEVO = re.compile(
    r"(?<!\w)(?:nuev[oa]s?(?!\s+(?:im[aá]genes|fotos|escenas))|otro\s+(?:video|vídeo|reel)|noticia|"
    r"(?:hac\w*|arm\w*|cre\w*|gener\w*|quiero|necesito|mand\w*)\s+(?:me\s+)?(?:un|una)\s+(?:video|vídeo|reel)|"
    r"sobre\s+(?:el|la|los|las|lo|que|c[oó]mo)\b)(?!\w)", re.I)
RE_MUDO = re.compile(r"(?<!\w)mudo(?!\w)", re.I)
RE_OTRAS_IMAGENES = re.compile(r"(?<!\w)(?:otras|distintas|diferentes|nuevas|cambi\w*\s+(?:las\s+)?)\s*"
                               r"(?:im[aá]genes|fotos|escenas)(?!\w)|(?<!\w)otra\s+(?:imagen|foto)(?!\w)", re.I)
# "cambiá la escena 3", "otra toma en la 2", "no me gusta la escena 4": otra toma para esa escena sola. "Sacá" y
# "quitá" no cuentan: son pedidos de sacar algo ("sacá la música de la escena 3" no es cambiar la toma). Si entre
# el verbo y el numero se habla del texto, de la voz o de lo que dice la escena, es otra cosa (/texto N,
# /narracion N, sin subtítulos)
_NUMERO_ESCENA = r"(?:n[uú]mero\s+|n[°º]\s*)?(\d{1,2})(?!\d)"
RE_ESCENA = re.compile(r"(?<!\w)(?:cambi\w*|otra|otro|reemplaz\w*|distinta|no\s+me\s+gusta)\b"
                       rf"[^.\d]{{0,40}}?\b(?:(?:escena|toma|parte)\s+{_NUMERO_ESCENA}|"
                       rf"tomas?\s+(?:en|para)\s+la\s+{_NUMERO_ESCENA})", re.I)
RE_NO_ES_LA_TOMA = re.compile(r"(?<!\w)(?:texto|narraci[oó]n|guion|guión|frase|dice|diga|voz|audio|m[uú]sica|"
                              r"sonido|subt[ií]tulos?|titular|t[ií]tulo)(?!\w)", re.I)


def escena_pedida(texto: str) -> int | None:
    """La escena a la que le piden otra toma (None si el mensaje no pide eso)."""
    m = RE_ESCENA.search(texto)
    if m is None or RE_NO_ES_LA_TOMA.search(m.group(0)):
        return None
    return int(m.group(1) or m.group(2))


RE_OTRO_GUION = re.compile(r"(?<!\w)(?:otro\s+(?:guion|guión|texto)|reescrib\w*|cambi\w*\s+(?:el\s+)?gui[oó]n)"
                           r"(?!\w)", re.I)


@dataclass(frozen=True)
class Ajuste:
    voz: bool | None = None
    subtitulos: bool | None = None
    titular: bool | None = None
    solo_animacion: bool = False
    otras_imagenes: bool = False
    otro_guion: bool = False
    segundos: int | None = None
    escena: int | None = None  # otra toma de video real para esa escena sola

    def vacio(self) -> bool:
        return self == Ajuste()

    def opciones(self) -> dict:
        """Los si/no que cambian (para Servicio.cambiar_opciones)."""
        return {k: v for k, v in (("voz", self.voz), ("subtitulos", self.subtitulos), ("titular", self.titular))
                if v is not None}

    def describir(self) -> str:
        partes = [f"{'con' if v else 'sin'} {k}" for k, v in self.opciones().items()]
        partes += ["solo animación"] * self.solo_animacion + ["otras imágenes"] * self.otras_imagenes
        partes += ["otro guion"] * self.otro_guion + ([f"~{self.segundos} s"] if self.segundos else [])
        partes += [f"otra toma en la escena {self.escena}"] if self.escena else []
        return ", ".join(partes)


def leer(texto: str) -> Ajuste:
    """Ajuste vacio si el texto no pide ningun cambio (o es demasiado largo para ser un pedido de cambio)."""
    texto = texto.strip()
    if not texto or len(texto) > MAX_LARGO_AJUSTE or RE_NUEVO.search(texto):
        return Ajuste()
    p = pedido.leer(texto)
    valores = {}
    for (campo, valor), patron in RE.items():
        if patron.search(texto):
            valores.setdefault(campo, valor)
    if RE_MUDO.search(texto):
        valores.setdefault("voz", False)
    if p.solo_animacion and valores.get("voz") is None:
        return Ajuste(solo_animacion=True, segundos=p.segundos)
    if valores.get("voz") is False:  # sin audio no van subtitulos, salvo que los pida
        valores.setdefault("subtitulos", False)
    if valores.get("voz") is True:
        valores.setdefault("subtitulos", True)
    escena = escena_pedida(texto)
    return Ajuste(**valores, otras_imagenes=bool(RE_OTRAS_IMAGENES.search(texto)),
                  otro_guion=bool(RE_OTRO_GUION.search(texto)), segundos=None if escena else p.segundos,
                  escena=escena)


RE_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])")


def id_citado(mensaje: dict) -> str | None:
    """El ID de proyecto del mensaje del bot al que responde el editor (plan, reel o texto para publicar)."""
    citado = mensaje.get("reply_to_message") or {}
    m = RE_ID.search(citado.get("text") or citado.get("caption") or "")
    return m.group(0) if m else None
