"""Publicacion en Facebook y control de calidad antes de publicar."""
import json
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from tests.conftest import ZONA_PRUEBA
from tests.test_almacen_servicio_acceso import _proyecto
from tests.test_bot_trabajador import entorno  # noqa: F401 - fixture
from tests.test_planificador_llm import LlmFalso
from vstudio import automatico, calidad, facebook, pronunciacion, publicador, verificador
from vstudio import trabajador as trabajador_mod
from vstudio.modelos import Estado

ZONA = ZoneInfo(ZONA_PRUEBA)
TOKEN = "EAAB-SECRETO-de-la-pagina"
PAGINA = facebook.Pagina(id="1234567890", nombre="GTA 6 Fans", token=TOKEN)


# ---------- cliente de Facebook ----------
def _meta(pedidos, finish_ok=True):
    def responder(req: httpx.Request) -> httpx.Response:
        pedidos.append(req)
        if req.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True})
        if req.method == "POST" and b"start" in req.content:
            return httpx.Response(200, json={"video_id": "998877", "upload_url":
                                             "https://rupload.facebook.com/video-upload/v27.0/998877"})
        if req.method == "POST":
            return httpx.Response(200, json={"success": finish_ok})
        return httpx.Response(400, json={"error": {"message": f"token inválido {TOKEN}"}})
    return httpx.Client(transport=httpx.MockTransport(responder))


def test_programa_un_reel_en_tres_pasos(tmp_path):
    reel = tmp_path / "reel.mp4"
    reel.write_bytes(b"x" * 1000)
    pedidos = []
    cliente = facebook.ClienteFacebook(PAGINA, _meta(pedidos))
    cuando = datetime(2026, 11, 12, 12, 0, tzinfo=ZONA)
    assert cliente.publicar_reel(reel, "Se viene GTA VI #GTA6", cuando) == "998877"
    inicio, subida, fin = pedidos
    assert inicio.url.path == "/v27.0/1234567890/video_reels" and json.loads(inicio.content)["upload_phase"] == "start"
    assert subida.headers["authorization"] == f"OAuth {TOKEN}" and subida.headers["file_size"] == "1000"
    assert subida.headers["offset"] == "0" and subida.content == b"x" * 1000
    campos = dict(httpx.QueryParams(fin.content.decode()))
    assert campos["upload_phase"] == "finish" and campos["video_id"] == "998877"
    assert campos["video_state"] == "SCHEDULED" and campos["scheduled_publish_time"] == str(int(cuando.timestamp()))
    assert campos["is_ai_generated"] == "true" and campos["description"] == "Se viene GTA VI #GTA6"


def test_los_errores_no_muestran_el_token(tmp_path):
    cliente = facebook.ClienteFacebook(PAGINA, _meta([]))
    with pytest.raises(facebook.FacebookError) as exc:
        cliente.nombre()
    assert TOKEN not in str(exc.value) and "<token>" in str(exc.value)
    reel = tmp_path / "reel.mp4"
    reel.write_bytes(b"x")
    with pytest.raises(facebook.FacebookError, match="no confirmó la publicación"):
        facebook.ClienteFacebook(PAGINA, _meta([], finish_ok=False)).publicar_reel(reel, "post")


def test_elige_la_pagina_con_permiso_de_publicar():
    def responder(req):
        return httpx.Response(200, json={"data": [
            {"id": "1", "name": "GTA 6 Fans", "access_token": "tok1", "tasks": ["CREATE_CONTENT", "MODERATE"]},
            {"id": "2", "name": "Otra", "access_token": "tok2", "tasks": ["ANALYZE"]}]})
    paginas = facebook.paginas_del_usuario("usuario", httpx.Client(transport=httpx.MockTransport(responder)))
    assert paginas == [facebook.Pagina(id="1", nombre="GTA 6 Fans", token="tok1")]


