from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_almacen_servicio_acceso import _proyecto
from tests.test_bot_trabajador import LlmQuieto, TgFalso
from vstudio import pronunciacion, trabajador as trab_mod
from vstudio.almacen import Almacen
from vstudio.modelos import reemplazar_escena
from vstudio.servicio import Servicio
from vstudio.voz import Audio


@pytest.mark.parametrize("guion, oido, esperadas", [
    # lo que oyo Whisper (sin pista) en los reels
    ("Porque se movió dos veces. Otoño de dos mil veinticinco, mayo de dos mil veintiséis.",
     "Porque se moví dos veces. Otono de 2025, mayo de 2026.", ["movió", "otoño"]),
    ("Todo ese mundo que manejás, en la caja es un código y un póster.",
     "Todo ese mundo que manejas en la caja es un coligo y un póster.", ["manejás", "código"]),
    ("Ahora Zelnick lo plantó frente a los inversores: la fecha no se toca.",
     "Ahora Zelnik lo plantó frente a los inversores. La fecha no se toca.", []),
    # las cifras y las tildes que Whisper pone a su manera no cuentan
    ("¿Qué pasa si sí sale el veintiséis? Más días de espera.", "Que pasa si si sale el 26? Mas dias de espera.", []),
])
def test_palabras_dudosas(guion, oido, esperadas):
    assert pronunciacion.dudosas(guion, oido) == esperadas


class Voz:
    """sintetizar falso: cada toma escribe un archivo distinto."""

    def __init__(self):
        self.pedidos = []

    def __call__(self, cfg, textos, carpeta, voz=None, semilla=0):
        self.pedidos.append((list(textos), semilla))
        carpeta.mkdir(parents=True, exist_ok=True)
        audios = []
        for i, _ in enumerate(textos, start=1):
            ruta = carpeta / f"{i}.wav"
            ruta.write_bytes(b"x")
            audios.append(Audio(archivo=ruta, segundos=1.0))
        return audios


def _trabajador(cfg):
    tg = TgFalso()
    cfg = replace(cfg, voz=replace(cfg.voz, motor="chatterbox", reintentos_pronunciacion=2))
    s = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), LlmQuieto(), tg)
    p = _proyecto(chat=70)
    plan = p.plan
    for e, n in zip(plan.escenas, ["El otoño llegó.", "Sin tildes acá no hay nada", "Un código raro."]):
        plan = reemplazar_escena(plan, replace(e, narracion=n))
    s.almacen.guardar(replace(p, plan=plan))
    return trab_mod.Trabajador(s), tg


def test_rehace_solo_las_escenas_mal_dichas_y_se_queda_con_la_mejor(cfg, monkeypatch):
    t, tg = _trabajador(cfg)
    voz_falsa = Voz()
    monkeypatch.setattr(trab_mod.voz, "sintetizar", voz_falsa)
    oidos = iter([
        ["El otono llegó.", "Sin tildes acá no hay nada", "Un coligo raro."],  # primera toma: 1 y 3 mal
        ["El otoño llegó.", "Un coligo raro."],  # segunda toma (solo 1 y 3): la 1 queda bien
        ["Un codigo raro."],  # tercera toma (solo la 3): sigue mal
    ])
    monkeypatch.setattr(trab_mod.subtitulos, "oir", lambda cfg, audios, carpeta: next(oidos))
    p = t.s.almacen.obtener("abc")
    p = t._preparar_voz(p)
    assert voz_falsa.pedidos[1] == (["El otoño llegó.", "Un código raro."], 1000)
    assert voz_falsa.pedidos[2] == (["Un código raro."], 2000)
    rutas = [e.audio for e in p.plan.escenas]
    assert rutas[0] == "voz/toma_2/1.wav" and rutas[1] == "voz/2.wav"  # la 1 de la segunda toma, la 2 la original
    assert rutas[2] == "voz/3.wav"  # la 3 nunca salio bien: queda la primera (empate, no empeora)
    assert "escena 3: código" in tg.enviados[-1][2]  # se avisa lo que quedo dudoso


def test_sin_whisper_queda_la_voz_original(cfg, monkeypatch):
    t, _ = _trabajador(cfg)
    monkeypatch.setattr(trab_mod.voz, "sintetizar", Voz())

    def falla(*a):
        raise trab_mod.subtitulos.SubtitulosError("sin venv")
    monkeypatch.setattr(trab_mod.subtitulos, "oir", falla)
    p = t._preparar_voz(t.s.almacen.obtener("abc"))
    assert [e.audio for e in p.plan.escenas] == ["voz/1.wav", "voz/2.wav", "voz/3.wav"]
