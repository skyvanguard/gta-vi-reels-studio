import os
import sys
import time

import pytest

from vstudio.acceso import Acceso
from vstudio.almacen import Almacen
from vstudio.modelos import Escena, Estado, Fuente, Plan, Proyecto
from vstudio.servicio import EdicionInvalida, Servicio


def _proyecto(estado=Estado.ESPERANDO_APROBACION, chat=1, id_="abc") -> Proyecto:
    escenas = tuple(Escena(n=i, imagen=f"escena_{i}.png", origen="nota_1.jpg", centro_x=0.5, texto=f"t{i}",
                           narracion=f"n{i}", motion_prompt="walk", clip=f"clip_{i}.mp4") for i in (1, 2, 3))
    return Proyecto(id=id_, chat_id=chat, estado=estado, fuente=Fuente(url="https://n.com", imagenes=("nota_1.jpg",)),
                    plan=Plan(titular="TITULAR", escenas=escenas, texto_post="post"))


def test_almacen_roundtrip_y_consultas(tmp_path):
    a = Almacen(tmp_path / "db.sqlite")
    p = a.guardar(_proyecto())
    a.guardar(_proyecto(estado=Estado.EN_COLA, id_="def"))
    assert a.obtener("abc") == p
    assert [x.id for x in a.listar((Estado.EN_COLA,))] == ["def"]
    assert a.ultimo_esperando(1).id == "abc" and a.ultimo_esperando(2) is None
    nuevo = a.actualizar("abc", lambda actual: actual.con(estado=Estado.CANCELADO))
    assert nuevo.estado == Estado.CANCELADO and a.obtener("abc").estado == Estado.CANCELADO
    with pytest.raises(KeyError):
        a.actualizar("zzz", lambda x: x)


@pytest.fixture
def servicio(cfg):
    return Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), llm=None, notificador=None)


def test_editar_no_muta_y_valida_estado(servicio):
    original = servicio.almacen.guardar(_proyecto())
    editado = servicio.editar("abc", titular="NUEVO", escena=2, texto="pantalla")
    assert original.plan.titular == "TITULAR"  # inmutable
    assert editado.plan.titular == "NUEVO"
    assert editado.plan.escenas[1].texto == "pantalla" and editado.plan.escenas[1].narracion == "n2"  # voz aparte
    with pytest.raises(EdicionInvalida):
        servicio.editar("abc", escena=9, texto="x")
    servicio.aprobar("abc")
    with pytest.raises(EdicionInvalida):
        servicio.editar("abc", titular="tarde")


def test_regenerar_escena_borra_clip_y_cambia_seed(servicio):
    servicio.almacen.guardar(_proyecto(estado=Estado.ENTREGADO))
    p = servicio.regenerar_escena("abc", 2)
    assert p.estado == Estado.EN_COLA and p.plan.escenas[1].clip == "" and p.plan.escenas[0].clip == "clip_1.mp4"
    with pytest.raises(EdicionInvalida):
        servicio.regenerar_escena("abc", 2)  # ya esta en cola


def test_acceso_codigo_de_un_solo_uso(tmp_path):
    acceso = Acceso(tmp_path / "s" / "usuarios.json", tmp_path / "s" / "codigo")
    assert acceso.asegurar_codigo_si_no_hay_usuarios()
    codigo = (tmp_path / "s" / "codigo").read_text()
    assert not acceso.vincular(42, "incorrecto")
    assert not acceso.vincular(42, "código-ñ")  # no ASCII: no explota
    assert acceso.vincular(42, codigo) and acceso.autorizado(42)
    assert not acceso.vincular(43, codigo)  # ya se uso
    assert not acceso.asegurar_codigo_si_no_hay_usuarios()  # ya hay dueno
    if sys.platform != "win32":
        assert oct(os.stat(tmp_path / "s" / "usuarios.json").st_mode & 0o777) == "0o600"


def test_codigo_vencido_no_sirve(tmp_path):
    acceso = Acceso(tmp_path / "u.json", tmp_path / "codigo")
    codigo = acceso.nuevo_codigo()
    viejo = time.time() - 25 * 3600
    os.utime(tmp_path / "codigo", (viejo, viejo))
    assert not acceso.vincular(1, codigo) and not (tmp_path / "codigo").exists()


def test_cancelar_gana_aunque_se_este_planificando(servicio):
    servicio.almacen.guardar(_proyecto(estado=Estado.PLANIFICANDO))
    servicio.cancelar("abc")
    with pytest.raises(EdicionInvalida):  # el plan que termina despues no pisa la cancelacion
        servicio.transicion("abc", (Estado.PLANIFICANDO,), lambda a: a.con(estado=Estado.ESPERANDO_APROBACION))
    assert servicio.almacen.obtener("abc").estado == Estado.CANCELADO


def test_trabados_se_cancelan_solo_si_no_se_esta_produciendo(servicio):
    servicio.almacen.guardar(_proyecto(estado=Estado.GENERANDO))
    servicio.produciendo.set()
    with pytest.raises(EdicionInvalida):
        servicio.cancelar("abc")
    servicio.produciendo.clear()
    assert servicio.cancelar("abc").estado == Estado.CANCELADO