# ---------- horarios ----------
def test_proximo_horario_libre():
    horarios = ("08:00", "12:00", "18:00")
    ahora = datetime(2026, 11, 12, 11, 50, tzinfo=ZONA)
    # 12:00 esta a menos de 15 min: la siguiente es 18:00
    assert publicador.proximo_horario(ahora, horarios, set(), 8) == datetime(2026, 11, 12, 18, 0, tzinfo=ZONA)
    ocupado = {datetime(2026, 11, 12, 18, 0, tzinfo=ZONA)}
    assert publicador.proximo_horario(ahora, horarios, ocupado, 8) == datetime(2026, 11, 13, 8, 0, tzinfo=ZONA)
    temprano = datetime(2026, 11, 12, 7, 0, tzinfo=ZONA)
    llenos = {datetime(2026, 11, 12, 8, 0, tzinfo=ZONA)}
    assert publicador.proximo_horario(temprano, horarios, llenos, 1) == datetime(2026, 11, 13, 8, 0, tzinfo=ZONA)


# ---------- control de calidad ----------
def test_motivos_para_que_lo_mire_el_editor():
    p = _proyecto()
    assert calidad.motivos_del_plan(p, 30.0) == []  # "nota vieja" y similares no frenan
    adv = verificador.con_dudas("La nota es de ayer", ["el motel Starlet — no está"])
    adv = pronunciacion.con_aviso(adv, "escena 3: movió")
    con = p.con(plan=replace(p.plan, advertencia=adv))
    assert calidad.motivos_del_plan(con, 95.0) == [
        "la verificación encontró datos que la nota no respalda", "la voz puede decir mal alguna palabra",
        "dura 95 s (Facebook acepta reels de 3 a 90 s)"]


def test_el_aviso_de_pronunciacion_se_reemplaza():
    adv = pronunciacion.con_aviso("nota vieja", "escena 3: movió")
    assert adv == "nota vieja | Pronunciación dudosa: escena 3: movió"
    assert pronunciacion.con_aviso(adv, "") == "nota vieja"  # otra toma sin dudas: se va
    assert pronunciacion.con_aviso(adv, "escena 1: año") == "nota vieja | Pronunciación dudosa: escena 1: año"


def test_dudas_de_la_advertencia():
    adv = verificador.con_dudas("nota | Guion para revisar: x", ["kayak — no está", "Starlet — tampoco"])
    assert verificador.dudas_de(adv) == ("kayak — no está", "Starlet — tampoco")
    assert verificador.dudas_de("nota") == ()


def test_escenas_deformes(tmp_path):
    from PIL import Image
    cuadros = []
    for n in (1, 2):
        ruta = tmp_path / f"c{n}.jpg"
        Image.new("RGB", (90, 160)).save(ruta)
        cuadros.append((n, ruta))
    falso = LlmFalso([{"defectuosas": [{"escena": 2, "problema": "la cara se derrite"}, {"escena": 9}]}])
    assert calidad.escenas_defectuosas(falso, cuadros) == ["escena 2: la cara se derrite"]
    assert calidad.escenas_defectuosas(LlmFalso([ValueError("x")]), cuadros) is None  # no pudo mirar: no es "bien"
    assert calidad.escenas_defectuosas(LlmFalso([{"otra": 1}]), cuadros) is None
    assert calidad.escenas_defectuosas(LlmFalso([]), []) is None


# ---------- el publicador ----------
class FbFalso:
    def __init__(self, falla_en_fin=False, igual_salio=False):
        self.subidos, self.falla_en_fin, self.igual_salio = [], falla_en_fin, igual_salio

    def publicar_reel(self, ruta, descripcion, cuando=None, con_ia=True, al_iniciar=None):
        video_id = f"fb{len(self.subidos) + 1}"
        if al_iniciar:
            al_iniciar(video_id)
        if self.falla_en_fin:
            raise facebook.FacebookError("publicación: se cortó la conexión")
        self.subidos.append((ruta.name, descripcion, cuando))
        return video_id

    def ya_salio(self, video_id):
        return self.igual_salio

    def borrar(self, video_id):
        self.subidos.append(("borrado", video_id, None))


