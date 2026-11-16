"""Cliente minimo de la Bot API de Telegram (long polling, sin puertos abiertos en el servidor).

El token va en la URL de cada llamada: nunca se loguea (httpx en WARNING) y se borra de los errores.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

MAX_TEXTO = 4000
MAX_CAPTION = 1000
MAX_DESCARGA = 20 * 1024 * 1024  # limite de getFile de la Bot API


class TelegramError(RuntimeError):
    pass


def teclado(filas: list[list[tuple[str, str]]] | None) -> dict | None:
    if not filas:
        return None
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in fila] for fila in filas]}


class ClienteTelegram:
    def __init__(self, token: str):
        if not token or ":" not in token:
            raise TelegramError("token de Telegram invalido")
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}"
        self._base_archivos = f"https://api.telegram.org/file/bot{token}"
        self._http = httpx.Client(timeout=httpx.Timeout(75.0, connect=15.0))

    def _limpiar(self, texto: str) -> str:
        return str(texto).replace(self._token, "<token>")

    def _llamar(self, metodo: str, *, json_: dict | None = None, datos: dict | None = None,
                archivos: dict | None = None) -> Any:
        try:
            resp = self._http.post(f"{self._base}/{metodo}", json=json_, data=datos, files=archivos)
            cuerpo = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError(f"{metodo}: {self._limpiar(exc)}") from None
        if not cuerpo.get("ok"):
            raise TelegramError(f"{metodo}: {self._limpiar(cuerpo.get('description', 'error desconocido'))}")
        return cuerpo["result"]

    def actualizaciones(self, offset: int, espera_s: int) -> list[dict]:
        return self._llamar("getUpdates", json_={"offset": offset, "timeout": espera_s,
                                                 "allowed_updates": ["message", "callback_query"]})

    def mensaje(self, chat_id: int, texto: str, botones: list | None = None) -> None:
        cuerpo = {"chat_id": chat_id, "text": texto[:MAX_TEXTO], "disable_web_page_preview": True}
        if botones:
            cuerpo["reply_markup"] = teclado(botones)
        self._llamar("sendMessage", json_=cuerpo)

    def editar(self, chat_id: int, mensaje_id: int, texto: str, botones: list | None = None) -> None:
        """Cambia un mensaje ya enviado (el plan despues de tocar un boton). Si quedo igual, no es un error."""
        cuerpo = {"chat_id": chat_id, "message_id": mensaje_id, "text": texto[:MAX_TEXTO],
                  "disable_web_page_preview": True}
        if botones:
            cuerpo["reply_markup"] = teclado(botones)
        try:
            self._llamar("editMessageText", json_=cuerpo)
        except TelegramError as exc:
            if "message is not modified" not in str(exc):
                raise

    def _enviar_archivo(self, metodo: str, campo: str, chat_id: int, ruta: Path, texto: str,
                        botones: list | None = None, extra: dict | None = None) -> None:
        datos = {"chat_id": str(chat_id), "caption": texto[:MAX_CAPTION], **(extra or {})}
        if botones:
            datos["reply_markup"] = json.dumps(teclado(botones))
        with ruta.open("rb") as fh:
            self._llamar(metodo, datos=datos, archivos={campo: (ruta.name, fh)})

    def foto(self, chat_id: int, ruta: Path, texto: str, botones: list | None = None) -> None:
        self._enviar_archivo("sendPhoto", "photo", chat_id, ruta, texto, botones)

    def video(self, chat_id: int, ruta: Path, texto: str, botones: list | None = None) -> None:
        self._enviar_archivo("sendVideo", "video", chat_id, ruta, texto, botones,
                             extra={"supports_streaming": "true", "width": "1080", "height": "1920"})

    def audio(self, chat_id: int, ruta: Path, texto: str) -> None:
        self._enviar_archivo("sendAudio", "audio", chat_id, ruta, texto)

    def responder_boton(self, callback_id: str, texto: str = "") -> None:
        self._llamar("answerCallbackQuery", json_={"callback_query_id": callback_id, "text": texto[:190]})

    def bajar_archivo(self, file_id: str) -> bytes:
        info = self._llamar("getFile", json_={"file_id": file_id})
        if int(info.get("file_size", 0)) > MAX_DESCARGA:
            raise TelegramError("el archivo supera 20 MB")
        try:
            resp = self._http.get(f"{self._base_archivos}/{info['file_path']}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise TelegramError(f"descarga: {self._limpiar(exc)}") from None
        if len(resp.content) > MAX_DESCARGA:
            raise TelegramError("el archivo supera 20 MB")
        return resp.content
