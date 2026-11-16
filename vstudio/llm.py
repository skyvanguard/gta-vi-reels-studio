"""Cliente minimo del Qwen local (llama-server, API OpenAI) con vision y respuestas JSON."""
from __future__ import annotations

import base64
import json
import logging

import httpx

from .config import ConfigLlm

log = logging.getLogger(__name__)


class LlmError(RuntimeError):
    pass


def extraer_json(texto: str) -> dict:
    """Saca el primer objeto JSON de una respuesta (tolera ```json y texto alrededor)."""
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio < 0 or fin <= inicio:
        raise LlmError(f"la respuesta no trae JSON: {texto[:200]!r}")
    try:
        return json.loads(texto[inicio:fin + 1])
    except json.JSONDecodeError as exc:
        raise LlmError(f"JSON invalido: {exc}") from exc


def parte_texto(texto: str) -> dict:
    return {"type": "text", "text": texto}


def parte_imagen(jpeg: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}


class ClienteLlm:
    def __init__(self, cfg: ConfigLlm):
        self.cfg = cfg

    def _cabeceras(self) -> dict:
        try:
            clave = self.cfg.clave_archivo.read_text(encoding="utf-8").split()[0]
        except (OSError, IndexError) as exc:
            raise LlmError(f"no se pudo leer la clave del LLM en {self.cfg.clave_archivo}") from exc
        return {"Authorization": f"Bearer {clave}"}

    def chat_json(self, sistema: str, partes: list[dict], max_tokens: int = 4000,
                  esfuerzo: str = "low", semilla: int | None = None, reintentos: int = 1,
                  razonamiento_max: int | None = None) -> dict:
        cuerpo = {
            "model": self.cfg.modelo,
            "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": partes}],
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"reasoning_effort": esfuerzo},
        }
        if semilla is not None:
            cuerpo["seed"] = semilla
        if razonamiento_max is not None:  # llama-server corta el razonamiento ahi y sigue con la respuesta
            cuerpo["thinking_budget_tokens"] = razonamiento_max
        ultimo_error: Exception | None = None
        for intento in range(reintentos + 1):
            try:
                resp = httpx.post(f"{self.cfg.url}/v1/chat/completions", json=cuerpo,
                                  headers=self._cabeceras(), timeout=self.cfg.timeout_s)
                resp.raise_for_status()
                contenido = resp.json()["choices"][0]["message"]["content"] or ""
                return extraer_json(contenido)
            except (httpx.HTTPError, KeyError, LlmError) as exc:
                ultimo_error = exc
                log.warning("LLM intento %d fallo: %s", intento + 1, exc)
        raise LlmError(f"el LLM no devolvio un JSON valido: {ultimo_error}")

    def metricas(self) -> dict[str, float]:
        """Contadores de /metrics de llama-server (para saber si alguien lo esta usando)."""
        resp = httpx.get(f"{self.cfg.url}/metrics", headers=self._cabeceras(), timeout=10)
        resp.raise_for_status()
        valores = {}
        for linea in resp.text.splitlines():
            if linea.startswith("llamacpp:") and " " in linea:
                nombre, valor = linea.rsplit(" ", 1)
                try:
                    valores[nombre.removeprefix("llamacpp:")] = float(valor)
                except ValueError:
                    continue
        return valores

    def disponible(self) -> bool:
        try:
            return httpx.get(f"{self.cfg.url}/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False
