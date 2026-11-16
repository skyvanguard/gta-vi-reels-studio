"""Publicar reels en la pagina de Facebook (Reels Publishing API de Meta, Graph v27.0).

Tres pasos (developers.facebook.com/docs/video-api/guides/reels-publishing): iniciar la subida
(POST /{pagina}/video_reels upload_phase=start), mandar el archivo a rupload.facebook.com (header
"Authorization: OAuth <token>") y terminar (upload_phase=finish) con video_state=SCHEDULED y
scheduled_publish_time (entre 10 minutos y 29 dias desde ahora) o PUBLISHED. Limite de Meta: 30 publicaciones
por API cada 24 h. El token es un token de PAGINA (pages_show_list, pages_read_engagement, pages_manage_posts);
vive en secrets/ con permisos 600 y nunca se loguea (se borra de los errores).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

VERSION = "v27.0"
GRAPH = f"https://graph.facebook.com/{VERSION}"
RUPLOAD = f"https://rupload.facebook.com/video-upload/{VERSION}"
MAX_DESCRIPCION = 2200
RE_NO_EXISTE = re.compile(r"does not exist|nonexist|Unsupported (?:get|delete) request|Unknown path", re.I)
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
TIMEOUT_SUBIDA = httpx.Timeout(600.0, connect=15.0)


class FacebookError(RuntimeError):
    pass


@dataclass(frozen=True)
class Pagina:
    id: str
    nombre: str
    token: str = field(repr=False)  # que no aparezca si alguien loguea la pagina


def configurado(cfg) -> bool:
    """Si hay pagina configurada (cfg: ConfigFacebook)."""
    return cfg.token_archivo.exists() and cfg.pagina_archivo.exists()


def leer_pagina(token_archivo: Path, pagina_archivo: Path) -> Pagina | None:
    """La pagina configurada (None si todavia no se configuro)."""
    if not (token_archivo.exists() and pagina_archivo.exists()):
        return None
    datos = json.loads(pagina_archivo.read_text(encoding="utf-8"))
    return Pagina(id=str(datos["id"]), nombre=str(datos.get("nombre", "")),
                  token=token_archivo.read_text(encoding="utf-8").strip())


class ClienteFacebook:
    def __init__(self, pagina: Pagina, http: httpx.Client | None = None):
        if not pagina.token or not pagina.id.isdigit():
            raise FacebookError("página o token de Facebook inválidos")
        self.pagina = pagina
        self._http = http or httpx.Client(timeout=TIMEOUT, trust_env=False)

    def _limpiar(self, texto: object) -> str:
        return str(texto).replace(self.pagina.token, "<token>")

    def _respuesta(self, metodo: str, resp: httpx.Response) -> Any:
        try:
            cuerpo = resp.json()
        except ValueError:
            raise FacebookError(f"{metodo}: respuesta no JSON (HTTP {resp.status_code})") from None
        if resp.status_code >= 400 or (isinstance(cuerpo, dict) and "error" in cuerpo):
            error = cuerpo.get("error", {}) if isinstance(cuerpo, dict) else {}
            raise FacebookError(f"{metodo}: {self._limpiar(error.get('message', resp.status_code))}")
        return cuerpo

    def _post(self, metodo: str, url: str, **kw) -> Any:
        try:
            return self._respuesta(metodo, self._http.post(url, **kw))
        except httpx.HTTPError as exc:
            raise FacebookError(f"{metodo}: {self._limpiar(exc)}") from None

    def _get(self, metodo: str, url: str, params: dict) -> Any:
        try:
            return self._respuesta(metodo, self._http.get(url, params={**params, "access_token": self.pagina.token}))
        except httpx.HTTPError as exc:
            raise FacebookError(f"{metodo}: {self._limpiar(exc)}") from None

    def cerrar(self) -> None:
        self._http.close()

    def nombre(self) -> str:
        """Comprueba el token: el nombre de la pagina."""
        return str(self._get("pagina", f"{GRAPH}/{self.pagina.id}", {"fields": "name"}).get("name", ""))

    def publicar_reel(self, ruta: Path, descripcion: str, cuando: datetime | None = None,
                      con_ia: bool = True, al_iniciar=None) -> str:
        """Sube el reel y lo programa para `cuando` (con zona) o lo publica ya. Devuelve el ID del video.
        `al_iniciar(video_id)` se llama apenas Meta da el ID (antes de subir): si algo falla despues, se sabe
        que video revisar o borrar en vez de subir otro."""
        inicio = self._post("inicio", f"{GRAPH}/{self.pagina.id}/video_reels",
                            json={"upload_phase": "start", "access_token": self.pagina.token})
        video_id = str(inicio.get("video_id", ""))
        if not video_id.isdigit():
            raise FacebookError(f"inicio: Meta no devolvió el ID del video ({self._limpiar(inicio)})")
        if al_iniciar:
            al_iniciar(video_id)
        destino = str(inicio.get("upload_url") or "")
        partes = urlsplit(destino)
        if partes.scheme != "https" or not (partes.hostname or "").endswith(".facebook.com"):
            destino = f"{RUPLOAD}/{video_id}"  # el token va en el header: solo a un host de Facebook
        datos = ruta.read_bytes()
        subida = self._post("subida", destino, content=datos,
                            timeout=TIMEOUT_SUBIDA,
                            headers={"Authorization": f"OAuth {self.pagina.token}", "offset": "0",
                                     "file_size": str(len(datos)), "Content-Type": "application/octet-stream"})
        if not subida.get("success"):
            raise FacebookError(f"subida: Meta no confirmó el archivo ({self._limpiar(subida)})")
        fin = {"upload_phase": "finish", "video_id": video_id, "description": descripcion[:MAX_DESCRIPCION],
               "is_ai_generated": "true" if con_ia else "false", "access_token": self.pagina.token,
               "video_state": "SCHEDULED" if cuando else "PUBLISHED"}
        if cuando:
            fin["scheduled_publish_time"] = str(int(cuando.timestamp()))
        if not self._post("publicación", f"{GRAPH}/{self.pagina.id}/video_reels", data=fin).get("success"):
            raise FacebookError("publicación: Meta no confirmó la publicación")
        return video_id

    def estado(self, video_id: str) -> dict:
        return self._get("estado", f"{GRAPH}/{video_id}", {"fields": "status"}).get("status", {})

    def borrar(self, video_id: str) -> None:
        """Borra el video (programado o publicado). Si ya no existe en Meta, no es un error."""
        try:
            self._respuesta("borrar", self._http.delete(f"{GRAPH}/{video_id}",
                                                        params={"access_token": self.pagina.token}))
        except httpx.HTTPError as exc:
            raise FacebookError(f"borrar: {self._limpiar(exc)}") from None
        except FacebookError as exc:
            if not RE_NO_EXISTE.search(str(exc)):
                raise

    def ya_salio(self, video_id: str) -> bool:
        """Si Meta tiene el video programado o publicado (para no subir otro si se perdio la respuesta)."""
        try:
            fase = self.estado(video_id).get("publishing_phase", {})
        except FacebookError:
            return False
        return fase.get("publish_status") in ("scheduled", "published")


def paginas_del_usuario(token_usuario: str, http: httpx.Client | None = None) -> list[Pagina]:
    """Con un token de USUARIO (de larga duracion), las paginas que administra con su token de pagina (que no
    vence si el de usuario es de larga duracion). Para configurar una sola vez."""
    http = http or httpx.Client(timeout=TIMEOUT, trust_env=False)
    try:
        resp = http.get(f"{GRAPH}/me/accounts", params={"fields": "id,name,access_token,tasks",
                                                        "access_token": token_usuario})
        cuerpo = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise FacebookError(f"cuentas: {str(exc).replace(token_usuario, '<token>')}") from None
    if "error" in cuerpo:
        raise FacebookError(f"cuentas: {cuerpo['error'].get('message', '')}".replace(token_usuario, "<token>"))
    return [Pagina(id=str(p["id"]), nombre=p.get("name", ""), token=p["access_token"])
            for p in cuerpo.get("data", []) if "CREATE_CONTENT" in p.get("tasks", []) and p.get("access_token")]
