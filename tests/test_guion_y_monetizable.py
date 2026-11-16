from dataclasses import replace
from datetime import date

import pytest

from tests.test_almacen_servicio_acceso import _proyecto
from tests.test_bot_trabajador import LlmQuieto, TgFalso, _msg
from tests.test_planificador_llm import PLAN_OK, LlmFalso, _fuente, guion_json
from vstudio import guion, guionista, montaje
from vstudio.acceso import Acceso
from vstudio.almacen import Almacen
from vstudio.bot import Bot, resumen_plan
from vstudio.llm import LlmError
from vstudio.modelos import Escena, Estado, Gancho, proyecto_a_dict, proyecto_desde_dict
from vstudio.planificador import planificar, validar_plan
from vstudio.servicio import EdicionInvalida, Servicio
from vstudio.trabajador import Trabajador, minutos_de_turno

GUION_OK = ["Doscientos doce gigas: eso es lo que te va a pedir GTA seis.",
            "Y lo más fuerte no es el tamaño, es la fecha en que arranca la precarga.",
            "¿Tú lo vas a precargar el primer día o esperas las reseñas?"]
GANCHOS = [{"titular": "¿TE ENTRA GTA VI?", "narracion": "¿Te entra GTA seis en la consola? Revisá esto."},
           {"titular": "HOLA", "narracion": "Hola a todos, hoy les traigo una noticia de GTA seis."},
           "basura"]
GUION_BUENO = guion_json(GUION_OK, ganchos=GANCHOS)
MOVIMIENTO = {"motion_prompt_en": "people walk", "negative_extra_en": "warping"}
ROBOTICO = [  # el guion de un reel real que sonaba soso y robotico (recortado)
    "GTA seis ya te pide doscientos doce gigas en tu consola.",
    "Y ojo, un detalle de las cajas en papel que nadie te contaba.",
    "El pre-load digital arranca el diez de noviembre, confirmado por Rockstar.",
    "Pero ojo: la fecha se adelantó, antes era el doce, ahora el diez.",
    "Sony ya avisó: libera doscientos doce gigas y pensá en lugar de sobra.",
    "Ojo: el pre-load no es acceso anticipado, se descarga antes, se juega el diecinueve.",
    "¿Cargás los doscientos doce gigas ya o esperás a ver el lanzamiento?"]

VERIFICADO = {"sin_respaldo": []}  # la respuesta del verificador de datos: todo respaldado


def test_guion_que_cumple_no_tiene_problemas():
    assert guion.revisar(GUION_OK) == []


@pytest.mark.parametrize("primera, motivo", [
    ("¡Hola a todos! GTA seis ya tiene fecha de precarga confirmada.", "arranca con"),
    ("En este video te cuento todo sobre la precarga de GTA seis.", "arranca con"),
    ("¡Atención, gamers! GTA seis ya tiene fecha de precarga.", "arranca con"),
    ("GTA seis " + "muy " * 14 + "grande.", "palabras"),
])
def test_gancho_debil_o_largo(primera, motivo):
    assert any(motivo in p for p in guion.revisar([primera, *GUION_OK[1:]]))


def test_cierre_sin_pregunta_y_escenas_fuera_de_rango():
    problemas = guion.revisar([GUION_OK[0], "Muy corta.", "Seguinos para no perderte ningún detalle de este juego."])
    assert any("escena 2" in p for p in problemas) and any("pregunta" in p for p in problemas)


def test_el_guion_robotico_se_detecta():
    problemas = " | ".join(guion.revisar(ROBOTICO))
    assert "muletilla \"ojo\" 3 veces" in problemas  # el prompt viejo daba "pero ojo" de ejemplo y lo copiaba
    assert "la precarga" in problemas  # anglicismo
    assert "doscientos doce gigas" in problemas  # la misma cifra en 3 escenas


@pytest.mark.parametrize("mal, bien", [
    ("El pre-carga arranca el diez.", "La precarga arranca el diez."),
    ("Te baja el pre-load a medianoche.", "Te baja la precarga a medianoche."),
    ("PRE-CARGA: 10 DE NOVIEMBRE", "PRECARGA: 10 DE NOVIEMBRE"),
    ("La fecha del pre descarga.", "La fecha de la precarga."),
    ("La precarga ya tiene fecha.", "La precarga ya tiene fecha."),
])
def test_precarga_se_corrige_sola(mal, bien):
    assert guion.para_voz(mal) == bien and guion.en_pantalla(mal) == bien


def test_cifras_y_variantes_de_precarga():
    problemas = " | ".join(guion.revisar(["GTA seis te va a comer 212 gigas.", *GUION_OK[1:2],
                                          "¿El pre-descarga del diez te encuentra con espacio libre en tu consola?"]))
    assert "la escena 1 tiene números en cifras" in problemas and "la precarga" in problemas


