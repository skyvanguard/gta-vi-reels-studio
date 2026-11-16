import json
import subprocess
from pathlib import Path

import pytest

from vstudio import comfy
from vstudio.config import ConfigGpu
from vstudio.gpu import NOMBRE_MARCA, MonitorActividad, TurnoError, TurnoGpu, restaurar_si_hace_falta

PLANTILLA = json.loads((Path(__file__).resolve().parents[1] / "workflows/wan/wan22_i2v_14b.api.json").read_text())


def _pedido(con_lora=True):
    return comfy.PedidoClip(imagen=Path("x.png"), prompt="walk", negativo_extra="blur", ancho=720, alto=1280,
                            cuadros=81, seed=7, prefijo="p1_e1", con_lora=con_lora)


def test_workflow_con_lora_igual_a_plantilla_oficial():
    wf = comfy.armar_workflow(PLANTILLA, "in.png", _pedido())
    assert wf["86"]["inputs"] | {} == {**PLANTILLA["86"]["inputs"], "noise_seed": 7}
    assert (wf["85"]["inputs"]["start_at_step"], wf["85"]["inputs"]["end_at_step"]) == (2, 4)
    assert wf["98"]["inputs"]["length"] == 81 and wf["97"]["inputs"]["image"] == "in.png"
    assert wf["89"]["inputs"]["text"].endswith(", blur")
    assert wf["108"]["inputs"]["filename_prefix"].startswith("video/p1_e1_")
    assert "_origen" not in wf and PLANTILLA["93"]["inputs"]["text"] == "__PROMPT__"  # no muta la plantilla


def test_workflow_sin_lora_reconecta_modelos():
    wf = comfy.armar_workflow(PLANTILLA, "in.png", _pedido(con_lora=False))
    assert "101" not in wf and "102" not in wf
    assert wf["104"]["inputs"]["model"] == ["95", 0] and wf["86"]["inputs"]["steps"] == 20


def test_archivos_de_salida():
    entrada = {"outputs": {"108": {"images": [{"filename": "a.mp4", "subfolder": "video", "type": "output"}],
                                   "animated": [True]}}}
    assert comfy.archivos_de_salida(entrada) == [{"filename": "a.mp4", "subfolder": "video", "type": "output"}]


class Reloj:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_monitor_cuenta_inactividad_y_se_resetea(tmp_path):
    reloj, metricas = Reloj(), {"prompt_tokens_total": 10, "tokens_predicted_total": 5, "requests_processing": 0}
    mon = MonitorActividad(lambda: dict(metricas), tmp_path, reloj)
    mon.muestrear()
    assert mon.minutos_inactivo() == 0  # arranca asumiendo actividad
    reloj.t += 16 * 60
    mon.muestrear()
    assert mon.minutos_inactivo() >= 15
    metricas["tokens_predicted_total"] = 99
    mon.muestrear()
    assert mon.minutos_inactivo() == 0
    reloj.t += 20 * 60
    metricas["requests_processing"] = 1
    mon.muestrear()
    assert mon.minutos_inactivo() == 0


def test_monitor_sin_metricas_nunca_esta_inactivo(tmp_path):
    reloj = Reloj()

    def falla():
        raise ConnectionError("llm caido")
    mon = MonitorActividad(falla, tmp_path, reloj)
    reloj.t += 3600
    mon.muestrear()
    assert mon.minutos_inactivo() == 0


def test_monitor_ve_actividad_en_sesiones_de_otra_aplicacion(tmp_path):
    import os
    reloj = Reloj()
    sesion = tmp_path / "proyecto" / "session-1" / "session.jsonl.zstd"
    sesion.parent.mkdir(parents=True)
    sesion.write_bytes(b"x")
    mon = MonitorActividad(lambda: {"prompt_tokens_total": 1}, tmp_path, reloj)
    reloj.t += 30 * 60
    os.utime(sesion, (reloj.t - 60, reloj.t - 60))  # la otra aplicacion escribio hace 1 minuto
    mon.muestrear()
    assert mon.minutos_inactivo() < 2


class Ejecutor:
    """Simula los scripts: `codigos` mapea "qwen.sh start" -> codigo de salida o excepcion a lanzar."""

    def __init__(self, codigos=None):
        self.llamadas, self.codigos = [], dict(codigos or {})

    def __call__(self, args, **kw):
        clave = " ".join([Path(args[0]).name, *args[1:]])
        self.llamadas.append(clave)
        resultado = self.codigos.get(clave, 0)
        if isinstance(resultado, Exception):
            raise resultado
        if resultado and kw.get("check"):
            raise subprocess.CalledProcessError(resultado, args)
        return subprocess.CompletedProcess(args, resultado, "", "error simulado" if resultado else "")


