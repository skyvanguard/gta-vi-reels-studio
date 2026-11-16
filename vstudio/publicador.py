"""Programa en Facebook los reels terminados ("una maquina de generar reels").

Los reels automaticos (noticias del dia) que pasan el control (calidad.py) se programan solos en el proximo
horario libre; los que tienen algo dudoso, o que no se pudieron controlar, esperan al editor con un boton para
programarlos igual. Los que pide el editor a mano no salen solos: llevan el boton "📤 Programar en Facebook".
Un horario por reel, maximo `max_por_dia` por dia (Meta permite 30 publicaciones por API cada 24 h).

Estados de `Proyecto.publicacion`: "" (sin tocar) · "revisar" (espera al editor) · "subiendo" · "programado" ·
"retirado" (el editor lo saco: no vuelve a salir solo) · "error" (no se reintenta solo).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import calidad, facebook, montaje
from .modelos import MEDIO_IA, Estado, Proyecto
from .servicio import EdicionInvalida, Servicio

log = logging.getLogger(__name__)

INTERVALO_S = 60
MARGEN_MIN = 30  # Meta pide mas de 10 minutos; la subida puede tardar varios
DIAS_MAX = 28  # y menos de 29 dias
LIMITE = 200
MAX_ANTIGUEDAD_H = 24  # los automaticos terminados hace mas que esto ya no salen solos


class SinFacebook(RuntimeError):
    pass


def proximo_horario(ahora: datetime, horarios: tuple[str, ...], ocupados: set[datetime],
                    max_por_dia: int) -> datetime:
    """El primer horario libre desde `ahora` + MARGEN_MIN (hoy o los dias siguientes)."""
    zona = ahora.tzinfo
    horas = sorted(time(int(h.split(":")[0]), int(h.split(":")[1])) for h in horarios)
    desde = ahora + timedelta(minutes=MARGEN_MIN)
    for dia in range(DIAS_MAX):
        fecha = ahora.date() + timedelta(days=dia)
        del_dia = sum(1 for o in ocupados if o.astimezone(zona).date() == fecha)
        for hora in horas:
            candidato = datetime.combine(fecha, hora, zona)
            if candidato >= desde and candidato not in ocupados and del_dia < max_por_dia:
                return candidato
    raise SinFacebook(f"no hay horarios libres en los próximos {DIAS_MAX} días")


class Publicador(threading.Thread):
    def __init__(self, servicio: Servicio, cliente=None):
        super().__init__(daemon=True, name="publicador")
        self.s, self.cfg = servicio, servicio.cfg.facebook
        self.zona = ZoneInfo(servicio.cfg.zona_horaria)
        self._cliente = cliente  # para tests; si no, se arma con el token de secrets/
        self._parar = threading.Event()
        self._lock = threading.Lock()  # un reel a la vez (subidas de ~40 MB)

    def _nuevo_cliente(self):
        if self._cliente is not None:
            return self._cliente
        pagina = facebook.leer_pagina(self.cfg.token_archivo, self.cfg.pagina_archivo)
        if pagina is None:
            raise SinFacebook("Facebook todavía no está configurado (falta el token de la página)")
        return facebook.ClienteFacebook(pagina)

    def _soltar(self, cliente) -> None:
        if cliente is not self._cliente:
            cliente.cerrar()

    def configurado(self) -> bool:
        if self._cliente is not None:
            return True
        try:
            return facebook.leer_pagina(self.cfg.token_archivo, self.cfg.pagina_archivo) is not None
        except (OSError, ValueError, KeyError):
            return False

    # ---------- ciclo ----------
    def run(self) -> None:
        while not self._parar.is_set():
            try:
                self.ciclo()
            except Exception:  # noqa: BLE001 - el hilo no puede morir
                log.exception("error en el publicador")
            self._parar.wait(INTERVALO_S)

    def ciclo(self) -> None:
        if not (self.cfg.activo and self.configurado()):
            return
        limite_viejo = datetime.now(self.zona) - timedelta(hours=MAX_ANTIGUEDAD_H)
        for p in self.s.almacen.listar((Estado.ENTREGADO,), limite=LIMITE, recientes=True):
            if not (p.automatico and not p.publicacion):
                continue
            if datetime.fromisoformat(p.actualizado) < limite_viejo:
                continue  # una noticia de hace dias no sale sola (p. ej. al configurar Facebook tarde): con 📤
            try:  # un reel con problemas no frena a los demas ni se reintenta cada minuto
                self.revisar(p)
            except Exception as exc:  # noqa: BLE001
                log.exception("no pude publicar %s", p.id)
                self._marcar(p.id, "error")
                self._avisar(p.chat_id, f"⚠️ {p.id}: no lo pude programar en Facebook ({exc}). Probá con 📤.",
                             [[("📤 Programar en Facebook", f"pub:{p.id}")]])

    def revisar(self, p: Proyecto) -> None:
        """El control de un reel automatico: si esta limpio se programa; si no, espera al editor."""
        carpeta = self.s.carpeta(p)
        motivos = calidad.motivos_del_plan(p, montaje.duracion(carpeta / p.reel))
        # el video real y las fotos de la noticia no se deforman: solo se mira lo que animo la IA
        escenas = [(e.n, carpeta / e.clip) for e in p.plan.escenas if e.medio == MEDIO_IA]
        cuadros = calidad.cuadros(escenas, carpeta / "control")
        if len(cuadros) < len(escenas):
            motivos.append(f"no pude sacar cuadros de {len(escenas) - len(cuadros)} escena(s) para revisarlas")
        if escenas:
            with self.s.lock_llm:  # el Qwen tiene un solo slot
                self.s._esperar_llm()
                defectos = calidad.escenas_defectuosas(self.s.llm, cuadros)
            motivos += ["no pude revisar las imágenes"] if defectos is None else defectos
        if motivos:
            self._marcar(p.id, "revisar")
            self._avisar_revision(p, motivos)
            return
        self.programar(p.id)

    # ---------- programar ----------
    def ocupados(self) -> set[datetime]:
        """Los horarios ya tomados (en cualquier estado del proyecto: uno que se rearma sigue programado)."""
        return {datetime.fromisoformat(p.fb_hora) for p in self.s.almacen.listar(limite=LIMITE, recientes=True)
                if p.publicacion in ("programado", "subiendo") and p.fb_hora}

    def programar(self, id_: str, ahora: datetime | None = None) -> datetime:
        """Sube el reel y lo programa en el proximo horario libre. Devuelve la hora (y avisa al editor)."""
        with self._lock:
            p = self.s.almacen.obtener(id_)
            if p is None or p.estado != Estado.ENTREGADO or not p.reel:
                raise EdicionInvalida(f"{id_} no es un reel terminado")
            if p.publicacion in ("programado", "subiendo"):
                raise EdicionInvalida(f"{id_} ya está programado para {self._hora(p.fb_hora)}")
            cuando = proximo_horario(ahora or datetime.now(self.zona), self.cfg.horarios, self.ocupados(),
                                     self.cfg.max_por_dia)
            cliente = self._nuevo_cliente()
            subido = {}

            def al_iniciar(video_id: str) -> None:  # queda anotado antes de subir: si algo falla, se sabe cual
                subido["id"] = video_id
                self.s.almacen.actualizar(id_, lambda a: a.con(publicacion="subiendo", fb_id=video_id,
                                                               fb_hora=cuando.isoformat()))
            try:
                cliente.publicar_reel(self.s.carpeta(p) / p.reel, calidad.texto_para_publicar(p.plan.texto_post),
                                      cuando, con_ia=self.cfg.con_ia, al_iniciar=al_iniciar)
            except facebook.FacebookError as exc:
                if subido.get("id") and cliente.ya_salio(subido["id"]):
                    log.warning("Meta aceptó %s aunque la respuesta falló (%s)", id_, exc)
                else:
                    self._descartar(cliente, id_, subido.get("id"))
                    self._avisar(p.chat_id, f"⚠️ {id_}: Facebook no lo aceptó ({exc}). Probá de nuevo con 📤.",
                                 [[("📤 Programar en Facebook", f"pub:{id_}")]])
                    raise
            finally:
                self._soltar(cliente)
            self.s.almacen.actualizar(id_, lambda a: a.con(publicacion="programado"))
            self._avisar(p.chat_id, f"📤 {id_} ({p.plan.titular}) programado en Facebook para "
                                    f"{self._hora(cuando.isoformat())}.")
            return cuando

    def _descartar(self, cliente, id_: str, video_id: str | None) -> None:
        """Lo que quedo a medio subir se borra en Meta (si se puede) y el reel queda en "error"."""
        if video_id:
            try:
                cliente.borrar(video_id)
            except facebook.FacebookError as exc:
                log.warning("no pude borrar el video a medio subir %s: %s", video_id, exc)
        self.s.almacen.actualizar(id_, lambda a: a.con(publicacion="error", fb_id="", fb_hora=""))

    def despublicar(self, id_: str) -> None:
        """Saca de Facebook un reel programado (si ya salio, lo borra). Queda "retirado": no vuelve a salir solo."""
        with self._lock:
            p = self.s.almacen.obtener(id_)
            if p is None or not p.fb_id:
                raise EdicionInvalida(f"{id_} no está en Facebook")
            cliente = self._nuevo_cliente()
            try:
                cliente.borrar(p.fb_id)
            finally:
                self._soltar(cliente)
            self.s.almacen.actualizar(id_, lambda a: a.con(publicacion="retirado", fb_id="", fb_hora=""))

    # ---------- avisos ----------
    def _marcar(self, id_: str, estado: str) -> None:
        self.s.almacen.actualizar(id_, lambda a: a.con(publicacion=estado))

    def _hora(self, iso: str) -> str:
        cuando = datetime.fromisoformat(iso).astimezone(self.zona)
        hoy = datetime.now(self.zona).date()
        dia = "hoy" if cuando.date() == hoy else "mañana" if cuando.date() == hoy + timedelta(days=1) else \
            f"el {cuando:%d/%m}"
        return f"{dia} a las {cuando:%H:%M}"

    def _avisar(self, chat: int, texto: str, botones: list | None = None) -> None:
        try:
            self.s.notificador.mensaje(chat, texto, botones)
        except Exception as exc:  # noqa: BLE001
            log.warning("no pude avisar al editor: %s", exc)

    def _avisar_revision(self, p: Proyecto, motivos: list[str]) -> None:
        self._avisar(p.chat_id, f"✋ {p.id} ({p.plan.titular}) no sale solo a Facebook: {'; '.join(motivos)}. "
                                "Miralo y, si está bien, programalo.",
                     [[("📤 Programar igual", f"pub:{p.id}")]])

    def resumen(self) -> str:
        if not self.configurado():
            return "📤 Facebook: todavía no está configurado (falta el token de la página)."
        ahora = datetime.now(self.zona)
        recientes = self.s.almacen.listar(limite=LIMITE, recientes=True)
        programados = sorted((datetime.fromisoformat(p.fb_hora), p) for p in recientes
                             if p.publicacion == "programado" and p.fb_hora
                             and datetime.fromisoformat(p.fb_hora) > ahora)
        revisar = [p for p in recientes if p.publicacion == "revisar" and p.estado == Estado.ENTREGADO]
        lineas = [f"📤 Facebook: {'automático' if self.cfg.activo else 'solo a mano'} · horarios "
                  f"{', '.join(self.cfg.horarios)}"]
        lineas += [f"• {self._hora(c.isoformat())}: {p.plan.titular} ({p.id})" for c, p in programados] or \
            ["Nada programado."]
        if revisar:
            lineas.append("Esperan tu revisión: " + ", ".join(f"{p.id} ({p.plan.titular})" for p in revisar))
        return "\n".join(lineas)
