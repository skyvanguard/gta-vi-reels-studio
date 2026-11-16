"""Cambiar una sola escena desde Telegram, recibir los reels automaticos repartidos en el
dia (agenda de publicacion) y clips de IA mas fluidos (cuadros intermedios)."""
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import ZONA_PRUEBA, hay_ffmpeg, imagen_bytes
from tests.test_almacen_servicio_acceso import _proyecto, servicio  # noqa: F401 - fixture
from tests.test_bot_trabajador import LlmQuieto, TgFalso, TurnoFalso, entorno  # noqa: F401 - fixture
from tests.test_tomas_y_visuales import _biblioteca, _video
from vstudio import agenda as agenda_mod
from vstudio import ajustes, armado, comfy, tomas, visuales
from vstudio import trabajador as trab_mod
from vstudio.almacen import Almacen
from vstudio.comfy import ResultadoClip
from vstudio.config import Config
from vstudio.modelos import Estado, Proyecto, proyecto_a_dict, proyecto_desde_dict
from vstudio.servicio import EdicionInvalida, Servicio

con_ffmpeg = pytest.mark.skipif(not hay_ffmpeg(), reason="hace falta ffmpeg")
ZONA = ZoneInfo(ZONA_PRUEBA)
PLANTILLA = json.loads((Path(__file__).resolve().parents[1] / "workflows/wan/wan22_i2v_14b.api.json").read_text())


# ---------- 1. cambiar una sola escena ----------
@pytest.mark.parametrize("texto, escena", [
    ("cambiá la escena 3", 3), ("Cambia la toma 2 por otra", 2), ("otra toma en la escena 5", 5),
    ("no me gusta la escena 4", 4), ("cambiá la escena número 1", 1), ("reemplazá la parte 6", 6),
    ("otra toma en la 2", 2), ("cambiá la escena 3, no me dice nada", 3),
    ("cambiá el texto de la escena 3", None), ("que la escena 2 diga otra cosa", None),
    ("la escena 3 está buena", None), ("cambiá las escenas", None),
    # sacar o cambiar otra cosa de una escena no es cambiarle la toma
    ("sacá la música de la escena 3", None), ("quitá la voz en la escena 2", None),
    ("sacá el subtítulo de la parte 2", None), ("cambiá la voz de la escena 3", None),
    ("otra noticia de la parte 2 del tráiler", None), ("reel de 30 s", None),
])
def test_pedir_otra_toma_para_una_escena(texto, escena):
    cambio = ajustes.leer(texto)
    assert cambio.escena == escena
    assert not escena or cambio.describir() == f"otra toma en la escena {escena}"


def _con_tomas(servicio, monkeypatch, estado=Estado.ESPERANDO_APROBACION):  # noqa: F811
    """Un proyecto con sus imagenes en disco y una biblioteca de 4 tomas (los recortes de video son de mentira)."""
    _biblioteca(servicio.cfg.tomas_dir, {"trailer": [("Lucía maneja una lancha", ["lucia", "lancha"], 2.0, 3),
                                                      ("Jason corre por la playa", ["jason", "playa"], 2.5, 2),
                                                      ("Autos en la autopista", ["autos"], 2.0, 1),
                                                      ("Fiesta en un yate", ["fiesta", "yate"], 3.0, 1)]})
    monkeypatch.setattr(armado.visuales, "recortar_toma", lambda origen, destino, caja: destino.write_bytes(b"v"))
    p = _proyecto(estado=estado)
    carpeta = servicio.cfg.proyectos / p.id
    carpeta.mkdir(parents=True)
    for e in p.plan.escenas:
        (carpeta / e.imagen).write_bytes(imagen_bytes(720, 1280, formato="PNG"))
        (carpeta / e.clip).write_bytes(b"viejo")
    escenas = tuple(replace(e, narracion="Jason corre por la playa de Vice City.", audio=f"voz/{e.n}.wav")
                    for e in p.plan.escenas)
    servicio.almacen.guardar(replace(p, plan=replace(p.plan, escenas=escenas)))
    return carpeta


