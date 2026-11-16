"""Verificacion de datos: lo que el guion afirma y la nota no dice.

El Qwen, aun con reglas, inventa detalles ("el motel Starlet", "navegar en kayak", "a nadie le gusto"). Una
pasada aparte, solo texto, compara el guion con el material y lo que se ve en cada escena, y lista lo que no tiene
respaldo. No cambia el guion: lo deja en la advertencia del plan para que el editor decida (/narracion,
/titular, ✍️ Otro guion). Si el LLM falla, no frena el plan.
"""
from __future__ import annotations

import logging
import secrets
from datetime import date

from . import fechas
from .llm import ClienteLlm, LlmError, parte_texto
from .modelos import Fuente, Plan

log = logging.getLogger(__name__)

MAX_DUDAS = 5
MAX_LARGO_DUDA = 160
MAX_TOKENS = 6000
PRESUPUESTO_RAZONAMIENTO = 3000
AVISO = "Datos que la nota no respalda (revisalos): "
SIN_VERIFICAR = "No se pudo verificar los datos del guion."

PROMPT = """Sos el verificador de datos de una pagina de fans de GTA VI. Compara el GUION de un reel con el
MATERIAL (la unica fuente valida) y con lo que se ve en cada escena.
Lista SOLO las afirmaciones concretas del guion que el material no respalda o contradice: fechas, cifras, precios,
nombres de personas, lugares, vehiculos, locales o misiones, hechos, citas y reacciones de la gente ("a nadie le
gusto", "la comunidad explota").
NO cuentes: las preguntas al publico, las opiniones o expectativas dichas como tales ("podria", "se rumorea",
"seguro que"), ni lo que se describe de la imagen de cada escena (eso se ve en pantalla).
Las fechas relativas del guion ("hoy", "manana", "esta semana", "faltan N dias") se cuentan desde HOY (te lo doy en
el material), no desde la fecha de la nota: si la nota de ayer dice que algo empieza hoy, "empieza hoy" esta bien.
La cuenta de dias para el lanzamiento ya viene hecha en el material: no la vuelvas a calcular.
Escribi cada una como "lo que dice el guion — por que no tiene respaldo", en una linea corta.
Devolve SOLO un objeto JSON: {"sin_respaldo": ["...", "..."]} (lista vacia si todo esta respaldado; maximo 5)."""


def _guion(plan: Plan) -> str:
    escenas = "\n".join(f"ESCENA {e.n}: en pantalla \"{e.texto}\" · voz \"{e.narracion}\"" for e in plan.escenas)
    return f"GUION:\nTITULAR: {plan.titular}\n{escenas}\nPOST: {plan.texto_post}"


def _material(fuente: Fuente, plan: Plan, hoy: date) -> str:
    partes = [f"HOY: {fechas.contexto(hoy)}", f"TITULO: {fuente.titulo}" if fuente.titulo else "",
              f"FECHA DE LA NOTA: {fuente.fecha}" if fuente.fecha else "",
              f"NOTA DEL EDITOR: {fuente.nota_usuario}" if fuente.nota_usuario else "",
              f"TEXTO DE LA NOTA:\n{fuente.texto}" if fuente.texto else ""]
    vistas = "\n".join(f"ESCENA {e.n}: {e.sujeto}" for e in plan.escenas if e.sujeto)
    return "MATERIAL:\n" + "\n".join(p for p in partes if p) + (f"\n\nQUE SE VE EN CADA ESCENA:\n{vistas}" if vistas
                                                                else "")


def verificar(cliente: ClienteLlm, fuente: Fuente, plan: Plan, hoy: date) -> list[str] | None:
    """Las afirmaciones sin respaldo (vacia si todo esta respaldado; None si no se pudo verificar: el plan
    sigue, pero ese reel no sale solo a Facebook). `hoy`: las fechas relativas se miden desde aca (contando
    desde la fecha de la nota, "arrancaron hoy la votación" salia como falso si la nota era del dia anterior)."""
    try:
        datos = cliente.chat_json(PROMPT, [parte_texto(_material(fuente, plan, hoy)), parte_texto(_guion(plan))],
                                  max_tokens=MAX_TOKENS, esfuerzo="low", semilla=secrets.randbelow(2**31),
                                  reintentos=0, razonamiento_max=PRESUPUESTO_RAZONAMIENTO)
    except (LlmError, ValueError) as exc:
        log.warning("no se pudo verificar el guion: %s", exc)
        return None
    except Exception:  # noqa: BLE001 - verificar es una ayuda: nunca tiene que tirar el plan
        log.exception("el verificador fallo")
        return None
    lista = datos.get("sin_respaldo") if isinstance(datos, dict) else None
    if not isinstance(lista, list):
        log.warning("el verificador devolvio algo sin forma: %r", str(datos)[:200])
        return None
    dudas = [" ".join(str(d).split())[:MAX_LARGO_DUDA] for d in lista if str(d).strip()]
    return dudas[:MAX_DUDAS]


SEPARADOR = " | "  # el mismo que usa guionista.con_problemas
MAX_AVISO = 500


def dudas_de(advertencia: str) -> tuple[str, ...]:
    """Las dudas que quedaron anotadas en la advertencia (para reescribir sin ellas)."""
    if not advertencia.startswith(AVISO):
        return ()
    return tuple(d.strip() for d in advertencia[len(AVISO):].split(SEPARADOR, 1)[0].split("; ") if d.strip())


def sin_dudas(advertencia: str) -> str:
    """La advertencia sin las dudas (o el "no se pudo verificar") de una version anterior del guion."""
    if not advertencia.startswith((AVISO, SIN_VERIFICAR)):
        return advertencia
    return advertencia.split(SEPARADOR, 1)[1] if SEPARADOR in advertencia else ""


def con_dudas(advertencia: str, dudas: list[str] | None) -> str:
    """La advertencia del plan con las dudas al principio (lo primero que hay que mirar); las de antes se van.
    `dudas` None: no se pudo verificar (queda anotado: frena la publicacion automatica)."""
    resto = sin_dudas(advertencia)
    if dudas is None:
        aviso = SIN_VERIFICAR
    elif not dudas:
        return resto
    else:
        aviso = (AVISO + "; ".join(d.replace("|", "/") for d in dudas))[:MAX_AVISO]
    return f"{aviso}{SEPARADOR}{resto}" if resto else aviso
