"""Musica de fondo propia con ACE-Step."""
import json
import subprocess
from dataclasses import replace

from vstudio import biblioteca, musica


def test_faltantes_de_a_uno_por_estilo(tmp_path):
    (tmp_path / "synthwave").mkdir()
    (tmp_path / "synthwave" / "synthwave_01.wav").write_bytes(b"x")
    pedidos = musica.faltantes(tmp_path, por_estilo=2, maximo=6, segundos=95)
    estilos = [p.destino.parent.name for p in pedidos]
    assert len(pedidos) == 6 and "synthwave" not in estilos[:5]  # primero completa una ronda de todos los demas
    assert pedidos[0].destino.name.endswith("_01.wav") and pedidos[0].segundos == 95
    todos = musica.faltantes(tmp_path, por_estilo=2, maximo=100, segundos=95)
    assert len(todos) == 2 * len(musica.ESTILOS) - 1
    assert any(p.destino == tmp_path / "synthwave" / "synthwave_02.wav" for p in todos)


def test_el_clima_de_la_nota_elige_el_estilo():
    todos = set(musica.ESTILOS)
    assert musica.estilo_para("Filtran el mapa completo de GTA VI", todos) == "suspenso"
    assert musica.estilo_para("Rockstar confirmó la fecha: nuevo tráiler", todos) == "epico"
    assert musica.estilo_para("La policía y un robo en Vice City", todos) == "trap"
    neutros = {musica.estilo_para("Jason maneja por la playa", todos, turno=n) for n in range(4)}
    assert neutros == set(musica.POR_DEFECTO)  # sin clima: rota
    assert musica.estilo_para("filtración", {"chill"}) == "chill"  # si falta el estilo, el que haya


def test_elegir_rota_dentro_del_estilo(tmp_path):
    for n in (1, 2):
        (tmp_path / "suspenso").mkdir(exist_ok=True)
        (tmp_path / "suspenso" / f"suspenso_0{n}.wav").write_bytes(b"x")
    usos = biblioteca.Usos(tmp_path / "usos.json")
    primero = musica.elegir(tmp_path, "una filtración", usos)
    segundo = musica.elegir(tmp_path, "otra filtración", usos)
    assert {primero.name, segundo.name} == {"suspenso_01.wav", "suspenso_02.wav"}
    assert musica.elegir(tmp_path / "vacia", "x", usos) is None


def test_generar_lee_el_resultado_del_script(tmp_path, monkeypatch, cfg):
    m = replace(cfg.musica, acestep_dir=tmp_path, python=tmp_path / "py", script=tmp_path / "s.py")
    for f in (m.python, m.script):
        f.write_text("")
    pedidos = musica.faltantes(tmp_path / "musica", 1, 2, 95)

    def correr(cmd, **k):
        entrada = json.loads((tmp_path / "musica" / "pedidos.json").read_text(encoding="utf-8"))
        assert [p["destino"] for p in entrada] == [str(p.destino) for p in pedidos] and "--dispositivo" in cmd
        salida = [{"destino": entrada[0]["destino"]}, {"destino": entrada[1]["destino"], "error": "sin memoria"}]
        return subprocess.CompletedProcess(cmd, 0, "log\nRESULTADO_JSON " + json.dumps(salida), "")
    monkeypatch.setattr(musica.subprocess, "run", correr)
    assert musica.generar(m, pedidos) == [pedidos[0].destino]


def test_el_turno_completa_la_musica_y_no_se_cae_si_falla(cfg, monkeypatch):
    from tests.test_bot_trabajador import LlmQuieto, TgFalso
    from vstudio import trabajador as trab_mod
    from vstudio.almacen import Almacen
    from vstudio.servicio import Servicio
    cfg = replace(cfg, musica=replace(cfg.musica, activo=True),
                  montaje=replace(cfg.montaje, musica_dir=cfg.datos / "musica"))
    servicio = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), LlmQuieto(), TgFalso())
    t = trab_mod.Trabajador(servicio)
    pedidos = []
    monkeypatch.setattr(trab_mod.musica, "generar", lambda m, ps: pedidos.append(len(ps)) or (_ for _ in ()).throw(
        RuntimeError("ACE-Step no arranco")))
    t._completar_musica()
    assert pedidos == [cfg.musica.por_turno]  # pidio los de este turno; el error no salio del turno
