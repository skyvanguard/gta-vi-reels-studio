"""Arma la Fuente de un proyecto: nota (texto, fecha, imagenes) + fotos que mande el usuario."""
from __future__ import annotations

import codecs
import logging
import re
from datetime import date
from itertools import zip_longest
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx
import lxml.html
import trafilatura
from PIL import Image

from . import biblioteca, imagenes, red, tomas
from .modelos import Fuente

log = logging.getLogger(__name__)

MAX_HTML = 5 * 1024 * 1024
MAX_IMAGEN = 15 * 1024 * 1024
MAX_CANDIDATAS = 12
MAX_IMAGENES = 16
# con biblioteca, la nota ocupa como maximo la mitad de los lugares: las paginas traen fotos de otras notas y
# publicidad (una vez una nota lleno los 10 lugares y el plan uso solo 2 imagenes)
PARTE_NOTA = 0.5
MIN_NOTA = 3
MIN_TEXTO_PEGADO = 200  # caracteres: con menos, si el link no se puede leer no hay de que hablar
# con menos, lo que salio no es la nota: el recuadro de suscripcion de GamesRadar (261) o MSN, que arma el texto con
# JavaScript (0); una vez dos reels automaticos salieron del titulo solo
MIN_TEXTO_NOTA = 500
MAX_SNIFF = 64 * 1024  # donde se busca el <meta charset> (el de Vandal no esta en los primeros 3 KB)
RE_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE)
# como hacen los navegadores (WHATWG): "iso-8859-1" y "ascii" se leen como windows-1252, que ademas trae las
# comillas tipograficas y la raya
COMO_WINDOWS_1252 = frozenset(("iso-8859-1", "iso8859-1", "iso_8859-1", "latin1", "latin-1", "l1", "cp819",
                               "ascii", "us-ascii"))
# tomas de video real y fotos de la noticia (150 de 168 escenas salian de las mismas fotos oficiales)
PARTE_TOMAS = 0.45  # de las imagenes que ve el planificador, cuantas son tomas de video
MIN_TOMAS = 3
TOMAS_DE_MAS = 2  # candidatas de sobra, por si alguna se descarta (cuadro casi igual a otro)
MAX_FOTOS_NOTICIA = 3
MAX_CANDIDATAS_NOTICIA = 8  # las que mira el Qwen
POR_BUSQUEDA = 4
PREFIJO_TOMA, PREFIJO_FOTO = "toma", "foto"
ROTULO_TOMA, ROTULO_FOTO = "TOMA DE VIDEO", "FOTO DE LA NOTICIA"
CONSULTAS_IMAGENES = ("GTA VI official screenshot", "GTA 6 trailer screenshot", "GTA 6 Vice City",
                      "GTA 6 Leonida", "GTA VI Jason Lucia")
MAX_TEXTO = 8000
DISTANCIA_DUPLICADO = 5
RE_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
RE_DESCARTE = re.compile(r"logo|icon|avatar|sprite|badge|emoji|pixel|banner-ad|/ads?/|placeholder", re.I)
EXT_DESCARTE = (".svg", ".gif", ".ico")


class NotaSinTexto(ValueError):
    """La pagina se bajo pero no trae el texto de la nota."""


def extraer_url(texto: str) -> str | None:
    m = RE_URL.search(texto or "")
    return m.group(0).rstrip(").,;") if m else None


def sin_url(texto: str) -> str:
    return RE_URL.sub("", texto or "").strip()


def _mejor_de_srcset(srcset: str) -> str | None:
    mejor, mejor_w = None, -1
    for parte in srcset.split(","):
        trozos = parte.strip().split()
        if not trozos:
            continue
        w = int(trozos[1][:-1]) if len(trozos) > 1 and trozos[1].endswith("w") and trozos[1][:-1].isdigit() else 0
        if w > mejor_w:
            mejor, mejor_w = trozos[0], w
    return mejor


def urls_de_imagenes(html: str, url_base: str) -> list[str]:
    """og:image primero y despues las <img> en orden de aparicion, sin logos/iconos ni repetidas."""
    doc = lxml.html.fromstring(html)
    crudas = doc.xpath("//meta[@property='og:image' or @name='twitter:image']/@content")
    for img in doc.xpath("//img"):
        candidata = (_mejor_de_srcset(img.get("srcset", "")) or img.get("data-src") or img.get("src") or "")
        crudas.append(candidata)
    vistas, salida = set(), []
    for cruda in crudas:
        if not cruda or cruda.startswith("data:"):
            continue
        absoluta = urljoin(url_base, cruda.strip())
        limpia = absoluta.split("#")[0]
        if limpia in vistas or RE_DESCARTE.search(limpia) or limpia.lower().split("?")[0].endswith(EXT_DESCARTE):
            continue
        vistas.add(limpia)
        salida.append(limpia)
    return salida[:MAX_CANDIDATAS]


