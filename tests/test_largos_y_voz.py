import subprocess
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from tests.conftest import hay_ffmpeg
from tests.test_almacen_servicio_acceso import _proyecto
from tests.test_bot_trabajador import LlmQuieto, TgFalso, _msg
from vstudio import fuentes, imagenes, voz
from vstudio.acceso import Acceso
from vstudio.almacen import Almacen
from vstudio.bot import Bot
from vstudio.planificador import validar_plan
from vstudio.servicio import Servicio
from vstudio.trabajador import Trabajador


def test_recorte_con_zoom_es_un_primer_plano_dentro_de_la_imagen():
    img = Image.new("RGB", (1350, 900), (0, 0, 0))
    img.paste((255, 0, 0), (600, 0, 750, 900))  # franja roja en el centro
    general = imagenes.recorte_vertical(img, 0.5, 720, 1280, zoom=1.0)
    cerca = imagenes.recorte_vertical(img, 0.5, 720, 1280, zoom=1.6, centro_y=0.0)
    rojo = lambda im: sum(1 for x in range(0, 720, 8) if im.getpixel((x, 640))[0] > 200)  # noqa: E731
    assert general.size == cerca.size == (720, 1280)
    assert rojo(cerca) > rojo(general)  # el zoom agranda el sujeto
    assert imagenes.recorte_vertical(img, 2.0, 720, 1280, zoom=9, centro_y=-3).size == (720, 1280)  # clamps


def test_validar_plan_acepta_y_acota_zoom_y_altura():
    plan = {"titular": "T", "escenas": [{"imagen": 0, "centro_x": 0.5, "centro_y": 3, "zoom": 5, "texto": "a"},
                                        {"imagen": 0, "centro_x": 0.2, "texto": "b"}]}
    v = validar_plan(plan, n_imagenes=1, n_escenas=2)
    assert v["escenas"][0]["zoom"] == imagenes.ZOOM_MAX and v["escenas"][0]["centro_y"] == 1.0
    assert v["escenas"][1]["zoom"] == 1.0 and v["escenas"][1]["centro_y"] == 0.5


def test_buscar_imagenes_filtra_y_no_repite(monkeypatch):
    resultados = {"results": [{"img_src": "https://cdn.x.com/gta6-1.jpg"}, {"img_src": "https://cdn.x.com/logo.png"},
                              {"img_src": "data:image/png;base64,AA"}, {"img_src": "https://cdn.x.com/gta6-1.jpg"}]}
    monkeypatch.setattr(fuentes.httpx, "get", lambda *a, **k: httpx.Response(200, json=resultados))
    assert fuentes.buscar_imagenes("http://127.0.0.1:8888", ("a", "b")) == ["https://cdn.x.com/gta6-1.jpg"]

    def caido(*a, **k):
        raise httpx.ConnectError("sin searxng")
    monkeypatch.setattr(fuentes.httpx, "get", caido)
    assert fuentes.buscar_imagenes("http://127.0.0.1:8888", ("a",)) == []


def test_escenas_segun_duracion(cfg):
    s = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), None, None)
    assert [s.n_escenas(x) for x in (5, 15, 30, 75, 90, 600)] == [2, 3, 6, 15, 18, 18]


def test_comando_de_chatterbox_usa_la_voz_activa(cfg, monkeypatch, tmp_path):
    cfg_voz = replace(cfg.voz, motor="chatterbox", voces_dir=tmp_path / "voz")
    (tmp_path / "voz").mkdir()
    (tmp_path / "voz" / "pagina.wav").write_bytes(b"RIFF")
    assert voz.voces_disponibles(cfg_voz) == ["integrada", "pagina"]
    voz.elegir_voz(cfg_voz, "pagina")
    llamadas = []

    def falso(cmd, timeout_s):
        llamadas.append(cmd)
        return [{"archivo": str(tmp_path / "1.wav"), "segundos": 4.2}]
    monkeypatch.setattr(voz, "_correr", falso)
    audios = voz.sintetizar(cfg_voz, ["Hola"], tmp_path / "salida")
    cmd = llamadas[0]
    assert audios[0].segundos == 4.2 and cmd[cmd.index("--ref") + 1].endswith("pagina.wav")
    voz.sintetizar(cfg_voz, ["Hola"], tmp_path / "salida", voz="integrada")
    assert llamadas[1][llamadas[1].index("--ref") + 1] == ""
    with pytest.raises(voz.VozError):
        voz.elegir_voz(cfg_voz, "no_existe")


@pytest.mark.skipif(not hay_ffmpeg(), reason="sin ffmpeg")
def test_guardar_referencia_convierte_y_valida(cfg, tmp_path):
    cfg_voz = replace(cfg.voz, voces_dir=tmp_path / "voz")
    crudo = tmp_path / "nota.ogg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=30",
                    "-c:a", "libopus", str(crudo)], check=True)
    segundos = voz.guardar_referencia(cfg_voz, "mi_voz", crudo.read_bytes())
    assert 19 <= segundos <= 20.5 and (tmp_path / "voz" / "mi_voz.wav").exists()
    corto = tmp_path / "corto.ogg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
                    "-c:a", "libopus", str(corto)], check=True)
    with pytest.raises(voz.VozError, match="dura"):
        voz.guardar_referencia(cfg_voz, "corta", corto.read_bytes())
    with pytest.raises(voz.VozError, match="nombre"):
        voz.guardar_referencia(cfg_voz, "../hack", crudo.read_bytes())
    with pytest.raises(voz.VozError):
        voz.guardar_referencia(cfg_voz, "basura", b"no es audio")


def test_largo_por_comando(cfg, tmp_path):
    tg = TgFalso()
    servicio = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), LlmQuieto(), tg)
    acceso = Acceso(tmp_path / "s" / "u.json", tmp_path / "s" / "c")
    acceso.vincular(7, acceso.nuevo_codigo())
    bot = Bot(tg, servicio, Trabajador(servicio), acceso, tmp_path / "offset")
    bot._manejar(_msg("/largo 45"))
    assert "uso: /largo" in tg.enviados[-1][2]
    bot._manejar(_msg("/largo 60"))  # ya no se ofrece: no pasa del minuto con margen
    assert "uso: /largo 10|15|30|75|90" in tg.enviados[-1][2]
    bot._manejar(_msg("/largo 90"))
    assert "90 s" in tg.enviados[-1][2] and "el Qwen apagado ~" in tg.enviados[-1][2] and bot._largo(70) == 90
    servicio.almacen.guardar(_proyecto(chat=70))
    assert bot._largo(71) == cfg.segundos_por_defecto == 75  # otro chat: el de siempre, monetizable
