import subprocess

import pytest

from tests.conftest import hay_ffmpeg
from vstudio import montaje


def test_tiempos_sin_audio_usan_el_clip():
    t = montaje.calcular_tiempos([5.0625] * 3, [0, 0, 0])
    assert t.duraciones == (5.0625,) * 3 and t.factores == (1.0,) * 3 and t.relleno == (0.0,) * 3
    assert t.inicios == (0.0, 4.6625, 9.325) and t.total == pytest.approx(14.3875, abs=0.0001)


def test_la_voz_marca_el_ritmo():
    t = montaje.calcular_tiempos([5.0, 5.0, 5.0], [3.0, 4.0, 8.0])
    assert t.duraciones[0] == pytest.approx(3.65) and t.factores[0] == 1.0  # frase corta: el clip se corta antes
    assert t.relleno[0] == 0.0
    for i in (0, 1):  # entre el fin de una frase y el inicio de la siguiente queda la pausa, no 1 s de silencio
        fin_voz = t.inicios[i] + montaje.ENTRADA_VOZ_S + [3.0, 4.0][i]
        assert t.inicios[i + 1] + montaje.ENTRADA_VOZ_S - fin_voz == pytest.approx(montaje.PAUSA_ENTRE_ESCENAS_S)
    # la ultima (larga) se estira hasta MAX_LENTO, congela el resto y deja la cola despues de la pregunta
    assert t.duraciones[2] == pytest.approx(montaje.ENTRADA_VOZ_S + 8.0 + montaje.COLA_FINAL_S)
    assert t.factores[2] == montaje.MAX_LENTO and t.relleno[2] == pytest.approx(t.duraciones[2] - 5.0 * 1.35, abs=0.01)


def test_frase_muy_corta_respeta_el_minimo_de_escena():
    assert montaje.calcular_tiempos([5.0, 5.0], [0.5, 2.0]).duraciones[0] == montaje.MIN_ESCENA_S


@pytest.mark.parametrize("eventos, total, esperado", [
    ([("start", 0.0), ("end", 0.0987), ("start", 0.8077), ("end", 0.9153), ("start", 3.1789), ("end", 3.72)],
     3.72, (0.069, 3.259)),  # medido en la voz de un reel real
    ([("start", 0.0), ("end", 0.1), ("start", 3.0)], 3.5, (0.07, 3.08)),  # ffmpeg no cerro el ultimo silencio
    ([("start", 0.415), ("end", 0.507), ("start", 4.27), ("end", 4.8)], 4.8, (0.0, 4.35)),  # arranca hablando
    ([], 2.5, (0.0, 2.5)),
    ([("start", 0.0), ("end", 2.0)], 2.0, (0.0, 2.0)),  # todo silencio: no se recorta
])
def test_recorte_de_silencios(eventos, total, esperado):
    assert montaje.recorte_de_silencios(eventos, total) == pytest.approx(esperado, abs=0.001)


def test_voz_necesaria_para_el_largo():
    voz = montaje.voz_necesaria_s(15, 62.0)
    assert montaje.calcular_tiempos([5.0625] * 15, [voz / 15] * 15).total == pytest.approx(62.0, abs=0.01)


def test_bitrate_mantiene_el_reel_bajo_el_limite_de_telegram():
    for duracion in (15, 30, 57, 60, 120):
        kbps = montaje.bitrate_video_kbps(duracion)
        bytes_maximos = (kbps + montaje.AUDIO_KBPS) * 1000 / 8 * duracion
        assert bytes_maximos <= montaje.MAX_BYTES_SALIDA and kbps <= montaje.MAX_VIDEO_KBPS
    assert montaje.bitrate_video_kbps(15) == montaje.MAX_VIDEO_KBPS  # los cortos no se degradan