def dias_de_antiguedad(fecha_iso: str, hoy: date) -> int | None:
    try:
        return (hoy - date.fromisoformat(fecha_iso[:10])).days
    except ValueError:
        return None


class _Coleccion:
    """Junta imagenes validas y no repetidas dentro de la carpeta del proyecto."""

    def __init__(self, carpeta: Path, maximo: int = MAX_IMAGENES):
        self.carpeta, self.huellas, self.archivos, self.maximo = carpeta, [], [], maximo
        self.descripciones: list[str] = []  # lo que muestra cada imagen, si se sabe (las oficiales)

    def lleno(self) -> bool:
        return len(self.archivos) >= self.maximo

    def agregar(self, datos: bytes, prefijo: str, descripcion: str = "") -> bool:
        if self.lleno():
            return False
        try:
            img = imagenes.normalizar(datos)
        except imagenes.ImagenInvalida as exc:
            log.info("imagen descartada (%s): %s", prefijo, exc)
            return False
        h = imagenes.huella(img)
        if any(imagenes.distancia_huella(h, otra) <= DISTANCIA_DUPLICADO for otra in self.huellas):
            return False
        nombre = f"{prefijo}_{len(self.archivos) + 1}.jpg"
        imagenes.guardar_jpeg(img, self.carpeta / nombre)
        self.huellas.append(h)
        self.archivos.append(nombre)
        self.descripciones.append(descripcion)
        return True


def _codec(nombre: str) -> str:
    nombre = nombre.strip().strip("\"'").lower()
    if not nombre:
        return ""
    if nombre in COMO_WINDOWS_1252:
        return "cp1252"
    try:
        return codecs.lookup(nombre).name
    except LookupError:
        return ""


def decodificar_html(contenido: bytes, declarado: str = "") -> str:
    """El HTML como texto, con la codificacion que declara el servidor (o si no, la pagina). Vandal manda
    ISO-8859-1: leido siempre como UTF-8, cada tilde y enie llegaba al Qwen como U+FFFD.
    Sin declaracion: UTF-8 y, si no lo es, windows-1252."""
    if contenido.startswith(codecs.BOM_UTF8):
        return contenido.decode("utf-8-sig", errors="replace")
    meta = RE_META_CHARSET.search(contenido[:MAX_SNIFF])
    for nombre in (declarado, meta.group(1).decode("ascii", errors="ignore") if meta else ""):
        codec = _codec(nombre)
        if codec:
            return contenido.decode(codec, errors="replace")
    try:
        return contenido.decode("utf-8")
    except UnicodeDecodeError:
        return contenido.decode("cp1252", errors="replace")


def extraer_texto(html: str, url: str) -> str:
    """Primero el modo estricto (deja afuera menues y publicidad); si casi no saca nada, el amplio: en GamesRadar
    el estricto toma el recuadro de suscripcion como si fuera la nota y el amplio trae la nota entera."""
    texto = trafilatura.extract(html, url=url, include_comments=False, favor_precision=True) or ""
    if len(texto) >= MIN_TEXTO_NOTA:
        return texto
    amplio = trafilatura.extract(html, url=url, include_comments=False, favor_recall=True) or ""
    return amplio if len(amplio) > len(texto) else texto


def leer_articulo(url: str) -> tuple[str, str, str, list[str]]:
    """Devuelve (titulo, fecha_iso, texto, urls_de_imagenes) de una nota."""
    resp = red.descargar(url, MAX_HTML, ("text/html", "application/xhtml"))
    html = decodificar_html(resp.contenido, resp.charset)
    meta = trafilatura.extract_metadata(html, default_url=resp.url_final)
    texto = extraer_texto(html, resp.url_final)
    titulo = (meta.title if meta else "") or ""
    fecha = (meta.date if meta else "") or ""
    return titulo, fecha, texto[:MAX_TEXTO], urls_de_imagenes(html, resp.url_final)


