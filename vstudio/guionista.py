"""Guionista: escribe lo que se dice y lo que se lee en cada escena, con la nota y lo que muestra cada imagen.

El planificador elige imagenes y recortes y deja un borrador; el guionista (solo texto, con razonamiento) escribe
el guion final con las reglas de guion.py, lo mide y le pide al LLM que corrija lo que no cumple. Tambien rehace
el guion de un plan o de un reel ya generado sin tocar las imagenes: los clips se reusan y no hace falta GPU.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import dataclass, replace
from datetime import date

from . import fechas, formatos, guion, montaje, ortografia
from .llm import ClienteLlm, LlmError, parte_texto
from .modelos import Escena, Fuente, Gancho, Plan

log = logging.getLogger(__name__)

INTENTOS = 3
MAX_TOKENS = 16000
# sin tope, el razonamiento a veces se comia los 16000 tokens (respuesta vacia o JSON cortado: 2 de 3 intentos);
# thinking_budget_tokens lo corta y deja lugar para el JSON (~1500-2500 tokens)
PRESUPUESTO_RAZONAMIENTO = 6000
PALABRAS_VISTA = 30
AVISO_GUION = "Guion para revisar: "
# lo que rompe el reel (no retiene, no pasa del minuto, Facebook lo castiga, la voz lo lee mal) pesa mas que una
# muletilla o una frase repetida al elegir el mejor de los intentos
GRAVES = ("gancho", "pregunta", "cebo", "pasar del minuto", "cifras", "cuenta regresiva")
PESO_GRAVE = 3

PROMPT = """Sos el guionista de los reels de una página de fans de GTA VI. La narración va en español latino neutro,
tratando al espectador de TÚ ("tienes", "puedes", "mira", "¿confías?"), nunca de vos ("tenés", "podés", "mirá"):
la voz que lo lee pronuncia mal el voseo. {fecha} Con el material que te paso escribí el guion de un reel de {n} escenas de
unos 5 segundos (en total unos {segundos} segundos). Cada escena muestra una imagen fija del juego que no cambia
(te digo qué se ve); si podés, que lo que se dice pegue con lo que se ve.

{reglas}

"texto" (lo que se lee en pantalla): máximo 8 palabras, refuerza el dato de la escena sin copiar la narración, con
cifras ("212 GB", "10 DE NOVIEMBRE") y el juego escrito "GTA VI". "titular": 3 a 6 palabras en mayúsculas para los
primeros 3 segundos, refuerza el gancho con otras palabras.

