"""Del material de la noticia a un Plan: escenas (imagen + recorte), guion (guionista) y prompts de movimiento.

El LLM con vision elige las imagenes y los recortes y deja un borrador de textos; el guion final lo escribe el
guionista (solo texto) con las reglas de guion.py.
"""
from __future__ import annotations

import logging
import math
import secrets
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path

from PIL import Image

from . import armado, fechas, guion_propio, guionista, imagenes, ortografia, tomas, verificador, visuales
from .fuentes import PREFIJO_TOMA, ROTULO_FOTO, ROTULO_TOMA, dias_de_antiguedad
from .guion import MAX_ADVERTENCIA, MAX_NARRACION, MAX_POST, MAX_TEXTO, MAX_TITULAR, en_pantalla
from .imagenes import caja_relativa
from .llm import ClienteLlm, parte_imagen, parte_texto
from .modelos import Escena, Fuente, Plan

log = logging.getLogger(__name__)

MAX_MOTION = 800
MAX_NEGATIVO = 400
MAX_SUJETO = 200
INTENTOS_PLAN = 3
MAX_SOLAPE = 0.6  # interseccion/union de dos recortes de la misma imagen: por encima, muestran lo mismo
PREFIJO_USUARIO = "usuario_"  # las fotos que manda el editor
ZOOM_PRIMER_PLANO = 1.2  # una imagen que no aguanta ni este zoom es "chica": se le avisa al planificador

PROMPT_PLAN = """Sos el editor de una pagina de fans de GTA VI que publica reels verticales en espanol latinoamericano.
{fecha} {info_fecha}
Te paso {k} imagenes numeradas (IMAGEN 0 a IMAGEN {ultima}) y el material de la noticia.
Arma un reel de {n} escenas de unos 5 segundos cada una (en total unos {segundos} segundos): elegi la imagen y el
recorte de cada escena y deja un borrador de los textos (el guion final lo escribe despues un guionista).
Reglas de contenido:
- Usa SOLO hechos del material. Lo que Rockstar no confirmo va como rumor o expectativa.
- No inventes cifras, precios ni fechas. Si la nota es vieja, adapta el texto a hoy y explicalo en "advertencia".
- Cada escena usa una imagen recortada en vertical, centrada en "centro_x" y "centro_y" (0 a 1; 0 = izquierda/
  arriba) sobre algo que se pueda mover: personas, vehiculos, agua. "zoom" va de 1.0 (toda la altura, plano
  general) a 1.6 (primer plano). {regla_usos}
  Alterna planos generales y primeros planos. La escena 1 usa la imagen mas impactante, en primer plano.
- Usa SOLO imagenes de GTA VI (Vice City, Leonida, sus personajes y autos). Ignora las de otros juegos, logos,
  capturas de paginas web o imagenes con mucho texto.
{regla_medios}- "sujeto": que se ve en el recorte (personas, lugar, vehiculos, accion), en una frase. Si la imagen dice quien es
  o que lugar es ("imagen oficial de Rockstar: ..."), usa ese nombre.
- "texto": lo que se lee en pantalla, maximo 8 palabras. En pantalla (y en el titular) el juego se escribe "GTA VI".
- "narracion": borrador de lo que dice el locutor (un dato por escena; la ultima, una pregunta). Escribi los
  numeros y siglas como se pronuncian ("GTA seis", "diecinueve de noviembre").
- Escribi en espanol correcto, con tildes y enie (Lucía, días, año), aunque la nota este en ingles. Revisa la
  ortografia de cada texto antes de responder.
{variante}
Devolve SOLO un objeto JSON:
{{"titular": "3 a 6 palabras en mayusculas",
 "escenas": [{{"imagen": 0, "centro_x": 0.5, "centro_y": 0.5, "zoom": 1.0, "sujeto": "que se ve",
               "texto": "...", "narracion": "..."}}],
 "texto_post": "texto para publicar en Facebook: arranca con una frase gancho, 2 o 3 frases, termina con la
                pregunta del cierre y 3 a 5 hashtags",
 "advertencia": "vacio, o que hay que revisar (nota vieja, rumor, poca informacion)"}}"""

