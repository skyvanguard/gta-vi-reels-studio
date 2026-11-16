"""Mas control desde Telegram: voz, subtitulos y titular por separado, cambios con
palabras al ultimo plan o reel, otras imagenes, el guion del editor tal cual y el link que no se puede leer."""
from dataclasses import replace

import httpx
import pytest

from tests.test_almacen_servicio_acceso import _proyecto
from tests.test_bot_trabajador import TgFalso, entorno  # noqa: F401 - fixture
from tests.test_fuentes_imagenes import imagen_bytes
from vstudio import ajustes, botones, fuentes, guion_propio
from vstudio import trabajador as trab_mod
from vstudio.modelos import Estado
from vstudio.servicio import EdicionInvalida

GUION_REAL = """GTA VI podría estar preparando algo enorme: ¡GTA Online!

[INTRO – 0:00 a 0:10]

¿Y si GTA VI no solamente viene con una nueva historia, sino también con un universo online completamente independiente?

[DESARROLLO – 0:10 a 0:35]

Entre ellas aparecen funciones como:

- "joinSession"
- "launchActivity"

Estos nombres hacen referencia a operaciones relacionadas con sesiones y actividades multijugador.

[CIERRE – 0:55 a 1:15]

La gran pregunta es:

¿Será GTA Online 2 o estamos ante el comienzo de algo nuevo?

¡Te leo en los comentarios!"""


# ---------- cambios con palabras ----------
@pytest.mark.parametrize("texto, esperado", [
    ("Quiero uno sin subtitulos", ajustes.Ajuste(subtitulos=False)),  # mensaje real
    ("Usa otras imágenes", ajustes.Ajuste(otras_imagenes=True)),  # mensaje real
    ("pedí un video sin audio", ajustes.Ajuste(voz=False, subtitulos=False)),  # sin audio: sin subtitulos
    ("con voz pero sin subtítulos", ajustes.Ajuste(voz=True, subtitulos=False)),
    ("sin voz pero con los textos", ajustes.Ajuste(voz=False, subtitulos=True)),
    ("ponele voz", ajustes.Ajuste(voz=True, subtitulos=True)),
    ("sacale el titular", ajustes.Ajuste(titular=False)),
    ("solo video", ajustes.Ajuste(solo_animacion=True)),
    ("que dure 30 segundos", ajustes.Ajuste(segundos=30)),
    ("escribí otro guion", ajustes.Ajuste(otro_guion=True)),
    ("Crea un video con esto", ajustes.Ajuste()),
    ("Hacé un video nuevo sin voz sobre la fecha", ajustes.Ajuste()),  # pedido nuevo, no un cambio
    ("Haceme un reel de 1 minuto sobre los rumores del mapa", ajustes.Ajuste()),
    ("Quiero un video de 30 segundos con voz grave sobre Vice City", ajustes.Ajuste()),
    ("con la voz de Jason", ajustes.Ajuste()),  # no es "con voz si/no"
    ("sin voz y otras imágenes", ajustes.Ajuste(voz=False, subtitulos=False, otras_imagenes=True)),
    ("sin subtítulos, que dure 30 s", ajustes.Ajuste(subtitulos=False, segundos=30)),
    ("sin subtítulos ni voz", ajustes.Ajuste(voz=False, subtitulos=False)),
    ("poné nuevas imágenes", ajustes.Ajuste(otras_imagenes=True)),
    ("Rockstar confirmó la fecha de GTA VI y el precio de la edición física", ajustes.Ajuste()),
])
def test_lee_los_cambios_pedidos(texto, esperado):
    assert ajustes.leer(texto) == esperado


def test_un_texto_largo_no_es_un_cambio():
    assert ajustes.leer("sin subtítulos. " + "La nota dice muchas cosas. " * 10).vacio()


def test_id_del_mensaje_citado():
    m = {"reply_to_message": {"caption": "🎞️ EL BARCO NO RETROCEDE · 30.2 s · e14dfba0"}}
    assert ajustes.id_citado(m) == "e14dfba0"
    assert ajustes.id_citado({"reply_to_message": {"text": "Proyecto 3b17fc91 · ~70 s"}}) == "3b17fc91"
    assert ajustes.id_citado({}) is None


