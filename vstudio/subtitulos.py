"""Subtitulos palabra por palabra (estilo reels): 2-3 palabras por vez y la que se esta diciendo, resaltada.

En Facebook los reels arrancan sin sonido en el feed: lo que retiene es leer lo que dice la voz. El texto exacto
es la narracion del guion; Whisper (faster-whisper en el venv de Chatterbox, CPU) solo dice cuando se dice cada
palabra. Si Whisper falla o no reconoce una palabra (cifras, nombres), el tiempo se reparte por largo de palabra
dentro del tramo con voz, asi que los subtitulos salen igual.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigSubtitulos

log = logging.getLogger(__name__)

MAX_PALABRAS_PAGINA = 3
MAX_CARACTERES_PAGINA = 14  # una sola linea (WrapStyle 2): con mas letras se saldria del cuadro
PAUSA_CORTE_S = 0.45  # una pausa mas larga empieza otra pagina
COLA_PAGINA_S = 0.25  # la ultima palabra de una pagina queda un poco despues de dicha
MIN_PALABRA_S = 0.05
FIN_DE_FRASE = re.compile(r"[.,;:!?…]$")
SIGNOS_FINALES = re.compile(r"[.,;:…]+$")
TAMANO = 150  # con 96 se veia chico en el celular; 14 letras de Anton a 150 px ocupan ~650 de 1080 px
MARGEN_INFERIOR = 520  # px desde abajo: por encima de los 480 que tapa la interfaz de Facebook
AMARILLO = r"{\c&H00D4FF&\fscx112\fscy112}"  # ASS usa BGR: amarillo (255, 212, 0)
# las cifras: naranja, entran grandes y se asientan (un "golpe" visual cuando la voz las dice)
CIFRA = r"{\c&H0078FF&\fscx150\fscy150\t(0,160,\fscx122\fscy122)}"
RE_CIFRA = re.compile(r"^(?:[0-9][0-9.,:]*%?|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|"
                      r"trece|catorce|quince|dieci\w+|veinte|veinti\w+|treinta|cuarenta|cincuenta|sesenta|setenta|"
                      r"ochenta|noventa|cien|ciento|\w+cientos|\w+cientas|quinientos|quinientas|mil|millón|millones|"
                      r"gigas?|GB|TB|dólares|euros)$", re.I)
ENCABEZADO = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, \
Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, \
MarginV, Encoding
Style: Palabras,Anton,{TAMANO},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,1,0,1,8,3,2,60,60,\
{MARGEN_INFERIOR},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


class SubtitulosError(RuntimeError):
    pass


@dataclass(frozen=True)
class Palabra:
    texto: str
    inicio: float
    fin: float


def _plano(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto.lower())
    return re.sub(r"[^a-z0-9]+", "", "".join(c for c in sin_tildes if not unicodedata.combining(c)))


def _repartir(tokens: list[str], tiempos: list[tuple[float, float] | None], inicio: float, fin: float) -> None:
    """Completa los huecos (palabras que Whisper no reconocio) repartiendo el tiempo por largo de palabra."""
    i, n = 0, len(tokens)
    while i < n:
        if tiempos[i] is not None:
            i += 1
            continue
        j = i
        while j < n and tiempos[j] is None:
            j += 1
        desde = tiempos[i - 1][1] if i > 0 else inicio
        hasta = max(tiempos[j][0] if j < n else fin, desde)
        pesos = [len(tokens[k]) + 1 for k in range(i, j)]
        t = desde
        for k, peso in zip(range(i, j), pesos):
            dur = (hasta - desde) * peso / sum(pesos)
            tiempos[k] = (t, t + dur)
            t += dur
        i = j


def alinear(narracion: str, oidas: list[Palabra], tramo: tuple[float, float]) -> list[Palabra]:
    """Las palabras del guion con los tiempos de Whisper (segundos desde el comienzo del audio)."""
    tokens = narracion.split()
    if not tokens:
        return []
    oidas = [p for p in oidas if _plano(p.texto)]
    tiempos: list[tuple[float, float] | None] = [None] * len(tokens)
    coincidencias = difflib.SequenceMatcher(None, [_plano(t) for t in tokens], [_plano(p.texto) for p in oidas],
                                            autojunk=False)
    for bloque in coincidencias.get_matching_blocks():
        for k in range(bloque.size):
            oida = oidas[bloque.b + k]
            tiempos[bloque.a + k] = (oida.inicio, oida.fin)
    _repartir(tokens, tiempos, *tramo)
    palabras, previo = [], tramo[0]
    for token, (inicio, fin) in zip(tokens, tiempos):
        inicio = max(inicio, previo)  # nunca para atras
        palabras.append(Palabra(token, round(inicio, 3), round(max(fin, inicio + MIN_PALABRA_S), 3)))
        previo = inicio
    return _unir_gta(palabras)


def _unir_gta(palabras: list[Palabra]) -> list[Palabra]:
    """En pantalla el juego va como "GTA VI" aunque la voz diga "GTA seis" (y no se parte entre paginas)."""
    unidas: list[Palabra] = []
    for p in palabras:
        if unidas and _plano(unidas[-1].texto) == "gta" and _plano(p.texto) in ("seis", "6", "vi"):
            previa = unidas.pop()
            sufijo = SIGNOS_FINALES.search(p.texto)
            unidas.append(Palabra("GTA VI" + (sufijo.group(0) if sufijo else ""), previa.inicio, p.fin))
        else:
            unidas.append(p)
    return unidas


def paginas(palabras: list[Palabra]) -> list[list[Palabra]]:
    """Grupos de hasta 3 palabras; se corta en la puntuacion y en las pausas, como se lee hablando."""
    grupos: list[list[Palabra]] = []
    actual: list[Palabra] = []
    for p in palabras:
        largo = len(" ".join(x.texto for x in [*actual, p]))
        if actual and (len(actual) >= MAX_PALABRAS_PAGINA or largo > MAX_CARACTERES_PAGINA
                       or FIN_DE_FRASE.search(actual[-1].texto) or p.inicio - actual[-1].fin > PAUSA_CORTE_S):
            grupos.append(actual)
            actual = []
        actual.append(p)
    if actual:
        grupos.append(actual)
    return grupos


def _tiempo_ass(segundos: float) -> str:
    cs = max(0, round(segundos * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs % 6000 / 100:05.2f}"


def _mostrar(texto: str) -> str:
    """Mayusculas, sin la puntuacion del final (salvo ? y !) y sin caracteres que ASS interpreta."""
    return SIGNOS_FINALES.sub("", re.sub(r"[{}\\]", "", texto)).upper()


def _resaltada(palabra: str, anterior: str = "") -> str:
    """La palabra que se esta diciendo: amarilla; si es una cifra, naranja y con salto ("una" no: casi siempre es
    un articulo; el "seis" de "GTA seis" tampoco: es el nombre del juego)."""
    limpia = re.sub(r"[^\w.,:%]", "", palabra).rstrip(".,:")
    es_cifra = RE_CIFRA.match(limpia) and not (limpia.lower() == "seis" and anterior.strip().upper() == "GTA")
    estilo = CIFRA if es_cifra else AMARILLO
    return estilo + _mostrar(palabra) + r"{\r}"


def eventos_ass(palabras: list[Palabra], desplazamiento: float, hasta: float | None = None) -> list[str]:
    """Una linea Dialogue por palabra: la pagina entera con esa palabra resaltada. `desplazamiento` pasa del
    tiempo del audio al del reel; `hasta` corta la ultima pagina (inicio de la voz siguiente)."""
    lineas = []
    grupos = paginas(palabras)
    for g, grupo in enumerate(grupos):
        tope = grupos[g + 1][0].inicio if g + 1 < len(grupos) else (hasta - desplazamiento if hasta else None)
        for k, palabra in enumerate(grupo):
            fin = grupo[k + 1].inicio if k + 1 < len(grupo) else grupo[-1].fin + COLA_PAGINA_S
            if tope is not None:
                fin = min(fin, tope)
            if fin <= palabra.inicio:
                continue
            texto = " ".join(_resaltada(p.texto, grupo[i - 1].texto if i else "") if i == k else _mostrar(p.texto)
                             for i, p in enumerate(grupo))
            lineas.append(f"Dialogue: 0,{_tiempo_ass(palabra.inicio + desplazamiento)},"
                          f"{_tiempo_ass(fin + desplazamiento)},Palabras,,0,0,0,,{texto}")
    return lineas


def archivo_ass(lineas: list[str], destino: Path) -> Path:
    destino.write_text(ENCABEZADO + "\n".join(lineas) + "\n", encoding="utf-8")
    return destino


def oir(cfg: ConfigSubtitulos, audios: list[Path], carpeta: Path) -> list[str]:
    """Lo que dice cada audio segun Whisper SIN el guion de pista (control de pronunciacion)."""
    resultado = tiempos_whisper(cfg, audios, [""] * len(audios), carpeta, sin_pista=True)
    return [" ".join(p.texto for p in palabras) for palabras in resultado]


def tiempos_whisper(cfg: ConfigSubtitulos, audios: list[Path], textos: list[str], carpeta: Path,
                    sin_pista: bool = False) -> list[list[Palabra]]:
    """Palabras que oye Whisper en cada audio. Levanta SubtitulosError si algo falla."""
    if not cfg.python.exists() or not cfg.script.exists():
        raise SubtitulosError(f"falta el venv o el script de Whisper ({cfg.python}, {cfg.script})")
    entrada = carpeta / "palabras_entrada.json"
    entrada.write_text(json.dumps([{"audio": str(a), "texto": t} for a, t in zip(audios, textos)],
                                  ensure_ascii=False), encoding="utf-8")
    try:
        modelo = cfg.modelo_revision if sin_pista else cfg.modelo
        cmd = [str(cfg.python), str(cfg.script), "--entrada", str(entrada), "--modelo", modelo,
               "--hilos", str(cfg.hilos), "--dispositivo", cfg.dispositivo] + (["--sin-pista"] if sin_pista else [])
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.timeout_s, check=False,
                             env={**os.environ, "HF_HUB_OFFLINE": "1"})
        linea = next((l for l in res.stdout.splitlines() if l.startswith("RESULTADO_JSON ")), None)
        if res.returncode != 0 or linea is None:
            raise SubtitulosError(f"Whisper fallo (codigo {res.returncode}): {res.stderr[-500:]}")
        datos = json.loads(linea.removeprefix("RESULTADO_JSON "))
        if not isinstance(datos, list) or len(datos) != len(audios):
            raise SubtitulosError("Whisper devolvio un resultado con otra forma")
        return [[Palabra(str(w["texto"]), float(w["inicio"]), float(w["fin"])) for w in por_audio]
                for por_audio in datos]
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SubtitulosError(f"Whisper fallo: {exc}") from exc