def _sin_controles(monkeypatch, defectos=()):
    monkeypatch.setattr(publicador.montaje, "duracion", lambda ruta: 30.0)
    monkeypatch.setattr(publicador.calidad, "cuadros", lambda escenas, carpeta: list(escenas))
    monkeypatch.setattr(publicador.calidad, "escenas_defectuosas", lambda cliente, cuadros: list(defectos))


def _reel(servicio, id_="abc", **cambios):
    carpeta = servicio.cfg.proyectos / id_
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / f"reel_{id_}.mp4").write_bytes(b"mp4")
    p = _proyecto(estado=Estado.ENTREGADO, chat=70, id_=id_)
    return servicio.almacen.guardar(replace(p, reel=f"reel_{id_}.mp4", **{"automatico": True, **cambios}))


def test_un_reel_limpio_se_programa_solo(entorno, monkeypatch):  # noqa: F811
    tg, servicio, _, _, _ = entorno
    _reel(servicio)
    fb = FbFalso()
    pub = publicador.Publicador(servicio, cliente=fb)
    _sin_controles(monkeypatch)
    pub.ciclo()
    p = servicio.almacen.obtener("abc")
    assert p.publicacion == "programado" and p.fb_id == "fb1" and fb.subidos[0][:2] == ("reel_abc.mp4", "post")
    assert "programado en Facebook" in tg.enviados[-1][2]
    pub.ciclo()
    assert len(fb.subidos) == 1  # no se sube dos veces


def test_un_reel_dudoso_espera_al_editor(entorno, monkeypatch):  # noqa: F811
    tg, servicio, _, _, _ = entorno
    p = _reel(servicio)
    servicio.almacen.guardar(p.con(plan=replace(p.plan, advertencia=verificador.con_dudas("", ["kayak"]))))
    fb = FbFalso()
    pub = publicador.Publicador(servicio, cliente=fb)
    _sin_controles(monkeypatch)
    pub.ciclo()
    assert servicio.almacen.obtener("abc").publicacion == "revisar" and fb.subidos == []
    assert "no sale solo a Facebook" in tg.enviados[-1][2]
    pub.programar("abc")  # el editor toca "📤 Programar igual"
    assert servicio.almacen.obtener("abc").publicacion == "programado"


def test_los_manuales_no_salen_solos_y_los_horarios_no_se_pisan(entorno, monkeypatch):  # noqa: F811
    _, servicio, _, _, _ = entorno
    _reel(servicio, "aaa", automatico=False)
    fb = FbFalso()
    pub = publicador.Publicador(servicio, cliente=fb)
    pub.ciclo()
    assert fb.subidos == []
    _reel(servicio, "bbb", automatico=False)
    ahora = datetime(2026, 11, 12, 7, 0, tzinfo=ZoneInfo(servicio.cfg.zona_horaria))
    primero, segundo = pub.programar("aaa", ahora), pub.programar("bbb", ahora)
    assert (primero.hour, segundo.hour) == (8, 10)
    pub.despublicar("aaa")
    assert servicio.almacen.obtener("aaa").publicacion == "retirado" and fb.subidos[-1] == ("borrado", "fb1", None)


def test_despublicar_un_automatico_no_lo_vuelve_a_subir(entorno, monkeypatch):  # noqa: F811
    """Revision: con publicacion "" el ciclo lo volvia a programar al minuto."""
    _, servicio, _, _, _ = entorno
    _reel(servicio)
    fb = FbFalso()
    pub = publicador.Publicador(servicio, cliente=fb)
    _sin_controles(monkeypatch)
    pub.ciclo()
    pub.despublicar("abc")
    pub.ciclo()
    assert [s[0] for s in fb.subidos] == ["reel_abc.mp4", "borrado"]


def test_un_control_que_no_corrio_no_deja_salir_solo(entorno, monkeypatch):  # noqa: F811
    tg, servicio, _, _, _ = entorno
    _reel(servicio)
    pub = publicador.Publicador(servicio, cliente=FbFalso())
    monkeypatch.setattr(publicador.montaje, "duracion", lambda ruta: 30.0)
    monkeypatch.setattr(publicador.calidad, "cuadros", lambda escenas, carpeta: list(escenas)[:1])
    monkeypatch.setattr(publicador.calidad, "escenas_defectuosas", lambda cliente, cuadros: None)
    pub.ciclo()
    assert servicio.almacen.obtener("abc").publicacion == "revisar"
    assert "no pude sacar cuadros de 2 escena(s)" in tg.enviados[-1][2] and "no pude revisar las imágenes" in \
        tg.enviados[-1][2]


