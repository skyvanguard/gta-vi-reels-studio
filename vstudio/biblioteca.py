"""Biblioteca de imagenes oficiales (los paquetes de https://www.rockstargames.com/VI/media) para dar variedad.

Las etiquetas salen de la carpeta y del nombre de cada archivo ("People/Lucia Caminos/Lucia_Caminos_03.jpg" ->
"Lucia Caminos"). Para cada nota se eligen primero las imagenes de lo que la nota nombra (personajes, lugares,
autos de la Ultimate Edition) y el resto entre las que hace mas que no se usan, asi dos reels seguidos no repiten
fotos. Del arte oficial se usa solo la version vertical (_portrait, 9:16) y sin la variante "With Logos".
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

EXTENSIONES = (".jpg", ".jpeg", ".png")
VARIANTES_ARTE = re.compile(r"_(landscape|phone|square|tablet|ultrawide)$", re.IGNORECASE)
SUFIJO_VERTICAL = re.compile(r"_(portrait|phone(_[a-z])?)$", re.IGNORECASE)  # "Jason_and_Lucia_02_phone_B"
CON_LOGOS = re.compile(r"with_logos?", re.IGNORECASE)
NUMERO_FINAL = re.compile(r"[_ ]?\d+$")
# palabras de los nombres de archivo que no dicen de que se trata la imagen
GENERICAS = frozenset(("gtavi", "gta", "vi", "people", "places", "screenshots", "artwork", "wallpapers", "official",
                       "portrait", "edition", "benefits", "pack", "and", "the", "with", "01", "02"))
MAX_POR_GRUPO = 2  # no mas de 2 imagenes de la misma carpeta (mismo personaje o lugar) por reel
MAX_POR_TEMA = 2  # ni del mismo tema: Jason, Lucia y "Jason and Lucia" son uno solo (antes salian 6 de la pareja)
PROTAGONISTAS = ("jason", "lucia")
MIN_NOMBRADAS = 2
PARTE_NOMBRADAS = 2  # lo que la nota nombra ocupa a lo sumo la mitad: el resto rota entre los temas menos vistos
PUNTOS_FRASE = 3  # la nota nombra la etiqueta entera ("vice city", "lucia caminos")
PUNTOS_PALABRA = 1  # la nota nombra una parte ("lucia", "cheetah")


@dataclass(frozen=True)
class ImagenOficial:
    ruta: Path
    relativa: str  # clave estable para recordar cuando se uso
    grupo: str  # carpeta: personaje, lugar o paquete
    etiqueta: str  # lo que muestra, en palabras ("Lucia Caminos", "Ultimate Edition Grotti Cheetah")


def _plano(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto.lower())
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sin_tildes).split())


def _etiqueta(archivo: Path) -> str:
    nombre = SUFIJO_VERTICAL.sub("", archivo.stem)
    return " ".join(NUMERO_FINAL.sub("", nombre).replace("_", " ").split()).title()


def escanear(carpeta: Path) -> list[ImagenOficial]:
    """Las imagenes usables de la biblioteca (no falla si la carpeta no existe)."""
    if not carpeta.is_dir():
        return []
    imagenes = []
    for raiz, dirs, archivos in os.walk(carpeta):
        dirs[:] = sorted(d for d in dirs if not d.startswith(("_", ".")) and not CON_LOGOS.search(d))
        for nombre in sorted(archivos):
            ruta = Path(raiz) / nombre
            if ruta.suffix.lower() not in EXTENSIONES or VARIANTES_ARTE.search(ruta.stem):
                continue  # del arte se queda la version vertical (_portrait)
            etiqueta = _etiqueta(ruta)
            # grupo = carpeta + etiqueta: en los paquetes planos (Ultimate Edition) cada auto o local es su grupo
            grupo = f"{ruta.parent.relative_to(carpeta).as_posix()}:{etiqueta}"
            imagenes.append(ImagenOficial(ruta=ruta, relativa=ruta.relative_to(carpeta).as_posix(), grupo=grupo,
                                          etiqueta=etiqueta))
    return imagenes


def puntaje(imagen: ImagenOficial, nota_plana: str) -> int:
    """Cuanto tiene que ver la imagen con la nota (0 = nada en comun)."""
    carpeta = imagen.grupo.split(":")[0]
    frases = {_plano(imagen.etiqueta), _plano(Path(carpeta).name.replace("_", " "))}
    palabras = {w for f in frases for w in f.split() if w not in GENERICAS and len(w) >= 4}
    texto = f" {nota_plana} "
    return (sum(PUNTOS_FRASE for f in frases if len(f) >= 4 and f" {f} " in texto)
            + sum(PUNTOS_PALABRA for w in palabras if f" {w} " in texto))


class Usos:
    """Cuando se ofrecio por ultima vez cada imagen (para rotar)."""

    def __init__(self, archivo: Path):
        self.archivo = archivo

    def leer(self) -> dict[str, float]:
        try:
            datos = json.loads(self.archivo.read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in datos.items()} if isinstance(datos, dict) else {}
        except (OSError, ValueError):
            return {}

    def marcar(self, relativas: list[str], ahora: float | None = None) -> None:
        datos = self.leer()
        datos.update({r: ahora if ahora is not None else time.time() for r in relativas})
        self.archivo.parent.mkdir(parents=True, exist_ok=True)
        temporal = self.archivo.with_suffix(".tmp")
        temporal.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
        temporal.replace(self.archivo)


def tema(imagen: ImagenOficial) -> str:
    """De que trata la imagen, para repartir: la carpeta (personaje, lugar o paquete), salvo los protagonistas:
    Jason, Lucia y los dos juntos son un solo tema."""
    nombre = _plano(Path(imagen.grupo.split(":")[0]).name.replace("_", " "))  # "Vice_City" (arte) = "Vice City"
    return "protagonistas" if any(p in nombre.split() for p in PROTAGONISTAS) else nombre


def elegir(imagenes: list[ImagenOficial], nota: str, cantidad: int, usos: dict[str, float],
           azar: random.Random | None = None) -> list[ImagenOficial]:
    """Hasta la mitad, lo que la nota nombra; el resto, de los temas que hace mas que no salen (casi siempre
    salian Jason, Lucia y Vice City). Maximo MAX_POR_TEMA por tema y MAX_POR_GRUPO por carpeta."""
    azar = azar or random.Random()
    nota_plana = _plano(nota)
    temas = {i.relativa: tema(i) for i in imagenes}
    reciente: dict[str, float] = {}  # la ultima vez que se ofrecio algo de cada tema
    for i in imagenes:
        reciente[temas[i.relativa]] = max(reciente.get(temas[i.relativa], 0.0), usos.get(i.relativa, 0.0))
    puntos = {i.relativa: puntaje(i, nota_plana) for i in imagenes}
    nombradas = sorted((i for i in imagenes if puntos[i.relativa] > 0),
                       key=lambda i: (-puntos[i.relativa], usos.get(i.relativa, 0.0), azar.random()))
    sorteo = {i.relativa: azar.random() for i in imagenes}
    elegidas: list[ImagenOficial] = []
    por_grupo: dict[str, int] = {}
    por_tema: dict[str, int] = {}

    def sumar(imagen: ImagenOficial, tope: int) -> None:
        t = temas[imagen.relativa]
        if (len(elegidas) >= tope or imagen in elegidas or por_grupo.get(imagen.grupo, 0) >= MAX_POR_GRUPO
                or por_tema.get(t, 0) >= MAX_POR_TEMA):
            return
        elegidas.append(imagen)
        por_grupo[imagen.grupo] = por_grupo.get(imagen.grupo, 0) + 1
        por_tema[t] = por_tema.get(t, 0) + 1

    for imagen in nombradas:
        sumar(imagen, min(cantidad, max(MIN_NOMBRADAS, cantidad // PARTE_NOMBRADAS)))
    ultima = max(usos.values(), default=0.0)  # las del reel anterior (se marcan todas juntas) no se repiten

    def prioridad(i: ImagenOficial) -> tuple:
        t, uso = temas[i.relativa], usos.get(i.relativa, 0.0)
        return (0 < ultima <= uso, por_tema.get(t, 0), reciente[t], uso, sorteo[i.relativa])

    while len(elegidas) < cantidad:  # de a una: el tema con menos fotos en este reel y que hace mas que no sale
        libres = [i for i in imagenes if i not in elegidas and por_grupo.get(i.grupo, 0) < MAX_POR_GRUPO
                  and por_tema.get(temas[i.relativa], 0) < MAX_POR_TEMA]
        if not libres:
            break
        sumar(min(libres, key=prioridad), cantidad)
    return elegidas


# ---------- fotos que suma el editor (/biblioteca TEMA) ----------
CARPETA_EDITOR = "editor"
MAX_TEMA = 40
DISTANCIA_DUPLICADO = 5  # la misma que en fuentes: una foto casi igual a otra del tema no se guarda dos veces
RE_FUERA_DE_TEMA = re.compile(r"[^0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]+")


def nombre_de_tema(texto: str) -> str:
    """"swat  (policía)" -> "Swat Policía": solo letras, numeros y espacios (es el nombre de una carpeta)."""
    return " ".join(RE_FUERA_DE_TEMA.sub(" ", texto).split())[:MAX_TEMA].strip().title()


def agregar(biblioteca_dir: Path, tema_texto: str, datos: bytes) -> Path | None:
    """Guarda una foto del editor en la biblioteca: biblioteca/editor/TEMA/TEMA_NN.jpg (re-codificada, sin
    metadatos). Entra en la rotacion como cualquier oficial, y si la nota nombra el tema, va primero. None si el
    tema ya tenia una casi igual. Levanta ValueError sin tema e imagenes.ImagenInvalida si no es una imagen."""
    from . import imagenes  # aca: fuentes importa este modulo y no hace falta cargar PIL para elegir
    nombre = nombre_de_tema(tema_texto)
    if not nombre:
        raise ValueError("falta el tema: /biblioteca TEMA (por ejemplo /biblioteca SWAT)")
    img = imagenes.normalizar(datos)
    h = imagenes.huella(img)
    carpeta = biblioteca_dir / CARPETA_EDITOR / nombre.replace(" ", "_")
    carpeta.mkdir(parents=True, exist_ok=True)
    existentes = sorted(carpeta.glob("*.jpg"))
    for ruta in existentes:
        try:
            if imagenes.distancia_huella(h, imagenes.huella(imagenes.normalizar(ruta.read_bytes()))) <= \
                    DISTANCIA_DUPLICADO:
                return None
        except (OSError, imagenes.ImagenInvalida):
            continue
    numero = len(existentes) + 1
    while (destino := carpeta / f"{nombre.replace(' ', '_')}_{numero:02d}.jpg").exists():
        numero += 1
    return imagenes.guardar_jpeg(img, destino)


def temas_del_editor(biblioteca_dir: Path) -> dict[str, int]:
    """Los temas que sumo el editor y cuantas fotos tiene cada uno."""
    raiz = biblioteca_dir / CARPETA_EDITOR
    if not raiz.is_dir():
        return {}
    return {d.name.replace("_", " "): len(list(d.glob("*.jpg"))) for d in sorted(raiz.iterdir()) if d.is_dir()}
