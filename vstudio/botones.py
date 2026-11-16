"""Botones de Telegram del plan y del reel entregado (los usan el bot y el trabajador).

Cada boton muestra como esta la opcion ahora; tocarlo la cambia. En un plan pendiente cambia el plan; en un
reel ya entregado lo rearma sin GPU con la opcion nueva.
"""
from __future__ import annotations

from .modelos import Proyecto


def _si_no(valor: bool) -> str:
    return "sí" if valor else "no"


def opciones(p: Proyecto) -> list[tuple[str, str]]:
    return [(f"🔊 Voz: {_si_no(p.con_voz)}", f"voz:{p.id}"),
            (f"💬 Subtítulos: {_si_no(p.muestra_textos)}", f"sub:{p.id}"),
            (f"🔥 Titular: {_si_no(p.muestra_titular)}", f"tit:{p.id}")]


def plan(p: Proyecto) -> list:
    return [[("✅ Generar", f"ok:{p.id}"), ("🔁 Otro plan", f"otro:{p.id}")],
            opciones(p),
            [("🖼️ Otras imágenes", f"img:{p.id}"), ("✍️ Otro guion", f"gui:{p.id}"), ("❌ Cancelar", f"no:{p.id}")]]


def reel(p: Proyecto, con_facebook: bool = False) -> list:
    """`con_facebook`: solo si la pagina esta configurada (sin acceso a la API de Meta se publica a mano y el
    boton no tiene que aparecer)."""
    filas = [opciones(p), [("🖼️ Otras imágenes", f"img:{p.id}"), ("✍️ Otro guion", f"gui:{p.id}")]]
    if con_facebook and p.publicacion not in ("programado", "subiendo"):
        filas.append([("📤 Programar en Facebook", f"pub:{p.id}")])
    return filas