# ---------- opciones ----------
def test_sin_voz_apaga_los_subtitulos_y_el_reel_se_rearma(entorno):  # noqa: F811
    _, servicio, _, _, _ = entorno
    servicio.almacen.guardar(_proyecto(estado=Estado.ENTREGADO, chat=70).con(reel="reel_abc.mp4"))
    p = servicio.cambiar_opciones("abc", voz=False)
    assert (p.con_voz, p.con_subtitulos, p.con_titular) == (False, False, True)
    assert p.estado == Estado.EN_COLA and p.reel == ""  # sin GPU: los clips estan
    p = servicio.cambiar_opciones("abc", subtitulos=True)
    assert p.muestra_textos and not p.con_voz  # se pueden pedir textos sin voz


def test_opciones_que_no_se_pueden(entorno):  # noqa: F811
    _, servicio, _, _, _ = entorno
    servicio.almacen.guardar(_proyecto(estado=Estado.MONTANDO, chat=70))
    with pytest.raises(EdicionInvalida, match="se está armando"):
        servicio.cambiar_opciones("abc", subtitulos=False)
    servicio.almacen.guardar(replace(_proyecto(chat=70), con_voz=False, solo_animacion=True))
    for opcion in ("voz", "subtitulos"):  # la animacion no tiene guion: primero hay que escribirlo
        with pytest.raises(EdicionInvalida, match="Otro guion"):
            servicio.cambiar_opciones("abc", **{opcion: True})
    assert servicio.cambiar_opciones("abc", titular=True).muestra_titular


def test_los_botones_muestran_como_esta_cada_opcion():
    p = replace(_proyecto(), con_voz=False, con_subtitulos=False)
    textos = [t for fila in botones.reel(p) for t, _ in fila]
    assert "🔊 Voz: no" in textos and "💬 Subtítulos: no" in textos and "🔥 Titular: sí" in textos
    assert any(d == "ok:abc" for fila in botones.plan(p) for _, d in fila)
    assert not any(d.startswith("ok:") for fila in botones.reel(p) for _, d in fila)


def test_el_montaje_respeta_las_opciones(entorno, monkeypatch):  # noqa: F811
    _, servicio, trabajador, _, _ = entorno
    carpeta = servicio.cfg.proyectos / "abc"
    carpeta.mkdir(parents=True)
    p = _proyecto(estado=Estado.GENERANDO, chat=70)
    for e in p.plan.escenas:
        (carpeta / e.clip).write_bytes(b"mp4")
    servicio.almacen.guardar(replace(p, con_voz=False, con_subtitulos=False))
    pedidos = []

    def reel_falso(escenas, titular, carpeta_, salida, *a, **k):
        pedidos.append((escenas, k))
        return {"duracion_s": 30.0, "bytes": 4}
    monkeypatch.setattr(trab_mod.montaje, "armar_reel", reel_falso)
    trabajador._montar_y_entregar("abc")
    escenas, k = pedidos[0]
    assert k["con_textos"] is False and k["con_titular"] is True  # "sin audio" = sin subtitulos
    assert all(e.audio is None and e.narracion == "" for e in escenas)


# ---------- el bot ----------
def _texto(chat, texto, citado=None):
    m = {"chat": {"id": chat, "type": "private"}, "from": {"id": 7}, "text": texto}
    if citado:
        m["reply_to_message"] = {"caption": citado}
    return {"message": m}


def test_quiero_uno_sin_subtitulos_cambia_el_ultimo_reel(entorno):  # noqa: F811
    tg, servicio, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    servicio.almacen.guardar(replace(_proyecto(estado=Estado.ENTREGADO, chat=70), con_voz=False))
    bot._manejar(_texto(70, "Quiero uno sin subtitulos"))
    p = servicio.almacen.obtener("abc")
    assert p.estado == Estado.EN_COLA and not p.muestra_textos
    assert len(servicio.almacen.listar()) == 1  # no armo un reel nuevo
    assert any("Lo rearmo sin GPU" in t for _, _, t in tg.enviados)


def test_usa_otras_imagenes_va_al_plan_pendiente(entorno, monkeypatch):  # noqa: F811
    tg, servicio, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    servicio.almacen.guardar(_proyecto(chat=70))
    pedidos = []
    monkeypatch.setattr(servicio, "otra_version", lambda id_, **k: pedidos.append((id_, k)) or _proyecto(chat=70))
    bot._manejar(_texto(70, "Usa otras imágenes"))
    bot._ejecutor.shutdown(wait=True)
    assert pedidos == [("abc", {"otras_imagenes": True, "segundos": None, "opciones": {}})]