def test_voseo_se_marca_tuteo_no():
    vos = guion.revisar([*GUION_OK[:2], "¿Confiás en que esta vez sí, o ya podés hacer lugar en tu consola?"])
    assert any("usa tú, no vos" in p and "confiás" in p and "podés" in p for p in vos)
    assert not any("vos" in p for p in guion.revisar(
        [*GUION_OK[:2], "¿Confías en que esta vez sí, o ya puedes hacer lugar en tu consola?"]))


def test_guion_corto_para_pasar_del_minuto():
    minimas = guion.palabras_para(montaje.voz_necesaria_s(3, 62.0))
    assert any("pasar del minuto" in p for p in guion.revisar(GUION_OK, minimas))
    assert guion.revisar(GUION_OK, palabras_minimas=30) == []


def test_para_voz_saca_lo_que_la_voz_lee_mal():
    assert guion.para_voz('En PS5 (medianoche); en Xbox "GMT" / hora') == "En PS5 medianoche , en Xbox GMT hora"


@pytest.mark.parametrize("cebo", ["¿Lo vas a precargar? Comentá SÍ si ya reservaste tu copia.",
                                  "¿Lo vas a precargar? Etiquetá a ese amigo que no tiene espacio.",
                                  "¿Lo vas a precargar? Dejá tu like y compartí si te gustó la nota.",
                                  "¿Lo vas a precargar? Compartilo con tus amigos que esperan el juego."])
def test_cebo_de_interaccion_se_rechaza(cebo):
    assert any("cebo" in p for p in guion.revisar([*GUION_OK[:2], cebo]))


@pytest.mark.parametrize("relleno", ["Y la fecha que todos pedían, la revelo ahora sin vueltas.",
                                     "Ahora te cuento lo más importante de toda esta noticia.",
                                     "Quedate hasta el final porque lo mejor viene después."])
def test_escena_de_relleno_se_rechaza(relleno):
    assert any("relleno" in p for p in guion.revisar([*GUION_OK[:2], relleno, GUION_OK[2]]))


def test_la_promesa_de_la_escena_2_puede_anunciar():
    assert guion.revisar([GUION_OK[0], "Y al final te cuento lo que casi nadie vio del anuncio.", GUION_OK[2]]) == []


def test_pregunta_genuina_no_es_cebo():
    assert guion.revisar([*GUION_OK[:2], "Contanos en comentarios: ¿qué edición vas a comprar el diecinueve?"]) == []


def test_en_pantalla_el_juego_va_como_gta_vi():
    datos = {**PLAN_OK, "titular": "EL PRE-LOAD DE GTA SEIS",
             "escenas": [{**PLAN_OK["escenas"][0], "texto": "GTA 6 pide 212 GB"}, *PLAN_OK["escenas"][1:]]}
    v = validar_plan(datos, n_imagenes=2, n_escenas=3)
    assert v["titular"] == "LA PRECARGA DE GTA VI" and v["escenas"][0]["texto"] == "GTA VI pide 212 GB"
    g = guionista.validar({**GUION_BUENO, "titular": "TU CONSOLA SUDA POR GTA seis",
                           "ganchos_alternativos": [{"titular": "GTA 6 NO TRAE DISCO",
                                                     "narracion": "GTA seis no trae disco en la caja."}]}, 3)
    assert g.titular == "TU CONSOLA SUDA POR GTA VI" and g.ganchos[0].titular == "GTA VI NO TRAE DISCO"
    assert "GTA seis" in g.narraciones[0]  # la voz lo sigue diciendo como se pronuncia


def test_ganchos_alternativos_descarta_los_que_no_cumplen():
    validos = guion.ganchos_validos(GANCHOS)
    assert validos == [GANCHOS[0]]
    assert guion.ganchos_validos("no es lista") == []


def test_guionista_valida_la_forma():
    with pytest.raises(guionista.GuionInvalido, match="exactamente 3"):
        guionista.validar({**GUION_BUENO, "escenas": GUION_BUENO["escenas"][:2]}, 3)
    with pytest.raises(guionista.GuionInvalido, match="escena 2"):
        guionista.validar({**GUION_BUENO, "escenas": [GUION_BUENO["escenas"][0], {"n": 2, "texto": "x"},
                                                      GUION_BUENO["escenas"][2]]}, 3)