def test_un_reel_que_falla_no_frena_a_los_demas(entorno, monkeypatch):  # noqa: F811
    tg, servicio, _, _, _ = entorno
    _reel(servicio, "aaa")
    _reel(servicio, "bbb")
    pub = publicador.Publicador(servicio, cliente=FbFalso())
    _sin_controles(monkeypatch)
    monkeypatch.setattr(publicador.montaje, "duracion", lambda ruta: 1 / 0 if "aaa" in str(ruta) else 30.0)
    pub.ciclo()
    assert servicio.almacen.obtener("aaa").publicacion == "error"  # no se reintenta cada minuto
    assert servicio.almacen.obtener("bbb").publicacion == "programado"


def test_si_se_corta_al_final_no_sube_dos_veces(entorno, monkeypatch):  # noqa: F811
    _, servicio, _, _, _ = entorno
    _reel(servicio, automatico=False)
    fb = FbFalso(falla_en_fin=True, igual_salio=True)  # Meta lo acepto pero la respuesta se perdio
    pub = publicador.Publicador(servicio, cliente=fb)
    pub.programar("abc")
    p = servicio.almacen.obtener("abc")
    assert p.publicacion == "programado" and p.fb_id == "fb1"
    _reel(servicio, "bbb", automatico=False)
    fb2 = FbFalso(falla_en_fin=True, igual_salio=False)  # no salio: se borra lo subido y queda en error
    with pytest.raises(facebook.FacebookError):
        publicador.Publicador(servicio, cliente=fb2).programar("bbb")
    assert servicio.almacen.obtener("bbb").publicacion == "error" and fb2.subidos == [("borrado", "fb1", None)]


def test_el_post_sale_sin_links_ni_menciones():
    texto = "Mirá esto https://malo.com/x y www.otro.com @alguien\n¿Qué opinás? #GTA6 #GTAVI"
    assert calidad.texto_para_publicar(texto) == "Mirá esto y\n¿Qué opinás? #GTA6 #GTAVI"


def test_lo_grave_del_guion_frena_y_lo_menor_no():
    p = _proyecto()
    grave = p.con(plan=replace(p.plan, advertencia="nota | Guion para revisar: la última escena tiene que cerrar "
                                                    "con UNA pregunta concreta"))
    menor = p.con(plan=replace(p.plan, advertencia="nota | Guion para revisar: muletilla repetida"))
    assert calidad.motivos_del_plan(grave, 30) == ["el guion no cumple reglas importantes (pregunta)"]
    assert calidad.motivos_del_plan(menor, 30) == []


def test_upload_url_que_no_es_de_facebook_no_recibe_el_token(tmp_path):
    reel = tmp_path / "reel.mp4"
    reel.write_bytes(b"x")
    pedidos = []

    def responder(req):
        pedidos.append(req)
        if req.method == "POST" and b"start" in req.content:
            return httpx.Response(200, json={"video_id": "55", "upload_url": "https://evil.example/subir"})
        return httpx.Response(200, json={"success": True})
    facebook.ClienteFacebook(PAGINA, httpx.Client(transport=httpx.MockTransport(responder))).publicar_reel(reel, "p")
    assert pedidos[1].url.host == "rupload.facebook.com"


def test_el_token_no_aparece_en_el_repr():
    assert TOKEN not in repr(PAGINA)


def test_automatico_reescribe_sin_los_datos_dudosos(entorno, monkeypatch):  # noqa: F811
    _, servicio, _, acceso, _ = entorno
    p = _proyecto(chat=70)
    servicio.almacen.guardar(p.con(plan=replace(p.plan, advertencia=verificador.con_dudas("", ["kayak — no"]))))
    pedidos = []
    monkeypatch.setattr(servicio, "reescribir", lambda id_, evitar=(): pedidos.append((id_, evitar)) or p)
    a = automatico.Automatico(servicio, acceso, buscar=lambda *a: [])
    a._sin_datos_dudosos(servicio.almacen.obtener("abc"))
    assert pedidos == [("abc", ("kayak — no",))]


