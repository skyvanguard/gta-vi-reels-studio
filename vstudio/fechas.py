"""Cuentas de fechas que el LLM hace mal: cuantos dias faltan para el lanzamiento.

Una vez el titular dijo "SEIS DÍAS PARA GTA VI" con siete por delante, y el verificador, que contaba desde la
fecha de la nota (11/11) y no desde hoy, pidio corregirlo a "8 días": la reescritura volvio a salir mal. Ahora el
numero va calculado en los prompts y la cuenta regresiva del guion se chequea aca, sin el LLM.
"""
from __future__ import annotations

import re
from datetime import date

from .guion import _plano  # minusculas, sin tildes ni signos: lo mismo que usan los chequeos del guion

LANZAMIENTO = date(2026, 11, 19)  # GTA VI en PS5 y Xbox Series X|S: si Rockstar mueve la fecha, se cambia aca

NUMEROS = {"un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
           "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
           "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiuno": 21,
           "veintiun": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24, "veinticinco": 25,
           "veintiseis": 26, "veintisiete": 27, "veintiocho": 28, "veintinueve": 29, "treinta": 30}
_N = r"(\d{1,3}|" + "|".join(sorted(NUMEROS, key=len, reverse=True)) + r")"
_LANZAR = r"(?:el |su )?(?:gta|lanzamiento|estreno|salida|que (?:salga|llegue|sale|llega))\b"
# solo cuando la cuenta es la del lanzamiento: "faltan dos dias para la entrevista" no cuenta
CUENTA = (re.compile(rf"\b{_N} dias? (?:para|hasta) {_LANZAR}"),
          re.compile(rf"\b(?:falta|faltan|queda|quedan) (?:solo |apenas )?{_N} dias?\b(?! (?:para|hasta)\b)"),
          re.compile(rf"\ba {_N} dias? (?:de|del) (?:su |el )?(?:lanzamiento|estreno|salida)\b"))


def dias_para_lanzamiento(hoy: date) -> int:
    return (LANZAMIENTO - hoy).days


def contexto(hoy: date) -> str:
    """La fecha de hoy y la cuenta ya hecha, para los prompts (guionista, planificador y verificador)."""
    base = f"Hoy es {hoy.isoformat()}"
    dias = dias_para_lanzamiento(hoy)
    lanzamiento = LANZAMIENTO.isoformat()
    if dias > 1:
        return (f"{base}: faltan {dias} días para el lanzamiento de GTA VI ({lanzamiento}). Contá desde hoy, no "
                "desde la fecha de la nota.")
    if dias == 1:
        return f"{base}: GTA VI sale mañana ({lanzamiento})."
    if dias == 0:
        return f"{base}: GTA VI sale hoy ({lanzamiento})."
    return f"{base}: GTA VI ya salió (el {lanzamiento})."


def _numero(palabra: str) -> int:
    return int(palabra) if palabra.isdigit() else NUMEROS[palabra]


def cuentas(texto: str) -> list[int]:
    """Los "faltan N días" para el lanzamiento que dice un texto (en cifras o con letras)."""
    plano = _plano(texto)
    return [_numero(m.group(1)) for patron in CUENTA for m in patron.finditer(plano)]


def problemas_cuenta(textos: list[str], hoy: date) -> list[str]:
    """Una cuenta regresiva que no da con la de hoy (lista vacia = bien o no hay cuenta)."""
    reales = dias_para_lanzamiento(hoy)
    dichas = sorted({n for t in textos for n in cuentas(t)} - {reales})
    if not dichas:
        return []
    if reales < 1:
        return [f"cuenta regresiva mal: dice que faltan {dichas[0]} días, pero GTA VI ya salió o sale hoy "
                f"({LANZAMIENTO.isoformat()}): sacalo"]
    return [f"cuenta regresiva mal: dice que faltan {dichas[0]} días y faltan {reales} (hoy es {hoy.isoformat()}, "
            f"sale el {LANZAMIENTO.isoformat()}): corregilo o sacalo"]
