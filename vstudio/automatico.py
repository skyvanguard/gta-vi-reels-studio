"""Reels automaticos con las noticias de GTA VI del dia.

Todos los dias a la hora configurada (05:00, hora local, por defecto): junta las noticias de las ultimas
horas (noticias.py), el Qwen agrupa las que cuentan lo mismo, descarta lo que la pagina ya publico y lo que
no es de GTA VI, y ordena por interes. Se arman todos los planes primero (el Qwen esta libre) y despues se
aprueban juntos: el trabajador los genera en un solo turno de GPU y los entrega como siempre (video, avisos
y el texto para publicar). El editor elige "directo al video": no hay aprobacion.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import formatos, noticias, verificador
from .acceso import Acceso
from .llm import LlmError, parte_texto
from .modelos import Estado
from .servicio import Servicio
from .trabajador import escenas_pendientes, minutos_de_turno

log = logging.getLogger(__name__)

INTERVALO_S = 60
DIAS_DE_MEMORIA = 10  # lo publicado en estos dias no se repite
MAX_YA_PUBLICADO = 60
MAX_TOKENS = 8000
PRESUPUESTO_RAZONAMIENTO = 4000
INTENTOS_POR_NOTICIA = 2  # si la nota elegida no se puede leer, se prueba con otro medio que cuenta lo mismo

PROMPT = """Sos el editor de una pagina de fans de GTA VI en Facebook (en espanol). Fecha de hoy: {hoy}.
Te paso las NOTICIAS de las ultimas horas (numeradas: medio, fecha, titulo y resumen) y lo que la pagina YA
PUBLICO en los ultimos dias. Arma la lista de noticias para hacer un reel con cada una:
- Agrupa las que cuentan el mismo hecho (en distintos medios o idiomas): una sola por hecho.
- Descarta lo que ya se publico, salvo que haya un dato nuevo importante (decilo en "por_que").
- Descarta lo que no es sobre GTA VI (solo lo nombra de pasada: ventas de consolas, acciones de bolsa, otros
  juegos), las listas, guias y opiniones sin noticia, y lo de muy poco interes para los fans.