def _cfg():
    return ConfigGpu(script_qwen=Path("/x/qwen.sh"), script_comfy=Path("/x/comfy.sh"))


def test_turno_devuelve_el_qwen_aunque_falle_la_generacion(tmp_path):
    ej = Ejecutor()
    with pytest.raises(RuntimeError):
        with TurnoGpu(_cfg(), tmp_path, ej, esperar=lambda s: None):
            marca = json.loads((tmp_path / NOMBRE_MARCA).read_text())
            assert marca["qwen_arriba"] is True
            raise RuntimeError("fallo a mitad")
    assert ej.llamadas == ["qwen.sh status", "qwen.sh stop", "comfy.sh start all", "comfy.sh stop all",
                           "qwen.sh start"]
    assert not (tmp_path / NOMBRE_MARCA).exists()


def test_si_el_qwen_estaba_apagado_no_se_toca(tmp_path):
    ej = Ejecutor({"qwen.sh status": 3})
    with TurnoGpu(_cfg(), tmp_path, ej, esperar=lambda s: None):
        pass
    assert ej.llamadas == ["qwen.sh status", "comfy.sh start all", "comfy.sh stop all"]


def test_turno_que_no_puede_abrir_devuelve_el_qwen(tmp_path):
    ej = Ejecutor({"comfy.sh start all": 1})
    with pytest.raises(subprocess.CalledProcessError):
        with TurnoGpu(_cfg(), tmp_path, ej, esperar=lambda s: None):
            pass
    assert ej.llamadas[-1] == "qwen.sh start" and not (tmp_path / NOMBRE_MARCA).exists()


def test_cerrar_sigue_aunque_un_paso_explote(tmp_path):
    ej = Ejecutor({"comfy.sh stop all": subprocess.TimeoutExpired("comfy.sh", 900)})
    with TurnoGpu(_cfg(), tmp_path, ej, esperar=lambda s: None):
        pass
    assert ej.llamadas[-1] == "qwen.sh start" and not (tmp_path / NOMBRE_MARCA).exists()


def test_si_el_qwen_no_vuelve_queda_la_marca(tmp_path):
    ej = Ejecutor({"qwen.sh start": 1})
    with pytest.raises(TurnoError):
        with TurnoGpu(_cfg(), tmp_path, ej, esperar=lambda s: None):
            pass
    assert ej.llamadas.count("qwen.sh start") == 3 and (tmp_path / NOMBRE_MARCA).exists()


def test_restaurar_al_arrancar(tmp_path):
    (tmp_path / NOMBRE_MARCA).write_text("{}")
    ej = Ejecutor()
    assert restaurar_si_hace_falta(_cfg(), tmp_path, ej) is True
    assert ej.llamadas == ["comfy.sh stop all", "qwen.sh start"]
    assert restaurar_si_hace_falta(_cfg(), tmp_path, Ejecutor()) is False


def test_restaurar_respeta_qwen_apagado(tmp_path):
    (tmp_path / NOMBRE_MARCA).write_text(json.dumps({"qwen_arriba": False}))
    ej = Ejecutor()
    assert restaurar_si_hace_falta(_cfg(), tmp_path, ej) is True
    assert ej.llamadas == ["comfy.sh stop all"] and not (tmp_path / NOMBRE_MARCA).exists()


def test_monitor_qwen_caido_cuenta_como_inactivo(tmp_path):
    def falla():
        raise ConnectionError("caido")
    mon = MonitorActividad(falla, tmp_path, Reloj(), llm_arriba=lambda: False)
    mon.muestrear()
    assert mon.estado == "caido" and mon.minutos_inactivo() == float("inf")


def test_una_ruta_opcional_vacia_en_el_toml_es_sin_valor(tmp_path):
    """TOML no tiene null: `sesiones_app = ""` no puede terminar mirando la carpeta de trabajo."""
    from vstudio.config import cargar_config
    toml = tmp_path / "config.toml"
    toml.write_text('[gpu]\nsesiones_app = ""\n', encoding="utf-8")
    assert cargar_config(toml).gpu.sesiones_app is None
    toml.write_text('[gpu]\nsesiones_app = "~/sesiones"\n', encoding="utf-8")
    assert cargar_config(toml).gpu.sesiones_app == Path("~/sesiones").expanduser()
    assert cargar_config(tmp_path / "no_hay.toml").gpu.sesiones_app is None
