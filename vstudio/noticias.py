"""Las noticias de GTA VI de las ultimas horas, de varias fuentes (reels automaticos).

Feeds RSS/Atom de medios de videojuegos y de Bing Noticias (links directos; Google News cifra los suyos). Se
bajan con red.descargar (solo IPs publicas, tope de tamano y tiempo) y se parsean con lxml sin red ni entidades
(XXE). Quedan las que hablan de GTA VI, de las ultimas horas, sin links ni titulos repetidos. Que dos medios
cuenten lo mismo lo decide despues el Qwen (automatico.py).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit, urlunsplit

import lxml.html
from lxml import etree

from . import red

log = logging.getLogger(__name__)

BING = "https://www.bing.com/news/search?format=rss&q="
ROCKSTAR = "Rockstar Games (oficial)"
FUENTES = (  # en Bing, una consulta con OR devuelve el RSS vacio: va una por termino (12 notas cada una)
    ("Bing Noticias", BING + "%22GTA+VI%22&setlang=es&cc=MX"),
    ("Bing Noticias", BING + "%22GTA+6%22&setlang=es&cc=MX"),
    ("Bing News", BING + "%22GTA+6%22&setlang=en&cc=US"),
    ("3DJuegos", "https://www.3djuegos.com/feedburner.xml"),
    ("Vandal", "https://vandal.elespanol.com/xml.cgi"),
    ("Eurogamer.es", "https://www.eurogamer.es/feed"),
    ("IGN", "https://feeds.feedburner.com/ign/games-all"),
    ("GamesRadar", "https://www.gamesradar.com/feeds.xml"),
    ("VGC", "https://www.videogameschronicle.com/feed/"),
    # lo oficial: el Newswire de rockstargames.com no tiene RSS (se arma con una API interna), pero cada anuncio
    # de GTA VI sale en su canal de YouTube, que si tiene feed
    (ROCKSTAR, "https://www.youtube.com/feeds/videos.xml?channel_id=UC6VcWc1rAoWdBCM0JxrRQ3A"),
)
TIPOS_FEED = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")
MAX_FEED = 4 * 1024 * 1024
MAX_RESUMEN = 400
MAX_CANDIDATAS = 60
RE_GTA = re.compile(r"\bGTA\s?(?:VI|6)\b|Grand Theft Auto (?:VI|6)\b", re.I)
# Rockstar no siempre dice "GTA VI" en el titulo ("A Night in Vice City"): de lo oficial vale lo de su mundo
RE_GTA_OFICIAL = re.compile(r"\bGTA\s?(?:VI|6)\b|Grand Theft Auto (?:VI|6)\b|Vice City|Leonida|Jason|Lucia", re.I)
PARAMETROS_DE_RASTREO = re.compile(r"^(utm_|fbclid|gclid|ref$|ocid$|cvid$)", re.I)
ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class Candidata:
    titulo: str
    url: str
    medio: str
    fecha: datetime  # con zona
    resumen: str = ""


def _parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)


def _texto(nodo) -> str:
    if nodo is None or not (nodo.text or len(nodo)):
        return ""
    crudo = "".join(nodo.itertext()) if len(nodo) else nodo.text
    try:  # las descripciones traen HTML escapado
        return " ".join(lxml.html.fromstring(f"<div>{crudo}</div>").text_content().split())
    except (etree.ParserError, ValueError):
        return " ".join(crudo.split())


def url_real(url: str) -> str:
    """Los links de Bing Noticias pasan por apiclick.aspx?url=...: el de la nota es ese parametro. Sin
    parametros de rastreo (utm_*, ocid...) para reconocer la misma nota."""
    partes = urlsplit(url.strip())
    if partes.netloc.endswith("bing.com") and "apiclick" in partes.path:
        destino = parse_qs(partes.query).get("url", [""])[0]
        if destino.startswith(("http://", "https://")):
            partes = urlsplit(destino)
    consulta = "&".join(p for p in partes.query.split("&") if p and not PARAMETROS_DE_RASTREO.match(p.split("=")[0]))
    return urlunsplit((partes.scheme, partes.netloc, partes.path, consulta, ""))


def _fecha(texto: str) -> datetime | None:
    texto = texto.strip()
    if not texto:
        return None
    try:
        fecha = parsedate_to_datetime(texto)
    except (TypeError, ValueError):
        try:
            fecha = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        except ValueError:
            return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


def leer_feed(contenido: bytes, medio: str) -> list[Candidata]:
    """Items de un RSS 2.0 o un Atom (lo que no se entiende, se saltea)."""
    try:
        raiz = etree.fromstring(contenido, parser=_parser())
    except etree.XMLSyntaxError:
        return []
    if raiz is None:
        return []
    candidatas = []
    for item in raiz.iter("item", f"{ATOM}entry"):
        atom = item.tag == f"{ATOM}entry"
        titulo = _texto(item.find(f"{ATOM}title" if atom else "title"))
        if atom:
            enlace = next((e.get("href", "") for e in item.findall(f"{ATOM}link") if e.get("rel", "alternate")
                           == "alternate"), "")
            fecha = _fecha(_texto(item.find(f"{ATOM}published")) or _texto(item.find(f"{ATOM}updated")))
            resumen = (_texto(item.find(f"{ATOM}summary")) or _texto(item.find(f"{ATOM}content"))
                       or next((_texto(d) for d in item.iter() if isinstance(d.tag, str)
                                and etree.QName(d).localname == "description"), ""))  # YouTube: media:description
        else:
            enlace = _texto(item.find("link"))
            fecha = _fecha(_texto(item.find("pubDate")))
            resumen = _texto(item.find("description"))
        # Bing dice de que medio es cada nota en <News:Source>
        origen = next((_texto(h) for h in item if isinstance(h.tag, str) and etree.QName(h).localname == "Source"),
                      "")
        if titulo and enlace.startswith(("http://", "https://")) and fecha:
            candidatas.append(Candidata(titulo=titulo, url=url_real(enlace), medio=origen or medio, fecha=fecha,
                                        resumen=resumen[:MAX_RESUMEN]))
    return candidatas


def _clave_titulo(titulo: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", titulo.lower()).split())


def filtrar(candidatas: list[Candidata], ahora: datetime, horas: float) -> list[Candidata]:
    """Las de GTA VI, de las ultimas `horas`, sin links ni titulos repetidos; las mas nuevas primero."""
    desde = ahora - timedelta(hours=horas)
    vistas_url, vistos_titulos, quedan = set(), set(), []
    for c in sorted(candidatas, key=lambda c: c.fecha, reverse=True):
        if not (desde <= c.fecha <= ahora + timedelta(hours=1)):
            continue
        if not (RE_GTA_OFICIAL if c.medio == ROCKSTAR else RE_GTA).search(f"{c.titulo} {c.resumen}"):
            continue
        clave = _clave_titulo(c.titulo)
        if c.url in vistas_url or clave in vistos_titulos:
            continue
        vistas_url.add(c.url)
        vistos_titulos.add(clave)
        quedan.append(c)
    return quedan[:MAX_CANDIDATAS]


def buscar(ahora: datetime, horas: float = 30.0, fuentes=FUENTES, descargar=red.descargar) -> list[Candidata]:
    """Junta todas las fuentes (una que falla no frena a las otras)."""
    todas: list[Candidata] = []
    for medio, url in fuentes:
        try:
            todas += leer_feed(descargar(url, MAX_FEED, TIPOS_FEED).contenido, medio)
        except Exception as exc:  # noqa: BLE001 - una fuente caida no corta la busqueda
            log.warning("no pude leer %s (%s): %s", medio, url, exc)
    return filtrar(todas, ahora, horas)