# los clips casi no se movian ("static close-up", "breathing subtly", "slow push-in"): se veia como
# una foto. Ahora se pide una accion que se note y una camara con intencion, sin sumar nada a la imagen
PROMPT_MOVIMIENTO = """Esta imagen vertical es el primer cuadro de un clip de 5 segundos de un modelo image-to-video
(Wan 2.2) para un reel: tiene que notarse que es video, no una foto. Devolve SOLO un objeto JSON con:
- "motion_prompt_en": 50 a 80 palabras en INGLES. Primero la ACCION, clara y visible, de lo que YA esta en la
  imagen (la persona camina, gira la cabeza, levanta el arma, maneja; el auto avanza; el agua, el pelo, la ropa,
  el humo y las luces se mueven). Despues UN movimiento de camara con intencion (tracking, pan, orbit, push-in
  decidido o handheld). No uses "static", "still", "subtle" ni "slowly". No agregues objetos ni personas nuevas,
  no cambies la ropa ni las caras. Si hay elementos de interfaz (HUD, minimapa, textos en pantalla) pedi que queden
  fijos y nitidos.
- "negative_extra_en": cosas a evitar especificas de esta imagen, en ingles, separadas por comas."""

REGLA_TOMAS = """- Las imagenes rotuladas TOMA DE VIDEO son el cuadro del medio de una toma de video REAL del juego: en el reel
  se ve el video, no la foto. Usalas en al menos la mitad de las escenas y en la escena 1 (la de mas accion). Con
  ellas "zoom" va de 1.0 a 1.3.
"""
REGLA_FOTOS = """- Las rotuladas FOTO DE LA NOTICIA muestran algo real que la nota nombra (un objeto, un lugar, una persona):
  valen aunque no sean del juego. Usalas en la escena que cuenta ese dato.
"""


INSTRUCCION_SOLO_ANIMACION = """- SOLO ANIMACION: el reel va sin voz y sin textos en pantalla; solo se ve la imagen animada. Lo
  que importa es la variedad: aunque haya una sola imagen, cada escena con otro sujeto u otro encuadre (otro
  centro_x/centro_y o un zoom muy distinto), alternando planos generales, medios y primeros planos. "texto" y
  "narracion" van igual (cortos), pero no se usan."""


INSTRUCCION_GUION_PROPIO = """- El guion ya esta escrito (GUION YA ESCRITO, una parte por linea) y se usa TAL CUAL, en
  orden, repartido en las escenas: elegi para cada parte la imagen que mejor la acompane. "narracion" es solo un
  borrador (no se usa): no hace falta reescribirla."""


def con_guion_propio(plan: Plan, oraciones: tuple[str, ...]) -> Plan:
    """La narracion es el guion del editor, repartido por oraciones; no pasa por el guionista."""
    tramos = guion_propio.repartir(oraciones, len(plan.escenas))
    escenas = tuple(replace(e, narracion=tramo, audio="") for e, tramo in zip(plan.escenas, tramos))
    aviso = f"Narración: tu guion tal cual ({len(oraciones)} oraciones en {len(escenas)} escenas)."
    return replace(plan, escenas=escenas, ganchos=(),
                   advertencia=f"{aviso} {plan.advertencia}".strip()[:MAX_ADVERTENCIA])


class PlanInvalido(ValueError):
    pass


class SinImagenes(ValueError):
    pass


def _info_fecha(fuente: Fuente, hoy: date, dias_vieja: int) -> str:
    if not fuente.url:
        return "El material lo mando el editor directamente."
    dias = dias_de_antiguedad(fuente.fecha, hoy) if fuente.fecha else None
    if dias is None:
        return "No se pudo leer la fecha de la nota: no asumas que es de hoy."
    if dias > dias_vieja:
        return f"OJO: la nota es del {fuente.fecha} (hace {dias} dias); lo que dice puede estar desactualizado."
    return f"La nota es del {fuente.fecha}."


def max_usos_por_imagen(n_escenas: int, n_imagenes: int) -> int:
    """Con imagenes de sobra, cada escena lleva una distinta (no repetir fotos en el mismo
    reel); con menos (las fotos del editor, una sola foto para animar), se reparten."""
    return 1 if n_imagenes >= n_escenas else math.ceil(n_escenas / max(n_imagenes, 1))


