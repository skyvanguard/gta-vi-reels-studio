"""Genera clips con Wan 2.2 I2V 14B en un ComfyUI local (workflow API versionado en workflows/wan)."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import ConfigVideo

log = logging.getLogger(__name__)

POLL_S = 3
MAX_FALLOS_POLL = 5
NEGATIVE_OFICIAL = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，"
    "丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，"
    "静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
)
# pasos / cfg / corte entre modelo de ruido alto y bajo, tal cual la plantilla oficial
CONFIG_LORA = {"steps": 4, "cfg": 1.0, "split": 2}
CONFIG_SIN_LORA = {"steps": 20, "cfg": 3.5, "split": 10}


class ComfyError(RuntimeError):
    pass


@dataclass(frozen=True)
class PedidoClip:
    imagen: Path
    prompt: str
    negativo_extra: str
    ancho: int
    alto: int
    cuadros: int
    seed: int
    prefijo: str
    con_lora: bool = True


@dataclass(frozen=True)
class ResultadoClip:
    ruta: Path
    segundos: float
    vram_pico_mib: int
    prompt_id: str
    workflow: dict


def armar_workflow(plantilla: dict, imagen: str, p: PedidoClip) -> dict:
    """Copia la plantilla y rellena los nodos (mismos ids que la plantilla oficial)."""
    wf = json.loads(json.dumps({k: v for k, v in plantilla.items() if not k.startswith("_")}))
    cfg = CONFIG_LORA if p.con_lora else CONFIG_SIN_LORA
    negativo = NEGATIVE_OFICIAL + (", " + p.negativo_extra if p.negativo_extra else "")
    wf["93"]["inputs"]["text"] = p.prompt
    wf["89"]["inputs"]["text"] = negativo
    wf["97"]["inputs"]["image"] = imagen
    wf["98"]["inputs"].update({"width": p.ancho, "height": p.alto, "length": p.cuadros})
    wf["86"]["inputs"].update({"noise_seed": p.seed, "steps": cfg["steps"], "cfg": cfg["cfg"],
                               "end_at_step": cfg["split"]})
    wf["85"]["inputs"].update({"steps": cfg["steps"], "cfg": cfg["cfg"], "start_at_step": cfg["split"],
                               "end_at_step": cfg["steps"]})
    wf["108"]["inputs"]["filename_prefix"] = f"video/{p.prefijo}_{uuid.uuid4().hex[:8]}"
    if not p.con_lora:
        wf["104"]["inputs"]["model"] = ["95", 0]
        wf["103"]["inputs"]["model"] = ["96", 0]
        del wf["101"], wf["102"]
    return wf


NODO_MODELO_INTERPOLACION, NODO_INTERPOLACION = "interp_modelo", "interp"
CARPETA_INTERPOLACION = "frame_interpolation"  # dentro de ComfyUI/models


def con_interpolacion(plantilla: dict, modelo: str, fps: int) -> dict:
    """La plantilla con un cuadro intermedio entre cada par (FrameInterpolate x2, nodos propios de ComfyUI) y el
    video a `fps`: 81 cuadros a 16 por segundo pasan a 161 a 30 (5,37 s). Antes el reel repetia cuadros para
    llegar a 30 y los clips de IA se veian entrecortados al lado del video real. No cambia `plantilla`."""
    video = next((k for k, n in plantilla.items() if isinstance(n, dict) and n.get("class_type") == "CreateVideo"),
                 None)
    if video is None:
        raise ComfyError("la plantilla no tiene un nodo CreateVideo: no se como sumarle la interpolacion")
    nodo = plantilla[video]
    return {**plantilla,
            NODO_MODELO_INTERPOLACION: {"class_type": "FrameInterpolationModelLoader",
                                        "inputs": {"model_name": modelo}},
            NODO_INTERPOLACION: {"class_type": "FrameInterpolate",
                                 "inputs": {"interp_model": [NODO_MODELO_INTERPOLACION, 0],
                                            "images": nodo["inputs"]["images"], "multiplier": 2}},
            video: {**nodo, "inputs": {**nodo["inputs"], "images": [NODO_INTERPOLACION, 0], "fps": float(fps)}}}


def interpolacion_disponible(video: ConfigVideo, comfy_dir: Path) -> bool:
    """Si los clips van a salir con cuadros intermedios: esta pedido y el modelo esta bajado."""
    return video.interpolar and (comfy_dir / "models" / CARPETA_INTERPOLACION / video.modelo_interpolacion).is_file()


def plantilla_para(video: ConfigVideo, comfy_dir: Path) -> dict:
    """La plantilla del flujo de Wan segun la configuracion de video: con interpolacion si esta pedida y el modelo
    esta bajado. Si no se puede sumar (falta el modelo, la plantilla es otra), el clip sale como antes y queda en
    el log: la fluidez es una mejora, no puede tirar una tanda."""
    plantilla = json.loads(video.workflow.read_text(encoding="utf-8"))
    if not video.interpolar:
        return plantilla
    if not interpolacion_disponible(video, comfy_dir):
        log.warning("falta el modelo %s en %s: los clips salen sin cuadros intermedios (16 por segundo)",
                    video.modelo_interpolacion, comfy_dir / "models" / CARPETA_INTERPOLACION)
        return plantilla
    try:
        return con_interpolacion(plantilla, video.modelo_interpolacion, video.fps_salida)
    except (ComfyError, KeyError, TypeError) as exc:
        log.warning("no pude sumar la interpolacion a la plantilla (%s): los clips salen a 16 por segundo", exc)
        return plantilla


def archivos_de_salida(entrada_historial: dict) -> list[dict]:
    encontrados = []
    for salida_nodo in entrada_historial.get("outputs", {}).values():
        for valor in salida_nodo.values():
            if isinstance(valor, list):
                encontrados += [v for v in valor if isinstance(v, dict) and "filename" in v]
    return encontrados


class _MuestreoVram(threading.Thread):
    def __init__(self, gpu: int):
        super().__init__(daemon=True)
        self.gpu, self.pico, self._fin = gpu, 0, threading.Event()

    def run(self) -> None:
        while not self._fin.is_set():
            out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits",
                                  "-i", str(self.gpu)], capture_output=True, text=True, check=False)
            if out.returncode == 0 and out.stdout.strip().isdigit():
                self.pico = max(self.pico, int(out.stdout.strip()))
            self._fin.wait(2)

    def parar(self) -> int:
        self._fin.set()
        self.join(timeout=5)
        return self.pico


def generar_clip(servidor: str, gpu: int, comfy_dir: Path, plantilla: dict, pedido: PedidoClip,
                 destino: Path, timeout_s: int) -> ResultadoClip:
    nombre_entrada = f"{pedido.prefijo}_{pedido.imagen.name}"
    shutil.copy(pedido.imagen, comfy_dir / "input" / nombre_entrada)
    wf = armar_workflow(plantilla, nombre_entrada, pedido)
    muestreo = _MuestreoVram(gpu)
    muestreo.start()
    t0 = time.monotonic()
    prompt_id, terminado = None, False
    try:
        resp = httpx.post(f"{servidor}/prompt", json={"prompt": wf, "client_id": str(uuid.uuid4())}, timeout=30)
        cuerpo = resp.json()
        if resp.status_code != 200 or cuerpo.get("node_errors") or "prompt_id" not in cuerpo:
            raise ComfyError(f"workflow rechazado: {json.dumps(cuerpo)[:1500]}")
        prompt_id = cuerpo["prompt_id"]
        entrada = _esperar(servidor, prompt_id, timeout_s)
        terminado = True
    finally:
        pico = muestreo.parar()
        if prompt_id and not terminado:
            _interrumpir(servidor)
    archivos = archivos_de_salida(entrada)
    if not archivos:
        raise ComfyError("ComfyUI termino sin archivos de salida")
    f = archivos[0]
    origen = comfy_dir / f.get("type", "output") / f.get("subfolder", "") / f["filename"]
    shutil.copy(origen, destino)
    return ResultadoClip(ruta=destino, segundos=round(time.monotonic() - t0, 1), vram_pico_mib=pico,
                         prompt_id=prompt_id, workflow=wf)


def _interrumpir(servidor: str) -> None:
    """Corta la generacion en curso para que no siga ocupando la GPU (y vacia la cola de ese ComfyUI)."""
    try:
        httpx.post(f"{servidor}/interrupt", timeout=10)
        httpx.post(f"{servidor}/queue", json={"clear": True}, timeout=10)
    except httpx.HTTPError as exc:
        log.warning("no se pudo interrumpir ComfyUI en %s: %s", servidor, exc)


def _esperar(servidor: str, prompt_id: str, timeout_s: int) -> dict:
    limite, fallos = time.monotonic() + timeout_s, 0
    while time.monotonic() < limite:
        try:
            hist = httpx.get(f"{servidor}/history/{prompt_id}", timeout=30).json()
            fallos = 0
        except (httpx.HTTPError, ValueError) as exc:
            fallos += 1
            if fallos > MAX_FALLOS_POLL:
                raise ComfyError(f"ComfyUI no responde ({exc})") from exc
            time.sleep(POLL_S)
            continue
        if prompt_id in hist:
            entrada = hist[prompt_id]
            estado = entrada.get("status", {})
            if estado.get("status_str") != "success":
                raise ComfyError(f"ComfyUI fallo: {json.dumps(estado.get('messages', []))[:1500]}")
            return entrada
        time.sleep(POLL_S)
    raise ComfyError(f"sin resultado tras {timeout_s} s; se interrumpio la generacion")