def test_sin_voz_y_otras_imagenes_lleva_las_dos_cosas(entorno, monkeypatch):  # noqa: F811
    tg, servicio, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    servicio.almacen.guardar(_proyecto(estado=Estado.ENTREGADO, chat=70))
    pedidos = []
    monkeypatch.setattr(servicio, "otra_version", lambda id_, **k: pedidos.append(k) or _proyecto(chat=70))
    bot._manejar(_texto(70, "sin voz y otras imágenes"))
    bot._ejecutor.shutdown(wait=True)
    assert pedidos[0]["opciones"] == {"voz": False, "subtitulos": False} and pedidos[0]["otras_imagenes"]
    assert servicio.almacen.obtener("abc").estado == Estado.ENTREGADO  # el reel hecho no se toca


def test_otro_guion_sin_subtitulos_aplica_las_opciones_despues(entorno, monkeypatch):  # noqa: F811
    tg, servicio, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    servicio.almacen.guardar(replace(_proyecto(estado=Estado.ENTREGADO, chat=70), con_voz=False,
                                     con_subtitulos=False, con_titular=False, solo_animacion=True))

    def reescribir_falso(id_):  # como Servicio.reescribir con una "solo animacion": vuelve todo a si
        return servicio.almacen.actualizar(id_, lambda a: a.con(estado=Estado.ESPERANDO_APROBACION, con_voz=True,
                                                                con_subtitulos=True, con_titular=True,
                                                                solo_animacion=False))
    monkeypatch.setattr(servicio, "reescribir", reescribir_falso)
    bot._manejar(_texto(70, "otro guion, sin subtítulos"))
    bot._ejecutor.shutdown(wait=True)
    p = servicio.almacen.obtener("abc")
    assert p.con_voz and not p.muestra_textos and p.estado == Estado.ESPERANDO_APROBACION


def test_un_pedido_nuevo_corto_no_cambia_el_anterior(entorno, monkeypatch):  # noqa: F811
    tg, servicio, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    servicio.almacen.guardar(_proyecto(estado=Estado.ENTREGADO, chat=70))
    nuevos = []
    monkeypatch.setattr(bot, "_nuevo", lambda chat, texto, fotos: nuevos.append(texto))
    bot._manejar(_texto(70, "Haceme un reel de 1 minuto sobre los rumores del mapa"))
    assert nuevos and servicio.almacen.obtener("abc").estado == Estado.ENTREGADO


def test_voz_pedida_en_la_cola_se_narra_al_montar(entorno, monkeypatch):  # noqa: F811
    _, servicio, trabajador, _, _ = entorno
    carpeta = servicio.cfg.proyectos / "abc"
    carpeta.mkdir(parents=True)
    p = _proyecto(estado=Estado.GENERANDO, chat=70)
    for e in p.plan.escenas:
        (carpeta / e.clip).write_bytes(b"mp4")
    servicio.almacen.guardar(p)  # con voz y sin audios: la pidieron despues de que se narro el lote
    narrados = []

    def voz_falsa(p_):
        narrados.append(p_.id)
        return p_.con(con_voz=False)  # (como si la voz fallara: igual tiene que montar)
    monkeypatch.setattr(trabajador, "_preparar_voz", voz_falsa)
    monkeypatch.setattr(trab_mod.montaje, "armar_reel", lambda *a, **k: {"duracion_s": 30.0, "bytes": 4})
    trabajador._montar_y_entregar("abc")
    assert narrados == ["abc"] and servicio.almacen.obtener("abc").estado == Estado.ENTREGADO


def test_el_cambio_va_al_reel_citado(entorno):  # noqa: F811
    tg, servicio, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    servicio.almacen.guardar(_proyecto(estado=Estado.ENTREGADO, chat=70, id_="aaaa1111"))
    servicio.almacen.guardar(_proyecto(estado=Estado.ENTREGADO, chat=70, id_="bbbb2222"))
    bot._manejar(_texto(70, "sin titular", citado="🎞️ TITULAR · 30 s · aaaa1111"))
    assert not servicio.almacen.obtener("aaaa1111").con_titular
    assert servicio.almacen.obtener("bbbb2222").con_titular


def test_boton_de_subtitulos_en_el_plan_actualiza_el_mensaje(entorno):  # noqa: F811
    tg, servicio, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    editados = []
    tg.editar = lambda chat, mensaje_id, texto, botones_=None: editados.append((mensaje_id, texto, botones_))
    servicio.almacen.guardar(_proyecto(chat=70))
    bot._manejar({"callback_query": {"id": "q", "from": {"id": 7}, "data": "sub:abc",
                                     "message": {"chat": {"id": 70}, "message_id": 55, "text": "Proyecto abc · ~30 s"}}})
    assert not servicio.almacen.obtener("abc").con_subtitulos
    mensaje_id, texto, teclado = editados[0]
    assert mensaje_id == 55 and "💬 Subtítulos: no" in texto
    assert ("💬 Subtítulos: no", "sub:abc") in teclado[1]


