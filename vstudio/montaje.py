"""Arma el reel 1080x1920: clips + fundidos + titular gancho + subtitulos + narracion + musica opcional.

Con narracion, la voz marca el ritmo: cada escena dura lo que dura su frase mas una pausa natural (el clip se
corta antes o se estira), como un editor que corta sobre la locucion. Antes cada escena duraba al menos el clip
entero y entre frase y frase quedaban 0,9-1,5 s de silencio: sonaba a robot leyendo una lista.

FFmpeg siempre se invoca con lista de argumentos (sin shell) y el resultado se valida con ffprobe.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from . import efectos, subtitulos, textos_png, visuales
from .tomas import MAX_SEGMENTO_S

W, H, FPS = visuales.ANCHO, visuales.ALTO, visuales.FPS
FUNDIDO_S = 0.4
MAX_LENTO = visuales.MAX_LENTO  # cuanto se puede estirar un clip para que entre la narracion
COLA_COMPUESTA_S = 0.1  # ultimo cuadro repetido al final de una escena unida: nunca queda corta para el fundido
CLIP_CORTO_S = 4.0  # menos que esto es una toma de video real (los clips de IA duran 5 s)
MAX_CONGELADO_S = 0.3  # lo que una toma corta puede quedar quieta en su ultimo cuadro antes de repetirla
ENTRADA_VOZ_S = 0.25  # la voz arranca apenas empieza la escena (durante el fundido)
PAUSA_ENTRE_ESCENAS_S = 0.25  # silencio entre la frase de una escena y la de la siguiente
COLA_FINAL_S = 1.2  # la ultima escena se queda un momento despues de la pregunta del cierre
MIN_ESCENA_S = 2.0  # para que se llegue a leer el texto de pantalla
SILENCIO_DB = -40  # por debajo de esto, el borde de un audio de voz se considera silencio
SILENCIO_MIN_S = 0.05
MARGEN_VOZ_S = (0.03, 0.08)  # lo que se deja antes y despues del habla al recortar los silencios de los bordes
TITULAR_S = 2.8
ZONA_SEGURA_INFERIOR = 480  # px de abajo que tapa la interfaz de Reels/Facebook
TOLERANCIA_DURACION_S = 0.25
UMBRAL_MONETIZABLE_S = 60  # los reels de 60 s o mas se piensan para monetizar: tienen que pasar del minuto
MARGEN_PISO_S = 0.05  # para que el redondeo nunca deje el reel justo debajo del piso
MAX_BYTES_SALIDA = 45 * 1024 * 1024  # Telegram (bots) acepta hasta 50 MB por archivo
AUDIO_KBPS = 192
MAX_VIDEO_KBPS = 8000
MARGEN_BITRATE = 0.92  # el tope de VBV no es exacto: se deja aire para el contenedor
# ritmo: a mitad de cada escena, un corte seco con un acercamiento del mismo clip: el ojo lo toma como
# otra toma y la imagen cambia cada ~2,5 s en vez de cada 5 (sin generar mas clips)
PUNCH_IN = 1.2
MIN_ESCENA_PARA_CORTE_S = 3.0
ALTO_BARRA = 10
COLOR_BARRA = "0xFFD400"


def bitrate_video_kbps(duracion_s: float) -> int:
    """Tope de bitrate de video para que el reel entre en MAX_BYTES_SALIDA (y nunca mas de 8 Mbps)."""
    total_kbps = MAX_BYTES_SALIDA * 8 / 1000 / max(duracion_s, 1.0)
    return int(min(MAX_VIDEO_KBPS, (total_kbps - AUDIO_KBPS) * MARGEN_BITRATE))


class MontajeError(RuntimeError):
    pass


@dataclass(frozen=True)
class EscenaMontaje:
    clip: Path
    texto: str
    audio: Path | None = None
    narracion: str = ""  # lo que dice la voz: con texto, la escena lleva subtitulos palabra por palabra
    palabras: tuple[subtitulos.Palabra, ...] = ()  # lo que oyo Whisper (vacio = se reparte por largo de palabra)
    extras: tuple[Path, ...] = ()  # tomas de video que siguen al clip dentro de la escena (cortes secos)


@dataclass(frozen=True)
class Voz:
    """Tramo del audio que tiene habla (sin los silencios de los bordes)."""
    escena: int
    entrada: int  # indice del -i de ffmpeg
    inicio: float
    fin: float

    @property
    def segundos(self) -> float:
        return self.fin - self.inicio


@dataclass(frozen=True)
class Tiempos:
    duraciones: tuple[float, ...]  # duracion final de cada escena
    factores: tuple[float, ...]  # estiramiento del clip (1.0 = velocidad original)
    relleno: tuple[float, ...]  # segundos de ultimo cuadro congelado
    inicios: tuple[float, ...]  # inicio de cada escena en el reel (con fundidos)
    total: float


def duracion(ruta: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(ruta)],
                         capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def piso_s(objetivo_s: float, minimo_monetizable_s: float) -> float:
    """Duracion minima del reel: los pensados para monetizar no pueden quedar por debajo del minuto."""
    return minimo_monetizable_s if objetivo_s >= UMBRAL_MONETIZABLE_S else 0.0


def _estirar(objetivos: list[float], clips_s: list[float], falta: float) -> list[float]:
    """Reparte `falta` segundos estirando los clips (hasta MAX_LENTO) segun el margen de cada uno; lo que no
    entra estirando lo sostiene la ultima escena."""
    margen = [max(0.0, c * MAX_LENTO - o) for c, o in zip(clips_s, objetivos)]
    total_margen = sum(margen)
    usar = min(falta, total_margen)
    nuevos = [o + (m / total_margen * usar if total_margen else 0.0) for o, m in zip(objetivos, margen)]
    nuevos[-1] += falta - usar
    return nuevos


def recorte_de_silencios(eventos: list[tuple[str, float]], total_s: float) -> tuple[float, float]:
    """Inicio y fin del habla segun los eventos de silencedetect ("start"/"end", segundos)."""
    inicio, fin = 0.0, total_s
    if len(eventos) >= 2 and eventos[0][0] == "start" and eventos[0][1] <= 0.01 and eventos[1][0] == "end":
        inicio = max(0.0, eventos[1][1] - MARGEN_VOZ_S[0])
    if eventos and eventos[-1][0] == "start":  # termina en silencio y ffmpeg no llego a cerrarlo
        fin = min(total_s, eventos[-1][1] + MARGEN_VOZ_S[1])
    elif len(eventos) >= 2 and eventos[-2][0] == "start" and eventos[-1][1] >= total_s - 0.02:
        fin = min(total_s, eventos[-2][1] + MARGEN_VOZ_S[1])
    if fin - inicio < 0.2:  # todo silencio o algo raro: mejor no recortar
        return 0.0, total_s
    return round(inicio, 3), round(fin, 3)


def tramo_de_voz(ruta: Path) -> tuple[float, float]:
    """Inicio y fin del habla de un audio (los TTS dejan ~0,1 s antes y ~0,55 s de silencio despues)."""
    res = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(ruta), "-af",
                          f"silencedetect=n={SILENCIO_DB}dB:d={SILENCIO_MIN_S}", "-f", "null", "-"],
                         capture_output=True, text=True, check=True)
    eventos = [(tipo, float(valor)) for tipo, valor in re.findall(r"silence_(start|end): (-?[0-9.]+)", res.stderr)]
    return recorte_de_silencios(eventos, duracion(ruta))


def voz_necesaria_s(n_escenas: int, total_s: float) -> float:
    """Segundos de habla (sin silencios) para que un reel de n escenas narradas dure total_s."""
    return total_s - (n_escenas - 1) * PAUSA_ENTRE_ESCENAS_S - ENTRADA_VOZ_S - COLA_FINAL_S


def _objetivo(clip_s: float, voz_s: float, ultima: bool) -> float:
    """Duracion de una escena: sin voz, el clip entero; con voz, la frase + la pausa hasta la siguiente."""
    if not voz_s:
        return clip_s
    if ultima:
        return max(MIN_ESCENA_S, ENTRADA_VOZ_S + voz_s + COLA_FINAL_S)
    # la escena siguiente entra con un fundido y su voz arranca ENTRADA_VOZ_S despues: entre frase y frase queda
    # duracion - FUNDIDO_S - voz_s = PAUSA_ENTRE_ESCENAS_S de silencio
    return max(MIN_ESCENA_S, voz_s + FUNDIDO_S + PAUSA_ENTRE_ESCENAS_S)


def calcular_tiempos(clips_s: list[float], audios_s: list[float], minimo_s: float = 0.0) -> Tiempos:
    """`audios_s`: segundos de habla de cada escena (0 = sin voz)."""
    ultima = len(clips_s) - 1
    objetivos = [_objetivo(c, a, i == ultima) for i, (c, a) in enumerate(zip(clips_s, audios_s))]
    total = sum(objetivos) - (len(objetivos) - 1) * FUNDIDO_S
    if total < minimo_s:
        objetivos = _estirar(objetivos, clips_s, minimo_s - total + MARGEN_PISO_S)
    duraciones, factores, relleno = [], [], []
    for clip_s, objetivo in zip(clips_s, objetivos):
        factor = min(max(objetivo / clip_s, 1.0), MAX_LENTO)  # si la frase es corta, el clip se corta antes
        duraciones.append(round(objetivo, 4))
        factores.append(round(factor, 4))
        relleno.append(round(max(0.0, objetivo - clip_s * factor), 4))
    inicios = tuple(round(sum(duraciones[:i]) - i * FUNDIDO_S, 4) for i in range(len(duraciones)))
    return Tiempos(tuple(duraciones), tuple(factores), tuple(relleno), inicios,
                   round(inicios[-1] + duraciones[-1], 4))


def ruta_para_filtro(ruta: Path) -> str:
    """Ruta como valor de una opcion de filtro de FFmpeg (en Windows, "C:" tiene que ir escapado)."""
    return "'" + str(ruta).replace("\\", "/").replace("'", "\\'").replace(":", "\\:") + "'"


def cortes_de(t: Tiempos) -> dict[int, float]:
    """Donde va el corte con acercamiento de cada escena (segundos desde que empieza): a la mitad, en las que
    duran lo suficiente para que las dos tomas se lean."""
    return {i: round(d / 2, 3) for i, d in enumerate(t.duraciones) if d >= MIN_ESCENA_PARA_CORTE_S}


def _con_corte(i: int, medio: float) -> list[str]:
    """La escena i en dos tomas: la segunda es un acercamiento del mismo clip (arriba o al centro, alternando)."""
    ancho, alto = round(W * PUNCH_IN / 2) * 2, round(H * PUNCH_IN / 2) * 2
    y = int((alto - H) * (0.5 if i % 2 == 0 else 0.25))
    return [f"[v{i}]split=2[v{i}a][v{i}b]",
            f"[v{i}a]trim=duration={medio:.3f},setpts=PTS-STARTPTS[v{i}p]",
            f"[v{i}b]trim=start={medio:.3f},setpts=PTS-STARTPTS,scale={ancho}:{alto}:flags=lanczos,"
            f"crop={W}:{H}:{(ancho - W) // 2}:{y},setsar=1[v{i}q]",
            # fps despues de unir: concat deja otra base de tiempo y xfade no acepta mezclarla con la de una
            # escena sin corte (pasaba con una escena de menos de 3 s entre otras largas)
            f"[v{i}p][v{i}q]concat=n=2:v=1:a=0,fps={FPS}[v{i}c]"]


def grafo_video(t: Tiempos, idx_titular: int | None, idx_subs: list[int], idx_marca: int | None,
                ass: tuple[Path, Path] | None = None, titular_s: float = TITULAR_S,
                cortes: dict[int, float] | None = None, idx_fuente: int | None = None,
                barra: bool = False) -> tuple[str, str]:
    """Filtro de video. Devuelve (filtergraph, etiqueta_salida). `ass` = (subtitulos palabra por palabra, carpeta
    de fuentes); con subtitulos no van los textos por escena (`idx_subs` vacio): serian texto de mas.
    `idx_titular` None = sin titular (solo animacion): la marca va desde el principio. `cortes`: escena -> segundo
    del corte con acercamiento; `idx_fuente`: el sello "FUENTE: ..."; `barra`: barra de progreso arriba."""
    n = len(t.duraciones)
    partes, etiquetas = [], []
    for i in range(n):
        partes.append(
            f"[{i}:v]setpts={t.factores[i]}*(PTS-STARTPTS),fps={FPS},scale={W}:{H}:flags=lanczos,setsar=1,"
            f"format=yuv420p,tpad=stop_mode=clone:stop_duration={t.relleno[i]},"
            f"trim=duration={t.duraciones[i]},setpts=PTS-STARTPTS[v{i}]")
        if cortes and i in cortes:
            partes += _con_corte(i, cortes[i])
            etiquetas.append(f"v{i}c")
        else:
            etiquetas.append(f"v{i}")
    previo = etiquetas[0]
    for i in range(1, n):
        partes.append(f"[{previo}][{etiquetas[i]}]xfade=transition=fade:duration={FUNDIDO_S}:"
                      f"offset={t.inicios[i]}[x{i}]")
        previo = f"x{i}"
    if idx_titular is None:
        titular_s = 0.0
    else:
        partes.append(f"[{idx_titular}:v]format=rgba,fade=t=out:st={titular_s - 0.3:.3f}:d=0.3:alpha=1[tit]")
        partes.append(f"[{previo}][tit]overlay=0:0:enable='lt(t,{titular_s:.3f})'[o0]")
        previo = "o0"
    for i, idx in enumerate(idx_subs):
        ini = t.inicios[i] + ENTRADA_VOZ_S  # el subtitulo acompana a la voz (el titular va arriba, no se pisan)
        fin = t.inicios[i] + t.duraciones[i] - (FUNDIDO_S if i < n - 1 else 0.1)
        partes.append(f"[{idx}:v]format=rgba,fade=t=in:st={ini:.3f}:d=0.25:alpha=1,"
                      f"fade=t=out:st={fin - 0.25:.3f}:d=0.25:alpha=1[s{i}]")
        partes.append(f"[{previo}][s{i}]overlay=0:{H - ZONA_SEGURA_INFERIOR}-h:"
                      f"enable='between(t,{ini:.3f},{fin:.3f})'[o{i + 1}]")
        previo = f"o{i + 1}"
    if idx_marca is not None:
        partes.append(f"[{previo}][{idx_marca}:v]overlay=40:60:enable='gte(t,{titular_s:.3f})'[om]")
        previo = "om"
    if idx_fuente is not None:
        y_fuente = 60 + (88 if idx_marca is not None else 0)
        partes.append(f"[{previo}][{idx_fuente}:v]overlay=40:{y_fuente}:enable='gte(t,{titular_s:.3f})'[of]")
        previo = "of"
    if barra:  # crece de izquierda a derecha durante todo el reel
        partes.append(f"color=c={COLOR_BARRA}:s={W}x{ALTO_BARRA}:r={FPS}:d={t.total:.3f},format=rgba[barra]")
        partes.append(f"[{previo}][barra]overlay=x='-w+w*t/{t.total:.3f}':y=0:eval=frame:shortest=1[ob]")
        previo = "ob"
    if ass:
        partes.append(f"[{previo}]ass={ruta_para_filtro(ass[0])}:fontsdir={ruta_para_filtro(ass[1])}[sub]")
        previo = "sub"
    return ";".join(partes), previo


def subtitulos_del_reel(escenas: list[EscenaMontaje], tramos: list[tuple[float, float] | None], t: Tiempos,
                        destino: Path) -> Path | None:
    """Arma el .ass con las palabras de cada escena narrada, en el tiempo del reel. None si no hay narracion."""
    lineas = []
    for i, (e, tramo) in enumerate(zip(escenas, tramos)):
        if not (e.audio and tramo and e.narracion.strip()):
            continue
        palabras = subtitulos.alinear(e.narracion, list(e.palabras), tramo)
        # el audio entra recortado desde tramo[0] y suena desde inicio + ENTRADA_VOZ_S
        desplazamiento = t.inicios[i] + ENTRADA_VOZ_S - tramo[0]
        siguiente = next((t.inicios[k] + ENTRADA_VOZ_S for k in range(i + 1, len(escenas))
                          if escenas[k].audio and tramos[k]), t.total)
        lineas += subtitulos.eventos_ass(palabras, desplazamiento, hasta=siguiente)
    return subtitulos.archivo_ass(lineas, destino) if lineas else None


def grafo_audio(t: Tiempos, voces: list[Voz], idx_musica: int | None, volumen_musica: float,
                efectos: list[tuple[int, list[float]]] | None = None,
                volumen_efectos: float = 0.35) -> tuple[str, str] | None:
    """Narracion (cada voz al inicio de su escena) + musica de fondo (que baja sola cuando habla la voz) +
    efectos (`efectos`: entrada -> segundos donde suena). None si no hay audio.

    El recorte de los silencios de los bordes va en la entrada (-ss/-to, ver _entradas): con atrim dentro del
    grafo el encoder AAC recibia NaN en ~15 % de los renders (medido: 9 de 60; con -ss/-to, 0 de 30)."""
    partes, mezcla, voces_et = [], [], []
    for v in voces:
        ms = int((t.inicios[v.escena] + ENTRADA_VOZ_S) * 1000)
        partes.append(f"[{v.entrada}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms}[a{v.escena}]")
        voces_et.append(f"[a{v.escena}]")
    if voces_et:
        partes.append(f"{''.join(voces_et)}amix=inputs={len(voces_et)}:normalize=0:duration=longest[voz]"
                      if len(voces_et) > 1 else f"{voces_et[0]}anull[voz]")
    if idx_musica is not None:
        partes.append(f"[{idx_musica}:a]aresample=48000,aformat=channel_layouts=stereo,"
                      f"atrim=duration={t.total},volume={volumen_musica}[mus]")
        if voces_et:  # la voz manda: la musica se agacha cuando hay habla
            partes.append("[voz]asplit=2[vozm][vozsc]")
            partes.append("[mus][vozsc]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=400[musd]")
            mezcla += ["[vozm]", "[musd]"]
        else:
            mezcla.append("[mus]")
    elif voces_et:
        mezcla.append("[voz]")
    for k, (idx, tiempos) in enumerate(efectos or []):
        if not tiempos:
            continue
        salidas = "".join(f"[e{k}_{j}]" for j in range(len(tiempos)))
        reparto = f"asplit={len(tiempos)}{salidas}" if len(tiempos) > 1 else f"anull{salidas}"
        partes.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,volume={volumen_efectos},{reparto}")
        for j, segundo in enumerate(tiempos):
            ms = int(segundo * 1000)
            partes.append(f"[e{k}_{j}]adelay={ms}|{ms}[e{k}d{j}]")
            mezcla.append(f"[e{k}d{j}]")
    if not mezcla:
        return None
    partes.append(f"{''.join(mezcla)}amix=inputs={len(mezcla)}:normalize=0:duration=longest,"
                  f"atrim=duration={t.total},loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
                  f"afade=t=out:st={max(0.0, t.total - 0.6):.3f}:d=0.6[aout]")
    return ";".join(partes), "aout"


@dataclass(frozen=True)
class _Entradas:
    args: tuple[str, ...]
    titular: int | None
    subs: tuple[int, ...]
    marca: int | None
    voces: tuple[Voz, ...]
    musica: int | None
    siguiente: int
    fuente: int | None = None
    efectos: tuple[tuple[str, int], ...] = ()


def _entradas(escenas: list[EscenaMontaje], tramos: list[tuple[float, float] | None], t: Tiempos, titular: str,
              carpeta: Path, fuentes_dir: Path, marca_texto: str, musica: Path | None,
              textos_por_escena: bool = True, medio: str = "",
              sonidos: dict[str, Path] | None = None) -> _Entradas:
    """Arma los -i de ffmpeg (clips, PNG de textos, voces, musica) y recuerda el indice de cada uno. Sin
    `titular`, no hay titular."""
    anton, montserrat = fuentes_dir / "Anton-Regular.ttf", fuentes_dir / "Montserrat[wght].ttf"
    args: list[str] = []
    for e in escenas:
        args += ["-i", str(e.clip)]
    idx = len(escenas)
    imagen_fija = ["-loop", "1", "-t", f"{t.total}", "-i"]
    idx_titular = None
    if titular:
        args += imagen_fija + [str(textos_png.titular(titular, anton, carpeta / "txt_titular.png"))]
        idx_titular, idx = idx, idx + 1
    subs = []
    for i, e in enumerate(escenas if textos_por_escena else [], start=1):
        args += imagen_fija + [str(textos_png.subtitulo(e.texto, montserrat, carpeta / f"txt_sub_{i}.png"))]
        subs.append(idx)
        idx += 1
    idx_marca = None
    if marca_texto:
        args += imagen_fija + [str(textos_png.marca(marca_texto, montserrat, carpeta / "txt_marca.png"))]
        idx_marca, idx = idx, idx + 1
    voces = []
    for i, (e, tramo) in enumerate(zip(escenas, tramos)):
        if e.audio and tramo:
            args += ["-ss", f"{tramo[0]}", "-to", f"{tramo[1]}", "-i", str(e.audio)]
            voces.append(Voz(i, idx, *tramo))
            idx += 1
    idx_musica = None
    if musica:
        args += ["-stream_loop", "-1", "-i", str(musica)]
        idx_musica, idx = idx, idx + 1
    idx_fuente = None
    if medio:
        args += imagen_fija + [str(textos_png.sello_fuente(medio, montserrat, carpeta / "txt_fuente.png"))]
        idx_fuente, idx = idx, idx + 1
    efectos_idx = []
    for nombre, ruta in (sonidos or {}).items():
        args += ["-i", str(ruta)]
        efectos_idx.append((nombre, idx))
        idx += 1
    return _Entradas(tuple(args), idx_titular, tuple(subs), idx_marca, tuple(voces), idx_musica, idx, idx_fuente,
                     tuple(efectos_idx))


def segundos_de_imagen(clip_s: float, extras_s: list[float]) -> float:
    """Lo que una escena tiene para mostrar: su clip o, con tomas extra, la suma con tope por clip."""
    return sum(min(d, MAX_SEGMENTO_S) for d in (clip_s, *extras_s)) if extras_s else clip_s


def unir_extras(escenas: list[EscenaMontaje], t: Tiempos, carpeta: Path,
                clips_s: list[float]) -> tuple[list[EscenaMontaje], Tiempos, dict[int, list[float]]]:
    """Las escenas con tomas extra pasan a tener un solo clip, ya del largo de la escena (visual_N.mp4): recien
    aca se sabe cuanto dura cada una (lo marca la voz), asi que `visuales.componer` saca las tomas que no entran
    y repite la serie si no alcanza. Tambien se unen las de una sola toma corta que no llena la escena (una toma
    de 1,5 s en una escena de 5 quedaba congelada 3 s). Devuelve las escenas, los tiempos (esas escenas ya no se
    estiran) y donde cambia la imagen dentro de cada una."""
    nuevas, factores, relleno = list(escenas), list(t.factores), list(t.relleno)
    cambios: dict[int, list[float]] = {}
    for i, e in enumerate(escenas):
        queda_corta = clips_s[i] < CLIP_CORTO_S and clips_s[i] * MAX_LENTO + MAX_CONGELADO_S < t.duraciones[i]
        if not e.extras and not queda_corta:
            continue
        destino = carpeta / f"visual_{i + 1}.mp4"
        cambios[i] = visuales.componer([e.clip, *e.extras], t.duraciones[i], destino)
        nuevas[i] = replace(e, clip=destino, extras=())
        factores[i], relleno[i] = 1.0, COLA_COMPUESTA_S
    return nuevas, replace(t, factores=tuple(factores), relleno=tuple(relleno)), cambios


def armar_reel(escenas: list[EscenaMontaje], titular: str, carpeta: Path, salida: Path, fuentes_dir: Path,
               marca_texto: str = "", musica: Path | None = None, volumen_musica: float = 0.12,
               minimo_s: float = 0.0, titular_fijo: bool = False, con_titular: bool = True,
               con_textos: bool = True, medio: str = "", cortes_rapidos: bool = False, barra: bool = False,
               efectos_sonido: bool = False, volumen_efectos: float = 0.35) -> dict:
    """`titular_fijo`: el titular queda todo el reel (cuenta regresiva: "FALTAN 9 DIAS" es el mensaje).
    `con_textos` False: sin textos por escena (los subtitulos salen de `EscenaMontaje.narracion`: vacia, no hay);
    `con_titular` False: sin titular. La marca de la pagina queda siempre."""
    if not escenas:
        raise MontajeError("no hay escenas para montar")
    try:
        tramos = [tramo_de_voz(e.audio) if e.audio else None for e in escenas]
        clips_s = [duracion(e.clip) for e in escenas]
        imagen_s = [segundos_de_imagen(c, [duracion(x) for x in e.extras]) for c, e in zip(clips_s, escenas)]
        t = calcular_tiempos(imagen_s, [r[1] - r[0] if r else 0.0 for r in tramos], minimo_s)
        escenas, t, cambios = unir_extras(escenas, t, carpeta, clips_s)
        ass = subtitulos_del_reel(escenas, tramos, t, carpeta / "subtitulos.ass")
        # el acercamiento a mitad de escena es para las que tienen una sola toma: las unidas ya cambian de imagen
        cortes = {i: s for i, s in cortes_de(t).items() if i not in cambios} if cortes_rapidos else {}
        sonidos = efectos.generar(carpeta) if efectos_sonido else None
        ent = _entradas(escenas, tramos, t, titular if con_titular else "", carpeta, fuentes_dir, marca_texto, musica,
                        textos_por_escena=con_textos and ass is None,  # con subtitulos, el texto por escena sobra
                        medio=medio, sonidos=sonidos)
        grafo_v, salida_v = grafo_video(t, ent.titular, list(ent.subs), ent.marca,
                                        (ass, fuentes_dir) if ass else None,
                                        titular_s=t.total if titular_fijo else TITULAR_S,
                                        cortes=cortes, idx_fuente=ent.fuente, barra=barra)
        cuando = efectos.momentos(t.inicios, [t.inicios[i] + s for i, s in cortes.items()]
                                  + [t.inicios[i] + s for i, lista in cambios.items() for s in lista])
        audio = grafo_audio(t, list(ent.voces), ent.musica, volumen_musica,
                            [(idx, cuando[nombre]) for nombre, idx in ent.efectos], volumen_efectos)
        args = list(ent.args)
        if audio is None:
            args += ["-f", "lavfi", "-t", f"{t.total}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            grafo, mapa_a = grafo_v, f"{ent.siguiente}:a"
        else:
            grafo, mapa_a = grafo_v + ";" + audio[0], f"[{audio[1]}]"
        tope = bitrate_video_kbps(t.total)
        cmd = ["ffmpeg", "-v", "error", "-y", *args, "-filter_complex", grafo, "-map", f"[{salida_v}]",
               "-map", mapa_a, "-t", f"{t.total}", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
               "-maxrate", f"{tope}k", "-bufsize", f"{2 * tope}k", "-pix_fmt", "yuv420p", "-profile:v", "high",
               # Meta pide GOP cerrado de 2 a 5 s para los reels: un cuadro clave cada 2 s
               "-g", str(2 * FPS), "-keyint_min", str(2 * FPS), "-sc_threshold", "0",
               "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ar", "48000", "-movflags", "+faststart", str(salida)]
        log_render = carpeta / "render.log"
        with log_render.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(cmd, ensure_ascii=False) + "\n")
            res = subprocess.run(cmd, stderr=fh, check=False)
        if res.returncode != 0:
            raise MontajeError(f"ffmpeg fallo (codigo {res.returncode}); ver {log_render}")
        return validar(salida, t.total)
    except (subprocess.SubprocessError, ValueError, KeyError, visuales.VisualError) as exc:
        raise MontajeError(f"montaje fallo: {exc}") from exc


def validar(ruta: Path, esperado_s: float) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height:format=duration,size", "-of", "json", str(ruta)],
                         capture_output=True, text=True, check=True)
    info = json.loads(out.stdout)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    tiene_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    dur = float(info["format"]["duration"])
    if not video or (video["width"], video["height"]) != (W, H) or not tiene_audio:
        raise MontajeError(f"salida invalida: {info}")
    if abs(dur - esperado_s) > TOLERANCIA_DURACION_S:
        raise MontajeError(f"duracion {dur:.2f}s distinta de la esperada {esperado_s:.2f}s")
    return {"duracion_s": round(dur, 2), "bytes": int(info["format"]["size"]), "ancho": W, "alto": H}


def miniatura(reel: Path, destino: Path, segundo: float = 1.0) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(segundo), "-i", str(reel), "-frames:v", "1",
                    "-q:v", "3", str(destino)], check=True)
    return destino
