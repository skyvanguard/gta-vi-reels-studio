"""Turnos de GPU entre el Qwen (el LLM local) y ComfyUI.

- MonitorActividad decide si el Qwen esta sin uso (metricas del llama-server + sesiones de otra aplicacion).
- TurnoGpu baja el Qwen, levanta ComfyUI y GARANTIZA dejar el Qwen como estaba al salir, aun con errores.
  Mientras dura el turno deja una marca en disco; si el proceso muere, `restaurar_si_hace_falta`
  (en cada ciclo del trabajador) y scripts/restaurar.sh (ExecStopPost de systemd) lo devuelven.
"""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigGpu

log = logging.getLogger(__name__)

NOMBRE_MARCA = "turno_gpu.json"
REINTENTOS_QWEN = 3
ESPERA_REINTENTO_S = 10
TIMEOUT_SCRIPT_S = 900
CONTADORES = ("prompt_tokens_total", "tokens_predicted_total")


class TurnoError(RuntimeError):
    pass


class MonitorActividad:
    """Arranca asumiendo actividad (conservador) y cuenta minutos sin cambios en metricas ni sesiones.

    Si el Qwen esta caido (no responde /health) nadie puede estar usandolo: cuenta como inactivo.
    Si responde pero no da metricas, no se puede afirmar nada: cuenta como activo.
    """

    def __init__(self, leer_metricas: Callable[[], dict], sesiones_dir: Path | None,
                 reloj: Callable[[], float] = time.time, llm_arriba: Callable[[], bool] = lambda: True):
        self._leer, self._sesiones, self._reloj, self._arriba = leer_metricas, sesiones_dir, reloj, llm_arriba
        self._firma: tuple | None = None
        self._ultima = reloj()
        self.estado = "desconocido"  # ok | caido | sin_metricas | desconocido

    def _sesion_mas_reciente(self) -> float:
        if self._sesiones is None or not self._sesiones.is_dir():
            return 0.0
        return max((p.stat().st_mtime for p in self._sesiones.glob("*/*/session.jsonl*")), default=0.0)

    def muestrear(self) -> None:
        ahora = self._reloj()
        try:
            m = self._leer()
        except Exception as exc:  # noqa: BLE001 - cualquier fallo de red/parseo = sin metricas
            self.estado = "sin_metricas" if self._arriba() else "caido"
            log.info("sin metricas del LLM (%s): %s", self.estado, exc)
            self._ultima = ahora
            return
        self.estado = "ok"
        firma = tuple(m.get(c, 0.0) for c in CONTADORES)
        if m.get("requests_processing", 0) > 0 or (self._firma is not None and firma != self._firma):
            self._ultima = ahora
        self._firma = firma
        self._ultima = max(self._ultima, self._sesion_mas_reciente())

    def minutos_inactivo(self) -> float:
        if self.estado == "caido":
            return math.inf
        if self.estado != "ok":
            return 0.0
        return max(0.0, (self._reloj() - self._ultima) / 60)


class TurnoGpu:
    def __init__(self, cfg: ConfigGpu, datos: Path, ejecutar: Callable = subprocess.run,
                 esperar: Callable[[float], None] = time.sleep):
        self.cfg, self.marca, self._ejecutar, self._esperar = cfg, datos / NOMBRE_MARCA, ejecutar, esperar
        self.qwen_arriba = True

    def _correr(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        log.info("turno gpu: %s", " ".join(args))
        return self._ejecutar(list(args), capture_output=True, text=True, check=check, timeout=TIMEOUT_SCRIPT_S)

    def _intentar(self, *args: str) -> subprocess.CompletedProcess | None:
        """Como _correr pero nunca lanza: en el cierre cada paso tiene que intentarse igual."""
        try:
            return self._correr(*args, check=False)
        except (subprocess.SubprocessError, OSError) as exc:
            log.error("turno gpu: %s fallo: %s", " ".join(args), exc)
            return None

    def __enter__(self) -> "TurnoGpu":
        estado = self._intentar(str(self.cfg.script_qwen), "status")
        self.qwen_arriba = estado is not None and estado.returncode == 0
        self.marca.parent.mkdir(parents=True, exist_ok=True)
        self.marca.write_text(json.dumps({"inicio": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
                                          "qwen_arriba": self.qwen_arriba}))
        try:
            if self.qwen_arriba:
                self._correr(str(self.cfg.script_qwen), "stop")
            self._correr(str(self.cfg.script_comfy), "start", "all")
        except Exception:
            log.exception("fallo al abrir el turno de GPU; devolviendo el Qwen")
            self.cerrar()
            raise
        return self

    def __exit__(self, *exc_info) -> bool:
        self.cerrar()
        return False

    def cerrar(self) -> None:
        self._intentar(str(self.cfg.script_comfy), "stop", "all")
        if not self.qwen_arriba:
            self.marca.unlink(missing_ok=True)
            log.info("el Qwen estaba apagado antes del turno: se deja apagado")
            return
        for intento in range(1, REINTENTOS_QWEN + 1):
            res = self._intentar(str(self.cfg.script_qwen), "start")
            if res is not None and res.returncode == 0:
                self.marca.unlink(missing_ok=True)
                log.info("Qwen devuelto (intento %d)", intento)
                return
            log.error("el Qwen no volvio (intento %d): %s", intento, ((res.stderr if res else "") or "")[-500:])
            self._esperar(ESPERA_REINTENTO_S)
        raise TurnoError("el Qwen no volvio a levantar: revisar `docker logs llm-server` (la marca queda puesta)")


def _proceso_vivo(pid: int) -> bool:
    if os.name == "nt":
        return False  # en Windows os.kill(pid, 0) termina el proceso: solo se usa en tests
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def restaurar_si_hace_falta(cfg: ConfigGpu, datos: Path, ejecutar: Callable = subprocess.run) -> bool:
    """Si quedo un turno abierto por un proceso que ya murio, deja el Qwen como estaba. True si actuo."""
    turno = TurnoGpu(cfg, datos, ejecutar)
    if not turno.marca.exists():
        return False
    try:
        datos_marca = json.loads(turno.marca.read_text() or "{}")
    except json.JSONDecodeError:
        datos_marca = {}
    pid = int(datos_marca.get("pid") or 0)
    if pid and pid != os.getpid() and _proceso_vivo(pid):
        log.info("hay un turno de GPU en curso (pid %d): no se toca", pid)
        return False
    log.warning("turno de GPU abierto de una ejecucion anterior: restaurando")
    turno.qwen_arriba = bool(datos_marca.get("qwen_arriba", True))
    turno.cerrar()
    return True