def test_cambiar_una_escena_le_pone_otra_toma_y_no_toca_el_resto(servicio, monkeypatch):  # noqa: F811
    carpeta = _con_tomas(servicio, monkeypatch)
    tomas.anotar_usadas(carpeta, {"clip_1.mp4": "trailer/toma_0002.mp4", "extra_1_1.mp4": "trailer/toma_0003.mp4"})
    p = servicio.cambiar_escena("abc", 2)
    uno, dos, tres = p.plan.escenas
    assert p.estado == Estado.ESPERANDO_APROBACION and (uno.medio, tres.medio) == ("ia", "ia")
    # la toma de Jason (la que mejor pega con lo que dice) ya estaba en el reel: va la que sigue, sin repetir
    assert dos.medio == "toma" and dos.revisada and dos.origen not in ("trailer/toma_0002.mp4",
                                                                       "trailer/toma_0003.mp4")
    assert (dos.narracion, dos.audio, dos.texto) == ("Jason corre por la playa de Vice City.", "voz/2.wav", "t2")
    assert (carpeta / "clip_2.mp4").read_bytes() == b"v" and (carpeta / "clip_1.mp4").read_bytes() == b"viejo"
    usadas = tomas.usadas_del_proyecto(carpeta)
    assert usadas["clip_2.mp4"] == dos.origen and usadas["clip_1.mp4"] == "trailer/toma_0002.mp4"
    assert len(set(usadas.values())) == len(usadas) == 4  # las 4 tomas, cada una una vez
    assert dos.extras == ("extra_2_1.mp4",) and (carpeta / "storyboard.jpg").exists()
    with pytest.raises(EdicionInvalida, match="no quedan tomas"):
        servicio.cambiar_escena("abc", 3)  # no queda ninguna libre: no repite
    with pytest.raises(EdicionInvalida, match="no tiene escena 9"):
        servicio.cambiar_escena("abc", 9)


def test_cambiar_una_escena_de_un_reel_hecho_lo_rearma_sin_gpu(servicio, monkeypatch):  # noqa: F811
    carpeta = _con_tomas(servicio, monkeypatch, estado=Estado.ENTREGADO)
    p = servicio.cambiar_escena("abc", 1)
    assert p.estado == Estado.EN_COLA and p.reel == "" and p.plan.escenas[0].medio == "toma"
    assert trab_mod.escenas_pendientes(p, carpeta) == []  # los clips estan: no abre turno de GPU


def test_sin_tomas_o_con_fotos_del_editor_no_se_cambia(servicio, monkeypatch):  # noqa: F811
    servicio.almacen.guardar(_proyecto())
    with pytest.raises(EdicionInvalida, match="no hay tomas"):
        servicio.cambiar_escena("abc", 1)
    _con_tomas(servicio, monkeypatch)
    p = servicio.almacen.obtener("abc")
    servicio.almacen.guardar(replace(p, fuente=replace(p.fuente, imagenes=("usuario_1.jpg",))))
    with pytest.raises(EdicionInvalida, match="usa tus fotos"):
        servicio.cambiar_escena("abc", 1)
    servicio.almacen.guardar(replace(p, estado=Estado.GENERANDO))
    with pytest.raises(EdicionInvalida, match="generando"):
        servicio.cambiar_escena("abc", 1)


