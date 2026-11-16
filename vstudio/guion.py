"""Criterio de guion: que suene a una persona contando la noticia (no a un robot leyendo datos) y que retenga.

Las reglas van en el prompt del guionista (reglas()) y lo que se puede medir se chequea aca (revisar): si el
guion no cumple, el guionista lo corrige; en el ultimo intento los problemas quedan como advertencia para que el
editor los arregle con /narracion o pida /reescribir.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

PALABRAS_POR_SEGUNDO = 3.1  # habla de Chatterbox es-419 sin silencios en los bordes: 190 palabras en 60,7 s (b29891d1)
PALABRAS_GANCHO = 12  # lo que se pide para la primera frase
MAX_PALABRAS_GANCHO = 15  # lo que se acepta
PALABRAS_ESCENA = (13, 18)  # lo que se pide por escena (~4,5-6 s de habla: llena el clip de 5 s)
LIMITES_ESCENA = (9, 21)  # lo que se acepta
ALTERNATIVOS_PEDIDOS = 2
MAX_GANCHOS_ALTERNATIVOS = 3
MAX_TITULAR = 70
MAX_TEXTO = 90
MAX_NARRACION = 220
MAX_POST = 1000
MAX_ADVERTENCIA = 500
MIN_ESCENAS_REPETIDA = 3  # una misma frase de 3 palabras en 3 escenas o mas suena a disco rayado
MAX_REPETIDAS_INFORMADAS = 2

# arranques que no dicen nada y hacen que la gente pase de largo (se comparan sin tildes ni signos)
APERTURAS_PROHIBIDAS = ("hola", "buenas", "bienvenid", "que tal", "como estan", "amigos", "atencion",
                        "en este video", "en este reel", "en el video de hoy", "hoy les traigo", "hoy te traigo",
                        "hoy vamos a", "hoy les cuento", "hoy te cuento")
# cebo de interaccion: Facebook le baja el alcance (pedir reacciones, etiquetar, compartir o "comentar SI")
CEBO = (r"\b(comenta|escribi|escriban|pone|pongan)\w*\s+(si|yes|amen|un emoji)\b",
        r"\betiquet\w*", r"\bcompart\w*\s+(si|con\s+(tus|\d+))\b",
        r"\b(dale|dejame|deja|dejen|pone|regalame)\w*\s+(tu\s+|un\s+)?(like|me gusta|corazon)",
        r"\breaccion\w*\s+con\b")
# escenas que anuncian en vez de informar: 5 s sin dato es donde mas gente se va (se mira desde la escena 3)
RELLENO = (r"\b(te |la |lo |los |las )?revel(o|amos)\b", r"\bsin vueltas\b", r"\bte (lo |la )?(digo|cuento)\b",
           r"\b(ahora|ya) (te |les )?(digo|cuento|muestro)\b", r"\b(quedate|quedense|mira|miren) hasta el final\b",
           r"\bte (estaba|estoy) guardando\b")
# muletillas: una vez suenan naturales; repetidas, a locutor automatico (el guion de b29891d1 repetia "ojo" 3 veces)
MULETILLAS = ("ojo", "atento", "atentos", "atencion", "mira esto", "fijate", "agarrate", "y aca viene", "lo raro",
              "increible", "una locura")
# anglicismos con palabra en espanol y jerga de nota de prensa traducida
JERGA = ((r"\bpre (load|carga|descarga)\b|\bpreload\b", "decí \"la precarga\""),
         (r"\bcabe (destacar|mencionar|senalar|resaltar)\b", "\"cabe destacar\" es de nota de prensa"),
         (r"\bes importante (destacar|mencionar|senalar)\b", "\"es importante destacar\" es de nota de prensa"),
         (r"\ben todas las plataformas\b", "\"en todas las plataformas\" es de nota de prensa: decí cuáles"))
# voseo: la voz (Chatterbox es-419, entrenada con espanol de Mexico y Latinoamerica) lo pronuncia mal: "confiás"
# salio bien 0 de 3 veces y "confías" 3 de 3; "podés" 1 de 3 y "puedes" 2-3 de 3
VOSEO = re.compile(r"(?<!\w)(vos|sos|tenés|podés|querés|sabés|venís|decís|pedís|mirá|fijate|hacé|decí|andá|vení|"
                   r"pensá|dejá|activá|liberá|borrá|confiás|esperás|cargás|jugás|comprás|preferís|tené|ponete|"
                   r"acordate|imaginate)(?!\w)")  # con tilde: sin ella son formas de tú ("esperas", "mira")
VACIAS = frozenset(("de", "del", "la", "el", "los", "las", "un", "una", "y", "o", "que", "en", "a", "al", "no", "se",
                    "te", "tu", "es", "lo", "con", "por", "para", "su", "mas", "ya",
                    "gta", "seis"))  # el nombre del juego se repite por fuerza: no cuenta como repeticion

REGLAS_GUION = """CÓMO TIENE QUE SONAR (un fan que se acaba de enterar y se lo cuenta a un amigo, no un noticiero ni una
lista de datos leída por un robot):
- Un hilo, no una lista: elegí la tensión central de la noticia (lo que al fan le importa de verdad) y que cada
  escena la empuje. Ordená por impacto, no en el orden de la nota. Lo que hace que esto sea noticia HOY (lo que
  cambió, se anunció o se movió de fecha) no puede faltar.
