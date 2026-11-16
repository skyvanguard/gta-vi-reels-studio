"""Quien puede usar el bot: lista de usuarios de Telegram + codigo de vinculacion de un solo uso.

El codigo vence a las 24 h, se compara en tiempo constante y se borra al usarse. Los archivos se
escriben de forma atomica con permisos 600 dentro de una carpeta 700.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

VIGENCIA_CODIGO_S = 24 * 3600


def escribir_privado(ruta: Path, contenido: str) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ruta.parent, 0o700)
    except OSError:
        pass
    temporal = ruta.with_name(ruta.name + ".tmp")
    descriptor = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
        fh.write(contenido)
    os.replace(temporal, ruta)
    try:
        os.chmod(ruta, 0o600)
    except OSError:
        pass


def asegurar_privado(ruta: Path) -> bool:
    """Si un secreto quedo legible por otros usuarios, lo corrige a 600. True si tuvo que corregir."""
    if not ruta.exists() or os.name == "nt":
        return False
    if ruta.stat().st_mode & 0o077:
        os.chmod(ruta, 0o600)
        return True
    return False


class Acceso:
    def __init__(self, usuarios_archivo: Path, codigo_archivo: Path):
        self._usuarios_archivo, self._codigo_archivo = usuarios_archivo, codigo_archivo
        self._lock = threading.Lock()

    def usuarios(self) -> frozenset[int]:
        if not self._usuarios_archivo.exists():
            return frozenset()
        datos = json.loads(self._usuarios_archivo.read_text(encoding="utf-8"))
        return frozenset(int(u) for u in datos.get("usuarios", []))

    def autorizado(self, usuario_id: int) -> bool:
        return usuario_id in self.usuarios()

    def nuevo_codigo(self) -> str:
        codigo = secrets.token_urlsafe(9)
        escribir_privado(self._codigo_archivo, codigo)
        return codigo

    def asegurar_codigo_si_no_hay_usuarios(self) -> bool:
        """Al primer arranque crea un codigo para vincular al dueno. True si lo creo."""
        if self.usuarios() or self._codigo_vigente():
            return False
        self.nuevo_codigo()
        return True

    def _codigo_vigente(self) -> str | None:
        if not self._codigo_archivo.exists():
            return None
        if time.time() - self._codigo_archivo.stat().st_mtime > VIGENCIA_CODIGO_S:
            self._codigo_archivo.unlink(missing_ok=True)
            return None
        return self._codigo_archivo.read_text(encoding="utf-8").strip() or None

    def vincular(self, usuario_id: int, codigo: str) -> bool:
        with self._lock:
            esperado = self._codigo_vigente()
            if not esperado or not hmac.compare_digest(esperado.encode(), codigo.strip().encode()):
                return False
            usuarios = sorted(self.usuarios() | {usuario_id})
            escribir_privado(self._usuarios_archivo, json.dumps({"usuarios": usuarios}))
            self._codigo_archivo.unlink(missing_ok=True)  # un solo uso
            return True