def test_el_bot_cambia_la_escena_del_ultimo_plan_o_reel(entorno, monkeypatch):  # noqa: F811
    tg, servicio_, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    servicio_.almacen.guardar(_proyecto(estado=Estado.ENTREGADO, chat=70))
    pedidos = []
    monkeypatch.setattr(bot, "_cambiar_escena", lambda chat, id_, n, opciones=None: pedidos.append((id_, n, opciones)))
    bot._comando(70, "/escena 2")
    bot._comando(70, "/escena abc 3")
    bot._mensaje({"chat": {"id": 70, "type": "private"}, "from": {"id": 7}, "text": "cambiá la escena 1"})
    bot._mensaje({"chat": {"id": 70, "type": "private"}, "from": {"id": 7},
                  "text": "cambiá la escena 2 y sin subtítulos"})  # las dos cosas, no solo la escena
    assert pedidos == [("abc", 2, None), ("abc", 3, None), ("abc", 1, {}), ("abc", 2, {"subtitulos": False})]
    bot._comando(70, "/escena")
    assert "uso: /escena N" in tg.enviados[-1][2]


# ---------- 2. agenda de publicacion ----------
def _dia(*horas):
    return agenda_mod.horarios_del_dia(tuple(horas), date(2026, 11, 16), ZONA)


def test_cada_reel_va_al_proximo_horario_libre():
    horarios = _dia("10:30", "12:00", "raro", "21:00")
    assert [h.strftime("%H:%M") for h in horarios] == ["10:30", "12:00", "21:00"]  # el mal escrito se saltea
    def a_las(h, m):
        return datetime(2026, 11, 16, h, m, tzinfo=ZONA)
    assert agenda_mod.proximo_horario(horarios, a_las(9, 50), set()) == horarios[0]
    assert agenda_mod.proximo_horario(horarios, a_las(9, 50), {horarios[0]}) == horarios[1]
    assert agenda_mod.proximo_horario(horarios, a_las(10, 29), set()) == horarios[1]  # falta 1 min: ya no entra
    assert agenda_mod.proximo_horario(horarios, a_las(13, 0), {horarios[2]}) == horarios[2]  # se juntan al final
    assert agenda_mod.proximo_horario(horarios, a_las(21, 30), set()) is None  # ya paso el ultimo: sale ahora


def _agenda(servicio, enviados):  # noqa: F811
    return agenda_mod.Agenda(servicio, enviados.append)


def test_la_agenda_reparte_manda_a_su_hora_y_se_puede_adelantar_o_apagar(servicio):  # noqa: F811
    enviados = []
    a = _agenda(servicio, enviados)
    ahora = datetime(2026, 11, 16, 9, 55, tzinfo=ZONA)
    primero = a.horario_para(ahora)
    assert primero.strftime("%H:%M") == "10:30"
    servicio.almacen.guardar(replace(_proyecto(estado=Estado.PROGRAMADO, id_="uno"), enviar_a=primero.isoformat()))
    segundo = a.horario_para(ahora)
    assert segundo.strftime("%H:%M") == "12:00"  # el de las 10:30 ya esta tomado
    servicio.almacen.guardar(replace(_proyecto(estado=Estado.PROGRAMADO, id_="dos"), enviar_a=segundo.isoformat()))
    assert a.enviar_vencidos(ahora) == 0 and enviados == []
    assert a.enviar_vencidos(ahora + timedelta(minutes=40)) == 1 and enviados == ["uno"]
    assert "2 reeles esperando" in a.resumen() and "• 12:00 · TITULAR (dos)" in a.resumen()
    assert a.adelantar() == 2 and [p.enviar_a for p in a.pendientes()] == ["", ""]  # /agenda ya
    enviados.clear()
    assert a.enviar_vencidos(ahora) == 2 and enviados == ["uno", "dos"]
    a.activar(False)
    assert a.horario_para(ahora) is None and "Agenda apagada" in a.resumen()
    a.activar(True)
    assert a.horario_para(ahora) is not None


def test_un_envio_que_falla_no_frena_los_demas_ni_se_repite(servicio):  # noqa: F811
    hechos = []

    def entregar(id_):
        if id_ == "uno":
            raise RuntimeError("se rompió a mitad")
        hechos.append(id_)
    for id_ in ("uno", "dos"):
        servicio.almacen.guardar(_proyecto(estado=Estado.PROGRAMADO, id_=id_))
    a = agenda_mod.Agenda(servicio, entregar)
    assert a.enviar_vencidos() == 1 and hechos == ["dos"]
    # el que rompio queda en error: repetido cada 30 s mandaria el mismo video una y otra vez
    roto = servicio.almacen.obtener("uno")
    assert roto.estado == Estado.ERROR and "se rompió a mitad" in roto.error
    assert [p.id for p in a.pendientes()] == ["dos"]


