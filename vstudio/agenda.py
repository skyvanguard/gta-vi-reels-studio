"""Agenda de publicacion: los reels automaticos llegan repartidos en el dia, cada uno a su horario.

La corrida automatica entregaba todo junto y se publicaba todo en un rato: el resto del dia quedaba vacio y
cada reel lo veia muy poca gente. Ahora cada reel
automatico, cuando esta armado, queda PROGRAMADO para el proximo horario libre y el bot lo manda ahi, con su texto
listo para copiar. Los que pide el editor a mano llegan apenas estan, como siempre.

A proposito: un reel PROGRAMADO no se edita (el editor todavia no lo vio; los cambios con palabras y los botones
van al ultimo plan o reel que le llego); se puede cancelar o adelantar con /agenda ya. Y los horarios son los de
hoy: un reel que se termina despues del ultimo horario sale en el momento, no al dia siguiente (seria una
noticia de ayer).

/agenda (que hay pendiente) · /agenda ya (mandar todo ahora) · /agenda off (que lleguen apenas esten) · /agenda on
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .modelos import Estado, Proyecto
from .servicio import EdicionInvalida, Servicio

log = logging.getLogger(__name__)

INTERVALO_S = 30
MARGEN = timedelta(minutes=2)  # un horario que cae en menos de esto ya no se usa: el reel va al siguiente
LIMITE = 200


def horarios_del_dia(horarios: tuple[str, ...], dia: date, zona: ZoneInfo) -> list[datetime]:
    """Los horarios configurados ("10:30") como fechas de ese dia, en orden. Los mal escritos se saltean."""
    salida = []
    for texto in horarios:
        try:
            h, _, m = texto.partition(":")
            salida.append(datetime.combine(dia, time(int(h), int(m or 0)), zona))
        except ValueError:
            log.warning("horario de agenda mal escrito: %r", texto)
    return sorted(salida)


def hora_de(p: Proyecto) -> datetime | None:
    """Cuando sale el reel (None: ya, porque no tiene horario o quedo mal guardado; lo mal guardado no frena a
    los demas)."""
    try:
        hora = datetime.fromisoformat(p.enviar_a) if p.enviar_a else None
    except ValueError:
        log.warning("%s tiene un horario mal guardado (%r): sale ahora", p.id, p.enviar_a)
        return None
    return hora if hora is None or hora.tzinfo is not None else None


def proximo_horario(horarios: list[datetime], ahora: datetime, ocupados: set[datetime]) -> datetime | None:
    """El primer horario de hoy que todavia no paso y que no tiene otro reel. Si todos los que quedan estan
    tomados, el ultimo (se juntan ahi). None si ya paso el ultimo horario del dia: el reel se manda ahora."""
    futuros = [h for h in horarios if h >= ahora + MARGEN]
    libres = [h for h in futuros if h not in ocupados]
    return libres[0] if libres else (futuros[-1] if futuros else None)


class Agenda(threading.Thread):
    """Manda cada reel programado cuando llega su hora. `entregar(id)`: lo que hace el envio (el trabajador)."""

    def __init__(self, servicio: Servicio, entregar: Callable[[str], None]):
        super().__init__(daemon=True, name="agenda")
        self.s, self._entregar = servicio, entregar
        self.cfg = servicio.cfg.agenda
        self.zona = ZoneInfo(servicio.cfg.zona_horaria)
        self.archivo = servicio.cfg.datos / "agenda.json"
        self._parar = threading.Event()

    # ---------- estado ----------
    @property
    def activa(self) -> bool:
        try:
            datos = json.loads(self.archivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            datos = {}
        return bool(datos.get("activa", self.cfg.activa)) if isinstance(datos, dict) else self.cfg.activa

    def activar(self, activa: bool) -> None:
        self.archivo.parent.mkdir(parents=True, exist_ok=True)
        temporal = self.archivo.with_suffix(".tmp")
        temporal.write_text(json.dumps({"activa": activa}), encoding="utf-8")
        temporal.replace(self.archivo)

    def pendientes(self) -> list[Proyecto]:
        """Los reels armados que esperan su horario, en el orden en que salen (primero los que salen ya)."""
        programados = self.s.almacen.listar((Estado.PROGRAMADO,), limite=LIMITE)
        return sorted(programados, key=lambda p: (hora_de(p) is not None, hora_de(p) or datetime.min, p.creado))

    def horario_para(self, ahora: datetime | None = None) -> datetime | None:
        """Cuando sale un reel que se termino de armar ahora (None: ahora mismo, la agenda esta apagada o ya paso
        el ultimo horario del dia)."""
        if not self.activa:
            return None
        ahora = ahora or datetime.now(self.zona)
        ocupados = {hora for hora in map(hora_de, self.pendientes()) if hora is not None}
        return proximo_horario(horarios_del_dia(self.cfg.horarios, ahora.date(), self.zona), ahora, ocupados)

    # ---------- envio ----------
    def vencidos(self, ahora: datetime | None = None) -> list[Proyecto]:
        ahora = ahora or datetime.now(self.zona)
        return [p for p in self.pendientes() if (hora_de(p) or ahora) <= ahora]

    def enviar_vencidos(self, ahora: datetime | None = None) -> int:
        """Manda los reels a los que les llego la hora. Devuelve cuantos."""
        enviados = 0
        for p in self.vencidos(ahora):
            try:
                self._entregar(p.id)
                enviados += 1
            except Exception as exc:  # noqa: BLE001 - un reel que no sale no frena los demas
                # a error, no a "se reintenta": un envio que rompe a mitad repetido cada 30 s manda el mismo
                # video una y otra vez
                log.exception("no pude mandar el reel programado %s", p.id)
                try:
                    self.s.transicion(p.id, (Estado.PROGRAMADO,), lambda a, exc=exc: a.con(
                        estado=Estado.ERROR, enviar_a="", error=f"falló el envío programado: {exc}"[:500]))
                except EdicionInvalida:
                    pass
        return enviados

    def adelantar(self) -> int:
        """Todo lo pendiente sale en el proximo ciclo (/agenda ya). Devuelve cuantos reels."""
        adelantados = 0
        for p in self.pendientes():
            try:
                self.s.transicion(p.id, (Estado.PROGRAMADO,), lambda a: a.con(enviar_a=""))
                adelantados += 1
            except EdicionInvalida:
                continue  # salio justo ahora
        return adelantados

    def run(self) -> None:
        while not self._parar.is_set():
            try:
                self.enviar_vencidos()
            except Exception:  # noqa: BLE001 - el hilo no puede morir
                log.exception("error en la agenda de publicacion")
            self._parar.wait(INTERVALO_S)

    def detener(self) -> None:
        self._parar.set()

    # ---------- para el editor ----------
    def resumen(self) -> str:
        pendientes = self.pendientes()
        if not self.activa:
            cabeza = "🗓️ Agenda apagada: los reels automáticos te llegan apenas están (/agenda on para repartirlos)."
        else:
            cabeza = "🗓️ Agenda de publicación: " + ", ".join(self.cfg.horarios) + "."
        if not pendientes:
            return cabeza + "\nNo hay reels esperando su horario."
        lineas = [cabeza, f"{len(pendientes)} reel{'es' if len(pendientes) != 1 else ''} esperando:"]
        lineas += [linea_de(p, self.zona) for p in pendientes]
        lineas.append("/agenda ya para recibirlos todos ahora.")
        return "\n".join(lineas)


def linea_de(p: Proyecto, zona: ZoneInfo) -> str:
    hora = hora_de(p)
    hora = hora.astimezone(zona).strftime("%H:%M") if hora else "ahora"
    return f"• {hora} · {p.plan.titular if p.plan else p.id} ({p.id})"
