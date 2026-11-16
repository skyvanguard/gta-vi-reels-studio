import subprocess
from pathlib import Path

import pytest

from tests.conftest import hay_ffmpeg
from vstudio import montaje, subtitulos
from vstudio.config import ConfigSubtitulos
from vstudio.subtitulos import Palabra

NARRACION = "GTA seis pide doscientos treinta gigas, no doscientos doce."


def _oidas(*pares):
    return [Palabra(t, a, b) for t, a, b in pares]


def test_alinea_el_guion_con_lo_que_oyo_whisper():
    oidas = _oidas(("GTA", 0.10, 0.40), ("seis", 0.40, 0.70), ("pide", 0.70, 1.00), ("230", 1.00, 2.00),
                   ("gigas,", 2.00, 2.40), ("no", 2.60, 2.75), ("doscientos", 2.75, 3.30), ("doce.", 3.30, 3.70))
    palabras = subtitulos.alinear(NARRACION, oidas, (0.07, 3.78))
    textos = [p.texto for p in palabras]
    assert textos[0] == "GTA VI" and textos[2:4] == ["doscientos", "treinta"]  # se muestra el guion, no "230"
    assert palabras[0].inicio == 0.10 and palabras[0].fin == 0.70  # "GTA seis" -> una sola palabra en pantalla
    # "doscientos treinta" no lo reconocio (oyo "230"): se reparte el hueco entre "pide" y "gigas,"
    assert 1.0 <= palabras[2].inicio < palabras[3].inicio < 2.0
    assert all(a.inicio <= b.inicio for a, b in zip(palabras, palabras[1:]))


def test_sin_whisper_reparte_por_largo_de_palabra():
    palabras = subtitulos.alinear("Hola mundo largo", [], (1.0, 3.0))
    assert palabras[0].inicio == 1.0 and palabras[-1].fin == pytest.approx(3.0)
    assert palabras[2].fin - palabras[2].inicio > palabras[0].fin - palabras[0].inicio  # "largo" dura mas


def test_paginas_de_hasta_tres_palabras_y_corte_en_la_puntuacion():
    palabras = subtitulos.alinear("Se filtró la caja, y trae un dato que nadie contó.", [], (0.0, 4.0))
    grupos = [[p.texto for p in g] for g in subtitulos.paginas(palabras)]
    assert grupos[0] == ["Se", "filtró", "la"] and grupos[1] == ["caja,"]  # la coma corta
    assert all(len(g) <= subtitulos.MAX_PALABRAS_PAGINA for g in grupos)
    assert all(len(" ".join(g)) <= subtitulos.MAX_CARACTERES_PAGINA or len(g) == 1 for g in grupos)


def test_eventos_ass_resaltan_la_palabra_que_se_dice():
    palabras = _oidas(("¿Te", 0.0, 0.3), ("alcanza", 0.3, 0.8), ("el", 0.8, 0.9), ("espacio?", 0.9, 1.5))
    lineas = subtitulos.eventos_ass(palabras, desplazamiento=10.0)
    assert len(lineas) == 4
    assert lineas[0].startswith("Dialogue: 0,0:00:10.00,0:00:10.30,Palabras")
    assert subtitulos.AMARILLO + "¿TE" + r"{\r} ALCANZA EL" in lineas[0]  # pagina entera, la primera resaltada
    assert lineas[3].endswith(subtitulos.AMARILLO + r"ESPACIO?{\r}")  # ? queda, la pagina 2 es "espacio?"
    corto = subtitulos.eventos_ass(palabras, desplazamiento=10.0, hasta=11.0)
    assert corto[-1].split(",")[2] == "0:00:11.00"  # no se pisa con la voz de la escena siguiente


def test_tiempo_ass_no_redondea_a_60():
    assert subtitulos._tiempo_ass(59.996) == "0:01:00.00" and subtitulos._tiempo_ass(3725.5) == "1:02:05.50"


