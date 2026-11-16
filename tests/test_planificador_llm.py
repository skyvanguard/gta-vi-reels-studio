from datetime import date

import pytest

from tests.conftest import imagen_bytes
from vstudio import llm
from vstudio.modelos import Fuente
from vstudio.planificador import PlanInvalido, SinImagenes, planificar, validar_plan

PLAN_OK = {
    "titular": "FALTAN 15 DÍAS PARA GTA VI",
    "escenas": [
        {"imagen": 0, "centro_x": 0.2, "sujeto": "café", "texto": "Sale el 19 de noviembre",
         "narracion": "GTA seis sale el diecinueve de noviembre"},
        {"imagen": 1, "centro_x": 1.4, "texto": "Jason y Lucía", "narracion": "Dos protagonistas"},
        {"imagen": 0, "centro_x": 0.8, "texto": "Vuelve Vice City", "narracion": ""},
    ],
    "texto_post": "¡Se viene! #GTA6", "advertencia": "",
}


def test_extraer_json_tolera_ruido():
    assert llm.extraer_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extraer_json('Claro: {"a": {"b": 2}} listo') == {"a": {"b": 2}}
    with pytest.raises(llm.LlmError):
        llm.extraer_json("sin json")


def test_validar_plan_rechaza_imagenes_muy_repetidas():
    escena = {"imagen": 0, "centro_x": 0.5, "texto": "a"}
    with pytest.raises(PlanInvalido, match="repetidas"):
        validar_plan({**PLAN_OK, "escenas": [escena] * 3}, n_imagenes=3, n_escenas=3)
    # con una sola imagen no hay otra opcion: se permite repetirla
    assert len(validar_plan({**PLAN_OK, "escenas": [escena] * 3}, n_imagenes=1, n_escenas=3)["escenas"]) == 3


def test_no_repite_imagenes_si_alcanzan():
    """No repetir la misma foto en un reel cuando hay imagenes de sobra."""
    escenas = [{"imagen": i, "centro_x": 0.5, "texto": f"t{i}"} for i in (0, 1, 0)]
    with pytest.raises(PlanInvalido, match="repetidas"):
        validar_plan({**PLAN_OK, "escenas": escenas}, n_imagenes=5, n_escenas=3)
    flojo = validar_plan({**PLAN_OK, "escenas": escenas}, n_imagenes=5, n_escenas=3, estricto=False)
    assert flojo["advertencia"].startswith("Hay imágenes repetidas (imagen 0 2 veces)")  # el ultimo intento avisa


def test_validar_plan_pide_las_escenas_que_se_pidieron():
    escenas = [{"imagen": i, "centro_x": 0.1 * i, "texto": f"t{i}"} for i in range(4)]
    with pytest.raises(PlanInvalido, match="entre 5 y 7"):
        validar_plan({**PLAN_OK, "escenas": escenas}, n_imagenes=4, n_escenas=6)
    assert len(validar_plan({**PLAN_OK, "escenas": escenas[:3]}, n_imagenes=4, n_escenas=3)["escenas"]) == 3


def test_validar_plan_normaliza():
    v = validar_plan(PLAN_OK, n_imagenes=2, n_escenas=3)
    assert v["escenas"][1]["centro_x"] == 1.0  # clamp
    assert v["escenas"][2]["narracion"] == "Vuelve Vice City"  # sin narracion usa el texto


@pytest.mark.parametrize("cambio", [
    {"titular": ""}, {"titular": "X" * 80}, {"escenas": []},
    {"escenas": [{"imagen": 5, "centro_x": 0.5, "texto": "a"}] * 3},
    {"escenas": [{"imagen": 0, "centro_x": "x", "texto": "a"}] * 3},
    {"escenas": [{"imagen": 0, "centro_x": 0.5, "texto": ""}] * 3},
])
def test_validar_plan_rechaza(cambio):
    with pytest.raises(PlanInvalido):
        validar_plan({**PLAN_OK, **cambio}, n_imagenes=2, n_escenas=3)


class LlmFalso:
    def __init__(self, respuestas):
        self.respuestas, self.llamadas, self.partes = list(respuestas), [], []

    def chat_json(self, sistema, partes, **kw):
        self.llamadas.append(sistema)
        self.partes.append(" ".join(p.get("text", "") for p in partes))
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta

    def disponible(self):
        return True

    def metricas(self):
        return {}


def guion_json(narraciones, textos=None, ganchos=()):
    """Respuesta del guionista para esas narraciones."""
    textos = textos or [f"TEXTO {i}" for i in range(1, len(narraciones) + 1)]
    return {"titular": "212 GB Y SIN DISCO",
            "escenas": [{"n": i, "texto": t, "narracion": n}
                        for i, (t, n) in enumerate(zip(textos, narraciones), start=1)],
            "ganchos_alternativos": list(ganchos), "texto_post": "Post nuevo. ¿Lo precargás? #GTA6"}


def _fuente(tmp_path) -> Fuente:
    for i in range(2):
        (tmp_path / f"nota_{i + 1}.jpg").write_bytes(imagen_bytes(2700, 1800, color=(30 + i * 100, 90, 90)))
    return Fuente(url="https://n.com/a", titulo="Nota", fecha="2026-05-19", texto="GTA VI sale el 19/11.",
                  imagenes=("nota_1.jpg", "nota_2.jpg"))


def test_planificar_reintenta_y_arma_escenas(tmp_path):
    movimiento = {"motion_prompt_en": "people walk", "negative_extra_en": "warping"}
    narraciones = ["Doscientos doce gigas: eso es lo que te va a pedir GTA seis.",
                   "Y lo más fuerte no es el tamaño, es la fecha en que arranca la precarga.",
                   "¿Tú lo vas a precargar el primer día o esperas las reseñas?"]
    falso = LlmFalso([{"titular": ""}, PLAN_OK, movimiento, movimiento, movimiento, guion_json(narraciones)])
    plan = planificar(falso, tmp_path, _fuente(tmp_path), date(2026, 11, 4), 3, 7, 720, 1280)
    assert len(plan.escenas) == 3 and plan.escenas[1].origen == "nota_2.jpg"
    assert [e.narracion for e in plan.escenas] == narraciones and plan.escenas[0].sujeto == "café"
    assert "café" in falso.partes[-1]  # el guionista sabe que se ve en cada escena
    assert all((tmp_path / e.imagen).exists() for e in plan.escenas)
    assert "invalida" in falso.llamadas[1]  # el reintento lleva el motivo
    assert "hace 169 dias" in falso.llamadas[0]  # avisa que la nota es vieja


def test_planificar_sin_imagenes(tmp_path):
    with pytest.raises(SinImagenes):
        planificar(LlmFalso([]), tmp_path, Fuente(texto="algo"), date(2026, 11, 4), 3, 7, 720, 1280)