def test_un_horario_mal_guardado_no_frena_la_agenda(servicio):  # noqa: F811
    enviados = []
    a = _agenda(servicio, enviados)
    for id_, enviar_a in (("roto", "mañana a la tarde"), ("sin_zona", "2026-11-16T12:00:00"),
                          ("bien", "2026-11-16T12:00:00+00:00")):
        servicio.almacen.guardar(replace(_proyecto(estado=Estado.PROGRAMADO, id_=id_), enviar_a=enviar_a))
    ahora = datetime(2026, 11, 16, 11, 0, tzinfo=ZONA)
    assert a.enviar_vencidos(ahora) == 2 and sorted(enviados) == ["roto", "sin_zona"]  # salen ya; el otro espera
    assert a.horario_para(datetime(2026, 11, 16, 11, 30, tzinfo=ZONA)).strftime("%H:%M") == "13:30"
    assert "• ahora · TITULAR (roto)" in a.resumen()


def _producir(cfg, monkeypatch, automatico, con_agenda=True):
    """Un proyecto en cola pasa por la produccion (clips y montaje de mentira). Devuelve (servicio, tg)."""
    workflow = Path(__file__).resolve().parents[1] / "workflows/wan/wan22_i2v_14b.api.json"
    cfg = replace(cfg, video=replace(cfg.video, workflow=workflow))
    tg = TgFalso()
    s = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), LlmQuieto(), tg)
    trabajador = trab_mod.Trabajador(s)
    if con_agenda:
        trabajador.agenda = agenda_mod.Agenda(s, trabajador.entregar_programado)
        monkeypatch.setattr(trabajador.agenda, "horario_para",
                            lambda ahora=None: datetime(2026, 11, 16, 12, 0, tzinfo=ZONA))
    (cfg.proyectos / "abc").mkdir(parents=True)
    p = _proyecto(estado=Estado.EN_COLA, chat=70)
    s.almacen.guardar(replace(p, automatico=automatico,
                              plan=replace(p.plan, escenas=tuple(replace(e, clip="") for e in p.plan.escenas))))
    monkeypatch.setattr(trab_mod, "TurnoGpu", TurnoFalso)

    def clip_falso(servidor, gpu, comfy_dir, plantilla, pedido, destino, timeout_s):
        destino.write_bytes(b"mp4")
        return ResultadoClip(ruta=destino, segundos=1.0, vram_pico_mib=100, prompt_id="x", workflow={})

    def reel_falso(escenas, titular, carpeta_, salida, *a, **k):
        salida.write_bytes(b"reel")
        return {"duracion_s": 14.4, "bytes": 4}
    monkeypatch.setattr(trab_mod.comfy, "generar_clip", clip_falso)
    monkeypatch.setattr(trab_mod.montaje, "armar_reel", reel_falso)
    monkeypatch.setattr(trab_mod.montaje, "duracion", lambda ruta: 14.4)
    trabajador.forzar()
    trabajador.ciclo()
    return s, tg, trabajador


def test_un_reel_automatico_espera_su_horario_y_despues_llega(cfg, monkeypatch):
    s, tg, trabajador = _producir(cfg, monkeypatch, automatico=True)
    p = s.almacen.obtener("abc")
    assert p.estado == Estado.PROGRAMADO and p.reel == "reel_abc.mp4" and p.enviar_a.startswith("2026-11-16T12:00")
    assert not any(tipo == "video" for tipo, _, _ in tg.enviados)  # todavia no se mando
    aviso = [t for tipo, _, t in tg.enviados if "listo" in t][-1]
    assert "1 reel listo" in aviso and "• 12:00 · TITULAR (abc)" in aviso and "/agenda ya" in aviso
    trabajador.entregar_programado("abc")
    final = s.almacen.obtener("abc")
    assert final.estado == Estado.ENTREGADO and final.enviar_a == ""
    assert [tipo for tipo, _, _ in tg.enviados][-2:] == ["video", "mensaje"]  # el reel y el texto para publicar
    trabajador.entregar_programado("abc")  # ya salio: no se manda dos veces
    assert sum(tipo == "video" for tipo, _, _ in tg.enviados) == 1


