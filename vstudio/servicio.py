"""Operaciones sobre proyectos (las usan el bot y la CLI): crear, planificar, editar, aprobar, regenerar."""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from . import (armado, biblioteca, comfy, formatos, fuentes, guion, ilustracion, imagenes, ingesta, montaje,
               ortografia, tomas, verificador)
from .almacen import Almacen, nuevo_id
from .config import Config
from .gpu import NOMBRE_MARCA
from .llm import ClienteLlm
from .modelos import MEDIO_IA, Estado, Fuente, Gancho, Plan, Proyecto, reemplazar_escena
from .guionista import aplicar, escribir, vistas
from .planificador import planificar

log = logging.getLogger(__name__)

ESPERA_LLM_S = 30
CARPETA_CLIPS = "editor"  # dentro de tomas_dir: las tomas de los clips que suma el editor, por tema
# con "_": no se leen como tomas
CARPETA_ENTRADA, CARPETA_PROCESADOS, CARPETA_RECHAZADOS = "_entrada", "_procesados", "_rechazados"
TEMA_SIN_NOMBRE = "Gameplay"
MAX_CLIP_S = 10 * 60  # un clip del editor (los trailers, por la CLI, llegan a 20 min)
MAX_TOMAS_CLIP = 150  # por clip: cada toma es un recorte de video y una mirada del Qwen
ESTABLE_S = 60  # un archivo de la entrada que cambio hace menos se esta copiando todavia
MAX_ESPERA_LLM_S = 2 * 3600
REESCRIBIBLES = (Estado.ESPERANDO_APROBACION, Estado.ENTREGADO, Estado.ERROR)
CON_ESCENAS_CAMBIABLES = (Estado.ESPERANDO_APROBACION, Estado.ENTREGADO)
# voz/subtitulos/titular: se leen al montar, asi que se pueden cambiar hasta que empieza el montaje
OPCIONES_EDITABLES = (Estado.ESPERANDO_APROBACION, Estado.EN_COLA, Estado.GENERANDO, Estado.ENTREGADO)


class Notificador(Protocol):
    def mensaje(self, chat_id: int, texto: str, botones: list | None = None) -> None: ...
    def foto(self, chat_id: int, ruta: Path, texto: str, botones: list | None = None) -> None: ...
    def video(self, chat_id: int, ruta: Path, texto: str, botones: list | None = None) -> None: ...
    def audio(self, chat_id: int, ruta: Path, texto: str) -> None: ...


class EdicionInvalida(ValueError):
    pass


