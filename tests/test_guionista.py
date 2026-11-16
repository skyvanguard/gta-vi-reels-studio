"""Casos del guionista y de /reescribir que salieron de la revision."""
from dataclasses import replace
from datetime import date

import pytest

from tests.test_almacen_servicio_acceso import _proyecto
from tests.test_guion_y_monetizable import GUION_OK
from tests.test_planificador_llm import LlmFalso, guion_json
from vstudio import guion, guionista
from vstudio.almacen import Almacen
from vstudio.llm import LlmError
from vstudio.modelos import Estado, Fuente, reemplazar_escena
from vstudio.servicio import EdicionInvalida, Servicio

VISTAS = ["un auto", "una playa", "un helicóptero"]
SIN_PREGUNTA = [*GUION_OK[:2], "Y el diecinueve de noviembre sale para PlayStation cinco y Xbox Series."]
CON_MULETILLAS = [GUION_OK[0], "Ojo, lo más fuerte no es el tamaño, ojo, es la fecha en que arranca todo.",
                  GUION_OK[2]]


def _escribir(respuestas, **kw):
    falso = LlmFalso(respuestas)
    g = guionista.escribir(falso, Fuente(texto="nota"), VISTAS, date(2026, 11, 5), 15, **kw)
    return g, falso


def test_elige_el_intento_menos_grave_no_el_de_menos_problemas():
    assert len(guion.revisar(CON_MULETILLAS)) == 1 and len(guion.revisar(SIN_PREGUNTA)) == 1
    g, _ = _escribir([guion_json(CON_MULETILLAS), guion_json(SIN_PREGUNTA), guion_json(SIN_PREGUNTA)])
    assert list(g.narraciones) == CON_MULETILLAS  # una muletilla es menos grave que un cierre sin pregunta
    assert guionista.gravedad(g.problemas) < guionista.gravedad(tuple(guion.revisar(SIN_PREGUNTA)))


def test_tras_una_respuesta_invalida_sigue_pidiendo_la_correccion():
    g, falso = _escribir([guion_json(SIN_PREGUNTA), {"titular": ""}, guion_json(GUION_OK)])
    assert "inválida" in falso.partes[2] and "RESPUESTA ANTERIOR" in falso.partes[2] and "pregunta" in falso.partes[2]
    assert list(g.narraciones) == GUION_OK and g.problemas == ()


def test_si_nunca_hay_forma_valida_sube_el_ultimo_error():
    with pytest.raises(guionista.GuionInvalido, match="sin conexión"):
        _escribir([{"titular": ""}, {}, LlmError("sin conexión")])


def test_el_aviso_de_guion_sobrevive_al_recorte():
    largo = "x" * 600
    aviso = guionista.con_problemas(largo, ("falta la pregunta",))
    assert len(aviso) <= guion.MAX_ADVERTENCIA and aviso.endswith("Guion para revisar: falta la pregunta")


def test_el_post_lleva_hashtags_sin_espacios():
    from tests.test_guion_y_monetizable import GUION_BUENO
    g = guionista.validar({**GUION_BUENO, "texto_post": "Sale el 19. #GTA VI #GTA 6 #GTAVI #Leonida"}, 3)
    assert g.texto_post == "Sale el 19. #GTAVI #GTA6 #GTAVI #Leonida"


def test_el_nombre_del_juego_no_cuenta_como_repeticion():
    narraciones = ["La caja de GTA seis no trae disco.", "El mapa de GTA seis viene de regalo para imprimir.",
                   "La precarga de GTA seis arranca el diez.", "¿Te alcanza el espacio para GTA seis?"]
    assert not any("repetís" in p for p in guion.revisar(narraciones))


def _servicio_con(cfg, llm):
    s = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), llm, None)
    p = _proyecto(estado=Estado.ENTREGADO)
    s.almacen.guardar(replace(p, reel="reel_abc.mp4"))
    return s


def test_reescribir_que_falla_no_toca_el_reel_entregado(cfg):
    s = _servicio_con(cfg, LlmFalso([LlmError("caído")] * 3))
    with pytest.raises(guionista.GuionInvalido):
        s.reescribir("abc")
    p = s.almacen.obtener("abc")
    assert p.estado == Estado.ENTREGADO and p.reel == "reel_abc.mp4"


class LlmQueEditaMientrasEscribe(LlmFalso):
    """Mientras el LLM 'escribe', el editor regenera el clip 2 (se guarda otro clip)."""

    def __init__(self, respuestas, almacen):
        super().__init__(respuestas)
        self.almacen = almacen

    def chat_json(self, sistema, partes, **kw):
        self.almacen.actualizar("abc", lambda a: a.con(plan=reemplazar_escena(
            a.plan, replace(a.plan.escenas[1], clip="clip_2_nuevo.mp4"))))
        return super().chat_json(sistema, partes, **kw)


def test_reescribir_aplica_sobre_el_plan_de_ahora(cfg):
    s = _servicio_con(cfg, None)
    s.llm = LlmQueEditaMientrasEscribe([guion_json(GUION_OK)], s.almacen)
    nuevo = s.reescribir("abc")
    assert nuevo.plan.escenas[1].clip == "clip_2_nuevo.mp4"  # lo que cambio mientras tanto no se pierde
    assert [e.narracion for e in nuevo.plan.escenas] == GUION_OK


def test_reescribir_aborta_si_cambiaron_las_escenas(cfg):
    s = _servicio_con(cfg, None)

    class Replanifica(LlmFalso):
        def chat_json(self, sistema, partes, **kw):
            s.almacen.actualizar("abc", lambda a: a.con(plan=reemplazar_escena(
                a.plan, replace(a.plan.escenas[0], imagen="otra.png"))))
            return super().chat_json(sistema, partes, **kw)

    s.llm = Replanifica([guion_json(GUION_OK)])
    with pytest.raises(EdicionInvalida, match="cambió mientras"):
        s.reescribir("abc")