def test_grafos():
    t = montaje.calcular_tiempos([5.0, 5.0, 5.0], [3.0, 0.0, 3.0])
    video, salida = montaje.grafo_video(t, 3, [4, 5, 6], None)
    assert salida == "o3" and "xfade=transition=fade:duration=0.4:offset=3.25" in video
    assert f"enable='lt(t,{montaje.TITULAR_S:.3f})'" in video
    assert montaje.grafo_audio(t, [], None, 0.1) is None
    audio, etiqueta = montaje.grafo_audio(t, [montaje.Voz(0, 7, 0.07, 3.07), montaje.Voz(2, 8, 0.0, 3.0)], 9, 0.1)
    assert etiqueta == "aout" and "adelay=250|250" in audio and "loudnorm=I=-14" in audio and "[mus]" in audio
    assert "atrim=start" not in audio  # el recorte va en la entrada (-ss/-to): con atrim el AAC recibia NaN


@pytest.mark.skipif(not hay_ffmpeg(), reason="sin ffmpeg")
def test_reel_real_con_voz_y_validacion(tmp_path, fuentes_dir):
    escenas = []
    for i in range(3):
        clip = tmp_path / f"clip_{i}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=16",
                        "-frames:v", "81", "-pix_fmt", "yuv420p", str(clip)], check=True)
        voz = tmp_path / f"voz_{i}.wav"  # como los TTS: 0,2 s de silencio antes y 0,6 s despues
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={300 + i * 100}:duration="
                        f"{3 if i else 6}", "-af", "adelay=200,apad=pad_dur=0.6", str(voz)], check=True)
        escenas.append(montaje.EscenaMontaje(clip=clip, texto=f"Escena número {i + 1} con tildes: Lucía", audio=voz))
    inicio, fin = montaje.tramo_de_voz(escenas[1].audio)
    assert inicio == pytest.approx(0.17, abs=0.03) and fin == pytest.approx(3.28, abs=0.03)
    info = montaje.armar_reel(escenas, "Faltan 15 días para GTA VI", tmp_path, tmp_path / "reel.mp4", fuentes_dir,
                              marca_texto="@pagina")
    assert info["ancho"] == 1080 and info["alto"] == 1920
    habla = [6.11, 3.11, 3.11]  # cada voz sin sus silencios (+ los margenes)
    esperado = (habla[0] + 0.65) + (habla[1] + 0.65) + (0.25 + habla[2] + montaje.COLA_FINAL_S) - 2 * 0.4
    assert info["duracion_s"] == pytest.approx(esperado, abs=0.3)  # escena 1 estirada por la voz
    assert montaje.miniatura(tmp_path / "reel.mp4", tmp_path / "mini.jpg").exists()


@pytest.mark.skipif(not hay_ffmpeg(), reason="sin ffmpeg")
def test_reel_real_solo_animacion(tmp_path, fuentes_dir):
    """Sin voz ni textos: cada escena dura el clip entero y no se genera ningun PNG de texto (solo la marca)."""
    escenas = []
    for i in range(2):
        clip = tmp_path / f"clip_{i}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=16",
                        "-frames:v", "81", "-pix_fmt", "yuv420p", str(clip)], check=True)
        escenas.append(montaje.EscenaMontaje(clip=clip, texto="no se ve"))
    info = montaje.armar_reel(escenas, "NO SE VE", tmp_path, tmp_path / "reel.mp4", fuentes_dir,
                              marca_texto="@pagina", minimo_s=12.0, con_titular=False, con_textos=False)
    assert info["duracion_s"] == pytest.approx(12.05, abs=0.1)  # 2 clips de 5,06 s estirados hasta el piso
    assert not list(tmp_path.glob("txt_titular*")) and not list(tmp_path.glob("txt_sub_*"))
    assert (tmp_path / "txt_marca.png").exists()


# ---------- ritmo y sonido ----------
def test_cortes_solo_en_escenas_largas():
    t = montaje.calcular_tiempos([5.0, 5.0, 5.0], [4.0, 1.0, 4.0])
    cortes = montaje.cortes_de(t)
    assert set(cortes) == {i for i, d in enumerate(t.duraciones) if d >= montaje.MIN_ESCENA_PARA_CORTE_S}
    assert all(abs(cortes[i] - t.duraciones[i] / 2) < 0.01 for i in cortes)