# ---------- otras imagenes y otra version ----------
def test_otra_version_excluye_las_imagenes_y_cancela_el_pendiente(entorno, monkeypatch):  # noqa: F811
    _, servicio, _, _, _ = entorno
    p = _proyecto(chat=70)
    servicio.almacen.guardar(replace(p, con_voz=False, con_subtitulos=False,
                                     fuente=replace(p.fuente, url="https://n.com/a", nota_usuario="la nota")))
    pedidos = []

    def crear_falso(*a, **k):  # el plan nuevo trae otro guion (como si lo escribiera el planificador)
        pedidos.append((a, k))
        nuevo = _proyecto(id_="nuevo")
        escenas = tuple(replace(e, texto="otro", imagen=f"nueva_{e.n}.png") for e in nuevo.plan.escenas)
        return servicio.almacen.guardar(nuevo.con(plan=replace(nuevo.plan, titular="OTRO", escenas=escenas,
                                                               texto_post="otro post")))
    monkeypatch.setattr(servicio, "crear", crear_falso)
    nuevo = servicio.otra_version("abc", otras_imagenes=True)
    (chat, texto, fotos), k = pedidos[0]
    assert (chat, texto, fotos) == (70, "https://n.com/a la nota", [])
    assert [r.name for r in k["excluir"]] == ["nota_1.jpg"]
    assert k["opciones"] == {"voz": False, "subtitulos": False, "titular": True}
    assert k["guion_editor"] == ("n1", "n2", "n3")  # la narracion va tal cual
    assert nuevo.plan.titular == "TITULAR" and nuevo.plan.texto_post == "post"  # el resto del guion tambien
    assert [e.texto for e in nuevo.plan.escenas] == ["t1", "t2", "t3"]
    assert [e.imagen for e in nuevo.plan.escenas] == ["nueva_1.png", "nueva_2.png", "nueva_3.png"]
    assert servicio.almacen.obtener("abc").estado == Estado.CANCELADO  # el nuevo salio: el viejo se cancela


def test_otra_version_que_falla_deja_el_plan_original(entorno, monkeypatch):  # noqa: F811
    _, servicio, _, _, _ = entorno
    servicio.almacen.guardar(_proyecto(chat=70))
    monkeypatch.setattr(servicio, "crear", lambda *a, **k: _proyecto(estado=Estado.ERROR, id_="nuevo"))
    servicio.otra_version("abc", otras_imagenes=True)
    assert servicio.almacen.obtener("abc").estado == Estado.ESPERANDO_APROBACION


def test_otras_imagenes_con_fotos_del_editor(entorno):  # noqa: F811
    _, servicio, _, _, _ = entorno
    p = _proyecto(estado=Estado.ENTREGADO, chat=70)
    servicio.almacen.guardar(replace(p, fuente=replace(p.fuente, imagenes=("usuario_1.jpg",))))
    with pytest.raises(EdicionInvalida, match="usa tus fotos"):
        servicio.otra_version("abc", otras_imagenes=True)


def test_las_imagenes_excluidas_no_vuelven(tmp_path):
    vieja = tmp_path / "vieja.jpg"
    vieja.write_bytes(imagen_bytes(1350, 900, color=(200, 30, 30)))
    f = fuentes.preparar_fuente("nota", [imagen_bytes(1350, 900, color=(200, 30, 30))], tmp_path / "p",
                                excluir=(vieja,))
    assert f.imagenes == ()  # la unica foto era la del plan anterior


# ---------- link que no se puede leer ----------
def test_link_bloqueado_usa_el_texto_pegado(tmp_path, monkeypatch):
    def prohibido(url):
        raise httpx.HTTPStatusError("Client error '403 Forbidden'", request=httpx.Request("GET", url),
                                    response=httpx.Response(403))
    monkeypatch.setattr(fuentes, "leer_articulo", prohibido)
    pegado = "Aparecen indicios de GTA Online en los archivos de GTA VI. " * 5
    f = fuentes.preparar_fuente(f"https://www.forbes.com/x {pegado}", [], tmp_path / "p")
    assert f.texto == "" and "403" in f.aviso and f.nota_usuario.startswith("Aparecen indicios")
    with pytest.raises(httpx.HTTPStatusError):
        fuentes.preparar_fuente("https://www.forbes.com/x mirá esto", [], tmp_path / "q")


