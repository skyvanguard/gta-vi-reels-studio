"""Biblioteca de tomas de video real: los trailers oficiales de Rockstar y los clips que suma el editor.

Por que: casi todas las escenas salian de las mismas fotos oficiales, animadas con un movimiento minimo.
Cada video se corta en tomas (ingesta.py), el Qwen dice que se ve en
cada una y aca se eligen: unas se le ofrecen al planificador como si fueran imagenes (su cuadro del medio) y otras
acompanan a cada escena para que la imagen cambie cada ~3 s. No usan GPU: el clip ya existe, solo se recorta.

En disco: <tomas_dir>/<fuente>/toma_NNNN.mp4 + .jpg (el cuadro del medio) + indice.json. Las carpetas que empiezan
con "_" (los videos originales, la entrada del editor) no son tomas.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .biblioteca import Usos

log = logging.getLogger(__name__)

INDICE = "indice.json"
MAPA_PROYECTO = "tomas.json"  # en la carpeta del proyecto: imagen ofrecida -> toma de la biblioteca
USADAS_PROYECTO = "tomas_usadas.json"  # clip o toma extra del reel -> toma de la biblioteca (para no repetirlas)
MAX_POR_FUENTE = 3  # de las que se le ofrecen al planificador: que no salgan todas del mismo trailer
MAX_SEGMENTO_S = 3.0  # lo que se muestra de corrido de un mismo clip dentro de una escena
MIN_PALABRA = 4
RE_ARCHIVO = re.compile(r"toma_\d+\.mp4")  # lo unico que puede nombrar un indice: nada de rutas
VACIAS = frozenset(("para", "pero", "como", "esto", "esta", "este", "desde", "hasta", "sobre", "entre", "tiene",
                    "tienen", "seis", "juego", "video", "toma", "oficial", "rockstar", "games", "grand", "theft",
                    "auto"))


@dataclass(frozen=True)
class Toma:
    ruta: Path
    cuadro: Path  # el cuadro del medio, en JPEG: lo que ve el planificador
    relativa: str  # clave estable ("launch_trailer/toma_0012.mp4") para recordar cuando se uso
    fuente: str  # de que video sale (la carpeta)
    segundos: float
    sujeto: str  # que se ve, en una frase
    etiquetas: tuple[str, ...]  # personajes, lugares, vehiculos, accion
    energia: int  # 0 (quieta) a 3 (accion): la escena 1 quiere la mas movida
    centro_x: float  # donde esta lo importante (0-1), para el recorte vertical cuando acompana


def plano(texto: str) -> str:
    """Minusculas, sin tildes y sin signos."""
    sin_tildes = unicodedata.normalize("NFKD", texto.lower())
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sin_tildes).split())


def palabras_clave(texto: str) -> set[str]:
    return {w for w in plano(texto).split() if len(w) >= MIN_PALABRA and w not in VACIAS}


def _desde_indice(carpeta: Path, raiz: Path, dato: dict) -> Toma | None:
    try:
        archivo = str(dato["archivo"])
        ruta = carpeta / archivo
        cuadro = ruta.with_suffix(".jpg")
        segundos, energia, centro_x = float(dato["segundos"]), float(dato.get("energia", 1)), \
            float(dato.get("centro_x", 0.5))
        if not RE_ARCHIVO.fullmatch(archivo) or not all(map(math.isfinite, (segundos, energia, centro_x))) \
                or segundos <= 0:
            raise ValueError("valores fuera de rango")
        if not ruta.is_file() or not cuadro.is_file():
            return None
        return Toma(ruta=ruta, cuadro=cuadro, relativa=ruta.relative_to(raiz).as_posix(), fuente=carpeta.name,
                    segundos=segundos, sujeto=str(dato.get("sujeto", "")),
                    etiquetas=tuple(str(e) for e in dato.get("etiquetas", ())),
                    energia=min(max(round(energia), 0), 3), centro_x=min(max(centro_x, 0.0), 1.0))
    except (KeyError, TypeError, ValueError, OverflowError):  # un registro malo no puede frenar todos los planes
        log.warning("toma mal anotada en %s: %r", carpeta, dato)
        return None


def leer_indice(carpeta: Path) -> list[dict]:
    try:
        datos = json.loads((carpeta / INDICE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [d for d in datos if isinstance(d, dict)] if isinstance(datos, list) else []


def guardar_indice(carpeta: Path, datos: list[dict]) -> None:
    carpeta.mkdir(parents=True, exist_ok=True)
    escribir(carpeta / INDICE, json.dumps(datos, ensure_ascii=False, indent=1))


def escribir(destino: Path, texto: str) -> None:
    """Escritura atomica (el temporal lleva el PID: dos procesos no se pisan el mismo archivo a medias)."""
    temporal = destino.with_name(f"{destino.name}.{os.getpid()}.tmp")
    temporal.write_text(texto, encoding="utf-8")
    temporal.replace(destino)


def escanear(raiz: Path | None) -> list[Toma]:
    """Todas las tomas usables (no falla si la carpeta no existe)."""
    if raiz is None or not raiz.is_dir():
        return []
    tomas = []
    for actual, dirs, _ in os.walk(raiz):
        dirs[:] = sorted(d for d in dirs if not d.startswith(("_", ".")))
        carpeta = Path(actual)
        tomas += [t for t in (_desde_indice(carpeta, raiz, d) for d in leer_indice(carpeta)) if t is not None]
    return tomas


def puntaje(toma: Toma, claves: set[str]) -> int:
    """Cuanto tiene que ver la toma con un texto (palabras en comun con lo que se ve en ella)."""
    return len(claves & palabras_clave(" ".join((toma.sujeto, *toma.etiquetas))))


def elegir(tomas: list[Toma], nota: str, cantidad: int, usos: dict[str, float],
           azar: random.Random | None = None, excluir: frozenset[str] = frozenset()) -> list[Toma]:
    """Las que se le ofrecen al planificador: primero las que muestran lo que la nota nombra, despues las de mas
    accion entre las que hace mas que no salen. No mas de MAX_POR_FUENTE del mismo video. `excluir`: las del plan
    anterior ("otras imágenes")."""
    azar = azar or random.Random()
    claves = palabras_clave(nota)
    tomas = [t for t in tomas if t.relativa not in excluir]
    varias_fuentes = len({t.fuente for t in tomas}) > 1
    sorteo = {t.relativa: azar.random() for t in tomas}
    orden = sorted(tomas, key=lambda t: (-puntaje(t, claves), usos.get(t.relativa, 0.0), -t.energia,
                                         sorteo[t.relativa]))
    elegidas: list[Toma] = []
    por_fuente: dict[str, int] = {}
    for toma in orden:
        if len(elegidas) >= cantidad:
            break
        if varias_fuentes and por_fuente.get(toma.fuente, 0) >= MAX_POR_FUENTE:
            continue
        elegidas.append(toma)
        por_fuente[toma.fuente] = por_fuente.get(toma.fuente, 0) + 1
    return elegidas


def acompanar(tomas: list[Toma], texto: str, excluir: set[str], usos: dict[str, float], faltan_s: float,
              maximo: int, azar: random.Random | None = None) -> list[Toma]:
    """Tomas que siguen al clip de una escena hasta cubrir `faltan_s` segundos: las que mas tienen que ver con lo
    que se dice y, a igualdad, las que hace mas que no salen. `excluir`: las que ya estan en este reel."""
    azar = azar or random.Random()
    claves = palabras_clave(texto)
    libres = [t for t in tomas if t.relativa not in excluir]
    sorteo = {t.relativa: azar.random() for t in libres}
    libres.sort(key=lambda t: (-puntaje(t, claves), usos.get(t.relativa, 0.0), sorteo[t.relativa]))
    elegidas: list[Toma] = []
    for toma in libres:
        if faltan_s <= 0 or len(elegidas) >= maximo:
            break
        elegidas.append(toma)
        faltan_s -= min(toma.segundos, MAX_SEGMENTO_S)
    return elegidas


# ---------- las tomas de un proyecto ----------
def anotar_en_proyecto(carpeta: Path, mapa: dict[str, str]) -> None:
    """Que toma de la biblioteca es cada imagen ofrecida al planificador ("toma_7.jpg" -> relativa)."""
    escribir(carpeta / MAPA_PROYECTO, json.dumps(mapa, ensure_ascii=False))


def mapa_del_proyecto(carpeta: Path) -> dict[str, str]:
    """Imagen ofrecida -> relativa de su toma, como quedo anotado ({} si el proyecto no tiene tomas)."""
    try:
        mapa = json.loads((carpeta / MAPA_PROYECTO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(nombre): str(rel) for nombre, rel in mapa.items()} if isinstance(mapa, dict) else {}


def anotar_usadas(carpeta: Path, por_archivo: dict[str, str]) -> None:
    """Que toma de la biblioteca es cada clip del reel ("clip_2.mp4", "extra_1_1.mp4" -> relativa)."""
    escribir(carpeta / USADAS_PROYECTO, json.dumps(por_archivo, ensure_ascii=False))


def usadas_del_proyecto(carpeta: Path) -> dict[str, str]:
    """Clip del reel -> relativa de su toma ({} en los proyectos de antes, que no lo anotaban)."""
    try:
        datos = json.loads((carpeta / USADAS_PROYECTO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(a): str(r) for a, r in datos.items()} if isinstance(datos, dict) else {}


def del_proyecto(carpeta: Path, tomas: list[Toma]) -> dict[str, Toma]:
    por_relativa = {t.relativa: t for t in tomas}
    return {nombre: por_relativa[rel] for nombre, rel in mapa_del_proyecto(carpeta).items() if rel in por_relativa}


@dataclass(frozen=True)
class Catalogo:
    """La biblioteca de tomas y cuando se uso cada una (para rotar)."""
    tomas: tuple[Toma, ...] = ()
    usos: Usos | None = None

    @classmethod
    def abrir(cls, raiz: Path | None, archivo_usos: Path | None) -> "Catalogo":
        return cls(tuple(escanear(raiz)), Usos(archivo_usos) if archivo_usos else None)

    def usadas(self) -> dict[str, float]:
        return self.usos.leer() if self.usos else {}

    def marcar(self, relativas: list[str]) -> None:
        if self.usos and relativas:
            self.usos.marcar(relativas)


def resumen(raiz: Path | None) -> dict[str, tuple[int, float]]:
    """Por cada video de origen: cuantas tomas y cuantos segundos."""
    salida: dict[str, tuple[int, float]] = {}
    for toma in escanear(raiz):
        cantidad, segundos = salida.get(toma.fuente, (0, 0.0))
        salida[toma.fuente] = (cantidad + 1, round(segundos + toma.segundos, 1))
    return salida