def test_grafo_con_cortes_barra_y_sello():
    t = montaje.calcular_tiempos([5.0, 5.0], [0.0, 0.0])
    video, salida = montaje.grafo_video(t, 2, [], None, cortes={0: 2.5}, idx_fuente=3, barra=True)
    assert "[v0]split=2[v0a][v0b]" in video and "[v0p][v0q]concat=n=2" in video
    assert "[v0c][v1]xfade" in video  # la escena con corte entra al fundido como una sola
    assert "[3:v]overlay=40:60" in video and "eval=frame" in video and salida == "ob"


def test_audio_con_musica_que_se_agacha_y_efectos():
    t = montaje.calcular_tiempos([5.0, 5.0], [3.0, 3.0])
    voces = [montaje.Voz(0, 5, 0.0, 3.0), montaje.Voz(1, 6, 0.0, 3.0)]
    audio, etiqueta = montaje.grafo_audio(t, voces, 7, 0.15, [(8, [0.0]), (9, [4.0, 6.0])])
    assert "sidechaincompress" in audio and "[voz]asplit=2[vozm][vozsc]" in audio
    assert "asplit=2[e1_0][e1_1]" in audio and "adelay=4000|4000" in audio and "anull[e0_0]" in audio
    sin_voz, _ = montaje.grafo_audio(t, [], 7, 0.15, [(8, [0.0])])  # solo animacion: musica sin agacharse
    assert "sidechaincompress" not in sin_voz and "[mus]" in sin_voz


def test_efectos_donde_suenan():
    from vstudio import efectos
    m = efectos.momentos((0.0, 4.6, 9.2), [2.3, 6.9])
    assert m["golpe"] == [0.0] and m["tick"] == [2.3, 6.9]
    assert m["whoosh"] == [4.6 - efectos.ADELANTO_WHOOSH_S, 9.2 - efectos.ADELANTO_WHOOSH_S]


@pytest.mark.skipif(not hay_ffmpeg(), reason="sin ffmpeg")
def test_reel_real_con_ritmo_y_sonido(tmp_path, fuentes_dir):
    escenas = []
    for i in range(2):
        clip = tmp_path / f"clip_{i}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=16",
                        "-frames:v", "81", "-pix_fmt", "yuv420p", str(clip)], check=True)
        voz = tmp_path / f"voz_{i}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={300 + i * 100}:duration=3",
                        "-af", "adelay=200,apad=pad_dur=0.6", str(voz)], check=True)
        escenas.append(montaje.EscenaMontaje(clip=clip, texto="t", audio=voz))
    musica = tmp_path / "musica.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=110:duration=20", "-ac", "2",
                    str(musica)], check=True)
    info = montaje.armar_reel(escenas, "TITULAR", tmp_path, tmp_path / "reel.mp4", fuentes_dir, musica=musica,
                              medio="IGN", cortes_rapidos=True, barra=True, efectos_sonido=True)
    assert info["ancho"] == 1080 and (tmp_path / "txt_fuente.png").exists() and (tmp_path / "sfx_whoosh.wav").exists()


@pytest.mark.parametrize("texto", ["ROCKSTAR VUELVE A ROMPER", "GTA VI: EL TRAILER 3 YA TIENE FECHA CONFIRMADA",
                                   "LUCIA Y JASON EN VICE CITY CON UN GAMEPLAY NUEVO QUE NADIE ESPERABA HOY"])
def test_titular_entra_en_la_banda(texto, fuentes_dir, tmp_path):
    """Regresion: la 3a linea del titular salia cortada por el borde inferior de la banda."""
    from PIL import Image

    from vstudio import textos_png

    png = textos_png.titular(texto, fuentes_dir / "Anton-Regular.ttf", tmp_path / "t.png")
    img = Image.open(png)
    alfa = img.getchannel("A")
    alto = img.height
    franja = alfa.crop((0, alto - textos_png.AIRE_INFERIOR_TITULAR // 2, img.width, alto))
    assert franja.getextrema()[1] < 40, "hay texto (opaco) pegado al borde inferior: el titular se corta"