def _bajar_imagenes(urls: list[str], coleccion: _Coleccion, prefijo: str) -> None:
    for url_img in urls:
        if coleccion.lleno():
            return
        try:
            resp = red.descargar(url_img, MAX_IMAGEN, ("image/",))
        except (red.UrlRechazada, httpx.HTTPError, OSError) as exc:  # una imagen rota no frena la nota
            log.info("no se pudo bajar %s: %s", url_img, exc)
            continue
        coleccion.agregar(resp.contenido, prefijo)


def buscar_imagenes(buscador_url: str, consultas: tuple[str, ...], por_consulta: int = 10) -> list[str]:
    """URLs de imagenes grandes desde el SearXNG local (las descargas despues pasan por red.py)."""
    urls: list[str] = []
    for consulta in consultas:
        try:
            resp = httpx.get(f"{buscador_url}/search", params={"q": consulta, "categories": "images",
                                                                "format": "json"}, timeout=30, trust_env=False)
            resultados = resp.json().get("results", [])
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("busqueda de imagenes '%s' fallo: %s", consulta, exc)
            continue
        for r in resultados[:por_consulta] if isinstance(resultados, list) else []:
            url = (r.get("img_src") or "") if isinstance(r, dict) else ""
            if url.startswith(("http://", "https://")) and url not in urls and not RE_DESCARTE.search(url):
                urls.append(url)
    return urls


def _sumar_oficiales(coleccion: _Coleccion, biblioteca_dir: Path, usos: biblioteca.Usos, nota: str,
                     extra: int = 0) -> None:
    """Completa con imagenes oficiales: las de lo que nombra la nota y, el resto, las menos usadas. `extra`:
    candidatas de mas, por si algunas se descartan (las del plan anterior, con "otras imágenes")."""
    faltan = coleccion.maximo - len(coleccion.archivos)
    if faltan <= 0:
        return
    usadas = []
    for oficial in biblioteca.elegir(biblioteca.escanear(biblioteca_dir), nota, faltan + extra, usos.leer()):
        try:
            datos = oficial.ruta.read_bytes()
        except OSError as exc:
            log.warning("no pude leer %s: %s", oficial.ruta, exc)
            continue
        if coleccion.agregar(datos, "oficial", oficial.etiqueta):
            usadas.append(oficial.relativa)
    if usadas:
        usos.marcar(usadas)


def cupo_de_tomas(catalogo: tomas.Catalogo | None, maximo_imagenes: int) -> int:
    """Cuantas de las imagenes que ve el planificador son tomas de video (0 si no hay biblioteca de tomas)."""
    if catalogo is None or not catalogo.tomas:
        return 0
    return max(0, min(len(catalogo.tomas), max(MIN_TOMAS, round(maximo_imagenes * PARTE_TOMAS)),
                      maximo_imagenes - 2))


def _sumar_tomas(coleccion: _Coleccion, catalogo: tomas.Catalogo, cupo: int, nota: str, carpeta: Path,
                 excluir: frozenset[str] = frozenset()) -> None:
    """Suma el cuadro del medio de cada toma elegida y anota en el proyecto cual es cual. No las marca como
    usadas: eso pasa cuando el plan las usa de verdad. `excluir`: las tomas del plan anterior."""
    tope = coleccion.maximo
    coleccion.maximo = min(tope, len(coleccion.archivos) + cupo)
    mapa: dict[str, str] = {}
    try:
        for toma in tomas.elegir(list(catalogo.tomas), nota, cupo + TOMAS_DE_MAS, catalogo.usadas(),
                                 excluir=excluir):
            try:
                datos = toma.cuadro.read_bytes()
            except OSError as exc:
                log.warning("no pude leer %s: %s", toma.cuadro, exc)
                continue
            rotulo = f"{ROTULO_TOMA} de {toma.segundos:.1f} s, accion {toma.energia} de 3: {toma.sujeto}"
            if coleccion.agregar(datos, PREFIJO_TOMA, rotulo):
                mapa[coleccion.archivos[-1]] = toma.relativa
    finally:
        coleccion.maximo = tope
    tomas.anotar_en_proyecto(carpeta, mapa)


