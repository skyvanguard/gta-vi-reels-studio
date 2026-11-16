"""Formatos de reel ademas de la noticia (todos los reels tenian el mismo molde).

Cada formato es una estructura de guion: lo que cambia es como se cuenta, no de donde salen los datos (siempre de
la nota). En los automaticos, el editor (Qwen) le pone a cada noticia el formato que mejor le queda; a mano, se pide
en el mensaje ("sabías que…", "hacé un ranking", "antes y ahora").
"""
from __future__ import annotations

import re
from dataclasses import dataclass

NOTICIA = "noticia"


@dataclass(frozen=True)
class Formato:
    clave: str
    nombre: str  # como se le muestra al editor
    segundos: int | None  # largo propio (None: el que se pida)
    cuando: str  # para el editor automatico: a que noticias les queda
    instrucciones: str  # para el guionista ("" = la estructura de siempre)
    pedido: str = ""  # como lo pide el editor en un mensaje (expresion regular, sin tildes)


FORMATOS = {f.clave: f for f in (
    Formato(NOTICIA, "Noticia", None, "una novedad con varios datos (el formato de siempre)", ""),
    Formato("sabias", "¿Sabías que…?", 15,
            "un solo dato curioso o un detalle del juego que se cuenta en una frase",
            "FORMATO «¿SABÍAS QUE…?»: un solo dato curioso de la nota, contado en corto. La escena 1 arranca con "
            "\"¿Sabías que\" y dice el dato entero; las del medio cuentan qué significa para el que juega; la "
            "última cierra con UNA pregunta. Acá no hay promesa para el final: el dato va primero.",
            r"sabias que"),
    Formato("ranking", "Ranking", 30,
            "la nota trae una lista o varios datos parejos (canciones, autos, funciones, cifras)",
            "FORMATO RANKING: una cuenta regresiva con datos de la nota. La escena 1 dice qué se rankea y cuántos "
            "son (\"Las tres cosas…\"); después un puesto por escena, del último al primero, con el número dicho "
            "con letras (\"Tres\", \"Dos\", \"Y uno\"); el número uno es lo más fuerte; la última escena cierra "
            "con UNA pregunta. Solo cosas que la nota dice: no inventes puestos ni un orden que la nota no da "
            "(si no hay orden, decí que es tu ranking).",
            r"ranking|top (\d+|tres|cinco|diez)"),
    Formato("comparacion", "Antes y ahora", 30,
            "la nota compara GTA VI con GTA V, con otro juego o con cómo era antes",
            "FORMATO ANTES Y AHORA: contá el contraste que la nota hace (GTA V contra GTA VI, antes contra ahora). "
            "La escena 1 plantea la diferencia más fuerte; después alterná una escena para el \"antes\" y la "
            "siguiente para el \"ahora\"; la última cierra con UNA pregunta. Solo comparaciones que la nota hace: "
            "no inventes ninguna.",
            r"antes y ahora|comparacion|compara(r|lo|la)?\b"),
)}


def formato(clave: str) -> Formato:
    """El formato pedido; uno desconocido (un dato viejo, una respuesta rara del LLM) es una noticia."""
    return FORMATOS.get(str(clave).strip().lower(), FORMATOS[NOTICIA])


def pedido_en(texto_plano: str) -> str:
    """El formato que pide un mensaje corto del editor (ya en minusculas y sin tildes); "" si no pide ninguno."""
    return next((f.clave for f in FORMATOS.values() if f.pedido and re.search(rf"\b(?:{f.pedido})", texto_plano)), "")


def para_el_editor() -> str:
    """La lista de formatos para el prompt del editor automatico."""
    return "\n".join(f'  "{f.clave}": {f.cuando}.' for f in FORMATOS.values())


def repartir(claves: list[str], maximo_especiales: int) -> list[str]:
    """Los formatos de una tanda, con tope de los que no son noticia (los primeros, que son las notas de mas
    interes, conservan el suyo)."""
    salida, especiales = [], 0
    for clave in claves:
        clave = formato(clave).clave
        if clave != NOTICIA and especiales >= maximo_especiales:
            clave = NOTICIA
        especiales += clave != NOTICIA
        salida.append(clave)
    return salida