- Pocos datos bien contados valen más que todos sueltos: un dato puede ocupar dos escenas (el dato, y después qué
  significa para el que mira: qué tiene que hacer, qué gana, qué pierde). Los detalles menores van al post o se
  dejan afuera.
- Reacción de fan en algunas escenas (sorpresa, ironía, alivio, bronca), breve y con motivo. Nada de euforia vacía.
  La reacción es tuya: no inventes lo que opina "la gente" ni detalles que la nota no dice.
- Frases completas, como se hablan, con verbo: nada de estilo telegrama (sustantivo, dos puntos y dato).
- Ritmo variado: a veces una frase corta que pega y otra larga; a veces una sola frase que fluye. Cada escena
  termina donde la voz hace una pausa natural (punto, "?" o "!").
- Encadená: cada escena retoma, contrasta o sube lo anterior. Ninguna muletilla ni conector se repite: cada uno,
  una vez como máximo en todo el guion.
- Ninguna escena solo anuncia lo que viene: si anunciás, el dato va en la misma frase.
- Palabras de todos los días: nada de anglicismos que tienen palabra en español, de jerga de nota de prensa
  ("cabe destacar", "confirmado por", "en todas las plataformas", "oficialmente") ni de siglas que nadie dice.
- Una misma cifra o fecha se dice como máximo dos veces en todo el guion.
- Puntuación para la voz: comas donde se respira; "¡!" y "¿?" donde corresponde; sin punto y coma, paréntesis,
  comillas ni barras. Números y siglas como se pronuncian ("GTA seis", "diez de noviembre").
- Solo hechos de la nota; lo no confirmado va como rumor. No inventes cifras, fechas ni comparaciones (tampoco
  con otros juegos de la saga). Respetá los matices: si la nota dice "hasta", "según" o "se espera", decilo así.
- En la narración, todos los números con letras ("doscientos doce", nunca "212"): la voz lee mal las cifras.

ESTRUCTURA (la gente decide en 3 segundos si se queda; cada escena tiene que ganarse la siguiente):
1. GANCHO (escena 1): hasta {gancho} palabras que metan al espectador de golpe: el dato que más sorprende, una
   pregunta que abra curiosidad o una afirmación fuerte. Nombrá GTA seis. Nada de saludos, "atención", "en este
   video" ni el nombre de la página.
2. PROMESA (escena 2): anticipá algo concreto de la nota que se cuenta recién al final (un dato nuevo, no lo que
   todos ya saben) y que de verdad se cumpla. No lo reveles antes de la anteúltima escena.
3. DESARROLLO: cada vez más fuerte; cada 3 o 4 escenas, un giro que renueve la atención, siempre con un dato.
4. REMATE: cumplí la promesa en la anteúltima o en la última escena.
5. CIERRE (última escena): UNA pregunta concreta y opinable que termine en "?". Sin "seguinos", sin pedir likes,
   etiquetar ni compartir y sin "comentá SÍ": Facebook le baja el alcance a eso.

LARGO: cada escena, una o dos frases de {min_palabras} a {max_palabras} palabras (la escena 1, hasta {gancho}). No
cuentes palabras una por una ni repases el guion varias veces: el largo lo mido yo y, si algo no entra, te lo marco.