def test_planificar_pasa_por_el_guionista_y_corrige(tmp_path):
    flojo = guion_json(["Hola a todos, ¿cómo están?", *GUION_OK[1:]], ganchos=GANCHOS)
    falso = LlmFalso([PLAN_OK, MOVIMIENTO, MOVIMIENTO, MOVIMIENTO, flojo, GUION_BUENO, VERIFICADO])
    plan = planificar(falso, tmp_path, _fuente(tmp_path), date(2026, 11, 4), 3, 7, 720, 1280, segundos=15)
    assert "GANCHO" in falso.llamadas[4] and "unos 15 segundos" in falso.llamadas[4]  # el prompt del guionista
    assert "GANCHO" not in falso.llamadas[0]  # el plan con imagenes ya no carga las reglas de guion
    assert "RESPUESTA ANTERIOR" in falso.partes[5] and "arranca con" in falso.partes[5]  # dice que corregir
    assert plan.ganchos == (Gancho(**GANCHOS[0]),) and plan.titular == "212 GB Y SIN DISCO"
    assert [e.narracion for e in plan.escenas] == GUION_OK and plan.escenas[1].texto == "TEXTO 2"
    assert plan.texto_post.startswith("Post nuevo") and plan.advertencia == ""


def test_planificar_acepta_guion_flojo_al_final_con_advertencia(tmp_path):
    flojo = guion_json([*GUION_OK[:2], "Seguinos para más noticias de GTA seis, que se viene con todo."])
    falso = LlmFalso([PLAN_OK, MOVIMIENTO, MOVIMIENTO, MOVIMIENTO, flojo, flojo, flojo])
    plan = planificar(falso, tmp_path, _fuente(tmp_path), date(2026, 11, 4), 3, 7, 720, 1280)
    assert "Guion para revisar" in plan.advertencia and "pregunta" in plan.advertencia
    assert len(plan.escenas) == 3 and not falso.respuestas


def test_si_el_guionista_falla_queda_el_borrador(tmp_path):
    falso = LlmFalso([PLAN_OK, MOVIMIENTO, MOVIMIENTO, MOVIMIENTO, {"titular": ""}, LlmError("vacio"), {}])
    plan = planificar(falso, tmp_path, _fuente(tmp_path), date(2026, 11, 4), 3, 7, 720, 1280)
    assert plan.escenas[0].narracion == PLAN_OK["escenas"][0]["narracion"]
    assert "quedó el borrador" in plan.advertencia and "/reescribir" in plan.advertencia


def test_el_aviso_de_guion_no_se_acumula():
    primero = guionista.con_problemas("Nota vigente.", ("falta la pregunta",))
    assert primero == "Nota vigente. | Guion para revisar: falta la pregunta"
    assert guionista.con_problemas(primero, ()) == "Nota vigente."
    assert guionista.con_problemas(primero, ("otra cosa",)) == "Nota vigente. | Guion para revisar: otra cosa"
    assert guionista.con_problemas("Guion para revisar: x", ()) == ""


def test_reescribir_un_reel_hecho_reusa_los_clips(cfg):
    guion_nuevo = guion_json(GUION_OK, ganchos=GANCHOS)
    s = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), LlmFalso([guion_nuevo, VERIFICADO]), None)
    p = _proyecto(estado=Estado.ENTREGADO)
    p = replace(p, reel="reel_abc.mp4", plan=replace(p.plan, advertencia="Nota vigente. | Guion para revisar: viejo",
                                                     escenas=tuple(replace(e, audio=f"voz/{e.n}.wav")
                                                                   for e in p.plan.escenas)))
    s.almacen.guardar(p)
    nuevo = s.reescribir("abc")
    assert nuevo.estado == Estado.ESPERANDO_APROBACION and nuevo.reel == ""
    assert [e.narracion for e in nuevo.plan.escenas] == GUION_OK
    assert [e.clip for e in nuevo.plan.escenas] == [e.clip for e in p.plan.escenas]  # los clips quedan
    assert all(e.audio == "" for e in nuevo.plan.escenas)  # la voz se rehace
    assert nuevo.plan.advertencia == "Nota vigente."
    assert "walk" in s.llm.partes[0]  # sin sujeto guardado, lo que se ve sale del prompt de movimiento
    s.almacen.guardar(nuevo.con(estado=Estado.EN_COLA))
    with pytest.raises(EdicionInvalida):  # en cola o generando no se toca
        s.reescribir("abc")


def _con_ganchos(p):
    return replace(p, plan=replace(p.plan, ganchos=(Gancho("OTRO GANCHO", "¿Sabías que GTA seis pesa tanto?"),)))


def test_usar_gancho_intercambia_y_resetea_la_voz(cfg):
    s = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), None, None)
    p = _con_ganchos(_proyecto())
    p = replace(p, plan=replace(p.plan, escenas=(replace(p.plan.escenas[0], audio="voz_1.wav"), *p.plan.escenas[1:])))
    s.almacen.guardar(p)
    nuevo = s.usar_gancho("abc", 1)
    assert nuevo.plan.titular == "OTRO GANCHO" and nuevo.plan.escenas[0].narracion.startswith("¿Sabías")
    assert nuevo.plan.escenas[0].audio == ""  # la voz de la escena 1 se vuelve a generar
    assert nuevo.plan.ganchos == (Gancho("TITULAR", "n1"),)  # el anterior queda como alternativa
    assert s.usar_gancho("abc", 1).plan.titular == "TITULAR"  # y se puede volver
    with pytest.raises(EdicionInvalida, match="no hay gancho"):
        s.usar_gancho("abc", 2)