@pytest.mark.parametrize("automatico, con_agenda", [(False, True), (True, False)])
def test_los_pedidos_a_mano_y_sin_agenda_llegan_apenas_estan(cfg, monkeypatch, automatico, con_agenda):
    s, tg, _ = _producir(cfg, monkeypatch, automatico=automatico, con_agenda=con_agenda)
    final = s.almacen.obtener("abc")
    assert final.estado == Estado.ENTREGADO and final.entregas == 1
    assert any(tipo == "video" for tipo, _, _ in tg.enviados)


def test_un_reel_automatico_que_el_editor_pide_cambiar_no_vuelve_a_la_agenda(cfg, monkeypatch):
    """El bot le dice "te lo mando en ~2 min": no puede quedar esperando el horario de la tarde."""
    s, tg, trabajador = _producir(cfg, monkeypatch, automatico=True)
    trabajador.entregar_programado("abc")
    assert s.almacen.obtener("abc").entregas == 1
    s.cambiar_opciones("abc", subtitulos=False)  # vuelve a la cola para rearmarse
    trabajador.ciclo()
    final = s.almacen.obtener("abc")
    assert final.estado == Estado.ENTREGADO and final.entregas == 2
    assert sum(tipo == "video" for tipo, _, _ in tg.enviados) == 2
    # y un automatico de otro dia (de antes de que se contaran las entregas) tampoco se agenda
    viejo = replace(_proyecto(estado=Estado.EN_COLA, id_="viejo"), automatico=True, creado="2026-11-01T08:00:00+00:00")
    assert not trabajador._va_a_la_agenda(viejo) and not trabajador._va_a_la_agenda(replace(viejo, automatico=False))


def test_un_reel_programado_que_ya_no_esta_queda_en_error_y_se_puede_cancelar(cfg, monkeypatch):
    s, tg, trabajador = _producir(cfg, monkeypatch, automatico=True)
    (cfg.proyectos / "abc" / "reel_abc.mp4").unlink()
    trabajador.entregar_programado("abc")
    assert s.almacen.obtener("abc").estado == Estado.ERROR and "ya no está" in tg.enviados[-1][2]
    s.almacen.guardar(_proyecto(estado=Estado.PROGRAMADO, id_="otro"))
    assert s.cancelar("otro").estado == Estado.CANCELADO


def test_el_bot_muestra_adelanta_y_apaga_la_agenda(entorno):  # noqa: F811
    tg, servicio_, trabajador, _, bot = entorno
    bot.agenda = agenda_mod.Agenda(servicio_, trabajador.entregar_programado)
    servicio_.almacen.guardar(replace(_proyecto(estado=Estado.PROGRAMADO, chat=70),
                                      enviar_a="2026-11-16T12:00:00+00:00"))
    bot._comando(70, "/agenda")
    assert "1 reel esperando" in tg.enviados[-1][2] and "• 12:00 · TITULAR (abc)" in tg.enviados[-1][2]
    bot._comando(70, "/agenda ya")
    assert "Te mando ahora 1 reel" in tg.enviados[-1][2] and servicio_.almacen.obtener("abc").enviar_a == ""
    servicio_.almacen.guardar(replace(_proyecto(estado=Estado.PROGRAMADO, chat=70, id_="otro"),
                                      enviar_a="2026-11-16T15:00:00+00:00"))
    bot._comando(70, "/agenda off")
    assert "Agenda apagada" in tg.enviados[-1][2] and not bot.agenda.activa
    assert servicio_.almacen.obtener("otro").enviar_a == ""  # apagarla manda lo que esperaba su horario
    assert "otro programado" in bot._estado()
    bot._comando(70, "/agenda cualquiera")
    assert "uso: /agenda" in tg.enviados[-1][2]


