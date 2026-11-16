"""Como se hace el clip de cada escena segun de donde sale su imagen (video real, fotos de la
noticia y que la imagen cambie mas seguido).

- Una TOMA DE VIDEO de la biblioteca: se recorta en vertical. Ya es el clip: no pasa por la GPU.
- Una FOTO DE LA NOTICIA (algo real: una persona, un objeto, un lugar): un paneo, sin GPU. No se anima con IA: no
  se le inventa movimiento a una persona real.
- El resto (imagenes del juego): la anima Wan en el turno de GPU, como siempre.
Ademas, cada escena lleva tomas extra que siguen a su clip: la imagen cambia cada ~3 s en vez de cada 5.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from PIL import Image

from . import imagenes, tomas, visuales
from .fuentes import PREFIJO_FOTO
from .modelos import MEDIO_FOTO, MEDIO_IA, MEDIO_TOMA, Escena

log = logging.getLogger(__name__)

ZOOM_MAX_TOMA = 1.3  # el video se recorta de 1440 de alto: mas zoom se ve blando
COBERTURA_S = 5.5  # lo que dura una escena tipica: las tomas extra se suman hasta cubrirlo
MAX_EXTRAS = 2  # despues de un clip de IA o de una foto
MAX_EXTRAS_TOMA = 3  # despues de una toma (que suele durar 2-3 s)
INTENTOS_OTRA_TOMA = 3  # al cambiar una escena: si una toma no se puede recortar, se prueba con la siguiente


def medio_de(origen: str, mapa: dict[str, tomas.Toma]) -> str:
    if origen in mapa:
        return MEDIO_TOMA
    return MEDIO_FOTO if origen.startswith(f"{PREFIJO_FOTO}_") else MEDIO_IA


def clip_sin_gpu(carpeta: Path, n: int, medio: str, recorte: Path, toma: tomas.Toma | None,
                 caja: imagenes.Caja, segundos_foto: float) -> str:
    """Arma clip_N.mp4 de una escena de video real o de foto. Devuelve el nombre del archivo ("" si no se pudo:
    la escena queda para que la anime la IA, como antes)."""
    destino = carpeta / f"clip_{n}.mp4"
    try:
        if medio == MEDIO_TOMA and toma is not None:
            visuales.recortar_toma(toma.ruta, destino, caja)
        elif medio == MEDIO_FOTO:
            visuales.foto_animada(recorte, destino, segundos_foto, acercar=n % 2 == 1)
        else:
            return ""
    except (visuales.VisualError, OSError) as exc:
        log.warning("escena %d: no pude armar el clip sin GPU (%s): la anima la IA", n, exc)
        return ""
    return destino.name


def _aspecto(toma: tomas.Toma) -> float:
    with Image.open(toma.cuadro) as img:
        return img.width / img.height


def sumar_extras(escenas: tuple[Escena, ...], carpeta: Path, catalogo: tomas.Catalogo,
                 principales: dict[int, tomas.Toma], segundos_clip: float,
                 excluir: frozenset[str] = frozenset()) -> tuple[tuple[Escena, ...], dict[str, str]]:
    """A cada escena, las tomas que siguen a su clip (las que mas tienen que ver con lo que dice y hace mas que no
    salen; ninguna se repite en el reel). Devuelve las escenas y que toma de la biblioteca es cada archivo del
    reel (los clips de las principales y las extra). `excluir`: tomas que ya estan en otra parte del reel."""
    por_archivo = {e.clip: principales[e.n].relativa for e in escenas if e.n in principales}
    # las probadas incluyen las que fallaron: no se vuelven a probar en este reel, pero no rotan
    probadas = set(por_archivo.values()) | set(excluir)
    disponibles, usos = list(catalogo.tomas), catalogo.usadas()
    faltan = {e.n: COBERTURA_S - min(principales[e.n].segundos if e.n in principales else segundos_clip,
                                     tomas.MAX_SEGMENTO_S) for e in escenas}
    archivos: dict[int, list[str]] = {e.n: [] for e in escenas}
    # de a una por escena y por ronda: con pocas tomas, les toca a todas antes de que alguna tenga dos
    for _ in range(MAX_EXTRAS_TOMA):
        for e in escenas:
            tope = MAX_EXTRAS_TOMA if e.n in principales else MAX_EXTRAS
            if faltan[e.n] <= 0 or len(archivos[e.n]) >= tope:
                continue
            for toma in tomas.acompanar(disponibles, f"{e.narracion} {e.sujeto}", probadas, usos, faltan[e.n], 1):
                destino = carpeta / f"extra_{e.n}_{len(archivos[e.n]) + 1}.mp4"
                probadas.add(toma.relativa)
                try:
                    visuales.recortar_toma(toma.ruta, destino,
                                           imagenes.caja_relativa(_aspecto(toma), toma.centro_x, 0.5, 1.0))
                except (visuales.VisualError, OSError) as exc:
                    log.warning("escena %d: no pude sumar la toma %s: %s", e.n, toma.relativa, exc)
                    continue
                por_archivo[destino.name] = toma.relativa
                archivos[e.n].append(destino.name)
                faltan[e.n] -= min(toma.segundos, tomas.MAX_SEGMENTO_S)
    return tuple(replace(e, extras=tuple(archivos[e.n])) for e in escenas), por_archivo


def otra_toma(escena: Escena, carpeta: Path, catalogo: tomas.Catalogo, excluir: frozenset[str], ancho: int,
              alto: int, segundos_clip: float) -> tuple[Escena, dict[str, str]]:
    """La escena con otra toma de video real como clip, y otras tomas extra (cambiar una
    sola escena sin rehacer todo el plan). El guion y la voz no cambian. `excluir`: las tomas que ya estan en el
    reel. Devuelve la escena y que toma es cada archivo nuevo. Levanta ValueError si no hay tomas libres."""
    candidatas = tomas.acompanar(list(catalogo.tomas), f"{escena.narracion} {escena.sujeto}", set(excluir),
                                 catalogo.usadas(), COBERTURA_S, INTENTOS_OTRA_TOMA)
    for toma in candidatas:
        temporal = carpeta / f"clip_{escena.n}.nuevo.mp4"
        try:
            with Image.open(toma.cuadro) as img:
                cuadro = img.convert("RGB")
            caja = imagenes.caja_relativa(cuadro.width / cuadro.height, toma.centro_x, 0.5, 1.0)
            visuales.recortar_toma(toma.ruta, temporal, caja)
            temporal.replace(carpeta / f"clip_{escena.n}.mp4")  # primero el clip: la imagen es solo la tira
            imagenes.recorte_vertical(cuadro, toma.centro_x, ancho, alto, 1.0, 0.5).save(carpeta / escena.imagen)
        except (visuales.VisualError, OSError) as exc:
            log.warning("escena %d: no pude poner la toma %s: %s", escena.n, toma.relativa, exc)
            temporal.unlink(missing_ok=True)
            continue
        nueva = replace(escena, clip=f"clip_{escena.n}.mp4", medio=MEDIO_TOMA, origen=toma.relativa,
                        sujeto=toma.sujeto, centro_x=toma.centro_x, centro_y=0.5, zoom=1.0, motion_prompt="",
                        negative_extra="", revisada=True, error="", extras=())
        (nueva,), por_archivo = sumar_extras((nueva,), carpeta, catalogo, {escena.n: toma}, segundos_clip,
                                            excluir=excluir | {toma.relativa})
        return nueva, por_archivo
    raise ValueError("no quedan tomas de video real sin usar para esa escena")