def test_texto_seguro_para_ass():
    assert subtitulos._mostrar(r"hola{\b1}") == "HOLAB1" and subtitulos._mostrar("gigas,") == "GIGAS"


def test_whisper_que_falla_levanta_error(tmp_path):
    cfg = ConfigSubtitulos(python=tmp_path / "no" / "python", script=tmp_path / "x.py")
    with pytest.raises(subtitulos.SubtitulosError, match="falta"):
        subtitulos.tiempos_whisper(cfg, [tmp_path / "a.wav"], ["hola"], tmp_path)


def test_grafo_con_subtitulos_dibuja_el_ass():
    t = montaje.calcular_tiempos([5.0, 5.0], [3.0, 3.0])
    video, salida = montaje.grafo_video(t, 2, [], None, (Path("/p/subtitulos.ass"), Path("/f")))
    assert salida == "sub" and "ass='/p/subtitulos.ass':fontsdir='/f'[sub]" in video
    sin, _ = montaje.grafo_video(t, 2, [3, 4], None)
    assert "ass=" not in sin and f"overlay=0:{montaje.H - montaje.ZONA_SEGURA_INFERIOR}-h" in sin


def test_titular_fijo_queda_todo_el_reel():
    t = montaje.calcular_tiempos([5.0, 5.0], [3.0, 3.0])
    video, _ = montaje.grafo_video(t, 2, [], 5, titular_s=t.total)
    assert f"enable='lt(t,{t.total:.3f})'" in video and f"gte(t,{t.total:.3f})" in video
    normal, _ = montaje.grafo_video(t, 2, [], None)
    assert f"enable='lt(t,{montaje.TITULAR_S:.3f})'" in normal


def test_ruta_para_filtro_en_windows():
    assert montaje.ruta_para_filtro(Path("C:/Users/x/sub.ass")) == r"'C\:/Users/x/sub.ass'"


@pytest.mark.skipif(not hay_ffmpeg(), reason="sin ffmpeg")
def test_reel_real_con_subtitulos(tmp_path, fuentes_dir):
    escenas = []
    for i, narracion in enumerate(["GTA seis pide doscientos treinta gigas.", "¿Te alcanza el espacio libre?"]):
        clip = tmp_path / f"clip_{i}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=16",
                        "-frames:v", "81", "-pix_fmt", "yuv420p", str(clip)], check=True)
        voz = tmp_path / f"voz_{i}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                        "-af", "adelay=200,apad=pad_dur=0.6", str(voz)], check=True)
        escenas.append(montaje.EscenaMontaje(clip=clip, texto=f"DATO {i + 1}", audio=voz, narracion=narracion))
    info = montaje.armar_reel(escenas, "Prueba", tmp_path, tmp_path / "reel.mp4", fuentes_dir)
    ass = (tmp_path / "subtitulos.ass").read_text(encoding="utf-8")
    assert ass.count("Dialogue:") == 10 and "GTA VI" in ass  # 5 + 5 palabras ("GTA seis" es una)
    comando = (tmp_path / "render.log").read_text(encoding="utf-8")
    assert "ass=" in comando and "txt_sub_" not in comando  # con subtitulos no va el texto por escena
    assert info["ancho"] == 1080 and info["duracion_s"] > 6


def test_las_cifras_saltan_y_gta_seis_no():
    from vstudio.subtitulos import CIFRA, AMARILLO, _resaltada
    assert _resaltada("doscientos").startswith(CIFRA) and _resaltada("212").startswith(CIFRA)
    assert _resaltada("gigas,").startswith(CIFRA)
    assert _resaltada("seis", "GTA").startswith(AMARILLO)  # el nombre del juego no
    assert _resaltada("seis", "faltan").startswith(CIFRA)
    assert _resaltada("una").startswith(AMARILLO) and _resaltada("alianza").startswith(AMARILLO)