def test_el_guionista_recibe_lo_que_no_tiene_que_decir():
    from datetime import date

    from tests.test_planificador_llm import guion_json
    from vstudio import guionista
    from vstudio.modelos import Fuente
    narr = ["Doscientos doce gigas: eso es lo que te va a pedir GTA seis.",
            "Y lo más fuerte no es el tamaño, es la fecha en que arranca la precarga.",
            "¿Tú lo vas a precargar el primer día o esperas las reseñas?"]
    falso = LlmFalso([guion_json(narr)])
    guionista.escribir(falso, Fuente(nota_usuario="nota"), ["a", "b", "c"], date(2026, 11, 12), 15,
                       evitar=("el motel Starlet — no está en la nota",))
    assert "NO DIGAS ESTO" in falso.partes[0] and "motel Starlet" in falso.partes[0]


def test_el_boton_de_facebook_solo_si_esta_configurado():
    from vstudio import botones
    datos = [d for fila in botones.reel(_proyecto()) for _, d in fila]
    assert "pub:abc" not in datos  # sin la pagina configurada se publica a mano
    assert "pub:abc" in [d for fila in botones.reel(_proyecto(), con_facebook=True) for _, d in fila]


def test_bot_boton_programar(entorno):  # noqa: F811
    tg, servicio, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    _reel(servicio, automatico=False)
    fb = FbFalso()
    bot.publicador = publicador.Publicador(servicio, cliente=fb)
    bot._manejar({"callback_query": {"id": "q", "from": {"id": 7}, "data": "pub:abc",
                                     "message": {"chat": {"id": 70}, "message_id": 5}}})
    for hilo in __import__("threading").enumerate():
        if hilo.name == "fb-abc":
            hilo.join(5)
    assert servicio.almacen.obtener("abc").publicacion == "programado"


def test_publicar_sin_configurar_avisa(entorno):  # noqa: F811
    tg, servicio, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    bot.publicador = publicador.Publicador(servicio)  # sin token en secrets/
    bot._manejar({"message": {"chat": {"id": 70, "type": "private"}, "from": {"id": 7}, "text": "/publicar abc"}})
    assert "no está configurado" in tg.enviados[-1][2]
    bot._manejar({"message": {"chat": {"id": 70, "type": "private"}, "from": {"id": 7}, "text": "/facebook"}})
    assert "todavía no está configurado" in tg.enviados[-1][2]


def test_horarios_futuros_del_resumen(entorno):  # noqa: F811
    _, servicio, _, _, _ = entorno
    zona = ZoneInfo(servicio.cfg.zona_horaria)
    _reel(servicio, publicacion="programado", fb_id="1",
          fb_hora=(datetime.now(zona) + timedelta(hours=2)).isoformat())
    texto = publicador.Publicador(servicio, cliente=FbFalso()).resumen()
    assert "TITULAR (abc)" in texto


def test_un_automatico_viejo_no_sale_solo(entorno, monkeypatch):  # noqa: F811
    """Si Facebook se configura dias despues, las noticias de esos dias no se programan solas."""
    _, servicio, _, _, _ = entorno
    p = _reel(servicio)
    viejo = (datetime.now(ZoneInfo("UTC")) - timedelta(hours=30)).isoformat(timespec="seconds")
    servicio.almacen.guardar(replace(p, actualizado=viejo))
    fb = FbFalso()
    _sin_controles(monkeypatch)
    publicador.Publicador(servicio, cliente=fb).ciclo()
    assert fb.subidos == [] and servicio.almacen.obtener("abc").publicacion == ""


# ---------- escenas deformes: se rehacen ----------
def _generando(servicio):
    carpeta = servicio.cfg.proyectos / "abc"
    carpeta.mkdir(parents=True, exist_ok=True)
    p = _proyecto(estado=Estado.GENERANDO, chat=70)
    for e in p.plan.escenas:
        (carpeta / e.clip).write_bytes(b"mp4")
    servicio.cfg = replace(servicio.cfg, video=replace(servicio.cfg.video, revisar_deformes=True))
    return servicio.almacen.guardar(p)