def regla_usos(max_usos: int) -> str:
    if max_usos == 1:
        return "Cada escena con una imagen DISTINTA: no repitas ninguna."
    return (f"Usa la mayor cantidad posible de imagenes DISTINTAS: ninguna mas de {max_usos} veces, y si repetis "
            "una, que sea con otro sujeto (otro centro_x/centro_y o un zoom muy distinto).")


def solape(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Interseccion sobre union de dos cajas (0 = nada en comun, 1 = la misma)."""
    ancho = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    alto = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    comun = ancho * alto
    area = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - comun
    return comun / area if area > 0 else 0.0


def encuadres_repetidos(escenas: list[dict], aspectos: list[float]) -> list[tuple[int, int]]:
    """Pares de escenas (numeradas desde 1) que muestran casi lo mismo de la misma imagen. Con
    una sola foto, 3 de 15 escenas salian con el mismo plano (con zoom 1.0 el centro_y no cambia nada)."""
    cajas = [caja_relativa(aspectos[e["imagen"]], e["centro_x"], e["centro_y"], e["zoom"]) for e in escenas]
    return [(i + 1, j + 1) for i in range(len(escenas)) for j in range(i + 1, len(escenas))
            if escenas[i]["imagen"] == escenas[j]["imagen"] and solape(cajas[i], cajas[j]) > MAX_SOLAPE]


def validar_plan(datos: dict, n_imagenes: int, n_escenas: int, aspectos: list[float] | None = None,
                 estricto: bool = True, zoom_maximo: list[float] | None = None) -> dict:
    """Valida y normaliza la respuesta del LLM. Levanta PlanInvalido con un motivo concreto.

    Con `aspectos` (ancho/alto de cada imagen) tambien rechaza escenas con el mismo encuadre; sin `estricto`
    (ultimo intento) no las rechaza: quedan anotadas en la advertencia para que el editor decida.
    `zoom_maximo`: el zoom mas cerrado que aguanta cada imagen (ver `topes_de_zoom`); se aplica antes de mirar
    los encuadres: dos escenas de una imagen chica con "zooms distintos" terminarian con el mismo recorte."""
    titular = en_pantalla(str(datos.get("titular", "")).strip())
    if not titular or len(titular) > MAX_TITULAR:
        raise PlanInvalido(f"titular vacio o de mas de {MAX_TITULAR} caracteres")
    escenas = datos.get("escenas")
    minimo = max(2, n_escenas - 1)  # una vez pidio 6 y devolvio 4: el reel de 30 s quedaba de 20
    if not isinstance(escenas, list) or not minimo <= len(escenas) <= n_escenas + 1:
        raise PlanInvalido(f"se esperaban entre {minimo} y {n_escenas + 1} escenas (pedí {n_escenas})")
    limpias = []
    for i, e in enumerate(escenas, start=1):
        if not isinstance(e, dict):
            raise PlanInvalido(f"escena {i} no es un objeto")
        try:
            idx, centro = int(e["imagen"]), float(e["centro_x"])
            centro_y, zoom = float(e.get("centro_y", 0.5)), float(e.get("zoom", 1.0))
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanInvalido(f"escena {i}: imagen/centro/zoom invalidos") from exc
        if not 0 <= idx < n_imagenes:
            raise PlanInvalido(f"escena {i}: imagen {idx} no existe (hay {n_imagenes})")
        texto, narracion = en_pantalla(str(e.get("texto", "")).strip()), str(e.get("narracion", "")).strip()
        if not texto or len(texto) > MAX_TEXTO or len(narracion) > MAX_NARRACION:
            raise PlanInvalido(f"escena {i}: texto vacio o textos demasiado largos")
        tope_zoom = zoom_maximo[idx] if zoom_maximo else imagenes.ZOOM_MAX
        limpias.append({"imagen": idx, "centro_x": min(max(centro, 0.0), 1.0), "texto": texto,
                        "centro_y": min(max(centro_y, 0.0), 1.0), "zoom": min(max(zoom, 1.0), tope_zoom),
                        "narracion": narracion or texto, "sujeto": str(e.get("sujeto", "")).strip()[:MAX_SUJETO]})
    tope = max_usos_por_imagen(len(limpias), n_imagenes)
    repetidas = {idx: c for idx, c in Counter(e["imagen"] for e in limpias).items() if c > tope}
    advertencia = str(datos.get("advertencia", "")).strip()
    if repetidas:
        detalle = ", ".join(f"IMAGEN {i} {c} veces" for i, c in sorted(repetidas.items()))
        if estricto:
            raise PlanInvalido(f"imagenes repetidas ({detalle}); maximo {tope} por imagen, usa otras")
        advertencia = f"Hay imágenes repetidas ({detalle.replace('IMAGEN', 'imagen')}). {advertencia}".strip()
    pares = encuadres_repetidos(limpias, aspectos) if aspectos else []
    if pares:
        detalle = ", ".join(f"{a} y {b}" for a, b in pares)
        if estricto:
            raise PlanInvalido(f"escenas con casi el mismo encuadre de la misma imagen ({detalle}): cambia "
                               "centro_x, centro_y o zoom para que cada una muestre otra cosa")
        advertencia = f"Escenas con casi el mismo encuadre: {detalle}. {advertencia}".strip()
    return {"titular": titular, "escenas": limpias, "texto_post": str(datos.get("texto_post", "")).strip()[:MAX_POST],
            "advertencia": advertencia[:MAX_ADVERTENCIA]}


def topes_de_zoom(imgs: list[Image.Image], nombres: tuple[str, ...], ancho: int, alto: int) -> list[float]:
    """El zoom mas cerrado que se le acepta a cada imagen: las tomas de video tienen el suyo, las fotos del editor
    no tienen tope (se hace lo que pidio) y el resto, lo que su resolucion aguanta."""
    topes = []
    for img, nombre in zip(imgs, nombres):
        if nombre.startswith(PREFIJO_USUARIO):
            topes.append(imagenes.ZOOM_MAX)
        elif nombre.startswith(f"{PREFIJO_TOMA}_"):
            topes.append(armado.ZOOM_MAX_TOMA)
        else:
            topes.append(imagenes.zoom_nitido(img.size, imagenes.ZOOM_MAX, ancho, alto))
    return topes


def avisos_de_resolucion(imgs: list[Image.Image], nombres: tuple[str, ...], topes: list[float], ancho: int,
                         alto: int) -> dict[int, str]:
    """Lo que el planificador tiene que saber de cada imagen antes de elegir el encuadre: cual no se va a animar
    (chica: va como foto) y cual no aguanta un primer plano."""
    avisos = {}
    for i, (img, nombre, tope) in enumerate(zip(imgs, nombres, topes)):
        if nombre.startswith((PREFIJO_USUARIO, f"{PREFIJO_TOMA}_")):
            continue
        if imagenes.estirada(img.size, 1.0, ancho, alto):
            avisos[i] = "imagen chica: va como foto fija con paneo, usala entera (zoom 1.0)"
        elif tope < ZOOM_PRIMER_PLANO:
            avisos[i] = "resolución media: usala con zoom 1.0, no en primer plano"
    return avisos


def _partes_plan(fuente: Fuente, imgs: list[Image.Image], avisos: dict[int, str] | None = None) -> list[dict]:
    """`avisos`: lo que hay que decirle al planificador de la resolucion de algunas imagenes (por indice)."""
    partes = []
    for i, img in enumerate(imgs):
        quien = fuente.descripciones[i] if i < len(fuente.descripciones) else ""
        if quien.startswith((ROTULO_TOMA, ROTULO_FOTO)):
            rotulo = f"IMAGEN {i} ({quien}):"
        else:
            rotulo = f"IMAGEN {i} (imagen oficial de Rockstar: {quien}):" if quien else f"IMAGEN {i}:"
        if avisos and i in avisos:
            rotulo = f"{rotulo.removesuffix(':')} — {avisos[i]}:"
        partes += [parte_texto(rotulo), parte_imagen(imagenes.jpeg_para_llm(img))]
    material = [f"TITULO: {fuente.titulo}" if fuente.titulo else "", f"FECHA: {fuente.fecha}" if fuente.fecha else "",
                f"NOTA DEL EDITOR: {fuente.nota_usuario}" if fuente.nota_usuario else "",
                f"TEXTO DE LA NOTA:\n{fuente.texto}" if fuente.texto else ""]
    partes.append(parte_texto("MATERIAL:\n" + "\n".join(m for m in material if m)))
    if fuente.guion:
        lineas = "\n".join(f"{n}. {o}" for n, o in enumerate(fuente.guion, start=1))
        partes.append(parte_texto(f"GUION YA ESCRITO:\n{lineas}"))
    return partes


def aviso_de_fotos_chicas(escenas: tuple[Escena, ...], chicas: set[int], imagen_de: list[int]) -> str:
    """Lo que el editor tiene que saber: que escenas salieron como foto fija porque su imagen es chica y, si el
    reel entero quedo sin nada que se mueva (ni IA ni video real), que conviene pedir otras imagenes. `chicas`:
    indices de las imagenes chicas; `imagen_de`: la imagen de cada escena, en orden."""
    fijas = [e.n for e, idx in zip(escenas, imagen_de) if e.medio == armado.MEDIO_FOTO and idx in chicas]
    if not fijas:
        return ""
    quieto = all(e.medio == armado.MEDIO_FOTO and not e.extras for e in escenas)
    cuales = "la escena " + str(fijas[0]) if len(fijas) == 1 else "las escenas " + ", ".join(map(str, fijas))
    if quieto:
        return ("Todo el reel sale con fotos fijas: las imágenes son chicas para animarlas y no hay tomas de video "
                "(sumá clips con /biblioteca o pedí 🖼️ Otras imágenes).")
    return f"Imagen chica en {cuales}: va como foto con paneo, sin animar."


def _regla_medios(fuente: Fuente) -> str:
    """Las reglas de las tomas de video y de las fotos de la noticia, solo si el material las trae."""
    return ((REGLA_TOMAS if any(d.startswith(ROTULO_TOMA) for d in fuente.descripciones) else "")
            + (REGLA_FOTOS if any(d.startswith(ROTULO_FOTO) for d in fuente.descripciones) else ""))


def _escena(cliente: ClienteLlm, carpeta: Path, n: int, e: dict, img: Image.Image, origen: str,
            mapa: dict[str, tomas.Toma], ancho: int, alto: int, segundos_clip: float,
            chica_como_foto: bool = True) -> Escena:
    """La escena con su recorte y, segun de donde sale la imagen, su clip ya armado (video real o foto) o el
    prompt de movimiento para que la anime la IA. `chica_como_foto` False ("solo video": el editor pidio la
    imagen animada): una imagen chica se anima igual."""
    medio = armado.medio_de(origen, mapa)
    zoom = e["zoom"]
    if medio == armado.MEDIO_TOMA:
        zoom = min(zoom, armado.ZOOM_MAX_TOMA)
    elif not origen.startswith(PREFIJO_USUARIO):  # con las fotos del editor se hace lo que pidio, sean como sean
        # lo que entra blando sale blando: el recorte no se cierra mas de lo que la imagen aguanta y, si la imagen
        # es chica aun entera (la foto de la nota), va como foto con paneo: animarla con IA tarda 8 minutos de
        # GPU y devuelve el mismo borron
        zoom = imagenes.zoom_nitido(img.size, zoom, ancho, alto)
        if chica_como_foto and medio == armado.MEDIO_IA and imagenes.estirada(img.size, zoom, ancho, alto):
            medio = armado.MEDIO_FOTO
    recorte = imagenes.recorte_vertical(img, e["centro_x"], ancho, alto, zoom, e["centro_y"])
    nombre = f"escena_{n}.png"
    recorte.save(carpeta / nombre)
    escena = Escena(n=n, imagen=nombre, origen=origen, centro_x=e["centro_x"], texto=e["texto"],
                    narracion=e["narracion"], seed=secrets.randbelow(2**31), zoom=zoom, centro_y=e["centro_y"],
                    sujeto=e["sujeto"])
    if medio != armado.MEDIO_IA:
        grande = carpeta / f"escena_{n}_hd.png"  # el paneo sale del recorte al tamano del reel, no del de Wan
        imagenes.recorte_vertical(img, e["centro_x"], visuales.ANCHO, visuales.ALTO, zoom, e["centro_y"]).save(grande)
        caja = caja_relativa(img.width / img.height, e["centro_x"], e["centro_y"], zoom)
        clip = armado.clip_sin_gpu(carpeta, n, medio, grande, mapa.get(origen), caja, segundos_clip)
        if clip:  # el clip ya esta: no hay nada que animar ni que revisar por deforme
            return replace(escena, clip=clip, medio=medio, revisada=True)
        if imagenes.estirada(img.size, zoom, ancho, alto):
            log.warning("escena %d: no pude armar el paneo y la imagen es chica (%s): la IA la va a animar borrosa",
                        n, img.size)
    mov = cliente.chat_json(PROMPT_MOVIMIENTO, [parte_imagen(imagenes.jpeg_para_llm(recorte)),
                                                parte_texto("Escribi el prompt para esta imagen.")])
    motion = str(mov.get("motion_prompt_en", "")).strip()[:MAX_MOTION]
    if not motion:
        raise PlanInvalido(f"escena {n}: el LLM no devolvio motion_prompt_en")
    return replace(escena, motion_prompt=motion,
                   negative_extra=str(mov.get("negative_extra_en", "")).strip()[:MAX_NEGATIVO])


def planificar(cliente: ClienteLlm, carpeta: Path, fuente: Fuente, hoy: date, n_escenas: int,
               dias_vieja: int, ancho: int, alto: int, variante: int = 0, titular_previo: str = "",
               segundos: int = 0, minimo_s: float = 0.0, solo_animacion: bool = False,
               catalogo: tomas.Catalogo | None = None, segundos_clip: float = 5.0, extras: bool = True,
               formato: str = "noticia") -> Plan:
    """`solo_animacion`: el reel va sin voz ni textos; no pasa por el guionista (no hay guion que escribir).
    `catalogo`: la biblioteca de tomas de video real (las escenas que usan una no pasan por la GPU, y cada escena
    lleva tomas extra para que la imagen cambie mas seguido, salvo `extras` False). `formato`: la estructura del
    guion (formatos.py)."""
    if not fuente.imagenes:
        raise SinImagenes("no hay imagenes: mandame fotos o un link que tenga imagenes")
    if not (fuente.texto or fuente.nota_usuario):
        raise PlanInvalido("no hay texto de la noticia: mandame el link o escribime de que se trata")
    imgs = [Image.open(carpeta / nombre).convert("RGB") for nombre in fuente.imagenes]
    texto_variante = (f"- Propone un enfoque DISTINTO al anterior (titular anterior: {titular_previo!r})."
                      if variante else "")
    if solo_animacion:
        texto_variante += "\n" + INSTRUCCION_SOLO_ANIMACION
    elif fuente.guion:
        texto_variante += "\n" + INSTRUCCION_GUION_PROPIO
    sistema = PROMPT_PLAN.format(fecha=fechas.contexto(hoy), info_fecha=_info_fecha(fuente, hoy, dias_vieja),
                                 k=len(imgs), ultima=len(imgs) - 1, n=n_escenas, variante=texto_variante,
                                 segundos=segundos or n_escenas * 5,
                                 regla_usos=regla_usos(max_usos_por_imagen(n_escenas, len(imgs))),
                                 regla_medios=_regla_medios(fuente))
    topes = topes_de_zoom(imgs, fuente.imagenes, ancho, alto)
    partes, motivo = _partes_plan(fuente, imgs, avisos_de_resolucion(imgs, fuente.imagenes, topes, ancho, alto)), ""
    aspectos = [img.width / img.height for img in imgs]
    for intento in range(INTENTOS_PLAN):  # los reintentos llevan el motivo del rechazo
        extra = f"\nTu respuesta anterior fue invalida ({motivo}); corregila." if motivo else ""
        datos = cliente.chat_json(sistema + extra, partes, max_tokens=8000, semilla=secrets.randbelow(2**31))
        try:
            plan = validar_plan(datos, len(imgs), n_escenas, aspectos, estricto=intento < INTENTOS_PLAN - 1,
                                zoom_maximo=topes)
            break
        except PlanInvalido as exc:
            motivo = str(exc)
    else:
        raise PlanInvalido(motivo)

    mapa = tomas.del_proyecto(carpeta, list(catalogo.tomas)) if catalogo else {}
    escenas = tuple(_escena(cliente, carpeta, n, e, imgs[e["imagen"]], fuente.imagenes[e["imagen"]], mapa, ancho,
                            alto, segundos_clip, chica_como_foto=not solo_animacion)
                    for n, e in enumerate(plan["escenas"], start=1))
    con_fotos_del_editor = any(nombre.startswith("usuario_") for nombre in fuente.imagenes)
    principales = {e.n: mapa[e.origen] for e in escenas if e.medio == armado.MEDIO_TOMA}
    por_archivo = {e.clip: principales[e.n].relativa for e in escenas if e.n in principales}
    # con las fotos del editor, el reel sale solo con ellas: no se le suman tomas
    if extras and catalogo and catalogo.tomas and not con_fotos_del_editor:
        escenas, por_archivo = armado.sumar_extras(escenas, carpeta, catalogo, principales, segundos_clip)
    if catalogo:
        catalogo.marcar(sorted(set(por_archivo.values())))  # para rotar: van al final de la fila
        tomas.anotar_usadas(carpeta, por_archivo)  # para cambiar despues una escena sin repetir tomas
    aviso = aviso_de_fotos_chicas(escenas, {e["imagen"] for e in plan["escenas"]
                                            if imagenes.estirada(imgs[e["imagen"]].size, 1.0, ancho, alto)},
                                  [e["imagen"] for e in plan["escenas"]])
    borrador = Plan(titular=plan["titular"], escenas=escenas, texto_post=plan["texto_post"],
                    advertencia=f"{aviso} {plan['advertencia']}".strip()[:MAX_ADVERTENCIA])
    if solo_animacion:
        return con_ortografia(borrador, fuente)
    if fuente.guion:
        return con_ortografia(con_guion_propio(borrador, fuente.guion), fuente)
    plan = pulir(cliente, borrador, fuente, hoy, segundos or n_escenas * 5, minimo_s, formato)
    dudas = verificador.verificar(cliente, fuente, plan, hoy)
    return replace(plan, advertencia=verificador.con_dudas(plan.advertencia, dudas))


def conocidas(fuente: Fuente) -> str:
    """El material: lo que nombra no es un error de ortografia aunque el diccionario no lo traiga."""
    return " ".join((fuente.titulo, fuente.nota_usuario, fuente.texto, *fuente.descripciones, *fuente.guion))


def con_ortografia(plan: Plan, fuente: Fuente) -> Plan:
    """Para los planes que no pasan por el guionista (que ya corrige la ortografia): avisa lo que parezca mal
    escrito."""
    malas = ortografia.dudosas([plan.titular, *(e.texto for e in plan.escenas), *(e.narracion for e in plan.escenas),
                                plan.texto_post], conocidas(fuente))
    if not malas:
        return plan
    aviso = f"Revisá la ortografía: {', '.join(malas)}."
    return replace(plan, advertencia=f"{aviso} {plan.advertencia}".strip()[:MAX_ADVERTENCIA])


def pulir(cliente: ClienteLlm, plan: Plan, fuente: Fuente, hoy: date, segundos: int, minimo_s: float,
          formato: str = "noticia") -> Plan:
    """Pasa el plan por el guionista. Si no devuelve nada usable, queda el borrador con un aviso."""
    try:
        g = guionista.escribir(cliente, fuente, guionista.vistas(plan.escenas), hoy, segundos,
                               plan.advertencia, minimo_s, formato=formato)
    except guionista.GuionInvalido as exc:
        log.warning("el guionista fallo, queda el borrador: %s", exc)
        return guionista.aplicar(plan, guionista.Guion(
            titular=plan.titular, textos=tuple(e.texto for e in plan.escenas),
            narraciones=tuple(e.narracion for e in plan.escenas), ganchos=plan.ganchos, texto_post=plan.texto_post,
            problemas=(f"quedó el borrador (el guionista falló: {exc}); probá /reescribir",)))
    return guionista.aplicar(plan, g)