class Servicio:
    def __init__(self, cfg: Config, almacen: Almacen, llm: ClienteLlm, notificador: Notificador):
        # una sola verdad sobre cuanto dura un clip de IA: si la interpolacion esta pedida pero el modelo no esta
        # bajado, los clips salen a 16 cuadros por segundo y duran 5,06 s (no 5,37)
        if cfg.video.interpolar and not comfy.interpolacion_disponible(cfg.video, cfg.gpu.comfy_dir):
            cfg = replace(cfg, video=replace(cfg.video, interpolar=False))
        self.cfg, self.almacen, self.llm, self.notificador = cfg, almacen, llm, notificador
        ortografia.usar(cfg.diccionario)
        self.lock_llm = threading.Lock()  # el Qwen tiene un solo slot: planes y turnos de GPU no se pisan
        self.produciendo = threading.Event()  # lo marca el trabajador mientras genera/monta

    def carpeta(self, p: Proyecto) -> Path:
        return self.cfg.proyectos / p.id

    def hoy(self):
        return datetime.now(ZoneInfo(self.cfg.zona_horaria)).date()

    def catalogo(self) -> tomas.Catalogo:
        """La biblioteca de tomas de video como esta ahora (se relee: el editor suma clips sin reiniciar)."""
        return tomas.Catalogo.abrir(self.cfg.tomas_dir, self.cfg.datos / "tomas_usos.json")

    def con_llm(self, fn, *args):
        """Una llamada al Qwen fuera de un plan (buscar fotos, mirar una toma): espera su turno como los planes."""
        with self.lock_llm:
            self._esperar_llm()
            return fn(self.llm, *args)

    def _esperar_llm(self) -> None:
        """Si hay un turno de GPU en curso, espera a que vuelva el Qwen."""
        limite = time.monotonic() + MAX_ESPERA_LLM_S
        while (self.cfg.datos / NOMBRE_MARCA).exists() or not self.llm.disponible():
            if time.monotonic() > limite:
                raise TimeoutError("el Qwen no esta disponible (turno de GPU demasiado largo)")
            time.sleep(ESPERA_LLM_S)

    def transicion(self, id_: str, desde: tuple[Estado, ...], cambio) -> Proyecto:
        """Aplica `cambio` de forma atomica solo si el proyecto sigue en alguno de los estados `desde`."""
        def aplicar(actual: Proyecto) -> Proyecto:
            if actual.estado not in desde:
                raise EdicionInvalida(f"el proyecto {id_} esta en estado {actual.estado.value}")
            return cambio(actual)
        try:
            return self.almacen.actualizar(id_, aplicar)
        except KeyError as exc:
            raise EdicionInvalida(f"no existe el proyecto {id_}") from exc

    def _fallar(self, id_: str, exc: Exception) -> Proyecto:
        log.error("proyecto %s fallo: %s", id_, exc)
        try:
            return self.transicion(id_, (Estado.PLANIFICANDO,),
                                    lambda a: a.con(estado=Estado.ERROR, error=str(exc)[:500]))
        except EdicionInvalida:  # lo cancelaron mientras tanto: se respeta
            return self.almacen.obtener(id_)

    def n_escenas(self, segundos: int, guion_editor: tuple[str, ...] = ()) -> int:
        """Con el guion del editor no hay mas escenas que oraciones (cada escena lleva al menos una)."""
        n = min(max(round(segundos / self.cfg.segundos_por_escena), 2), self.cfg.max_escenas)
        return min(n, max(len(guion_editor), 2)) if guion_editor else n

    def piso(self, p: Proyecto) -> float:
        return montaje.piso_s(p.segundos_objetivo, self.cfg.montaje.minimo_monetizable_s)

    def duracion_estimada(self, p: Proyecto) -> float:
        """Largo previsto del reel segun las narraciones, antes de gastar GPU."""
        voces = [guion.segundos_de_voz(e.narracion) if p.con_voz else 0.0 for e in p.plan.escenas]
        return montaje.calcular_tiempos([self.cfg.video.segundos_clip] * len(voces), voces, self.piso(p)).total

    def crear(self, chat_id: int, texto: str, fotos: list[bytes], segundos: int | None = None,
              solo_animacion: bool = False, guion_editor: tuple[str, ...] = (), excluir: tuple[Path, ...] = (),
              opciones: dict | None = None, automatico: bool = False, medio: str = "",
              formato: str = "noticia") -> Proyecto:
        """`solo_animacion`: la imagen animada sin voz ni textos ("solo video"). `guion_editor`: la narracion del
        editor, tal cual. `excluir`: imagenes que no se repiten. `opciones`: voz/subtitulos/titular si o no."""
        segundos = segundos or self.cfg.segundos_por_defecto
        p = Proyecto(id=nuevo_id(), chat_id=chat_id, estado=Estado.PLANIFICANDO, fuente=Fuente(nota_usuario=texto),
                     segundos_objetivo=segundos, con_voz=not solo_animacion, solo_animacion=solo_animacion,
                     con_subtitulos=not solo_animacion, con_titular=not solo_animacion, automatico=automatico,
                     formato=formatos.formato(formato).clave)
        if opciones:
            p = _con_opciones(p, **opciones)
        self.almacen.guardar(p)
        n = self.n_escenas(segundos, guion_editor)
        con_fotos = self.cfg.fotos_de_la_noticia and bool(self.cfg.buscador_url)
        try:
            catalogo = self.catalogo()
            fuente = fuentes.preparar_fuente(
                texto, fotos, self.carpeta(p), minimo_imagenes=max(4, (n + 1) // 2 + 2),
                # con tomas de video el planificador ve mas imagenes: las tomas no le sacan lugar a las fotos
                maximo_imagenes=min(16, n + (8 if catalogo.tomas else 4)), buscador_url=self.cfg.buscador_url,
                biblioteca_dir=self.cfg.biblioteca_dir, usos=biblioteca.Usos(self.cfg.datos / "biblioteca_usos.json"),
                excluir=excluir, guion=guion_editor, exigir_nota=automatico, tomas_cat=catalogo,
                consultas=(lambda titulo, nota: self.con_llm(ilustracion.consultas_de, titulo, nota))
                if con_fotos else None,
                filtro=(lambda titulo, fotos_: self.con_llm(ilustracion.sirven, titulo, fotos_))
                if con_fotos else None)
            fuente = replace(fuente, medio=medio) if medio else fuente
            self.transicion(p.id, (Estado.PLANIFICANDO,), lambda a: a.con(fuente=fuente))
            return self._planificar(p.id)
        except EdicionInvalida:
            return self.almacen.obtener(p.id)
        except Exception as exc:  # noqa: BLE001 - cualquier fallo queda registrado en el proyecto y se avisa
            log.exception("fallo al crear el proyecto %s", p.id)
            return self._fallar(p.id, exc)

    def _planificar(self, id_: str, titular_previo: str = "") -> Proyecto:
        p = self.almacen.obtener(id_)
        with self.lock_llm:
            self._esperar_llm()
            plan = planificar(self.llm, self.carpeta(p), p.fuente, self.hoy(),
                              self.n_escenas(p.segundos_objetivo, p.fuente.guion),
                              self.cfg.noticia_vieja_dias, self.cfg.video.ancho, self.cfg.video.alto,
                              variante=p.variante, titular_previo=titular_previo, segundos=p.segundos_objetivo,
                              minimo_s=self.piso(p), solo_animacion=p.solo_animacion,
                              catalogo=self.catalogo(), segundos_clip=self.cfg.video.segundos_clip,
                              extras=self.cfg.tomas_extra, formato=p.formato)
        self._storyboard(p, plan)
        return self.transicion(id_, (Estado.PLANIFICANDO,),
                                lambda a: a.con(plan=plan, estado=Estado.ESPERANDO_APROBACION, error=""))

    def _storyboard(self, p: Proyecto, plan) -> Path:
        carpeta = self.carpeta(p)
        img = imagenes.storyboard([carpeta / e.imagen for e in plan.escenas], [str(e.n) for e in plan.escenas],
                                  self.cfg.montaje.fuentes_dir / "Anton-Regular.ttf")
        destino = carpeta / "storyboard.jpg"
        img.save(destino, quality=88)
        return destino

    def replanificar(self, id_: str) -> Proyecto:
        previo = self._obtener(id_)
        titular = previo.plan.titular if previo.plan else ""
        self.transicion(id_, (Estado.ESPERANDO_APROBACION, Estado.ERROR),
                         lambda a: a.con(estado=Estado.PLANIFICANDO, variante=a.variante + 1))
        try:
            return self._planificar(id_, titular_previo=titular)
        except EdicionInvalida:
            return self.almacen.obtener(id_)
        except Exception as exc:  # noqa: BLE001
            log.exception("fallo al replanificar %s", id_)
            return self._fallar(id_, exc)

    def _obtener(self, id_: str, estados: tuple[Estado, ...] | None = None) -> Proyecto:
        p = self.almacen.obtener(id_)
        if p is None:
            raise EdicionInvalida(f"no existe el proyecto {id_}")
        if estados and p.estado not in estados:
            raise EdicionInvalida(f"el proyecto {id_} esta en estado {p.estado.value}")
        return p

    def editar(self, id_: str, *, titular: str | None = None, escena: int | None = None,
               texto: str | None = None, narracion: str | None = None, texto_post: str | None = None) -> Proyecto:
        def cambio(p: Proyecto) -> Proyecto:
            plan = p.plan
            if titular is not None:
                plan = replace(plan, titular=titular.strip()[:70])
            if texto_post is not None:
                plan = replace(plan, texto_post=texto_post.strip()[:1000])
            if escena is not None:
                actual = next((e for e in plan.escenas if e.n == escena), None)
                if actual is None:
                    raise EdicionInvalida(f"no hay escena {escena}")
                nueva = replace(actual, texto=(texto or actual.texto).strip()[:90],
                                narracion=(narracion or actual.narracion).strip()[:220], audio="")
                plan = reemplazar_escena(plan, nueva)
            return p.con(plan=plan)
        return self.transicion(id_, (Estado.ESPERANDO_APROBACION,), cambio)

    def usar_gancho(self, id_: str, n: int) -> Proyecto:
        """Pone el gancho alternativo N (titular + narracion de la escena 1); el actual queda en su lugar."""
        def cambio(p: Proyecto) -> Proyecto:
            if not 1 <= n <= len(p.plan.ganchos):
                raise EdicionInvalida(f"no hay gancho {n} (hay {len(p.plan.ganchos)})")
            elegido, primera = p.plan.ganchos[n - 1], p.plan.escenas[0]
            actual = Gancho(titular=p.plan.titular, narracion=primera.narracion)
            ganchos = tuple(actual if i == n - 1 else g for i, g in enumerate(p.plan.ganchos))
            plan = reemplazar_escena(p.plan, replace(primera, narracion=elegido.narracion, audio=""))
            return p.con(plan=replace(plan, titular=elegido.titular, ganchos=ganchos))
        return self.transicion(id_, (Estado.ESPERANDO_APROBACION,), cambio)

    def reescribir(self, id_: str, evitar: tuple[str, ...] = ()) -> Proyecto:
        """Otro guion para las mismas escenas (plan pendiente o reel ya hecho). Las imagenes y los clips quedan:
        al aprobarlo solo se rehacen la voz y el montaje, sin GPU. `evitar`: lo que la verificacion encontro sin
        respaldo, para que no lo vuelva a decir."""
        p = self._obtener(id_, REESCRIBIBLES)
        if p.plan is None:
            raise EdicionInvalida(f"el proyecto {id_} no tiene plan: tocá 🔁 Otro plan")
        with self.lock_llm:  # si el guionista falla, el error sube y el proyecto queda como estaba
            self._esperar_llm()
            nuevo = escribir(self.llm, p.fuente, vistas(p.plan.escenas), self.hoy(), p.segundos_objetivo,
                             p.plan.advertencia, self.piso(p), evitar=evitar, formato=p.formato)
            dudas = verificador.verificar(self.llm, p.fuente, aplicar(p.plan, nuevo), self.hoy())

        def cambio(a: Proyecto) -> Proyecto:
            # el guion va sobre el plan de ahora: lo que se edito o regenero mientras escribia el LLM no se pierde
            if a.plan is None or [e.imagen for e in a.plan.escenas] != [e.imagen for e in p.plan.escenas]:
                raise EdicionInvalida(f"el plan de {id_} cambió mientras se reescribía: probá /reescribir de nuevo")
            plan = aplicar(a.plan, nuevo)
            plan = replace(plan, advertencia=verificador.con_dudas(plan.advertencia, dudas))
            listo = a.con(plan=plan, estado=Estado.ESPERANDO_APROBACION, reel="", error="")
            # pedir un guion para una "solo animacion" es pedir que hable: vuelven la voz, los textos y el titular
            return _con_opciones(listo, voz=True, subtitulos=True, titular=True) if a.solo_animacion else listo
        return self.transicion(id_, REESCRIBIBLES, cambio)

    def cambiar_escena(self, id_: str, n: int) -> Proyecto:
        """Otra toma de video real para la escena N, sin tocar el resto (plan pendiente o reel ya hecho, que se
        rearma sin GPU). El texto, la narracion y la voz de la escena quedan."""
        p = self._obtener(id_, CON_ESCENAS_CAMBIABLES)
        escena = next((e for e in p.plan.escenas if e.n == n), None) if p.plan else None
        if escena is None:
            raise EdicionInvalida(f"{id_} no tiene escena {n}")
        if any(nombre.startswith("usuario_") for nombre in p.fuente.imagenes):
            raise EdicionInvalida(f"{id_} usa tus fotos: para cambiar una escena, mandame otra foto")
        catalogo = self.catalogo()
        if not catalogo.tomas:
            raise EdicionInvalida("todavía no hay tomas de video real en la biblioteca (/tomas)")
        carpeta = self.carpeta(p)
        usadas = tomas.usadas_del_proyecto(carpeta)
        if not usadas:  # un proyecto de antes de que se anotaran: al menos las tomas principales, por el plan
            mapa = tomas.mapa_del_proyecto(carpeta)
            usadas = {e.clip: mapa[e.origen] for e in p.plan.escenas if e.origen in mapa and e.clip}
        try:
            nueva, por_archivo = armado.otra_toma(escena, carpeta, catalogo, frozenset(usadas.values()),
                                                  self.cfg.video.ancho, self.cfg.video.alto,
                                                  self.cfg.video.segundos_clip)
        except ValueError as exc:
            raise EdicionInvalida(str(exc)) from exc
        viejos = {escena.clip, *escena.extras}

        def cambio(a: Proyecto) -> Proyecto:
            actual = next((e for e in a.plan.escenas if e.n == n), None) if a.plan else None
            if actual is None:
                raise EdicionInvalida(f"el plan de {id_} cambió mientras buscaba otra toma: probá de nuevo")
            # el guion es el de ahora: lo que se edito mientras tanto no se pierde
            plan = reemplazar_escena(a.plan, replace(nueva, texto=actual.texto, narracion=actual.narracion,
                                                     audio=actual.audio))
            if a.estado == Estado.ENTREGADO:
                return a.con(plan=plan, estado=Estado.EN_COLA, reel="", error="")
            return a.con(plan=plan)
        p = self.transicion(id_, CON_ESCENAS_CAMBIABLES, cambio)
        # recien ahora, con el plan ya cambiado: si la transicion falla, las tomas no quedan como usadas
        tomas.anotar_usadas(carpeta, {**{a: r for a, r in usadas.items() if a not in viejos}, **por_archivo})
        catalogo.marcar(sorted(set(por_archivo.values())))
        try:
            self._storyboard(p, p.plan)
        except (OSError, ValueError) as exc:  # el reel ya esta en camino: sin la tira nueva se puede seguir
            log.warning("no pude rehacer la tira de escenas de %s: %s", id_, exc)
        return p

    def cambiar_largo(self, id_: str, segundos: int) -> Proyecto:
        """Cambia la duracion objetivo de un plan pendiente y lo vuelve a planificar."""
        self.transicion(id_, (Estado.ESPERANDO_APROBACION, Estado.ERROR),
                        lambda a: a.con(segundos_objetivo=segundos))
        return self.replanificar(id_)

    def cambiar_opciones(self, id_: str, **opciones: bool) -> Proyecto:
        """Voz, subtitulos y titular si o no. En un plan pendiente solo cambia el plan; en un reel ya entregado
        lo vuelve a la cola: se rearma sin GPU (los clips estan; si se pide voz y no habia, se narra)."""
        def cambio(a: Proyecto) -> Proyecto:
            if a.estado == Estado.MONTANDO:
                raise EdicionInvalida(f"{id_} se está armando o enviando: probá de nuevo en un minuto, con los "
                                      "botones del reel")
            if a.estado not in OPCIONES_EDITABLES:
                raise EdicionInvalida(f"{id_} está {a.estado.value}: no se puede cambiar")
            if (opciones.get("voz") or opciones.get("subtitulos")) and a.solo_animacion:
                raise EdicionInvalida(f"{id_} se armó como solo animación y no tiene guion: pedí ✍️ Otro guion y "
                                      "vuelve con voz y subtítulos")
            nuevo = _con_opciones(a, **opciones)
            if a.estado == Estado.ENTREGADO:
                return nuevo.con(estado=Estado.EN_COLA, reel="", error="")
            return nuevo
        try:
            return self.almacen.actualizar(id_, cambio)
        except KeyError as exc:
            raise EdicionInvalida(f"no existe el proyecto {id_}") from exc

    def otra_version(self, id_: str, *, otras_imagenes: bool = False, segundos: int | None = None,
                     opciones: dict | None = None) -> Proyecto:
        """Un plan nuevo con el mismo material y las mismas opciones (mas las de `opciones`): con otras imagenes
        y/o con otro largo. Si sale y el original seguia pendiente, el original se cancela (si fallo, queda)."""
        p = self._obtener(id_)
        if otras_imagenes and any(n.startswith("usuario_") for n in p.fuente.imagenes):
            raise EdicionInvalida(f"{id_} usa tus fotos: para cambiarlas, mandame las otras fotos")
        carpeta = self.carpeta(p)
        fotos = [] if otras_imagenes else [(carpeta / n).read_bytes() for n in p.fuente.imagenes
                                           if n.startswith("usuario_") and (carpeta / n).exists()]
        excluir = tuple(carpeta / n for n in p.fuente.imagenes) if otras_imagenes else ()
        texto = f"{p.fuente.url} {p.fuente.nota_usuario}".strip()
        # "otras imagenes" cambia las imagenes, no el guion: la narracion de cada escena
        # va tal cual y las imagenes nuevas se eligen para acompanarla
        mismo_guion = otras_imagenes and not segundos and p.plan is not None and not p.solo_animacion
        narraciones = tuple(e.narracion for e in p.plan.escenas) if mismo_guion else p.fuente.guion
        nuevo = self.crear(p.chat_id, texto, fotos, segundos=segundos or p.segundos_objetivo,
                           solo_animacion=p.solo_animacion, guion_editor=narraciones, excluir=excluir,
                           formato=p.formato,
                           opciones={"voz": p.con_voz, "subtitulos": p.con_subtitulos, "titular": p.con_titular,
                                     **(opciones or {})})
        if mismo_guion and nuevo.estado == Estado.ESPERANDO_APROBACION:
            nuevo = self.almacen.actualizar(nuevo.id, lambda a: a.con(plan=_mismo_guion(a.plan, p.plan, id_)))
        if nuevo.estado == Estado.ESPERANDO_APROBACION:
            try:  # solo si sigue pendiente: si mientras tanto lo aprobaron, se respeta
                self.transicion(id_, (Estado.ESPERANDO_APROBACION,), lambda a: a.con(estado=Estado.CANCELADO))
            except EdicionInvalida:
                pass
        return nuevo

    # ---------- clips del editor (video real para la biblioteca de tomas) ----------
    def sumar_clip(self, video: Path, tema: str) -> ingesta.Resultado:
        """Corta un clip del editor en tomas y las suma a la biblioteca (tomas/editor/TEMA): el Qwen dice que se
        ve en cada una. Levanta ValueError sin tema e ingesta.IngestaError si el archivo no es un video."""
        nombre = biblioteca.nombre_de_tema(tema)
        if not nombre:
            raise ValueError("falta el tema: /biblioteca TEMA (por ejemplo /biblioteca Persecución)")
        carpeta = self.cfg.tomas_dir / CARPETA_CLIPS / nombre.replace(" ", "_")
        return ingesta.ingresar(video, carpeta, lambda cuadro: self.con_llm(ingesta.describir, cuadro, nombre),
                                tema=nombre, max_segundos=MAX_CLIP_S, max_tomas=MAX_TOMAS_CLIP)

    def sumar_archivo(self, video: Path, tema: str) -> ingesta.Resultado | str:
        """Suma un clip que esta en la entrada y lo saca de ahi: a _procesados si se corto, a _rechazados si no
        es un video que se pueda cortar (no se reintenta en cada pasada). Si el Qwen no llego a mirar todas las
        tomas, queda en la entrada: la proxima pasada sigue donde quedo, sin repetir. Devuelve el resultado o el
        error."""
        resultado: ingesta.Resultado | str
        try:
            resultado = self.sumar_clip(video, tema)
            destino = None if resultado.sin_mirar else CARPETA_PROCESADOS
        except (ValueError, ingesta.IngestaError) as exc:
            log.warning("no pude sumar %s: %s", video, exc)
            resultado, destino = str(exc), CARPETA_RECHAZADOS
        if destino:
            try:
                carpeta = self.cfg.tomas_dir / destino
                carpeta.mkdir(parents=True, exist_ok=True)
                video.replace(carpeta / f"{int(time.time())}_{secrets.token_hex(3)}_{video.name}")
            except OSError as exc:  # queda en la entrada: lo ya cortado no se repite (ingesta.ingresar)
                log.warning("no pude sacar %s de la entrada: %s", video, exc)
        return resultado

    def clips_en_entrada(self) -> tuple[list[Path], int]:
        """Los videos de tomas/_entrada listos para cortar y cuantos se estan copiando todavia (cambiaron hace
        menos de un minuto: no se tocan). Los enlaces simbolicos no cuentan."""
        entrada = self.cfg.tomas_dir / CARPETA_ENTRADA
        if not entrada.is_dir():
            return [], 0
        videos = sorted(p for p in entrada.rglob("*") if p.suffix.lower() in ingesta.EXTENSIONES
                        and p.is_file() and not p.is_symlink())
        limite = time.time() - ESTABLE_S
        listos = [p for p in videos if p.stat().st_mtime <= limite]
        return listos, len(videos) - len(listos)

    def sumar_entrada(self) -> list[tuple[str, ingesta.Resultado | str]]:
        """Los videos que el editor copio a tomas/_entrada (los que pesan mas de lo que deja bajar Telegram): la
        subcarpeta es el tema. Devuelve (archivo, resultado o el error) de cada uno."""
        entrada = self.cfg.tomas_dir / CARPETA_ENTRADA
        hechos: list[tuple[str, ingesta.Resultado | str]] = []
        for video in self.clips_en_entrada()[0]:
            tema = video.parent.name if video.parent != entrada else TEMA_SIN_NOMBRE
            try:
                hechos.append((video.name, self.sumar_archivo(video, tema)))
            except Exception as exc:  # noqa: BLE001 - un video que rompe algo no deja sin procesar a los demas
                log.exception("fallo al sumar %s", video)
                hechos.append((video.name, f"error inesperado ({type(exc).__name__})"))
        return hechos

    def aprobar(self, id_: str) -> Proyecto:
        return self.transicion(id_, (Estado.ESPERANDO_APROBACION,), lambda a: a.con(estado=Estado.EN_COLA))

    def cancelar(self, id_: str) -> Proyecto:
        cancelables = (Estado.PLANIFICANDO, Estado.ESPERANDO_APROBACION, Estado.EN_COLA, Estado.ERROR,
                       Estado.PROGRAMADO)
        if not self.produciendo.is_set():  # trabados tras un corte: se pueden cancelar
            cancelables += (Estado.GENERANDO, Estado.MONTANDO)
        return self.transicion(id_, cancelables, lambda a: a.con(estado=Estado.CANCELADO))

    def rehacer_voz(self, id_: str) -> Proyecto:
        """Vuelve a narrar con la voz activa y a montar. Los clips se reusan: no hace falta GPU."""
        def cambio(p: Proyecto) -> Proyecto:
            plan = p.plan
            for e in p.plan.escenas:
                plan = reemplazar_escena(plan, replace(e, audio=""))
            return p.con(plan=plan, con_voz=True, estado=Estado.EN_COLA, reel="", error="")
        return self.transicion(id_, (Estado.ENTREGADO, Estado.ERROR), cambio)

    def regenerar_escena(self, id_: str, n: int) -> Proyecto:
        def cambio(p: Proyecto) -> Proyecto:
            escena = next((e for e in p.plan.escenas if e.n == n), None)
            if escena is None:
                raise EdicionInvalida(f"no hay escena {n}")
            if escena.medio != MEDIO_IA:
                raise EdicionInvalida(f"la escena {n} de {id_} es video real o una foto, no la genera la IA: para "
                                      f"cambiarla, /escena {n} le pone otra toma de video real (o pedí 🖼️ Otras "
                                      "imágenes)")
            nueva = replace(escena, clip="", seed=secrets.randbelow(2**31), error="", revisada=False)
            return p.con(plan=reemplazar_escena(p.plan, nueva), estado=Estado.EN_COLA, reel="", error="")
        return self.transicion(id_, (Estado.ENTREGADO, Estado.ERROR), cambio)


def _con_opciones(p: Proyecto, voz: bool | None = None, subtitulos: bool | None = None,
                  titular: bool | None = None) -> Proyecto:
    """Sin voz no van subtitulos ("pedí un video sin audio y salió igual con subtítulo"),
    salvo que se pidan; con algo para leer u oir deja de ser "solo animacion"."""
    cambios: dict = {}
    if voz is not None:
        cambios["con_voz"] = voz
        if subtitulos is None:
            subtitulos = voz
    if subtitulos is not None:
        cambios["con_subtitulos"] = subtitulos
    if titular is not None:
        cambios["con_titular"] = titular
    if voz or subtitulos or titular:
        cambios["solo_animacion"] = False
    return p.con(**cambios)


def _mismo_guion(nuevo: Plan, original: Plan, id_original: str) -> Plan:
    """El plan con otras imagenes conserva el resto del guion del original: titular, texto en pantalla de cada
    escena, ganchos alternativos y post."""
    escenas = tuple(replace(e, texto=o.texto) for e, o in zip(nuevo.escenas, original.escenas))
    escenas += nuevo.escenas[len(escenas):]
    aviso = f"Mismo guion que {id_original}, con otras imágenes."
    return replace(nuevo, titular=original.titular, escenas=escenas, ganchos=original.ganchos,
                   texto_post=original.texto_post, advertencia=f"{aviso} {original.advertencia}".strip())
