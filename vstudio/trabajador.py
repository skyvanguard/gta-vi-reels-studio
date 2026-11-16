"""Cola de produccion: voz (CPU) -> turno de GPU (clips) -> devolver el Qwen -> montaje -> entrega.

Reglas de robustez: un proyecto que falla no frena a los demas; ninguna excepcion mata el hilo;
si un turno no puede abrirse se espera BACKOFF antes de reintentar (no hay bucles de apagados del Qwen);
al arrancar se reencolan los proyectos que quedaron a mitad.
"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import secrets
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import biblioteca, botones, calidad, comfy, facebook, fuentes, montaje, musica, pronunciacion, subtitulos, voz
from .agenda import Agenda, hora_de, linea_de
from .gpu import NOMBRE_MARCA, MonitorActividad, TurnoError, TurnoGpu, restaurar_si_hace_falta
from .modelos import MEDIO_IA, Escena, Estado, Proyecto, reemplazar_escena
from .servicio import EdicionInvalida, Servicio

log = logging.getLogger(__name__)

INTERVALO_S = 30
BACKOFF_TURNO_S = 600
MINUTOS_POR_CLIP = 7.5  # medido: 433-486 s por clip 720x1280 con LoRA 4 pasos (15 clips: 58,5 min)
MARGEN_TURNO = 1.25  # el turno puede durar hasta 25 % mas de lo estimado antes de cortar
MAX_BYTES_TELEGRAM = 49 * 1024 * 1024
REINTENTOS_ENVIO = 3
LIMITE_COLA = 500
AVISO_DEFORME = "Puede verse deforme: "
AVISO_SIN_REVISAR = "No se pudo revisar si hay escenas deformes."
MAX_SEGUNDOS_TITULAR_FIJO = 15  # en los reels cortos (cuenta regresiva) el titular queda todo el reel


class ColaOcupada(RuntimeError):
    pass


@contextmanager
def bloqueo_de_proceso(ruta: Path):
    """Un solo trabajador por maquina (servicio o CLI). En Windows (tests) no bloquea."""
    if os.name == "nt":
        yield
        return
    import fcntl
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with ruta.open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ColaOcupada("ya hay otro trabajador procesando la cola (¿el servicio?)") from exc
        yield


def escenas_pendientes(p: Proyecto, carpeta: Path) -> list[Escena]:
    return [e for e in p.plan.escenas if not e.clip or not (carpeta / e.clip).exists()]


class Trabajador(threading.Thread):
    def __init__(self, servicio: Servicio):
        super().__init__(daemon=True, name="trabajador")
        self.s, self.cfg = servicio, servicio.cfg
        self.monitor = MonitorActividad(servicio.llm.metricas, self.cfg.gpu.sesiones_app,
                                        llm_arriba=servicio.llm.disponible)
        self._forzar = threading.Event()
        self._parar = threading.Event()
        self._avisados: set[str] = set()
        self._reintento_turno = 0.0
        self.estado_turno = "sin turno"
        self.alerta = ""
        self.politica = self.cfg.gpu.politica  # se puede cambiar en caliente con /politica
        # la pone el arranque del bot (sin ella, todo se entrega apenas esta)
        self.agenda: Agenda | None = None

    def forzar(self) -> None:
        self._forzar.set()
        self._reintento_turno = 0.0

    def detener(self) -> None:
        self._parar.set()

    # ---------- bucle ----------
    def run(self) -> None:
        with bloqueo_de_proceso(self.cfg.datos / "trabajador.lock"):
            self.reconciliar()
            while not self._parar.is_set():
                try:
                    self.ciclo()
                except Exception:  # noqa: BLE001 - el hilo no puede morir; el error queda en el log
                    log.exception("error en el ciclo del trabajador")
                self._parar.wait(INTERVALO_S)

    def reconciliar(self) -> None:
        """Proyectos que quedaron a mitad por un corte: vuelven a la cola o a error (para replanificar)."""
        for p in self.s.almacen.listar((Estado.GENERANDO, Estado.MONTANDO), limite=LIMITE_COLA):
            self.s.almacen.guardar(p.con(estado=Estado.EN_COLA))
            log.warning("proyecto %s reencolado tras un corte", p.id)
        for p in self.s.almacen.listar((Estado.PLANIFICANDO,), limite=LIMITE_COLA):
            self.s.almacen.guardar(p.con(estado=Estado.ERROR,
                                         error="se reinicio el servicio mientras se planificaba: tocá 🔁 Otro plan"))

    def _restaurar(self) -> bool:
        """Devuelve el Qwen si quedo un turno colgado. False si no se pudo (no se abre otro turno)."""
        if not (self.cfg.datos / NOMBRE_MARCA).exists():
            return True
        try:
            restaurar_si_hace_falta(self.cfg.gpu, self.cfg.datos)
            self.alerta = ""
            return not (self.cfg.datos / NOMBRE_MARCA).exists()
        except Exception as exc:  # noqa: BLE001
            self.alerta = f"⚠️ el Qwen no vuelve a levantar: {exc}"
            log.error(self.alerta)
            return False

    def _puede_abrir_turno(self) -> bool:
        if time.monotonic() < self._reintento_turno:
            return False
        if self._forzar.is_set() or self.politica == "inmediato":
            return True
        return self.monitor.minutos_inactivo() >= self.cfg.gpu.inactividad_min

    def ciclo(self) -> None:
        if not self._restaurar():
            return
        self.monitor.muestrear()
        cola = self.s.almacen.listar((Estado.EN_COLA,), limite=LIMITE_COLA)
        if not cola:
            self._forzar.clear()
            return
        # lo que no necesita clips nuevos (rehacer voz o montaje) se hace ya: no apaga el Qwen
        con_gpu = [p for p in cola if escenas_pendientes(p, self.s.carpeta(p))]
        sin_gpu = [p for p in cola if p not in con_gpu]
        if con_gpu and self._puede_abrir_turno():
            self._forzar.clear()
            lote = sin_gpu + con_gpu
        else:
            if con_gpu:
                self._avisar_espera(con_gpu)
            lote = sin_gpu
        if not lote:
            return
        self._producir(lote)

    def _producir(self, cola: list[Proyecto]) -> None:
        self.s.produciendo.set()
        try:
            # con la CPU saturada, la voz de lo que igual necesita GPU se hace en el turno, con el Qwen apagado
            voz_en_turno = self.cfg.voz.gpu_si_cpu_ocupada and self._cpu_ocupada()
            lote = [p if voz_en_turno and escenas_pendientes(p, self.s.carpeta(p)) else self._preparar_voz(p)
                    for p in cola]
            lote = self._marcar_generando(lote)
            if lote:
                self._turno(lote, voz_en_gpu=voz_en_turno)
            for p in lote:
                self._avisados.discard(p.id)
                try:
                    self._montar_y_entregar(p.id)
                except Exception as exc:  # noqa: BLE001 - un proyecto roto no frena al resto
                    log.exception("proyecto %s fallo al montar/entregar", p.id)
                    self._a_error(p.id, (Estado.GENERANDO, Estado.MONTANDO), f"falló el montaje: {exc}")
            self._avisar_programados([p.id for p in lote])
        finally:
            self.s.produciendo.clear()

    # ---------- avisos ----------
    def _avisar(self, chat_id: int, texto: str) -> None:
        try:
            self.s.notificador.mensaje(chat_id, texto)
        except Exception as exc:  # noqa: BLE001 - sin red no se frena la produccion
            log.warning("no se pudo avisar a %s: %s", chat_id, exc)

    def _avisar_espera(self, cola: list[Proyecto]) -> None:
        for p in cola:
            espera_h = (time.time() - datetime.fromisoformat(p.actualizado).timestamp()) / 3600
            if espera_h >= self.cfg.gpu.aviso_espera_h and p.id not in self._avisados:
                self._avisados.add(p.id)
                self._avisar(p.chat_id, f"⏳ {p.id}: el Qwen sigue en uso hace {espera_h:.0f} h. "
                                        "Mandá /ya si querés apagarlo y generar ahora.")

    def _a_error(self, id_: str, desde: tuple[Estado, ...], motivo: str) -> None:
        try:
            p = self.s.transicion(id_, desde, lambda a: a.con(estado=Estado.ERROR, error=motivo[:500]))
            self._avisar(p.chat_id, f"❌ {id_}: {motivo}")
        except EdicionInvalida:
            pass

    # ---------- voz ----------
    def _musica_para(self, p: Proyecto) -> Path | None:
        """El tema de fondo segun lo que cuenta el reel (None si no hay biblioteca o esta apagada)."""
        if not self.cfg.musica.activo:
            return None
        texto = " ".join([p.plan.titular, p.fuente.titulo, *(e.narracion for e in p.plan.escenas)])
        return musica.elegir(self.cfg.montaje.musica_dir, texto, biblioteca.Usos(self.cfg.datos / "musica_usos.json"))

    def _completar_musica(self) -> None:
        if not self.cfg.musica.activo:
            return
        m = self.cfg.musica
        pedidos = musica.faltantes(self.cfg.montaje.musica_dir, m.por_estilo, m.por_turno, m.segundos)
        if not pedidos:
            return
        self.estado_turno = f"turno abierto: música ({len(pedidos)} temas)"
        try:
            salieron = musica.generar(m, pedidos)
            log.info("música: %d tema(s) nuevos", len(salieron))
        except Exception as exc:  # noqa: BLE001 - sin musica nueva el turno sigue igual
            log.warning("no pude generar música en este turno: %s", exc)

    def _cpu_ocupada(self) -> bool:
        try:
            carga = os.getloadavg()[0] / (os.cpu_count() or 1)
        except (OSError, AttributeError):  # Windows no tiene getloadavg
            return False
        if carga > self.cfg.voz.umbral_carga:
            log.info("CPU ocupada (carga %.2f por núcleo): la voz va en la GPU, en el turno", carga)
            return True
        return False

    def _preparar_voz(self, p: Proyecto, en_gpu: bool = False) -> Proyecto:
        """Narra las escenas que no tienen audio. `en_gpu`: dentro del turno (Qwen apagado); si ahi falla, no
        se apaga la voz: queda para la CPU al montar."""
        if not p.con_voz or self.cfg.voz.motor == "ninguna":
            return p
        carpeta = self.s.carpeta(p)
        if all(e.audio and (carpeta / e.audio).exists() for e in p.plan.escenas):
            return p
        cfg_voz = replace(self.cfg.voz, dispositivo="cuda") if en_gpu else self.cfg.voz
        cfg_subs = replace(self.cfg.subtitulos, dispositivo="cuda") if en_gpu else self.cfg.subtitulos
        try:
            audios = voz.sintetizar(cfg_voz, [e.narracion for e in p.plan.escenas], carpeta / "voz")
            audios, dudas_voz = self._revisar_pronunciacion(p, carpeta, audios, cfg_voz, cfg_subs)
            rutas = [a.archivo.relative_to(carpeta).as_posix() for a in audios]
        except Exception as exc:  # noqa: BLE001 - sin voz el reel igual sale
            if en_gpu:
                log.warning("la voz en GPU falló en %s (%s): se hace en la CPU al montar", p.id, exc)
                return p
            log.error("voz fallo en %s: %s", p.id, exc)
            self._avisar(p.chat_id, f"⚠️ {p.id}: la voz falló, el reel va sin narración.")
            return self.s.almacen.actualizar(p.id, lambda a: a.con(con_voz=False))

        def con_audios(a: Proyecto) -> Proyecto:
            plan = a.plan
            for escena, ruta in zip(a.plan.escenas, rutas):
                plan = reemplazar_escena(plan, replace(escena, audio=ruta))
            return a.con(plan=replace(plan, advertencia=pronunciacion.con_aviso(plan.advertencia, dudas_voz)))
        return self.s.almacen.actualizar(p.id, con_audios)

    def _revisar_pronunciacion(self, p: Proyecto, carpeta: Path, audios: list[voz.Audio], cfg_voz=None,
                               cfg_subs=None) -> tuple[list[voz.Audio], str]:
        """Whisper escucha cada escena sin el guion; las que tienen palabras con tilde o enie mal dichas se
        vuelven a generar (otra semilla) y queda la toma con menos palabras dudosas. Devuelve los audios y lo que
        quedo dudoso ("escena 3: movió"; vacio si nada)."""
        cfg_voz, cfg_subs = cfg_voz or self.cfg.voz, cfg_subs or self.cfg.subtitulos
        reintentos = cfg_voz.reintentos_pronunciacion
        if reintentos <= 0:
            return audios, ""
        textos = [e.narracion for e in p.plan.escenas]
        candidatos = dict(enumerate(audios))
        mejor: dict[int, tuple[list[str], voz.Audio]] = {}
        for ronda in range(reintentos + 1):
            indices = list(candidatos)
            try:
                oidos = subtitulos.oir(cfg_subs, [candidatos[i].archivo for i in indices], carpeta / "voz")
            except subtitulos.SubtitulosError as exc:
                log.warning("sin control de pronunciacion en %s: %s", p.id, exc)
                break
            for i, oido in zip(indices, oidos):
                dudas = pronunciacion.dudosas(textos[i], oido)
                if i not in mejor or len(dudas) < len(mejor[i][0]):
                    mejor[i] = (dudas, candidatos[i])
            malas = [i for i in indices if mejor[i][0]]
            if not malas or ronda == reintentos:
                break
            log.info("%s: rehago la voz de %s (%s)", p.id, [i + 1 for i in malas],
                     "; ".join(f"{i + 1}: {', '.join(mejor[i][0])}" for i in malas))
            nuevos = voz.sintetizar(cfg_voz, [textos[i] for i in malas], carpeta / "voz" / f"toma_{ronda + 2}",
                                    semilla=1000 * (ronda + 1))
            candidatos = dict(zip(malas, nuevos))
        quedan = {i + 1: dudas for i, (dudas, _) in mejor.items() if dudas}
        detalle = "; ".join(f"escena {n}: {', '.join(d)}" for n, d in sorted(quedan.items()))
        if quedan:
            self._avisar(p.chat_id, f"🗣️ {p.id}: la voz puede estar diciendo mal ({detalle}). Si se nota, cambiá "
                                    "la palabra con /narracion o probá otra toma con /revoz.")
        return [mejor[i][1] if i in mejor else audios[i] for i in range(len(audios))], detalle

    # ---------- turno de GPU ----------
    def _marcar_generando(self, lote: list[Proyecto]) -> list[Proyecto]:
        marcados = []
        for p in lote:
            try:
                marcados.append(self.s.transicion(p.id, (Estado.EN_COLA,), lambda a: a.con(estado=Estado.GENERANDO)))
            except EdicionInvalida:
                log.info("proyecto %s ya no esta en cola (¿cancelado?)", p.id)
        return marcados

    def _turno(self, lote: list[Proyecto], voz_en_gpu: bool = False) -> None:
        tareas: queue.Queue = queue.Queue()
        for p in lote:
            for e in escenas_pendientes(p, self.s.carpeta(p)):
                tareas.put((p.id, e.n))
        total = tareas.qsize()
        if total == 0:
            return
        minutos = minutos_de_turno(total, len(self.cfg.gpu.comfy_puertos))
        chats = {p.chat_id for p in lote}
        for chat in chats:
            self._avisar(chat, f"🎬 Genero {total} clip(s). El Qwen queda apagado ~{minutos} min.")
        # el tope protege al que usa el Qwen de un turno trabado, pero no puede cortar un lote largo que va bien
        limite = time.monotonic() + max(self.cfg.gpu.max_turno_min, minutos * MARGEN_TURNO) * 60
        try:
            plantilla = comfy.plantilla_para(self.cfg.video, self.cfg.gpu.comfy_dir)  # antes de apagar el Qwen
            with self.s.lock_llm, TurnoGpu(self.cfg.gpu, self.cfg.datos):
                self._completar_musica()  # mientras falten temas, unos pocos por turno (GPU libre)
                if voz_en_gpu:  # antes de los clips: con el Qwen apagado la GPU esta libre (Wan carga despues)
                    self.estado_turno = "turno abierto: voz en la GPU"
                    for p in lote:
                        actual = self.s.almacen.obtener(p.id)
                        if actual is not None:
                            self._preparar_voz(actual, en_gpu=True)
                self.estado_turno = f"turno abierto: {total} clip(s)"
                hilos = [threading.Thread(target=self._consumir, args=(tareas, gpu, puerto, plantilla, limite),
                                          name=f"gpu{gpu}") for gpu, puerto in enumerate(self.cfg.gpu.comfy_puertos)]
                for h in hilos:
                    h.start()
                for h in hilos:
                    h.join()
        except TurnoError as exc:
            self.alerta = f"⚠️ {exc}"
            for chat in chats:
                self._avisar(chat, f"⚠️ Los clips están, pero el Qwen no volvió: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - no se pudo abrir el turno: se reintenta mas tarde
            log.exception("no se pudo abrir el turno de GPU")
            self._reintento_turno = time.monotonic() + BACKOFF_TURNO_S
            for chat in chats:
                self._avisar(chat, f"⚠️ No pude preparar las GPU ({exc}). Reintento en 10 min.")
            return
        finally:
            self.estado_turno = "sin turno"
        for chat in chats:
            self._avisar(chat, "✅ El Qwen está de vuelta. Armo el reel.")

    def _consumir(self, tareas: queue.Queue, gpu: int, puerto: int, plantilla: dict, limite: float) -> None:
        while time.monotonic() < limite:
            try:
                id_, n = tareas.get_nowait()
            except queue.Empty:
                return
            try:
                cambio = self._generar_escena(id_, n, gpu, f"http://127.0.0.1:{puerto}", plantilla)
            except Exception as exc:  # noqa: BLE001 - cualquier falla queda en la escena, no mata el hilo
                log.exception("clip %s/%d fallo", id_, n)
                cambio = {"error": str(exc)[:300] or type(exc).__name__}
            try:
                self.s.almacen.actualizar(id_, lambda a: a.con(plan=reemplazar_escena(
                    a.plan, replace(next(e for e in a.plan.escenas if e.n == n), **cambio))))
            except (KeyError, StopIteration):
                log.error("no se pudo guardar el resultado de %s/%d", id_, n)

    def _generar_escena(self, id_: str, n: int, gpu: int, servidor: str, plantilla: dict) -> dict:
        p = self.s.almacen.obtener(id_)
        escena = next(e for e in p.plan.escenas if e.n == n)
        carpeta = self.s.carpeta(p)
        if escena.medio != MEDIO_IA:  # su clip se arma con el plan: si no esta, no se le puede pedir a Wan
            raise RuntimeError("falta el clip de video real de esta escena: pedí 🖼️ Otras imágenes")
        pedido = comfy.PedidoClip(
            imagen=carpeta / escena.imagen, prompt=escena.motion_prompt, negativo_extra=escena.negative_extra,
            ancho=self.cfg.video.ancho, alto=self.cfg.video.alto,
            cuadros=self.cfg.video.cuadros,
            seed=escena.seed, prefijo=f"{p.id}_e{n}")
        r = comfy.generar_clip(servidor, gpu, self.cfg.gpu.comfy_dir, plantilla, pedido,
                               carpeta / f"clip_{n}.mp4", self.cfg.video.timeout_clip_s)
        (carpeta / f"clip_{n}.json").write_text(json.dumps(
            {"gpu": gpu, "segundos": r.segundos, "vram_pico_mib": r.vram_pico_mib, "seed": escena.seed,
             "prompt_id": r.prompt_id, "workflow": r.workflow}, ensure_ascii=False, indent=2))
        return {"clip": r.ruta.name, "segundos_gen": r.segundos, "vram_pico_mib": r.vram_pico_mib, "error": ""}

    # ---------- montaje y entrega ----------
    def _montar_y_entregar(self, id_: str) -> None:
        p = self.s.almacen.obtener(id_)
        carpeta = self.s.carpeta(p)
        faltan = escenas_pendientes(p, carpeta)
        fallidas = [e for e in faltan if e.error]
        if fallidas:
            errores = "; ".join(f"escena {e.n}: {e.error}" for e in fallidas)
            self._a_error(id_, (Estado.GENERANDO,), f"fallaron clips ({errores}). Probá /regenerar {id_} N")
            return
        if faltan:  # no se llego a generar (tiempo del turno o turno que no abrio): sigue en cola
            self.s.transicion(id_, (Estado.GENERANDO,), lambda a: a.con(estado=Estado.EN_COLA))
            return
        if self.cfg.video.revisar_deformes and self._regenerar_deformes(p, carpeta):
            return  # volvio a la cola: el proximo turno rehace esas escenas y se vuelve a mirar
        p = self.s.transicion(id_, (Estado.GENERANDO,), lambda a: a.con(estado=Estado.MONTANDO))
        if p.con_voz and any(not e.audio or not (carpeta / e.audio).exists() for e in p.plan.escenas):
            p = self._preparar_voz(p)  # la voz se pidio con el proyecto ya en la cola (despues de narrar el lote)
        con_subtitulos = p.con_voz and p.muestra_textos and self.cfg.subtitulos.activo
        oidas = self._palabras_oidas(p, carpeta) if con_subtitulos else [()] * len(p.plan.escenas)
        for e in p.plan.escenas:
            faltan = [x for x in e.extras if not (carpeta / x).exists()]
            if faltan:
                log.warning("%s escena %d: faltan tomas extra (%s): sale con menos cortes", p.id, e.n, faltan)
        escenas = [montaje.EscenaMontaje(
            clip=carpeta / e.clip, texto=e.texto,
            audio=(carpeta / e.audio) if p.con_voz and e.audio else None,
            narracion=e.narracion if con_subtitulos else "", palabras=palabras,
            extras=tuple(carpeta / x for x in e.extras if (carpeta / x).exists()))
            for e, palabras in zip(p.plan.escenas, oidas)]
        salida = carpeta / f"reel_{p.id}.mp4"
        info = montaje.armar_reel(escenas, p.plan.titular, carpeta, salida, self.cfg.montaje.fuentes_dir,
                                  self.cfg.montaje.marca_texto, self._musica_para(p),
                                  self.cfg.montaje.volumen_musica, minimo_s=self.s.piso(p),
                                  titular_fijo=p.segundos_objetivo <= MAX_SEGUNDOS_TITULAR_FIJO,
                                  con_titular=p.muestra_titular, con_textos=p.muestra_textos,
                                  medio=fuentes.nombre_del_medio(p.fuente) if self.cfg.montaje.sello_fuente else "",
                                  cortes_rapidos=self.cfg.montaje.cortes_rapidos,
                                  barra=self.cfg.montaje.barra_progreso, efectos_sonido=self.cfg.montaje.efectos,
                                  volumen_efectos=self.cfg.montaje.volumen_efectos)
        hora = self.agenda.horario_para() if self._va_a_la_agenda(p) else None
        if hora is not None:  # los automaticos llegan repartidos en el dia: este espera su horario
            try:
                self.s.transicion(id_, (Estado.MONTANDO,), lambda a: a.con(
                    estado=Estado.PROGRAMADO, reel=salida.name, error="", enviar_a=hora.isoformat()))
            except EdicionInvalida:
                log.info("proyecto %s cancelado mientras se armaba", id_)
            return
        # ENTREGADO recien cuando Telegram lo recibio: una vez un reinicio del servicio corto la subida de un
        # reel ya marcado como entregado y nadie lo reenvio. Si se corta durante la subida, sigue en MONTANDO y al
        # arrancar `reconciliar` lo vuelve a la cola: se rearma (sin GPU, los clips estan) y se envia de nuevo.
        error = self._entregar(p, salida, info)
        try:
            self.s.transicion(id_, (Estado.MONTANDO,), lambda a: a.con(
                estado=Estado.ENTREGADO, reel=salida.name, error=error, entregas=a.entregas + 1))
        except EdicionInvalida:
            log.info("proyecto %s cancelado mientras se enviaba", id_)

    def _va_a_la_agenda(self, p: Proyecto) -> bool:
        """Solo la primera entrega de un reel automatico de hoy espera su horario. Un reel que el editor pidio
        cambiar ("sin subtítulos", otra toma) se rearma y le llega enseguida, como le dice el bot; y los de dias
        anteriores (que no llevaban la cuenta de entregas) tampoco vuelven a la agenda."""
        if self.agenda is None or not p.automatico or p.entregas:
            return False
        try:
            creado = datetime.fromisoformat(p.creado).astimezone(self.agenda.zona).date()
        except ValueError:
            return False
        return creado == datetime.now(self.agenda.zona).date()

    def entregar_programado(self, id_: str) -> None:
        """Manda un reel que esperaba su horario (lo llama la agenda). Si el archivo ya no esta, queda en error."""
        p = self.s.almacen.obtener(id_)
        if p is None or p.estado != Estado.PROGRAMADO:
            return
        salida = self.s.carpeta(p) / p.reel
        if not p.reel or not salida.is_file():
            log.error("el reel programado %s no esta en %s", id_, salida)
            self._a_error(id_, (Estado.PROGRAMADO,), "el reel ya no está en el servidor: pedí ✍️ Otro guion o "
                                                      "🖼️ Otras imágenes para rearmarlo")
            return
        try:
            segundos = round(montaje.duracion(salida), 2)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:  # el archivo esta: se manda igual
            log.warning("no pude medir %s (%s): va con el largo pedido", salida, exc)
            segundos = float(p.segundos_objetivo)
        info = {"duracion_s": segundos, "bytes": salida.stat().st_size}
        error = self._entregar(p, salida, info)
        try:
            self.s.transicion(id_, (Estado.PROGRAMADO,), lambda a: a.con(
                estado=Estado.ENTREGADO, enviar_a="", error=error, entregas=a.entregas + 1))
        except EdicionInvalida:
            log.info("proyecto %s cancelado mientras se enviaba", id_)

    def _avisar_programados(self, ids: list[str]) -> None:
        """Un solo aviso por tanda: que reels quedaron listos y a que hora llega cada uno."""
        if self.agenda is None:
            return
        programados = [p for p in map(self.s.almacen.obtener, ids) if p is not None and p.estado == Estado.PROGRAMADO]
        for chat in {p.chat_id for p in programados}:
            propios = sorted((p for p in programados if p.chat_id == chat),
                             key=lambda p: hora_de(p) or datetime.min.replace(tzinfo=self.agenda.zona))
            lineas = [f"🗓️ {len(propios)} reel{'es' if len(propios) != 1 else ''} listo"
                      f"{'s' if len(propios) != 1 else ''}. Te los mando a su horario para que los publiques:"]
            lineas += [linea_de(p, self.agenda.zona) for p in propios]
            lineas.append("/agenda ya para recibirlos todos ahora · /agenda off para que lleguen apenas estén.")
            self._avisar(chat, "\n".join(lineas))

    def _regenerar_deformes(self, p: Proyecto, carpeta: Path) -> bool:
        """El Qwen mira un cuadro de cada clip nuevo; los que ve deformes (caras, manos, cuerpos) se rehacen con
        otra semilla, hasta `max_regeneraciones` por escena. True si volvio a la cola."""
        nuevas = [e for e in p.plan.escenas if not e.revisada]
        if not nuevas:
            return False
        cuadros = calidad.cuadros([(e.n, carpeta / e.clip) for e in nuevas], carpeta / "control")
        with self.s.lock_llm:  # despues del turno el Qwen vuelve: se espera a que responda
            self.s._esperar_llm()
            encontrados = calidad.defectos(self.s.llm, cuadros)
        maximo = self.cfg.video.max_regeneraciones
        if encontrados is None:
            log.warning("no pude mirar las escenas de %s: sigue sin revisar", p.id)
            aviso, rehacer, rendidas = AVISO_SIN_REVISAR, {}, {}
        else:
            rehacer = {e.n: encontrados[e.n] for e in nuevas if e.n in encontrados and e.regeneraciones < maximo}
            rendidas = {e.n: encontrados[e.n] for e in nuevas if e.n in encontrados and e.n not in rehacer}
            aviso = ""

        def cambio(a: Proyecto) -> Proyecto:
            plan = a.plan
            for e in a.plan.escenas:
                if e.revisada:
                    continue
                if e.n in rehacer:
                    nueva = replace(e, clip="", seed=secrets.randbelow(2**31), error="",
                                    regeneraciones=e.regeneraciones + 1)
                else:
                    nueva = replace(e, revisada=True)
                plan = reemplazar_escena(plan, nueva)
            avisos = [aviso] if aviso else []
            avisos += [f"{AVISO_DEFORME}escena {n}: {problema} (ya se rehizo {maximo} veces)"
                       for n, problema in sorted(rendidas.items())]
            if avisos:
                plan = replace(plan, advertencia=" | ".join([*avisos, plan.advertencia]).strip(" |"))
            return a.con(plan=plan, estado=Estado.EN_COLA if rehacer else a.estado)
        self.s.transicion(p.id, (Estado.GENERANDO,), cambio)
        if rehacer:
            detalle = "; ".join(f"escena {n}: {problema}" for n, problema in sorted(rehacer.items()))
            self._avisar(p.chat_id, f"🔁 {p.id}: rehago {len(rehacer)} escena(s) que salieron deformes ({detalle}).")
        return bool(rehacer)

    def _palabras_oidas(self, p: Proyecto, carpeta: Path) -> list[tuple]:
        """Cuando dice la voz cada palabra (Whisper). Si falla, los subtitulos salen igual con tiempos repartidos."""
        con_audio = [(i, e) for i, e in enumerate(p.plan.escenas) if e.audio and (carpeta / e.audio).exists()]
        oidas: list[tuple] = [()] * len(p.plan.escenas)
        if not con_audio:
            return oidas
        try:
            resultado = subtitulos.tiempos_whisper(self.cfg.subtitulos, [carpeta / e.audio for _, e in con_audio],
                                                   [e.narracion for _, e in con_audio], carpeta / "voz")
        except subtitulos.SubtitulosError as exc:
            log.warning("subtitulos de %s sin Whisper (tiempos repartidos): %s", p.id, exc)
            return oidas
        for (i, _), palabras in zip(con_audio, resultado):
            oidas[i] = tuple(palabras)
        return oidas

    def _entregar(self, p: Proyecto, salida: Path, info: dict) -> str:
        """Manda el reel (con los botones), lo que hay que revisar y, al final y SOLO, el texto para publicar:
        el editor lo copia entero sin borrar nada. Devuelve el error ("" si llego)."""
        pasos = [lambda: self._enviar_video(p, salida, info)]
        if p.plan.advertencia:
            pasos.append(lambda: self.s.notificador.mensaje(
                p.chat_id, f"⚠️ {p.id} · revisar antes de publicar: {p.plan.advertencia}"))
        if p.plan.texto_post.strip():
            pasos.append(lambda: self.s.notificador.mensaje(p.chat_id, p.plan.texto_post.strip()))
        hechos = 0  # un reintento sigue desde lo que fallo: no repite el video
        for intento in range(1, REINTENTOS_ENVIO + 1):
            try:
                while hechos < len(pasos):
                    pasos[hechos]()
                    hechos += 1
                return ""
            except Exception as exc:  # noqa: BLE001
                log.warning("envio de %s fallo (intento %d): %s", p.id, intento, exc)
                time.sleep(5 * intento)
        return f"no se pudo enviar por Telegram; está en {salida}"

    def _enviar_video(self, p: Proyecto, salida: Path, info: dict) -> None:
        if info["bytes"] > MAX_BYTES_TELEGRAM:
            self.s.notificador.mensaje(p.chat_id, f"🎞️ {p.id}: el reel pesa {info['bytes'] // 2**20} MB, más de "
                                                  f"lo que acepta Telegram. Está en el servidor: {salida}",
                                       botones.reel(p, facebook.configurado(self.cfg.facebook)))
            return
        # el ID va en el texto: respondiendo al video, el editor le pide cambios a este reel
        leyenda = (f"🎞️ {p.plan.titular} · {info['duracion_s']} s · {p.id}\n"
                   f"Botones: voz, subtítulos o titular (se rearma sin GPU). O respondé a este video con lo que "
                   f"quieras cambiar. Una escena: /regenerar {p.id} N")
        self.s.notificador.video(p.chat_id, salida, leyenda, botones.reel(p, facebook.configurado(self.cfg.facebook)))


def minutos_de_turno(clips: int, gpus: int) -> int:
    """Minutos que el Qwen queda apagado para generar `clips` (cada GPU hace uno por vez) + arranque y vuelta."""
    return math.ceil(MINUTOS_POR_CLIP * math.ceil(clips / max(gpus, 1))) + 2