- Lo que publica "Rockstar Games (oficial)" es el anuncio oficial: va primero y es la mejor fuente del hecho.
- Ordena de mas a menos interesante. Como maximo {maximo}. Si no hay nada nuevo, la lista va vacia.
Para cada una: "fuente": el numero de la noticia con mas datos (un medio conocido; en espanol si hay), "otras": los
numeros de las demas que cuentan lo mismo, "titulo": titulo corto en espanol (maximo 12 palabras), "por_que": por
que vale un reel, en una frase, y "formato": como conviene contarla, uno de estos (ante la duda, "noticia"):
{formatos}
Devolve SOLO un objeto JSON:
{{"elegidas": [{{"fuente": 3, "otras": [7, 12], "titulo": "...", "por_que": "...", "formato": "noticia"}}]}}"""


@dataclass(frozen=True)
class Elegida:
    titulo: str
    por_que: str
    notas: tuple[noticias.Candidata, ...]  # la elegida primero; despues las otras que cuentan lo mismo
    formato: str = formatos.NOTICIA  # como se cuenta (formatos.py)


def _lista_de_noticias(candidatas: list[noticias.Candidata], zona: ZoneInfo) -> str:
    return "\n".join(f"{n}. [{c.medio}, {c.fecha.astimezone(zona):%d/%m %H:%M}] {c.titulo} — {c.resumen[:240]}"
                     for n, c in enumerate(candidatas, start=1))


def elegir(cliente, candidatas: list[noticias.Candidata], ya_publicado: list[str], hoy: date, maximo: int,
           zona: ZoneInfo, con_formatos: bool = True) -> list[Elegida]:
    """Las noticias para hacer reels, en orden de interes. Levanta LlmError si el Qwen no responde.
    `con_formatos`: hasta la mitad puede salir en un formato que no es la noticia de siempre."""
    if not candidatas:
        return []
    publicado = "\n".join(f"- {t}" for t in ya_publicado[:MAX_YA_PUBLICADO]) or "(nada)"
    partes = [parte_texto(f"NOTICIAS:\n{_lista_de_noticias(candidatas, zona)}"),
              parte_texto(f"YA PUBLICADO:\n{publicado}")]
    sistema = PROMPT.format(hoy=hoy.isoformat(), maximo=maximo, formatos=formatos.para_el_editor())
    datos = cliente.chat_json(sistema, partes, max_tokens=MAX_TOKENS,
                              esfuerzo="low", semilla=secrets.randbelow(2**31), reintentos=1,
                              razonamiento_max=PRESUPUESTO_RAZONAMIENTO)
    lista = datos.get("elegidas") if isinstance(datos, dict) else None
    if not isinstance(lista, list):
        raise LlmError(f"el editor no devolvio la lista de noticias: {str(datos)[:200]}")
    elegidas, usadas = [], set()
    for e in lista:
        if not isinstance(e, dict):
            continue
        try:
            principal = int(e.get("fuente"))
            otras = [int(o) for o in e.get("otras", []) if str(o).strip().lstrip("-").isdigit()]
        except (TypeError, ValueError):
            continue
        indices = [i for i in [principal, *otras] if 1 <= i <= len(candidatas) and i not in usadas]
        titulo = " ".join(str(e.get("titulo", "")).split())[:120]
        if not indices or indices[0] != principal or not titulo:
            continue  # la principal no existe o ya la uso otra noticia
        usadas.update(indices)
        elegidas.append(Elegida(titulo=titulo, por_que=" ".join(str(e.get("por_que", "")).split())[:200],
                                notas=tuple(candidatas[i - 1] for i in indices),
                                formato=str(e.get("formato", formatos.NOTICIA))))
    elegidas = elegidas[:maximo]
    claves = formatos.repartir([e.formato for e in elegidas], len(elegidas) // 2 if con_formatos else 0)
    return [replace(e, formato=clave) for e, clave in zip(elegidas, claves)]


def _formato_en_lista(e: Elegida) -> str:
    return "" if e.formato == formatos.NOTICIA else f" · {formatos.formato(e.formato).nombre}"


class Automatico(threading.Thread):
    """Corre una vez por dia a la hora configurada; /auto ya lo corre en el momento."""

    def __init__(self, servicio: Servicio, acceso: Acceso, buscar=noticias.buscar):
        super().__init__(daemon=True, name="automatico")
        self.s, self.acceso, self._buscar = servicio, acceso, buscar
        self.cfg = servicio.cfg.automatico
        self.zona = ZoneInfo(servicio.cfg.zona_horaria)
        self.archivo = servicio.cfg.datos / "automatico.json"
        self._parar = threading.Event()
        self._corriendo = threading.Lock()

    # ---------- estado ----------
    def leer(self) -> dict:
        try:
            datos = json.loads(self.archivo.read_text(encoding="utf-8"))
            return datos if isinstance(datos, dict) else {}
        except (OSError, ValueError):
            return {}

    def _guardar(self, datos: dict) -> None:
        self.archivo.parent.mkdir(parents=True, exist_ok=True)
        temporal = self.archivo.with_suffix(".tmp")
        temporal.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
        temporal.replace(self.archivo)

    @property
    def activo(self) -> bool:
        return bool(self.leer().get("activo", self.cfg.activo))

    def activar(self, activo: bool) -> None:
        self._guardar({**self.leer(), "activo": activo})

    def hora(self) -> time:
        h, _, m = self.cfg.hora.partition(":")
        return time(int(h), int(m or 0))

    def toca(self, ahora: datetime) -> bool:
        """Si ya es la hora y hoy todavia no corrio."""
        return self.activo and ahora.time() >= self.hora() and self.leer().get("ultima") != ahora.date().isoformat()

    def destinatario(self) -> int | None:
        """El chat del editor (en un chat privado de Telegram, el ID del chat es el del usuario)."""
        usuarios = sorted(self.acceso.usuarios())
        return usuarios[0] if usuarios else None

    def ya_publicado(self, hoy: date) -> list[str]:
        """Lo publicado en los ultimos dias: lo automatico y lo que el editor pidio a mano."""
        desde = (hoy - timedelta(days=DIAS_DE_MEMORIA)).isoformat()
        titulos = [p["titulo"] for p in self.leer().get("publicadas", []) if p.get("fecha", "") >= desde]
        for p in self.s.almacen.listar(limite=500, recientes=True):
            if p.creado[:10] < desde or p.estado == Estado.CANCELADO:
                continue
            titulo = p.fuente.titulo or (p.plan.titular if p.plan else "") or p.fuente.nota_usuario[:100]
            if titulo and titulo not in titulos:
                titulos.append(titulo)
        return titulos

    # ---------- ciclo ----------
    def run(self) -> None:
        if "ultima" not in self.leer():  # la primera vez (al instalarlo a la noche) no arranca en el momento:
            self._guardar({**self.leer(), "ultima": datetime.now(self.zona).date().isoformat()})  # /auto ya
        while not self._parar.is_set():
            try:
                if self.toca(datetime.now(self.zona)):
                    self.ejecutar()
            except Exception:  # noqa: BLE001 - el hilo no puede morir; el error queda en el log
                log.exception("error en los reels automaticos")
            self._parar.wait(INTERVALO_S)

    def detener(self) -> None:
        self._parar.set()

    def ejecutar(self) -> str:
        """Busca, elige, planifica y manda a generar. Devuelve el resumen que se le manda al editor."""
        if not self._corriendo.acquire(blocking=False):
            return "ya estoy buscando las noticias de hoy"
        try:
            return self._ejecutar()
        finally:
            self._corriendo.release()

    def _avisar(self, chat: int, texto: str) -> None:
        try:
            self.s.notificador.mensaje(chat, texto)
        except Exception as exc:  # noqa: BLE001 - un aviso que no sale no corta el trabajo
            log.warning("no pude avisar al editor: %s", exc)

    def _ejecutar(self) -> str:
        ahora = datetime.now(self.zona)
        hoy = ahora.date()
        self._guardar({**self.leer(), "ultima": hoy.isoformat()})  # aunque falle: no reintenta cada minuto
        chat = self.destinatario()
        if chat is None:
            log.warning("reels automaticos: no hay un editor vinculado")
            return "no hay un editor vinculado"
        candidatas = self._buscar(ahora, self.cfg.horas_atras)
        try:
            with self.s.lock_llm:  # el Qwen tiene un solo slot: no se pisa con un plan del bot
                self.s._esperar_llm()
                elegidas = elegir(self.s.llm, candidatas, self.ya_publicado(hoy), hoy, self.cfg.maximo, self.zona,
                                  con_formatos=self.cfg.formatos)
        except LlmError as exc:
            texto = f"🗞️ No pude elegir las noticias de hoy (el Qwen no respondió: {exc}). Probá /auto ya."
            self._avisar(chat, texto)
            return texto
        if not elegidas:
            texto = f"🗞️ Hoy no hay noticias nuevas de GTA VI para hacer reels (revisé {len(candidatas)})."
            self._avisar(chat, texto)
            return texto
        lista = "\n".join(f"{n}. {e.titulo} ({e.notas[0].medio}){_formato_en_lista(e)}"
                          for n, e in enumerate(elegidas, start=1))
        self._avisar(chat, f"🗞️ Noticias de GTA VI de hoy: elegí {len(elegidas)} de {len(candidatas)} notas "
                           f"(agrupé las repetidas y saqué lo ya publicado):\n{lista}\n\nArmo los planes y después "
                           "los genero todos juntos; te llegan listos para publicar.")
        listos, fallidas = self._planificar(chat, elegidas, hoy)
        for p in listos:
            self.s.aprobar(p.id)
        # solo las escenas que anima la IA pasan por la GPU: las de video real y las fotos ya tienen su clip
        clips = sum(len(escenas_pendientes(p, self.s.carpeta(p))) for p in listos)
        minutos = minutos_de_turno(clips, len(self.s.cfg.gpu.comfy_puertos)) if clips else 0
        texto = (f"🎬 {len(listos)} reel{'es' if len(listos) != 1 else ''} en cola: {clips} clip(s) de IA "
                 f"(~{minutos} min de GPU, el Qwen queda apagado mientras tanto); el resto es video real y fotos.")
        if fallidas:
            texto += "\nNo pude armar: " + "; ".join(fallidas)
        self._avisar(chat, texto)
        return texto

    def _planificar(self, chat: int, elegidas: list[Elegida], hoy: date) -> tuple[list, list[str]]:
        listos, fallidas = [], []
        publicadas = self.leer().get("publicadas", [])
        for e in elegidas:
            p = None
            for nota in e.notas[:INTENTOS_POR_NOTICIA]:
                texto = f"{nota.url}\n\n{nota.titulo}\n{nota.resumen}"
                p = self.s.crear(chat, texto, [], segundos=formatos.formato(e.formato).segundos or self.cfg.segundos,
                                 automatico=True, medio=nota.medio, formato=e.formato)
                if p.estado == Estado.ESPERANDO_APROBACION:
                    p = self._sin_datos_dudosos(p)
                    break
                log.warning("reel automatico de %s fallo con %s: %s", e.titulo, nota.url, p.error)
            if p is not None and p.estado == Estado.ESPERANDO_APROBACION:
                listos.append(p)
                publicadas.append({"fecha": hoy.isoformat(), "titulo": e.titulo, "url": e.notas[0].url, "id": p.id})
            else:
                fallidas.append(f"{e.titulo} ({p.error[:80] if p else 'sin nota'})")
        desde = (hoy - timedelta(days=DIAS_DE_MEMORIA)).isoformat()
        self._guardar({**self.leer(), "publicadas": [x for x in publicadas if x.get("fecha", "") >= desde]})
        return listos, fallidas

    def _sin_datos_dudosos(self, p):
        """Si la verificacion encontro datos sin respaldo, se reescribe una vez sin ellos (y se vuelve a
        verificar). Lo que quede, frena la publicacion automatica (calidad.py)."""
        dudas = verificador.dudas_de(p.plan.advertencia if p.plan else "")
        if not dudas:
            return p
        try:
            return self.s.reescribir(p.id, evitar=dudas)
        except Exception as exc:  # noqa: BLE001 - queda el plan como estaba (y no sale solo)
            log.warning("no pude reescribir %s sin los datos dudosos: %s", p.id, exc)
            return p

    def resumen(self) -> str:
        ahora = datetime.now(self.zona)
        datos = self.leer()
        hoy = [p for p in datos.get("publicadas", []) if p.get("fecha") == ahora.date().isoformat()]
        lineas = [f"🗞️ Reels automáticos: {'activados' if self.activo else 'apagados'} · todos los días a las "
                  f"{self.cfg.hora} · {self.cfg.segundos} s · hasta {self.cfg.maximo} por día",
                  f"Última vez: {datos.get('ultima', 'nunca')}"]
        lineas += [f"Hoy: {p['titulo']} ({p['id']})" for p in hoy]
        lineas.append("/auto ya (buscar ahora) · /auto on · /auto off")
        return "\n".join(lineas)
