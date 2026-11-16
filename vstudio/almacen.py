"""Persistencia en SQLite: un registro JSON por proyecto. Los binarios quedan en disco."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from pathlib import Path

from .modelos import Estado, Proyecto, proyecto_a_dict, proyecto_desde_dict

ESQUEMA = """
CREATE TABLE IF NOT EXISTS proyectos (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    estado TEXT NOT NULL,
    creado TEXT NOT NULL,
    actualizado TEXT NOT NULL,
    datos TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proyectos_estado ON proyectos(estado, creado);
CREATE INDEX IF NOT EXISTS idx_proyectos_chat ON proyectos(chat_id, actualizado);
"""


def nuevo_id() -> str:
    return secrets.token_hex(4)


class Almacen:
    def __init__(self, ruta_db: Path):
        ruta_db.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(ruta_db, check_same_thread=False, isolation_level=None)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(ESQUEMA)
        self._lock = threading.RLock()

    def actualizar(self, id_: str, cambio) -> Proyecto:
        """Leer-modificar-guardar atomico: `cambio` recibe el Proyecto actual y devuelve el nuevo."""
        with self._lock:
            actual = self.obtener(id_)
            if actual is None:
                raise KeyError(f"proyecto inexistente: {id_}")
            return self.guardar(cambio(actual))

    def guardar(self, p: Proyecto) -> Proyecto:
        datos = json.dumps(proyecto_a_dict(p), ensure_ascii=False)
        with self._lock:
            self._con.execute(
                "INSERT INTO proyectos (id, chat_id, estado, creado, actualizado, datos) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET estado=excluded.estado, actualizado=excluded.actualizado, "
                "datos=excluded.datos",
                (p.id, p.chat_id, p.estado.value, p.creado, p.actualizado, datos),
            )
        return p

    def obtener(self, id_: str) -> Proyecto | None:
        with self._lock:
            fila = self._con.execute("SELECT datos FROM proyectos WHERE id=?", (id_,)).fetchone()
        return proyecto_desde_dict(json.loads(fila[0])) if fila else None

    def listar(self, estados: tuple[Estado, ...] | None = None, chat_id: int | None = None,
               limite: int = 50, recientes: bool = False) -> list[Proyecto]:
        """Por orden de creacion: los mas viejos primero (la cola), o con `recientes` los ultimos `limite` (con
        el tope, si no, los nuevos nunca aparecerian pasado ese numero)."""
        consulta, args = "SELECT datos FROM proyectos WHERE 1=1", []
        if estados:
            consulta += f" AND estado IN ({','.join('?' * len(estados))})"
            args += [e.value for e in estados]
        if chat_id is not None:
            consulta += " AND chat_id=?"
            args.append(chat_id)
        consulta += f" ORDER BY creado {'DESC' if recientes else 'ASC'} LIMIT ?"
        args.append(limite)
        with self._lock:
            filas = self._con.execute(consulta, args).fetchall()
        return [proyecto_desde_dict(json.loads(f[0])) for f in filas]

    def ultimo_editable(self, chat_id: int) -> Proyecto | None:
        """Lo ultimo que el editor vio del chat: el plan pendiente o el reel entregado mas recientes. Los que
        estan en la cola o generandose no: el trabajador les renueva `actualizado` con cada clip y le ganarian al
        plan que el editor tiene en pantalla (para esos, responder a su mensaje)."""
        estados = (Estado.ESPERANDO_APROBACION, Estado.ENTREGADO)
        with self._lock:
            fila = self._con.execute(
                f"SELECT datos FROM proyectos WHERE chat_id=? AND estado IN ({','.join('?' * len(estados))}) "
                "ORDER BY actualizado DESC LIMIT 1", (chat_id, *(e.value for e in estados)),
            ).fetchone()
        return proyecto_desde_dict(json.loads(fila[0])) if fila else None

    def ultimo_esperando(self, chat_id: int) -> Proyecto | None:
        with self._lock:
            fila = self._con.execute(
                "SELECT datos FROM proyectos WHERE chat_id=? AND estado=? ORDER BY actualizado DESC LIMIT 1",
                (chat_id, Estado.ESPERANDO_APROBACION.value),
            ).fetchone()
        return proyecto_desde_dict(json.loads(fila[0])) if fila else None