def _sumar_fotos_de_la_noticia(coleccion: _Coleccion, buscador_url: str, busquedas: tuple[str, ...], titulo: str,
                               filtro: Callable[[str, list[Image.Image]], list[bool]]) -> None:
    """Busca lo que la nota nombra, baja unas pocas candidatas (de a una por busqueda, para que entren todas) y
    suma las que el filtro acepta."""
    if not busquedas:
        return
    listas = [buscar_imagenes(buscador_url, (b,), POR_BUSQUEDA) for b in busquedas]
    urls = list(dict.fromkeys(u for grupo in zip_longest(*listas) for u in grupo if u))
    candidatas: list[tuple[bytes, Image.Image]] = []
    for url_img in urls:
        if len(candidatas) >= MAX_CANDIDATAS_NOTICIA:
            break
        try:
            datos = red.descargar(url_img, MAX_IMAGEN, ("image/",)).contenido
            candidatas.append((datos, imagenes.normalizar(datos)))
        except (red.UrlRechazada, httpx.HTTPError, OSError, imagenes.ImagenInvalida) as exc:
            log.info("candidata descartada %s: %s", url_img, exc)
    if not candidatas:
        return
    buenas = filtro(titulo, [img for _, img in candidatas])
    for (datos, _), sirve in zip(candidatas, buenas):
        if sirve:
            coleccion.agregar(datos, PREFIJO_FOTO, ROTULO_FOTO)


def preparar_fuente(texto_usuario: str, fotos: list[bytes], carpeta: Path, minimo_imagenes: int = 0,
                    maximo_imagenes: int = MAX_IMAGENES, buscador_url: str = "", biblioteca_dir: Path | None = None,
                    usos: biblioteca.Usos | None = None, excluir: tuple[Path, ...] = (),
                    guion: tuple[str, ...] = (), exigir_nota: bool = False, tomas_cat: tomas.Catalogo | None = None,
                    consultas: Callable[[str, str], tuple[str, ...]] | None = None,
                    filtro: Callable[[str, list[Image.Image]], list[bool]] | None = None) -> Fuente:
    """Si el editor manda fotos, el reel sale solo con ellas (antes, una foto mandada
    para animarla se mezclaba con 9 oficiales). Sin fotos: las de la nota, las fotos de lo que la nota nombra, las tomas
    de video real, las oficiales de la biblioteca y, si aun no alcanzan (sin biblioteca), busca mas en SearXNG.

    `tomas_cat`: la biblioteca de tomas de video (se ofrece el cuadro del medio de cada una). `consultas` (titulo,
    texto -> que buscar) y `filtro` (titulo, candidatas -> cuales sirven): las fotos de la noticia; van juntas.

    `excluir`: imagenes que no tienen que repetirse (las del plan anterior). Si el link no se puede leer (o la
    pagina no trae el texto de la nota) pero el editor pego la nota, se arma con lo pegado (una vez Forbes
    dio 403 y el pedido fallo entero). `exigir_nota` (reels automaticos): sin el texto de la nota no se arma; lo
    "pegado" ahi es el titulo y el resumen del buscador, y con eso solo salen reels sin contenido."""
    carpeta.mkdir(parents=True, exist_ok=True)
    coleccion = _Coleccion(carpeta, maximo_imagenes)
    coleccion.huellas += [h for h in map(_huella_de, excluir) if h is not None]
    for datos in fotos:
        coleccion.agregar(datos, "usuario")
    url = extraer_url(texto_usuario)
    titulo = fecha = texto = aviso = ""
    urls: list[str] = []
    if url:
        try:
            titulo, fecha, texto, urls = leer_articulo(url)
            if len(texto) < MIN_TEXTO_NOTA:
                raise NotaSinTexto(f"la página no deja leer la nota (solo {len(texto)} caracteres): pegá el texto "
                                   "junto al link")
        except (httpx.HTTPError, red.UrlRechazada, ValueError) as exc:
            # con fotos del editor (una foto + un link de X para animarla) la nota no hace falta
            alcanza_con_fotos = bool(fotos) and isinstance(exc, NotaSinTexto)
            if exigir_nota or (len(sin_url(texto_usuario)) < MIN_TEXTO_PEGADO and not alcanza_con_fotos):
                raise
            log.warning("no pude leer %s (%s): sigo con lo que mando el editor", url, exc)
            texto = ""  # lo poco que salio no es la nota (suscripcion, menues)
            aviso = f"No pude leer el link ({str(exc)[:80]}): usé " + (
                "tus fotos y lo que escribiste." if alcanza_con_fotos else "el texto que pegaste.")
    if coleccion.archivos and fotos:
        return _fuente(url, titulo, fecha, texto, texto_usuario, coleccion, guion, aviso)
    nota = f"{titulo} {texto} {texto_usuario}"
    cupo_tomas = cupo_de_tomas(tomas_cat, maximo_imagenes)
    resto = maximo_imagenes - cupo_tomas  # lo que queda para las imagenes de la nota, las fotos y las oficiales
    if url:
        if (biblioteca_dir and usos) or cupo_tomas:
            coleccion.maximo = min(resto, len(coleccion.archivos) + max(MIN_NOTA, int(resto * PARTE_NOTA)))
        _bajar_imagenes(urls, coleccion, "nota")
    if buscador_url and consultas and filtro:
        coleccion.maximo = min(resto, len(coleccion.archivos) + MAX_FOTOS_NOTICIA)
        _sumar_fotos_de_la_noticia(coleccion, buscador_url, consultas(titulo, texto or sin_url(texto_usuario)),
                                   titulo or sin_url(texto_usuario)[:200], filtro)
    coleccion.maximo = maximo_imagenes
    if tomas_cat is not None and cupo_tomas:  # "otras imágenes": las tomas del plan anterior no vuelven
        ya_vistas = frozenset(rel for anterior in {ruta.parent for ruta in excluir}
                              for rel in tomas.mapa_del_proyecto(anterior).values())
        _sumar_tomas(coleccion, tomas_cat, cupo_tomas, nota, carpeta, ya_vistas)
    if biblioteca_dir and usos:
        _sumar_oficiales(coleccion, biblioteca_dir, usos, nota, extra=len(excluir))
    if buscador_url and len(coleccion.archivos) < minimo_imagenes:
        _bajar_imagenes(buscar_imagenes(buscador_url, CONSULTAS_IMAGENES), coleccion, "busqueda")
    return _fuente(url, titulo, fecha, texto, texto_usuario, coleccion, guion, aviso)


