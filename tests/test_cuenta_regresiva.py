"""Revision de la primera corrida automatica: "SEIS DÍAS PARA GTA VI" con siete por delante y el
verificador contando desde la fecha de la nota en vez de desde hoy."""
from datetime import date

import pytest

from tests.test_almacen_servicio_acceso import _proyecto
from tests.test_guion_y_monetizable import GUION_OK
from tests.test_planificador_llm import LlmFalso, guion_json
from vstudio import fechas, guionista, verificador
from vstudio.modelos import Fuente

HOY = date(2026, 11, 12)  # faltan 7 dias para el 19


# ---------- 3. la cuenta regresiva ----------
@pytest.mark.parametrize("texto, dichas", [
    ("SEIS DÍAS PARA GTA VI", [6]),
    ("Faltan 7 días.", [7]),
    ("y quedan solo siete días para el lanzamiento", [7]),
    ("A seis días del estreno, Rockstar mostró otro detalle.", [6]),
    ("Falta un día para que salga GTA seis.", [1]),
    ("Faltan dos días para la entrevista completa del director.", []),  # no es la del lanzamiento
    ("Sale el diecinueve de noviembre para PS cinco.", []),
    ("En siete días sumó dos millones de reservas.", []),
])
def test_encuentra_la_cuenta_del_lanzamiento(texto, dichas):
    assert fechas.cuentas(texto) == dichas


def test_la_cuenta_se_mide_desde_hoy():
    assert fechas.dias_para_lanzamiento(HOY) == 7
    problemas = fechas.problemas_cuenta(["SEIS DÍAS PARA GTA VI", "Faltan siete días."], HOY)
    assert len(problemas) == 1 and "faltan 6 días y faltan 7" in problemas[0]
    assert fechas.problemas_cuenta(["SIETE DÍAS PARA GTA VI", "Faltan 7 días"], HOY) == []
    assert fechas.problemas_cuenta(["Falta un día"], date(2026, 11, 18)) == []
    assert "ya salió" in fechas.problemas_cuenta(["Faltan 3 días"], date(2026, 11, 20))[0]


def test_el_contexto_trae_la_cuenta_hecha():
    assert "faltan 7 días" in fechas.contexto(HOY) and "no desde la fecha de la nota" in fechas.contexto(HOY)
    assert "sale mañana" in fechas.contexto(date(2026, 11, 18))
    assert "sale hoy" in fechas.contexto(date(2026, 11, 19))
    assert "ya salió" in fechas.contexto(date(2026, 11, 25))


def test_el_guionista_corrige_una_cuenta_regresiva_mal():
    mal = {**guion_json(GUION_OK), "titular": "SEIS DÍAS PARA GTA VI"}
    bien = {**guion_json(GUION_OK), "titular": "SIETE DÍAS PARA GTA VI"}
    falso = LlmFalso([mal, bien])
    g = guionista.escribir(falso, Fuente(texto="nota"), ["un auto", "una playa", "un helicóptero"], HOY, 15)
    assert "faltan 7 días para el lanzamiento" in falso.llamadas[0]  # la cuenta va hecha en el prompt
    assert "faltan 6 días y faltan 7" in falso.partes[1]  # y si la erra, se le pide corregirla
    assert g.titular == "SIETE DÍAS PARA GTA VI" and not g.problemas


def test_una_cuenta_mal_pesa_como_problema_grave():
    assert guionista.gravedad(("cuenta regresiva mal: dice que faltan 6 días y faltan 7",)) == guionista.PESO_GRAVE


def test_la_cuenta_mal_en_un_gancho_alternativo_tambien_se_marca():
    ganchos = [{"titular": "SEIS DÍAS Y LLEGA", "narracion": "Faltan seis días para GTA seis y esto cambia todo."}]
    falso = LlmFalso([guion_json(GUION_OK, ganchos=ganchos)] * 3)
    g = guionista.escribir(falso, Fuente(texto="nota"), ["un auto", "una playa", "un helicóptero"], HOY, 15)
    assert any("cuenta regresiva" in p for p in g.problemas)


# ---------- 4. el verificador cuenta desde hoy ----------
def test_el_verificador_recibe_hoy_y_la_fecha_de_la_nota_por_separado():
    p = _proyecto()
    fuente = Fuente(titulo="Votación sindical", fecha="2026-11-11", texto="votarán entre el 12 y el 26 de noviembre")
    falso = LlmFalso([{"sin_respaldo": []}])
    assert verificador.verificar(falso, fuente, p.plan, HOY) == []
    material = falso.partes[0]
    assert "HOY: Hoy es 2026-11-12: faltan 7 días" in material and "FECHA DE LA NOTA: 2026-11-11" in material
    assert "desde HOY" in falso.llamadas[0]