def test_ganchos_sobreviven_al_almacen(cfg):
    p = _con_ganchos(_proyecto())
    assert proyecto_desde_dict(proyecto_a_dict(p)) == p
    a = Almacen(cfg.datos / "db.sqlite")
    a.guardar(p)
    assert a.obtener("abc").plan.ganchos == p.plan.ganchos


def test_piso_monetizable_estira_sin_congelar():
    clips = [5.0625] * 12  # el reel de 60 s salio de 56,9 s
    sin_piso = montaje.calcular_tiempos(clips, [4.0] * 12)
    assert sin_piso.total < 60
    t = montaje.calcular_tiempos(clips, [4.0] * 12, minimo_s=62.0)
    assert 62.0 <= t.total < 62.2
    assert all(f <= montaje.MAX_LENTO for f in t.factores) and all(r == 0.0 for r in t.relleno)
    assert montaje.calcular_tiempos(clips, [4.0] * 12, minimo_s=30.0) == sin_piso  # ya pasa: no se toca


def test_piso_que_no_entra_estirando_lo_sostiene_la_ultima_escena():
    t = montaje.calcular_tiempos([5.0, 5.0], [0.0, 0.0], minimo_s=20.0)
    assert t.factores == (montaje.MAX_LENTO, montaje.MAX_LENTO) and t.relleno[0] == 0.0 and t.relleno[1] > 0
    assert t.total >= 20.0


def test_piso_solo_para_reels_monetizables(cfg):
    assert montaje.piso_s(75, 62.0) == 62.0 and montaje.piso_s(60, 62.0) == 62.0 and montaje.piso_s(30, 62.0) == 0


def test_bot_reescribe_en_segundo_plano(cfg, tmp_path):
    tg = TgFalso()
    servicio = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), LlmFalso([guion_json(GUION_OK)]), tg)
    acceso = Acceso(tmp_path / "s" / "u.json", tmp_path / "s" / "c")
    acceso.vincular(7, acceso.nuevo_codigo())
    bot = Bot(tg, servicio, Trabajador(servicio), acceso, tmp_path / "offset")
    servicio.almacen.guardar(_proyecto(chat=70))
    bot._manejar(_msg("/reescribir"))
    bot._ejecutor.shutdown(wait=True)
    textos = [m[2] for m in tg.enviados]
    assert any("Reescribo el guion de abc" in t for t in textos)
    assert any(GUION_OK[0] in t for t in textos) and any("rehace solo la voz" in t for t in textos)


def test_duracion_estimada_de_un_reel_de_75(cfg):
    s = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), None, None)
    n = s.n_escenas(75)
    escenas = tuple(Escena(n=i, imagen="e.png", origen="o.jpg", centro_x=0.5, texto="t",
                           narracion=" ".join(["palabra"] * 13), motion_prompt="m") for i in range(1, n + 1))
    p = replace(_proyecto(), segundos_objetivo=75, plan=replace(_proyecto().plan, escenas=escenas))
    assert n == 15 and 66 <= s.duracion_estimada(p) <= 80
    corto = replace(p, plan=replace(p.plan, escenas=escenas[:12]), con_voz=False)
    assert s.duracion_estimada(corto) >= 62.0  # aun sin voz, el piso lo lleva arriba del minuto


def test_turno_estimado_por_largo():
    assert minutos_de_turno(15, 2) == 62 and minutos_de_turno(3, 2) == 17 and minutos_de_turno(3, 0) > 0


def test_bot_muestra_ganchos_y_los_aplica(cfg, tmp_path):
    tg = TgFalso()
    servicio = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), LlmQuieto(), tg)
    acceso = Acceso(tmp_path / "s" / "u.json", tmp_path / "s" / "c")
    acceso.vincular(7, acceso.nuevo_codigo())
    bot = Bot(tg, servicio, Trabajador(servicio), acceso, tmp_path / "offset")
    p = replace(_con_ganchos(_proyecto(chat=70)), segundos_objetivo=75)
    servicio.almacen.guardar(p)
    texto = resumen_plan(p, 64.2)
    assert "Otros ganchos (/gancho N)" in texto and "1. OTRO GANCHO" in texto and "pasa del minuto ✅" in texto
    assert "no pasa del minuto" in resumen_plan(p, 58.0)
    bot._manejar(_msg("/gancho 1"))
    assert "🔥 OTRO GANCHO" in tg.enviados[-1][2]
    bot._manejar(_msg("/gancho x"))
    assert "uso: /gancho" in tg.enviados[-1][2]