def test_la_escena_deforme_se_rehace(entorno, monkeypatch):  # noqa: F811
    tg, servicio, trabajador, _, _ = entorno
    _generando(servicio)
    trabajador.cfg = servicio.cfg
    monkeypatch.setattr(calidad, "cuadros", lambda escenas, carpeta: list(escenas))
    monkeypatch.setattr(calidad, "defectos", lambda cliente, cuadros: {2: "la cara se derrite"})
    monkeypatch.setattr(trabajador_mod.montaje, "armar_reel", lambda *a, **k: pytest.fail("no tenia que montar"))
    trabajador._montar_y_entregar("abc")
    p = servicio.almacen.obtener("abc")
    e1, e2, e3 = p.plan.escenas
    assert p.estado == Estado.EN_COLA  # el proximo turno la rehace
    assert e2.clip == "" and e2.regeneraciones == 1 and e2.seed != 42 and not e2.revisada
    assert e1.revisada and e3.revisada and e1.clip  # las buenas no se vuelven a mirar
    assert "rehago 1 escena(s)" in tg.enviados[-1][2] and "la cara se derrite" in tg.enviados[-1][2]


def test_despues_de_rehacerla_dos_veces_sale_con_aviso(entorno, monkeypatch):  # noqa: F811
    tg, servicio, trabajador, _, _ = entorno
    p = _generando(servicio)
    escenas = tuple(replace(e, revisada=e.n != 2, regeneraciones=2 if e.n == 2 else 0) for e in p.plan.escenas)
    servicio.almacen.guardar(p.con(plan=replace(p.plan, escenas=escenas)))
    trabajador.cfg = servicio.cfg
    mirados = []
    monkeypatch.setattr(calidad, "cuadros", lambda escenas, carpeta: list(escenas))
    monkeypatch.setattr(calidad, "defectos", lambda cliente, cuadros: mirados.append([n for n, _ in cuadros])
                        or {2: "manos fundidas"})
    monkeypatch.setattr(trabajador_mod.montaje, "armar_reel", lambda *a, **k: {"duracion_s": 30.0, "bytes": 4})
    trabajador._montar_y_entregar("abc")
    final = servicio.almacen.obtener("abc")
    assert mirados == [[2]]  # solo la escena nueva
    assert final.estado == Estado.ENTREGADO and final.plan.escenas[1].revisada
    assert "Puede verse deforme: escena 2: manos fundidas (ya se rehizo 2 veces)" in final.plan.advertencia


def test_si_no_pudo_mirar_monta_igual_y_avisa(entorno, monkeypatch):  # noqa: F811
    _, servicio, trabajador, _, _ = entorno
    _generando(servicio)
    trabajador.cfg = servicio.cfg
    monkeypatch.setattr(calidad, "cuadros", lambda escenas, carpeta: list(escenas))
    monkeypatch.setattr(calidad, "defectos", lambda cliente, cuadros: None)
    monkeypatch.setattr(trabajador_mod.montaje, "armar_reel", lambda *a, **k: {"duracion_s": 30.0, "bytes": 4})
    trabajador._montar_y_entregar("abc")
    final = servicio.almacen.obtener("abc")
    assert final.estado == Estado.ENTREGADO
    assert final.plan.advertencia.startswith("No se pudo revisar si hay escenas deformes.")


def test_regenerar_a_mano_vuelve_a_mirar_la_escena(entorno):  # noqa: F811
    _, servicio, _, _, _ = entorno
    p = _proyecto(estado=Estado.ENTREGADO, chat=70)
    servicio.almacen.guardar(p.con(plan=replace(p.plan, escenas=tuple(replace(e, revisada=True)
                                                                        for e in p.plan.escenas))))
    servicio.regenerar_escena("abc", 2)
    assert not servicio.almacen.obtener("abc").plan.escenas[1].revisada