def test_el_estado_programado_se_guarda_y_los_proyectos_viejos_cargan():
    p = replace(_proyecto(estado=Estado.PROGRAMADO), enviar_a="2026-11-16T12:00:00+00:00")
    assert proyecto_desde_dict(proyecto_a_dict(p)) == p
    viejo = proyecto_a_dict(_proyecto())
    del viejo["enviar_a"]
    assert proyecto_desde_dict(viejo).enviar_a == ""
    assert Config().agenda.horarios[0] == "10:30" and len(Config().agenda.horarios) == 8


# ---------- 3. clips de IA mas fluidos ----------
def test_la_plantilla_con_interpolacion_duplica_los_cuadros_y_sale_a_30():
    wf = comfy.con_interpolacion(PLANTILLA, "rife_v4.25.safetensors", 30)
    assert wf["interp_modelo"] == {"class_type": "FrameInterpolationModelLoader",
                                   "inputs": {"model_name": "rife_v4.25.safetensors"}}
    assert wf["interp"]["inputs"] == {"interp_model": ["interp_modelo", 0], "images": ["87", 0], "multiplier": 2}
    assert wf["94"]["inputs"]["images"] == ["interp", 0] and wf["94"]["inputs"]["fps"] == 30.0
    assert PLANTILLA["94"]["inputs"] == {"fps": 16.0, "images": ["87", 0]} and "interp" not in PLANTILLA  # no muta
    # el resto del flujo no cambia y se sigue pudiendo rellenar
    pedido = comfy.PedidoClip(imagen=Path("x.png"), prompt="walk", negativo_extra="", ancho=720, alto=1280,
                              cuadros=81, seed=1, prefijo="p")
    listo = comfy.armar_workflow(wf, "in.png", pedido)
    assert listo["interp"]["class_type"] == "FrameInterpolate" and listo["93"]["inputs"]["text"] == "walk"
    with pytest.raises(comfy.ComfyError, match="CreateVideo"):
        comfy.con_interpolacion({"1": {"class_type": "SaveVideo", "inputs": {}}}, "x", 30)


def test_sin_el_modelo_bajado_el_clip_sale_como_antes(tmp_path):
    video = replace(Config().video, workflow=Path(__file__).resolve().parents[1] / "workflows/wan/wan22_i2v_14b.api.json")
    assert "interp" not in comfy.plantilla_para(video, tmp_path)  # falta el modelo: sin interpolacion
    modelos = tmp_path / "models" / "frame_interpolation"
    modelos.mkdir(parents=True)
    (modelos / "rife_v4.25.safetensors").write_bytes(b"x")
    assert comfy.plantilla_para(video, tmp_path)["94"]["inputs"]["fps"] == 30.0
    assert "interp" not in comfy.plantilla_para(replace(video, interpolar=False), tmp_path)
    assert video.segundos_clip == pytest.approx(161 / 30) and replace(video, interpolar=False).segundos_clip == 81 / 16


@con_ffmpeg
def test_cambiar_una_escena_recorta_la_toma_de_verdad(servicio, monkeypatch):  # noqa: F811
    carpeta = _con_tomas(servicio, monkeypatch)
    monkeypatch.undo()  # el recorte de video vuelve a ser el de verdad
    for t in tomas.escanear(servicio.cfg.tomas_dir):
        _video(t.ruta, colores=("smptebars",), segundos=(t.segundos,), tamano="1280x720")
    p = servicio.cambiar_escena("abc", 2)
    assert visuales.duracion(carpeta / "clip_2.mp4") > 1.5 and p.plan.escenas[1].medio == "toma"