Devolvé SOLO un objeto JSON:
{{"titular": "...", "escenas": [{{"n": 1, "texto": "...", "narracion": "..."}}],
 "ganchos_alternativos": [{{"titular": "...", "narracion": "..."}}],
 "texto_post": "texto para Facebook: frase gancho, 2 o 3 frases, la pregunta del cierre y 3 a 5 hashtags"}}"""

CORREGIR = """Tu respuesta anterior tiene estos problemas: {problemas}.
Corregí SOLO eso, sin perder el hilo ni los datos, y devolvé el JSON completo otra vez.
RESPUESTA ANTERIOR:
{previo}"""


class GuionInvalido(ValueError):
    pass


@dataclass(frozen=True)
class Guion:
    titular: str
    textos: tuple[str, ...]
    narraciones: tuple[str, ...]
    ganchos: tuple[Gancho, ...]
    texto_post: str
    problemas: tuple[str, ...] = ()


def vistas(escenas: tuple[Escena, ...]) -> list[str]:
    """Lo que muestra cada escena (los planes viejos no guardaban el sujeto: sale del prompt de movimiento)."""
    return [e.sujeto or " ".join(e.motion_prompt.split()[:PALABRAS_VISTA]) or e.texto for e in escenas]


def sin_problemas(advertencia: str) -> str:
    """La advertencia sin el aviso de guion de una version anterior."""
    return "" if advertencia.startswith(AVISO_GUION) else advertencia.split(" | " + AVISO_GUION)[0]


def con_problemas(advertencia: str, problemas: tuple[str, ...]) -> str:
    base = sin_problemas(advertencia)
    if not problemas:
        return base
    if not base:
        return (AVISO_GUION + "; ".join(problemas))[:guion.MAX_ADVERTENCIA]
    # las dos partes quedan (los avisos que frenan la publicacion van al principio de cada una)
    base = base[:guion.MAX_ADVERTENCIA // 2]
    aviso = (AVISO_GUION + "; ".join(problemas))[:guion.MAX_ADVERTENCIA - len(base) - len(" | ")]
    return f"{base} | {aviso}"


def gravedad(problemas: tuple[str, ...]) -> int:
    return sum(PESO_GRAVE if any(g in p for g in GRAVES) else 1 for p in problemas)


def validar(datos: dict, n: int) -> Guion:
    """Valida y normaliza la respuesta del LLM. Levanta GuionInvalido con un motivo concreto."""
    titular = guion.en_pantalla(str(datos.get("titular", "")).strip())
    if not titular or len(titular) > guion.MAX_TITULAR:
        raise GuionInvalido(f"titular vacío o de más de {guion.MAX_TITULAR} caracteres")
    escenas = datos.get("escenas")
    if not isinstance(escenas, list) or len(escenas) != n:
        raise GuionInvalido(f"tienen que ser exactamente {n} escenas")
    textos, narraciones = [], []
    for i, e in enumerate(escenas, start=1):
        if not isinstance(e, dict):
            raise GuionInvalido(f"la escena {i} no es un objeto")
        texto = guion.en_pantalla(str(e.get("texto", "")).strip())
        narracion = guion.para_voz(str(e.get("narracion", "")).strip())
        if not texto or not narracion or len(texto) > guion.MAX_TEXTO or len(narracion) > guion.MAX_NARRACION:
            raise GuionInvalido(f"escena {i}: texto o narración vacíos o demasiado largos")
        textos.append(texto)
        narraciones.append(narracion)
    ganchos = tuple(Gancho(titular=guion.en_pantalla(g["titular"]), narracion=guion.para_voz(g["narracion"]))
                    for g in guion.ganchos_validos(datos.get("ganchos_alternativos")))
    return Guion(titular=titular, textos=tuple(textos), narraciones=tuple(narraciones), ganchos=ganchos,
                 texto_post=_post(str(datos.get("texto_post", "")))[:guion.MAX_POST])


def _post(texto: str) -> str:
    """El post de Facebook: palabras corregidas y hashtags sin espacios ("#GTA VI" no es un hashtag)."""
    return re.sub(r"#GTA\s+(VI|6)\b", r"#GTA\1", guion.corregir_palabras(texto.strip()), flags=re.IGNORECASE)


def _material(fuente: Fuente, vistas_escenas: list[str], advertencia: str) -> str:
    partes = [f"TITULO: {fuente.titulo}" if fuente.titulo else "", f"FECHA DE LA NOTA: {fuente.fecha}"
              if fuente.fecha else "", f"NOTA DEL EDITOR: {fuente.nota_usuario}" if fuente.nota_usuario else "",
              f"AVISOS SOBRE LA NOTA (respetalos): {advertencia}" if advertencia else "",
              f"TEXTO DE LA NOTA:\n{fuente.texto}" if fuente.texto else ""]
    imagenes = "\n".join(f"ESCENA {n}: {v}" for n, v in enumerate(vistas_escenas, start=1))
    return "MATERIAL:\n" + "\n".join(p for p in partes if p) + f"\n\nQUÉ SE VE EN CADA ESCENA:\n{imagenes}"


def escribir(cliente: ClienteLlm, fuente: Fuente, vistas_escenas: list[str], hoy: date, segundos: int,
             advertencia: str = "", minimo_s: float = 0.0, evitar: tuple[str, ...] = (),
             formato: str = formatos.NOTICIA) -> Guion:
    """Guion final. Si nunca cumple todas las reglas, devuelve el de problemas menos graves (con `problemas`).
    `formato`: la estructura del reel (formatos.py); manda sobre la de siempre donde no coincidan.

    Levanta GuionInvalido (con el ultimo error) si ninguna respuesta tuvo la forma pedida."""
    n = len(vistas_escenas)
    sistema = PROMPT.format(fecha=fechas.contexto(hoy), n=n, segundos=segundos, reglas=guion.reglas())
    instrucciones = formatos.formato(formato).instrucciones
    if instrucciones:
        sistema += ("\n\nFORMATO DE ESTE REEL (manda sobre la ESTRUCTURA de arriba donde no coincidan):\n"
                    + instrucciones)
    material = parte_texto(_material(fuente, vistas_escenas, sin_problemas(advertencia)) + (
        "\n\nNO DIGAS ESTO (la verificación encontró que la nota no lo respalda):\n" + "\n".join(
            f"- {e}" for e in evitar) if evitar else ""))
    minimas = guion.palabras_para(montaje.voz_necesaria_s(n, minimo_s)) if minimo_s else 0
    mejor: Guion | None = None
    correccion, pedido, ultimo_error = "", "", ""  # pedido: lo que habia que corregir del ultimo guion valido
    for intento in range(1, INTENTOS + 1):
        partes = [material, parte_texto(correccion)] if correccion else [material]
        try:
            datos = cliente.chat_json(sistema, partes, max_tokens=MAX_TOKENS, esfuerzo="low",
                                      semilla=secrets.randbelow(2**31), reintentos=0,
                                      razonamiento_max=PRESUPUESTO_RAZONAMIENTO)
            candidato = validar(datos, n)
        except (GuionInvalido, LlmError) as exc:
            log.warning("guionista intento %d: %s", intento, exc)
            ultimo_error = str(exc)
            if isinstance(exc, GuionInvalido):  # sin perder lo que faltaba corregir del intento anterior
                correccion = f"Tu respuesta anterior fue inválida ({exc}).\n" + (pedido or "Devolvé el JSON completo.")
            continue
        todo = [candidato.titular, *candidato.textos, *candidato.narraciones, candidato.texto_post]
        problemas = (tuple(guion.revisar(list(candidato.narraciones), minimas))
                     + tuple(fechas.problemas_cuenta(todo + [g.titular for g in candidato.ganchos]
                                                     + [g.narracion for g in candidato.ganchos], hoy))
                     + tuple(ortografia.problema(ortografia.dudosas(todo, material["text"]))))
        candidato = replace(candidato, problemas=problemas)
        if mejor is None or gravedad(problemas) < gravedad(mejor.problemas):
            mejor = candidato
        if not problemas:
            break
        if intento > 1 and ortografia.solo_ortografia(problemas):
            break  # ya se pidio corregir: lo que queda suele ser una palabra valida que el diccionario no trae
        log.info("guionista intento %d: %s", intento, "; ".join(problemas))
        pedido = CORREGIR.format(problemas="; ".join(problemas), previo=json.dumps(datos, ensure_ascii=False))
        correccion = pedido
    if mejor is None:
        raise GuionInvalido(f"el guionista no devolvió un guion válido ({ultimo_error})")
    return mejor


def aplicar(plan: Plan, g: Guion) -> Plan:
    """Pone el guion en el plan: las imagenes, recortes y clips quedan; la voz se vuelve a generar."""
    escenas = tuple(replace(e, texto=texto, narracion=narracion, audio="")
                    for e, texto, narracion in zip(plan.escenas, g.textos, g.narraciones))
    return replace(plan, titular=g.titular, escenas=escenas, ganchos=g.ganchos,
                   texto_post=g.texto_post or plan.texto_post, advertencia=con_problemas(plan.advertencia, g.problemas))