"ganchos_alternativos": otros {alternativos} ganchos distintos para la escena 1 (titular + narración), con otro
ángulo: dato, pregunta, afirmación."""

GTA_DICHO = re.compile(r"\bGTA\s*(?:SEIS|6)\b", re.IGNORECASE)
# "pre-load" se traduce mal una y otra vez ("el pre-carga", "pre-descarga"): se corrige sin gastar otro intento
_PRE = r"pre[- ]?(?:load|carga|descarga)"
CORRECCIONES = ((re.compile(rf"\bdel\s+{_PRE}\b", re.IGNORECASE), "de la precarga"),
                (re.compile(rf"\bal\s+{_PRE}\b", re.IGNORECASE), "a la precarga"),
                (re.compile(rf"\b(?:el|un)\s+{_PRE}\b", re.IGNORECASE), "la precarga"),
                (re.compile(rf"\b{_PRE}\b", re.IGNORECASE), "precarga"))


def reglas() -> str:
    return REGLAS_GUION.format(gancho=PALABRAS_GANCHO, min_palabras=PALABRAS_ESCENA[0],
                               max_palabras=PALABRAS_ESCENA[1], alternativos=ALTERNATIVOS_PEDIDOS)


def _con_el_caso_de(original: str, nuevo: str) -> str:
    if original.isupper():
        return nuevo.upper()
    return nuevo[0].upper() + nuevo[1:] if original[:1].isupper() else nuevo


def corregir_palabras(texto: str) -> str:
    """Correcciones fijas de palabras que el LLM traduce mal (ver CORRECCIONES)."""
    for patron, bien in CORRECCIONES:
        texto = patron.sub(lambda m, bien=bien: _con_el_caso_de(m.group(0), bien), texto)
    return texto


def en_pantalla(texto: str) -> str:
    """En pantalla el juego va como "GTA VI" (en la narracion se dice "GTA seis")."""
    return corregir_palabras(GTA_DICHO.sub("GTA VI", texto))


def para_voz(texto: str) -> str:
    """Saca lo que la voz lee mal o con pausas raras: punto y coma, parentesis, comillas y barras."""
    limpio = re.sub(r"[()\[\]\"«»“”/]", " ", corregir_palabras(texto).replace(";", ","))
    return " ".join(limpio.split())


def _plano(texto: str) -> str:
    """Minusculas, sin tildes y sin signos: para comparar arranques y buscar cebo."""
    sin_tildes = unicodedata.normalize("NFKD", texto.lower())
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", sin_tildes).split())


def palabras(texto: str) -> int:
    return len(re.findall(r"\w+", texto))


def segundos_de_voz(texto: str) -> float:
    """Estimacion de lo que dura la narracion dicha por la voz (para prever el largo del reel)."""
    return palabras(texto) / PALABRAS_POR_SEGUNDO


def palabras_para(segundos_de_habla: float) -> int:
    """Palabras que hacen falta para llenar esos segundos de voz."""
    return math.ceil(max(segundos_de_habla, 0.0) * PALABRAS_POR_SEGUNDO)


def _lista(numeros: list[int]) -> str:
    return ", ".join(map(str, numeros[:-1])) + f" y {numeros[-1]}" if len(numeros) > 1 else str(numeros[0])


def problemas_gancho(narracion: str) -> list[str]:
    plano = _plano(narracion)
    problemas = []
    if palabras(narracion) > MAX_PALABRAS_GANCHO:
        problemas.append(f"el gancho (escena 1) tiene {palabras(narracion)} palabras; máximo {MAX_PALABRAS_GANCHO}")
    arranque = next((a for a in APERTURAS_PROHIBIDAS if plano.startswith(a)), None)
    if arranque:
        problemas.append(f"el gancho arranca con \"{arranque}\": tiene que entrar directo a la noticia")
    return problemas


def _problemas_largo(narraciones: list[str], palabras_minimas: int) -> list[str]:
    minimo, maximo = LIMITES_ESCENA
    cortas = [n for n, t in enumerate(narraciones[1:], start=2) if palabras(t) < minimo]
    largas = [n for n, t in enumerate(narraciones[1:], start=2) if palabras(t) > maximo]
    problemas = []
    if cortas:
        sujeto = f"la escena {cortas[0]} tiene" if len(cortas) == 1 else f"las escenas {_lista(cortas)} tienen"
        problemas.append(f"{sujeto} menos de {minimo} palabras: sumá qué significa el dato o una reacción "
                         f"(apuntá a {PALABRAS_ESCENA[0]}-{PALABRAS_ESCENA[1]})")
    if largas:
        sujeto = f"la escena {largas[0]} tiene" if len(largas) == 1 else f"las escenas {_lista(largas)} tienen"
        problemas.append(f"{sujeto} más de {maximo} palabras: recortá")
    total = sum(palabras(t) for t in narraciones)
    if total < palabras_minimas:
        problemas.append(f"el guion tiene {total} palabras y para pasar del minuto hacen falta al menos "
                         f"{palabras_minimas}: alargá las escenas con lo que significa cada dato")
    return problemas


def _repeticiones(narraciones: list[str]) -> list[str]:
    """Frases de 3 palabras (con al menos 2 que no sean vacias ni el nombre del juego) en muchas escenas."""
    conteo: Counter = Counter()
    for texto in narraciones:
        w = _plano(texto).split()
        conteo.update({" ".join(w[i:i + 3]) for i in range(len(w) - 2)
                       if sum(x not in VACIAS for x in w[i:i + 3]) >= 2})
    repetidas = [f for f, c in conteo.most_common() if c >= MIN_ESCENAS_REPETIDA][:MAX_REPETIDAS_INFORMADAS]
    return [f"repetís \"{f}\" en {conteo[f]} escenas: decilo una o dos veces" for f in repetidas]


def _muletillas(narraciones: list[str]) -> list[str]:
    plano = " " + " ".join(_plano(t) for t in narraciones) + " "
    usos = {m: len(re.findall(rf"\b{m}\b", plano)) for m in MULETILLAS}
    return [f"repetís la muletilla \"{m}\" {c} veces: una como máximo" for m, c in usos.items() if c > 1]


def revisar(narraciones: list[str], palabras_minimas: int = 0) -> list[str]:
    """Problemas medibles del guion (lista vacia = cumple)."""
    if not narraciones:
        return ["no hay narración"]
    problemas = problemas_gancho(narraciones[0]) + _problemas_largo(narraciones, palabras_minimas)
    if "?" not in narraciones[-1]:
        problemas.append("la última escena tiene que cerrar con UNA pregunta concreta (con \"?\")")
    for n, texto in enumerate(narraciones[2:], start=3):  # la escena 2 es la promesa: ahi anunciar vale
        if any(re.search(patron, _plano(texto)) for patron in RELLENO):
            problemas.append(f"la escena {n} anuncia sin dar un dato (relleno): decí el dato en la misma frase")
    for n, texto in enumerate(narraciones, start=1):
        if any(re.search(patron, _plano(texto)) for patron in CEBO):
            problemas.append(f"la escena {n} pide likes, etiquetar, compartir o \"comentar SÍ\" (cebo): sacalo")
    con_cifras = [n for n, t in enumerate(narraciones, start=1) if re.search(r"\d", t)]
    if con_cifras:
        donde = f"la escena {con_cifras[0]}" if len(con_cifras) == 1 else f"las escenas {_lista(con_cifras)}"
        problemas.append(f"{donde} tiene números en cifras: escribilos con letras (la voz lee mal \"212\")")
    plano_total = " ".join(_plano(t) for t in narraciones)
    problemas += [motivo for patron, motivo in JERGA if re.search(patron, plano_total)]
    voseo = sorted({m.group(0) for t in narraciones for m in VOSEO.finditer(t.lower())})
    if voseo:
        problemas.append(f"usa tú, no vos (la voz pronuncia mal el voseo): {', '.join(voseo)}")
    return problemas + _muletillas(narraciones) + _repeticiones(narraciones)


def ganchos_validos(datos: object) -> list[dict]:
    """Ganchos alternativos que cumplen las reglas (los invalidos se descartan sin fallar el plan)."""
    if not isinstance(datos, list):
        return []
    validos = []
    for g in datos:
        if not isinstance(g, dict):
            continue
        titular, narracion = str(g.get("titular", "")).strip(), str(g.get("narracion", "")).strip()
        if titular and narracion and len(titular) <= MAX_TITULAR and not problemas_gancho(narracion):
            validos.append({"titular": titular, "narracion": narracion})
    return validos[:MAX_GANCHOS_ALTERNATIVOS]