# ---------- guion del editor ----------
def test_lee_el_guion_del_editor_tal_cual():
    g = guion_propio.leer(GUION_REAL)
    assert g.segundos == 75
    assert g.oraciones[0] == "GTA seis podría estar preparando algo enorme: ¡GTA Online!"  # tambien se dice
    assert g.oraciones[1].startswith("¿Y si GTA seis no solamente")  # "GTA VI" se dice "GTA seis"
    assert 'Entre ellas aparecen funciones como: "joinSession", "launchActivity".' in g.oraciones
    assert g.oraciones[-1] == "¡Te leo en los comentarios!"
    assert not any("[" in o or "0:" in o for o in g.oraciones)  # ni secciones ni tiempos


@pytest.mark.parametrize("texto, oraciones, segundos", [
    # viñetas sueltas: cada una es una oracion y no se pegan a la seccion anterior
    ("[INTRO]\n- GTA VI sale el 19.\n- Tiene dos protagonistas.\n[MAPA]\n- Vice City vuelve.\n- Hay pantanos.",
     ("GTA seis sale el 19.", "Tiene dos protagonistas.", "Vice City vuelve.", "Hay pantanos."), None),
    # la primera frase, aunque este antes de las secciones, se dice
    ("Hola a todos, hoy hablamos de GTA VI.\n[INTRO 0:00 a 0:10]\nSale el 19.\n[CIERRE]\nNos vemos.",
     ("Hola a todos, hoy hablamos de GTA seis.", "Sale el 19.", "Nos vemos."), 10),
    # tiempos sin corchetes: no se leen en voz alta; un titulo sin puntuacion tampoco
    ("GUION REEL\n0:00 a 0:10\nPrimera parte del guion.\n0:10 a 0:25\nSegunda parte.\n(0:25-0:30) Tercera parte.",
     ("Primera parte del guion.", "Segunda parte.", "Tercera parte."), 30),
])
def test_casos_del_guion_del_editor(texto, oraciones, segundos):
    assert guion_propio.leer(texto) == guion_propio.GuionPropio(oraciones=oraciones, segundos=segundos)


def test_un_texto_comun_no_es_un_guion():
    assert guion_propio.leer("Rockstar publicó hoy un tráiler de 29 segundos. Se ve a Jason y Lucía.") is None


def test_repartir_no_corta_oraciones_y_equilibra():
    oraciones = tuple(f"Oración número {i} con algunas palabras más." for i in range(10))
    tramos = guion_propio.repartir(oraciones, 4)
    assert len(tramos) == 4 and " ".join(tramos) == " ".join(oraciones)
    assert guion_propio.repartir(oraciones[:2], 5) == list(oraciones[:2])  # no hay escenas vacias


def test_plan_con_guion_del_editor(tmp_path):
    from datetime import date

    from tests.test_planificador_llm import LlmFalso
    from vstudio.modelos import Fuente
    from vstudio.planificador import planificar
    for i in range(2):
        (tmp_path / f"oficial_{i + 1}.jpg").write_bytes(imagen_bytes(2700, 1800, color=(30 + i * 100, 90, 90)))
    g = guion_propio.leer(GUION_REAL)
    fuente = Fuente(nota_usuario=GUION_REAL, imagenes=("oficial_1.jpg", "oficial_2.jpg"), guion=g.oraciones)
    escenas = [{"imagen": imagen, "centro_x": x, "texto": "t", "narracion": "borrador"}
               for imagen, x in ((0, 0.1), (1, 0.5), (0, 0.9))]
    movimiento = {"motion_prompt_en": "camera pans", "negative_extra_en": ""}
    falso = LlmFalso([{"titular": "GTA ONLINE EN CAMINO", "escenas": escenas, "texto_post": "#GTA6"}]
                     + [movimiento] * 3)
    plan = planificar(falso, tmp_path, fuente, date(2026, 11, 11), 3, 7, 720, 1280)
    assert " ".join(e.narracion for e in plan.escenas) == " ".join(g.oraciones)
    assert len(falso.llamadas) == 4  # sin guionista: la narracion es la del editor
    assert "TAL CUAL" in falso.llamadas[0] and plan.advertencia.startswith("Narración: tu guion tal cual")