def _huella_de(ruta: Path):
    try:
        return imagenes.huella(imagenes.normalizar(ruta.read_bytes()))
    except (OSError, imagenes.ImagenInvalida) as exc:
        log.info("no pude leer %s para excluirla: %s", ruta, exc)
        return None


def _fuente(url: str | None, titulo: str, fecha: str, texto: str, texto_usuario: str,
            coleccion: _Coleccion, guion: tuple[str, ...] = (), aviso: str = "") -> Fuente:
    descripciones = coleccion.descripciones + [""] * (len(coleccion.archivos) - len(coleccion.descripciones))
    return Fuente(url=url or "", titulo=titulo, fecha=fecha, texto=texto, imagenes=tuple(coleccion.archivos),
                  nota_usuario=sin_url(texto_usuario), descripciones=tuple(descripciones), guion=guion, aviso=aviso)


# los que mas aparecen, con su nombre como lo escribe el medio (el resto: el dominio sin "www.")
MEDIOS = {"rockstargames.com": "Rockstar Games", "ign.com": "IGN", "vandal.elespanol.com": "Vandal",
          "3djuegos.com": "3DJuegos", "eurogamer.es": "Eurogamer", "eurogamer.net": "Eurogamer",
          "gamesradar.com": "GamesRadar", "videogameschronicle.com": "VGC", "forbes.com": "Forbes",
          "infobae.com": "Infobae", "xataka.com": "Xataka", "xataka.com.mx": "Xataka", "vidaextra.com": "VidaExtra",
          "hobbyconsolas.com": "HobbyConsolas", "levelup.com": "LevelUp", "kotaku.com": "Kotaku",
          "polygon.com": "Polygon", "theverge.com": "The Verge", "pcgamer.com": "PC Gamer", "youtube.com": "YouTube"}
RE_VIA = re.compile(r"\s+(?:on|en|vía|via)\s+MSN$|\s*\(oficial\)$", re.I)


def nombre_del_medio(fuente: Fuente) -> str:
    """Lo que dice el sello "FUENTE: ...": el medio que trajo la busqueda o, si el editor mando un link, el de
    su dominio. Vacio si no hay de donde (texto pegado sin link, fotos)."""
    if fuente.medio:
        return RE_VIA.sub("", fuente.medio).strip()
    if not fuente.url:
        return ""
    host = (urlsplit(fuente.url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    return next((nombre for dominio, nombre in MEDIOS.items() if host == dominio or host.endswith("." + dominio)),
                host)
