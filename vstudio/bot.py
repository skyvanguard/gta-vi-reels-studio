"""Conversacion de Telegram: recibir noticias, mostrar el plan, editar, aprobar y entregar."""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ajustes, biblioteca, botones, formatos, guion, guion_propio, ingesta, pedido, tomas, voz
from .acceso import Acceso
from .facebook import FacebookError
from .fuentes import extraer_url, sin_url
from .modelos import MEDIO_IA, MEDIO_TOMA, Estado, Proyecto
from .montaje import UMBRAL_MONETIZABLE_S
from .servicio import CARPETA_ENTRADA, EdicionInvalida, Servicio
from .telegram import ClienteTelegram, TelegramError
from .trabajador import Trabajador, minutos_de_turno

log = logging.getLogger(__name__)

OPCIONES_DE_BOTON = {"voz": "voz", "sub": "subtitulos", "tit": "titular"}
SIN_NADA = {"voz": False, "subtitulos": False, "titular": False}  # "solo video"
NOMBRE_OPCION = {"voz": "Voz", "subtitulos": "Subtítulos", "titular": "Titular"}

ESPERA_POLL_S = 50
ESPERA_ALBUM_S = 3.0
MAX_INTENTOS_VINCULO = 5
MAX_FALLOS_VINCULO_TOTAL = 20
MAX_FOTOS = 8
MAX_MOTION_RESUMEN = 220
LARGOS = (10, 15, 30, 75, 90)  # 75 y 90 pasan del minuto (monetizables); 10-30 son cortos (10: cuenta regresiva)

AYUDA = """🎬 Video Studio · GTA VI
Mandame un link de una noticia, un texto y/o fotos (podés mandar un álbum). Te devuelvo un plan para aprobar.
Si mandás fotos, el reel sale solo con ellas. En el mensaje podés pedir el largo ("que dure 1 min") y
"solo video" (la foto animada, sin voz ni textos). Si pegás tu guion con secciones o tiempos
("[INTRO – 0:00 a 0:10]"), la voz lo lee tal cual y dura lo que marcan tus tiempos.

Cambios con tus palabras (al último plan o reel, o al mensaje que respondas): "sin subtítulos", "sin voz",
"con voz", "sin titular", "otras imágenes", "otro guion", "que dure 30 s". En un reel ya hecho, voz,
subtítulos y titular se rearman sin GPU. También están los botones debajo del plan y del reel.
Una escena sola: "cambiá la escena 3" o /escena 3 le pone otra toma de video real, sin tocar el resto.
Formatos: además de la noticia, pedí "sabías que…" (un dato, 15 s), "ranking" o "antes y ahora" en el mensaje.
Biblioteca: mandá fotos (por ejemplo capturas del tráiler) con /biblioteca TEMA y entran en la rotación;
/biblioteca solo muestra los temas.
Tus clips: mandá un video del juego con /biblioteca TEMA y lo corto en tomas de video real para los reels (hasta
20 MB por Telegram; los más pesados, copialos al servidor en assets/tomas/_entrada/TEMA/ y mandá /tomas).
/tomas muestra cuántas tomas hay.
Automático: todos los días a las 5:00 busco las noticias de GTA VI, armo un reel de cada una (sin repetir) y te
los mando listos. /auto (estado) · /auto ya (ahora) · /auto off · /auto on
Agenda: los automáticos te llegan repartidos en el día, uno por horario, con el texto para publicar.
/agenda (qué hay pendiente) · /agenda ya (todos ahora) · /agenda off (que lleguen apenas estén) · /agenda on

Editar el último plan:
/gancho N (usar otro gancho) · /titular TEXTO · /texto N TEXTO · /narracion N TEXTO · /post TEXTO
/reescribir (otro guion con las mismas imágenes)
Cola y GPU:
/estado · /ya (apagar el Qwen y generar ahora) · /politica inactivo|inmediato
Después de entregar:
/regenerar ID N · /revoz ID (otra voz, sin apagar el Qwen) · /reescribir ID (otro guion, sin apagar el Qwen) · /cancelar ID
Largo del reel: /largo 75|90 (pasan del minuto: monetizables) o 10|15|30 (cortos). Se aplica al plan
pendiente y a los próximos
Voz:
/voces (escuchar las opciones) · /voz NOMBRE
Voz propia: mandá una nota de voz de 10-20 s con el texto /voz_nueva NOMBRE (solo tu voz o de alguien que te dé permiso)"""


def acuse(n_fotos: int, segundos: int, solo_animacion: bool, guion_editor: bool = False,
          formato: str = formatos.NOTICIA) -> str:
    """Lo primero que contesta el bot: lo que entendio del pedido, para que se note enseguida si entendio mal."""
    material = {0: "con video real, fotos de la nota y las oficiales", 1: "solo con tu foto"}.get(
        n_fotos, f"solo con tus {n_fotos} fotos")
    if formato != formatos.NOTICIA and not (guion_editor or solo_animacion):
        material += f", en formato «{formatos.formato(formato).nombre}»"
    if guion_editor:
        return (f"📥 Recibido: reel de ~{segundos} s {material}, con voz leyendo tu guion tal cual. Elijo las "
                "imágenes para cada parte y te mando el plan para aprobar (3-10 min)…")
    if solo_animacion:
        return (f"📥 Recibido: animación de ~{segundos} s {material}, sin voz ni textos. Elijo los encuadres y "
                "los movimientos y te mando el plan para aprobar (3-10 min)…")
    return (f"📥 Recibido: reel de ~{segundos} s {material}, con voz. Leo el material, armo las escenas y escribo "
            "el guion; te mando el plan para aprobar (5-15 min)…")


def resumen_plan(p: Proyecto, estimado_s: float | None = None) -> str:
    f, plan = p.fuente, p.plan
    largo = f"~{estimado_s:.0f} s" if estimado_s else f"~{p.segundos_objetivo} s"
    if estimado_s and p.segundos_objetivo >= UMBRAL_MONETIZABLE_S:
        largo += " (pasa del minuto ✅)" if estimado_s > UMBRAL_MONETIZABLE_S else " (⚠️ no pasa del minuto)"
    lineas = [f"Proyecto {p.id} · {largo} · {len(plan.escenas)} escenas"]
    if p.formato != formatos.NOTICIA:
        lineas.append(f"🎞️ Formato: {formatos.formato(p.formato).nombre}")
    if f.titulo or f.url:
        lineas.append(f"📰 {f.titulo or f.url}" + (f" ({f.fecha})" if f.fecha else ""))
    if f.aviso:
        lineas.append(f"⚠️ {f.aviso}")
    if plan.advertencia:
        lineas.append(f"⚠️ {plan.advertencia}")
    if p.solo_animacion:
        lineas += ["", "🎬 Solo animación: sin voz, sin titular y sin textos en pantalla"]
    else:
        lineas += ["", f"🔥 {plan.titular}" if p.muestra_titular else "🔥 (sin titular)"]
    for e in plan.escenas:
        movimiento = e.motion_prompt if len(e.motion_prompt) <= MAX_MOTION_RESUMEN else \
            e.motion_prompt[:MAX_MOTION_RESUMEN] + "…"
        if e.medio != MEDIO_IA:
            movimiento = "video real del juego" if e.medio == MEDIO_TOMA else "foto de la noticia (paneo)"
        if e.extras:
            movimiento += f" + {len(e.extras)} toma{'s' if len(e.extras) != 1 else ''} de video real"
        if p.solo_animacion:
            lineas.append(f"{e.n}. 🖼️ {e.sujeto or 'encuadre ' + str(e.n)}\n   🎥 {movimiento}")
            continue
        lineas.append(f"{e.n}. 📺 {e.texto}\n   🎙️ {e.narracion}\n   🎥 {movimiento}")
    if plan.ganchos:
        lineas += ["", "🪝 Otros ganchos (/gancho N):"]
        lineas += [f"{i}. {g.titular} · {g.narracion}" for i, g in enumerate(plan.ganchos, start=1)]
    lineas += ["", opciones_en_texto(p), "", f"📝 {plan.texto_post}"]
    return "\n".join(lineas)


def opciones_en_texto(p: Proyecto) -> str:
    def si_no(valor: bool) -> str:
        return "sí" if valor else "no"
    return (f"🔊 Voz: {si_no(p.con_voz)} · 💬 Subtítulos: {si_no(p.muestra_textos)} · "
            f"🔥 Titular: {si_no(p.muestra_titular)}")


botones_plan = botones.plan


class Bot:
    def __init__(self, tg: ClienteTelegram, servicio: Servicio, trabajador: Trabajador, acceso: Acceso,
                 offset_archivo: Path, automatico=None, publicador=None, agenda=None):
        self.tg, self.s, self.trabajador, self.acceso = tg, servicio, trabajador, acceso
        self.automatico = automatico  # reels con las noticias del dia (/auto)
        self.agenda = agenda  # los automaticos llegan repartidos en el dia (/agenda)
        self.publicador = publicador  # Facebook (/facebook, boton 📤)
        self._offset_archivo = offset_archivo
        self._ejecutor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plan")
        # cortar un clip en tomas tarda minutos: va en su hilo para no frenar los planes (el Qwen se turna solo)
        self._ejecutor_clips = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clips")
        self._albumes: dict[str, dict] = {}
        self._intentos: dict[int, int] = defaultdict(int)
        self._fallos_vinculo = 0
        self._prefs_archivo = offset_archivo.with_name("preferencias.json")

    # ---------- bucle ----------
    def correr(self) -> None:
        offset = int(self._offset_archivo.read_text()) if self._offset_archivo.exists() else 0
        while True:
            espera = 1 if self._albumes else ESPERA_POLL_S
            try:
                actualizaciones = self.tg.actualizaciones(offset, espera)
            except TelegramError as exc:
                log.warning("getUpdates: %s", exc)
                time.sleep(5)
                continue
            for act in actualizaciones:
                offset = act["update_id"] + 1
                try:
                    self._manejar(act)
                except Exception:  # noqa: BLE001 - un mensaje roto no puede tirar el bot
                    log.exception("error manejando update %s", act.get("update_id"))
            self._offset_archivo.write_text(str(offset))
            self._vaciar_albumes()

    def _manejar(self, act: dict) -> None:
        if "callback_query" in act:
            self._boton(act["callback_query"])
        elif "message" in act:
            self._mensaje(act["message"])

    # ---------- acceso ----------
    def _permitido(self, usuario_id: int, chat_id: int, texto: str) -> bool:
        if self.acceso.autorizado(usuario_id):
            return True
        bloqueado = (self._intentos[usuario_id] >= MAX_INTENTOS_VINCULO
                     or self._fallos_vinculo >= MAX_FALLOS_VINCULO_TOTAL)
        if texto.startswith("/vincular ") and not bloqueado:
            self._intentos[usuario_id] += 1  # se cuenta antes de comparar
            if self.acceso.vincular(usuario_id, texto.split(maxsplit=1)[1]):
                self.tg.mensaje(chat_id, "✅ Vinculado.\n\n" + AYUDA)
            else:
                self._fallos_vinculo += 1
                self.tg.mensaje(chat_id, "Código incorrecto.")
            return False
        log.info("mensaje ignorado de usuario no autorizado %s", usuario_id)  # sin respuesta: no se confirma el bot
        return False

    # ---------- mensajes ----------
    def _mensaje(self, m: dict) -> None:
        chat_id, usuario_id = m["chat"]["id"], m.get("from", {}).get("id", 0)
        if m["chat"].get("type", "private") != "private":
            return  # en grupos el plan y el video los veria todo el grupo
        texto = (m.get("text") or m.get("caption") or "").strip()
        if not self._permitido(usuario_id, chat_id, texto):
            return
        foto = max(m["photo"], key=lambda f: f.get("file_size", 0))["file_id"] if m.get("photo") else None
        documento = m.get("document") or {}
        if documento.get("mime_type", "").startswith("image/"):
            foto = documento["file_id"]
        video = m.get("video") or (documento if documento.get("mime_type", "").startswith("video/") else None)
        if m.get("media_group_id"):
            album = self._albumes.setdefault(m["media_group_id"], {"chat": chat_id, "texto": "", "fotos": []})
            album["texto"] = album["texto"] or texto
            if foto and len(album["fotos"]) < MAX_FOTOS:
                album["fotos"].append(foto)
            album["t"] = time.monotonic()
            if video:  # en un album solo el primer mensaje trae el texto: vale para todos sus videos
                album["videos"] = True
                self._clip_a_biblioteca(chat_id, album["texto"], video)
            return
        audio = m.get("voice") or m.get("audio")
        if video:  # un clip del juego para la biblioteca de tomas (los reels salen de noticias, no de videos)
            self._clip_a_biblioteca(chat_id, texto, video)
        elif foto and texto.startswith("/biblioteca"):  # antes que los comandos: si no, la foto se pierde
            self._a_biblioteca(chat_id, texto, [foto])
        elif audio and texto.startswith("/voz_nueva"):
            self._voz_nueva(chat_id, texto, audio["file_id"])
        elif texto.startswith("/"):
            self._comando(chat_id, texto)
        elif texto and not foto and self._pedido_de_cambio(chat_id, texto, m):
            return
        elif texto or foto:
            self._nuevo(chat_id, texto, [foto] if foto else [])

    def _vaciar_albumes(self) -> None:
        listos = [k for k, a in self._albumes.items() if time.monotonic() - a["t"] >= ESPERA_ALBUM_S]
        for clave in listos:
            album = self._albumes.pop(clave)
            if album.get("videos") and not album["fotos"]:
                continue  # un album de videos: cada uno ya fue a la biblioteca de tomas
            if album["texto"].startswith("/biblioteca"):
                self._a_biblioteca(album["chat"], album["texto"], album["fotos"])
            else:
                self._nuevo(album["chat"], album["texto"], album["fotos"])

    def _en_segundo_plano(self, chat_id: int, fn, *args, ejecutor: ThreadPoolExecutor | None = None) -> None:
        """Corre en el hilo de planes (un plan a la vez: el Qwen tiene un solo slot) y avisa si falla."""
        def envoltura():
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001 - se loguea y se avisa al usuario
                log.exception("tarea en segundo plano %s", getattr(fn, "__name__", fn))
                try:
                    self.tg.mensaje(chat_id, f"❌ No pude terminar: {exc}")
                except TelegramError:
                    log.warning("tampoco se pudo avisar del error")
        (ejecutor or self._ejecutor).submit(envoltura)

    def _nuevo(self, chat_id: int, texto: str, fotos: list[str]) -> None:
        fotos = fotos[:MAX_FOTOS]
        p = pedido.leer(sin_url(texto))
        formato = formatos.formato(p.formato)  # sin pedido: noticia
        segundos = pedido.largo_para(p.segundos, LARGOS) if p.segundos else (formato.segundos or self._largo(chat_id))
        propio = guion_propio.leer(sin_url(texto))
        oraciones = propio.oraciones if propio else ()
        if propio:  # dura lo que marcan sus tiempos; sin tiempos, lo que tarda la voz en leerlo
            pedido_s = propio.segundos or round(sum(map(guion.segundos_de_voz, oraciones)))
            segundos = pedido.largo_para(pedido_s, LARGOS)
        self.tg.mensaje(chat_id, acuse(len(fotos), segundos, p.solo_animacion, bool(propio), formato.clave))
        self._en_segundo_plano(chat_id, self._crear, chat_id, texto, fotos, segundos, p.solo_animacion, oraciones,
                               formato.clave)

    def _largo(self, chat_id: int) -> int:
        defecto = self.s.cfg.segundos_por_defecto
        if not self._prefs_archivo.exists():
            return defecto
        guardado = json.loads(self._prefs_archivo.read_text()).get(str(chat_id), {}).get("largo", defecto)
        return guardado if guardado in LARGOS else defecto  # un largo que ya no se ofrece vuelve al de siempre

    def _guardar_largo(self, chat_id: int, segundos: int) -> None:
        prefs = json.loads(self._prefs_archivo.read_text()) if self._prefs_archivo.exists() else {}
        prefs[str(chat_id)] = {**prefs.get(str(chat_id), {}), "largo": segundos}
        self._prefs_archivo.write_text(json.dumps(prefs))

    def _crear(self, chat_id: int, texto: str, fotos: list[str], segundos: int, solo_animacion: bool,
               oraciones: tuple[str, ...] = (), formato: str = formatos.NOTICIA) -> None:
        try:
            datos = [self.tg.bajar_archivo(f) for f in fotos]
            p = self.s.crear(chat_id, texto, datos, segundos=segundos, solo_animacion=solo_animacion,
                             guion_editor=oraciones, formato=formato)
        except Exception as exc:  # noqa: BLE001
            log.exception("crear proyecto")
            self.tg.mensaje(chat_id, f"❌ No pude armar el plan: {exc}")
            return
        self._mostrar_plan(p)

    def _mostrar_plan(self, p: Proyecto) -> None:
        if p.estado == Estado.ERROR or p.plan is None:
            self.tg.mensaje(p.chat_id, f"❌ {p.id}: {p.error}")
            return
        self.tg.foto(p.chat_id, self.s.carpeta(p) / "storyboard.jpg", f"Escenas del proyecto {p.id}")
        self._enviar_plan(p.chat_id, p)

    def _enviar_plan(self, chat_id: int, p: Proyecto) -> None:
        self.tg.mensaje(chat_id, resumen_plan(p, self.s.duracion_estimada(p)), botones_plan(p))

    # ---------- botones ----------
    def _boton(self, q: dict) -> None:
        usuario_id, chat_id = q["from"]["id"], q["message"]["chat"]["id"]
        if not self.acceso.autorizado(usuario_id):
            self.tg.responder_boton(q["id"], "Bot privado")
            return
        accion, _, id_ = q.get("data", "").partition(":")
        try:
            if accion == "ok":
                self.s.aprobar(id_)
                self.tg.responder_boton(q["id"], "En cola")
                self.tg.mensaje(chat_id, self._texto_en_cola(id_))
            elif accion == "otro":
                self.tg.responder_boton(q["id"], "Armando otro plan…")
                self._en_segundo_plano(chat_id, lambda: self._mostrar_plan(self.s.replanificar(id_)))
            elif accion in OPCIONES_DE_BOTON:
                self._boton_opcion(q, chat_id, id_, OPCIONES_DE_BOTON[accion])
            elif accion == "img":
                self.tg.responder_boton(q["id"], "Busco otras imágenes…")
                self._otra_version(chat_id, id_, ajustes.Ajuste(otras_imagenes=True))
            elif accion == "gui":
                self.tg.responder_boton(q["id"], "Escribo otro guion…")
                self._reescribir(chat_id, id_)
            elif accion == "pub":
                self.tg.responder_boton(q["id"], "Lo subo a Facebook…")
                self._programar(chat_id, id_)
            elif accion == "no":
                self.s.cancelar(id_)
                self.tg.responder_boton(q["id"], "Cancelado")
        except EdicionInvalida as exc:
            self.tg.responder_boton(q["id"], str(exc))

    def _boton_opcion(self, q: dict, chat_id: int, id_: str, campo: str) -> None:
        actual = self.s.almacen.obtener(id_)
        if actual is None:
            raise EdicionInvalida(f"no existe el proyecto {id_}")
        valor = {"voz": not actual.con_voz, "subtitulos": not actual.muestra_textos,
                 "titular": not actual.muestra_titular}[campo]
        self.tg.responder_boton(q["id"], f"{NOMBRE_OPCION[campo]}: {'sí' if valor else 'no'}")
        es_plan = (q["message"].get("text") or "").startswith("Proyecto ")
        self._aplicar_opciones(chat_id, actual, {campo: valor}, q["message"].get("message_id") if es_plan else None)

    def _aplicar_opciones(self, chat_id: int, actual: Proyecto, opciones: dict, mensaje_id: int | None = None) -> None:
        """Voz/subtitulos/titular si o no. Pedir voz o subtitulos en una "solo animacion" es pedir un guion (el
        plan no lo tiene): se escribe y vuelve como plan, con voz, subtitulos y titular."""
        if (opciones.get("voz") or opciones.get("subtitulos")) and actual.solo_animacion:
            self._reescribir(chat_id, actual.id, opciones)
            return
        p = self.s.cambiar_opciones(actual.id, **opciones)
        if actual.estado == Estado.ENTREGADO:
            hace_voz = p.con_voz and not all(e.audio for e in p.plan.escenas)
            aviso_fb = (f" Ojo: en Facebook sigue programada la versión anterior; para cambiarla, /despublicar "
                        f"{p.id} y después 📤." if actual.publicacion == "programado" else "")
            self.tg.mensaje(chat_id, f"🔁 {p.id}: {opciones_en_texto(p)}. Lo rearmo sin GPU y te lo mando "
                                     f"({'~4 min: hay que narrarlo' if hace_voz else '~2 min'}).{aviso_fb}")
            return
        if p.estado != Estado.ESPERANDO_APROBACION:  # en cola o generandose: se aplica al montarlo
            self.tg.mensaje(chat_id, f"✏️ {p.id}: {opciones_en_texto(p)}. Se aplica cuando lo arme.")
            return
        texto = resumen_plan(p, self.s.duracion_estimada(p))
        if mensaje_id is not None:  # el plan que tenia el boton se actualiza en el lugar
            try:
                self.tg.editar(chat_id, mensaje_id, texto, botones.plan(p))
                return
            except TelegramError as exc:
                log.info("no pude editar el plan %s (%s): lo mando de nuevo", p.id, exc)
        self.tg.mensaje(chat_id, texto, botones.plan(p))

    # ---------- cambios con palabras ----------
    def _pedido_de_cambio(self, chat_id: int, texto: str, m: dict) -> bool:
        """Un mensaje corto sin link que pide cambios ("sin subtítulos", "otras imágenes") va al proyecto del
        mensaje que responde o al ultimo del chat. True si lo tomo como cambio."""
        if extraer_url(texto):
            return False
        cambio = ajustes.leer(texto)
        if cambio.vacio():
            return False
        id_ = ajustes.id_citado(m)
        p = self.s.almacen.obtener(id_) if id_ else self.s.almacen.ultimo_editable(chat_id)
        if p is None or p.chat_id != chat_id or p.plan is None:
            self.tg.mensaje(chat_id, f"No tengo un plan ni un reel reciente para cambiar ({cambio.describir()}). "
                                     "Mandame el material (link, texto o fotos) y lo armo así.")
            return True
        self.tg.mensaje(chat_id, f"✏️ {p.id} ({p.plan.titular}): {cambio.describir()}. Si era para otro, "
                                 "respondé a su mensaje.")
        try:
            self._aplicar_cambio(chat_id, p, cambio)
        except EdicionInvalida as exc:
            self.tg.mensaje(chat_id, f"⚠️ {exc}")
        return True

    def _aplicar_cambio(self, chat_id: int, p: Proyecto, cambio: ajustes.Ajuste) -> None:
        """Todo lo pedido en el mensaje se aplica junto: con otras imagenes u otro largo de un reel hecho sale un
        plan nuevo que ya lleva las opciones; con otro guion, las opciones van despues del guion."""
        opciones = SIN_NADA if cambio.solo_animacion else cambio.opciones()
        if cambio.escena:
            self._cambiar_escena(chat_id, p.id, cambio.escena, opciones)
        elif cambio.otras_imagenes or (cambio.segundos and p.estado != Estado.ESPERANDO_APROBACION):
            self._otra_version(chat_id, p.id, cambio, opciones)
        elif cambio.otro_guion:
            self._reescribir(chat_id, p.id, opciones)
        elif cambio.segundos:  # plan pendiente: las opciones primero, el plan nuevo las conserva
            if opciones:
                self.s.cambiar_opciones(p.id, **opciones)
            self._cambiar_largo_de(chat_id, p.id, pedido.largo_para(cambio.segundos, LARGOS))
        else:
            self._aplicar_opciones(chat_id, p, opciones)

    def _cambiar_escena(self, chat_id: int, id_: str, n: int, opciones: dict | None = None) -> None:
        """Otra toma de video real para una escena sola (en segundo plano: hay que recortar los videos).
        `opciones`: voz/subtitulos/titular pedidos en el mismo mensaje; se aplican despues de la toma."""
        self.tg.mensaje(chat_id, f"🎞️ Busco otra toma de video real para la escena {n} de {id_}…")

        def tarea() -> None:
            p = self.s.cambiar_escena(id_, n)
            if opciones:
                p = self.s.cambiar_opciones(id_, **opciones)
            escena = next(e for e in p.plan.escenas if e.n == n)
            if p.estado == Estado.ESPERANDO_APROBACION:
                self._mostrar_plan(p)
                return
            self.tg.mensaje(chat_id, f"🔁 {id_}: la escena {n} ahora es «{escena.sujeto}». Lo rearmo sin GPU y te "
                                     "lo mando (~2 min).")
        self._en_segundo_plano(chat_id, tarea)

    def _escena(self, chat_id: int, resto: str) -> None:
        """/escena N (el ultimo plan o reel del chat) o /escena ID N."""
        partes = resto.split()
        if len(partes) == 1 and partes[0].isdigit():
            p = self.s.almacen.ultimo_editable(chat_id)
            if p is None:
                raise EdicionInvalida("no tengo un plan ni un reel reciente: usá /escena ID N")
            self._cambiar_escena(chat_id, p.id, int(partes[0]))
        elif len(partes) == 2 and partes[1].isdigit():
            self._cambiar_escena(chat_id, partes[0], int(partes[1]))
        else:
            raise ValueError("uso: /escena N (cambia esa escena del último plan o reel) o /escena ID N")

    def _otra_version(self, chat_id: int, id_: str, cambio: ajustes.Ajuste, opciones: dict | None = None) -> None:
        segundos = pedido.largo_para(cambio.segundos, LARGOS) if cambio.segundos else None
        que = "con otras imágenes" if cambio.otras_imagenes else f"de ~{segundos} s"
        self.tg.mensaje(chat_id, f"🖼️ Armo otro plan de {id_} {que}, con el mismo material (3-10 min)…")
        self._en_segundo_plano(chat_id, lambda: self._mostrar_plan(
            self.s.otra_version(id_, otras_imagenes=cambio.otras_imagenes, segundos=segundos, opciones=opciones)))

    def _cambiar_largo_de(self, chat_id: int, id_: str, segundos: int) -> None:
        self.tg.mensaje(chat_id, f"⏱ Rehago el plan {id_} para ~{segundos} s…")
        self._en_segundo_plano(chat_id, lambda: self._mostrar_plan(self.s.cambiar_largo(id_, segundos)))

    # ---------- facebook ----------
    def _facebook(self):
        if self.publicador is None:
            raise ValueError("la publicación en Facebook no está andando en este proceso")
        return self.publicador

    def _programar(self, chat_id: int, id_: str) -> None:
        """Sube y programa el reel en el proximo horario libre (en segundo plano: son ~40 MB)."""
        if not id_:
            raise ValueError("uso: /publicar ID")
        publicador = self._facebook()
        if not publicador.configurado():
            raise ValueError("Facebook todavía no está configurado: falta el token de la página")

        def tarea() -> None:
            try:
                publicador.programar(id_)  # avisa el horario (o el error) al chat del reel
            except FacebookError:
                pass  # el publicador ya aviso (con el boton para reintentar)
            except Exception as exc:  # noqa: BLE001 - sin horarios, archivo que falta...: que el editor lo sepa
                log.warning("no pude programar %s: %s", id_, exc)
                self.tg.mensaje(chat_id, f"⚠️ {id_}: no lo pude programar ({exc})")
        threading.Thread(target=tarea, daemon=True, name=f"fb-{id_}").start()

    def _despublicar(self, chat_id: int, id_: str) -> None:
        if not id_:
            raise ValueError("uso: /despublicar ID")
        self._facebook().despublicar(id_)
        self.tg.mensaje(chat_id, f"🗑️ {id_} salió de Facebook. Para volver a programarlo: /publicar {id_}")

    # ---------- reels automaticos ----------
    def _auto(self, chat_id: int, valor: str) -> None:
        if self.automatico is None:
            raise ValueError("los reels automáticos no están andando en este proceso")
        valor = valor.strip().lower()
        if valor in ("on", "off"):
            self.automatico.activar(valor == "on")
        elif valor == "ya":
            self.tg.mensaje(chat_id, "🗞️ Busco las noticias de GTA VI de las últimas horas (unos minutos)…")
            threading.Thread(target=self.automatico.ejecutar, daemon=True, name="auto-ya").start()
            return
        elif valor:
            raise ValueError("uso: /auto (estado) · /auto ya · /auto on · /auto off")
        self.tg.mensaje(chat_id, self.automatico.resumen())

    # ---------- agenda de publicacion ----------
    def _agenda(self, chat_id: int, valor: str) -> None:
        if self.agenda is None:
            raise ValueError("la agenda de publicación no está andando en este proceso")
        valor = valor.strip().lower()
        if valor in ("on", "off"):
            self.agenda.activar(valor == "on")
            if valor == "off":  # apagarla es "que lleguen apenas estén": lo que esperaba su horario sale ahora
                self.agenda.adelantar()
        elif valor == "ya":
            cuantos = self.agenda.adelantar()
            self.tg.mensaje(chat_id, f"🗓️ Te mando ahora {cuantos} reel{'es' if cuantos != 1 else ''} (en menos de "
                                     "un minuto)." if cuantos else "🗓️ No hay reels esperando su horario.")
            return
        elif valor:
            raise ValueError("uso: /agenda (qué hay pendiente) · /agenda ya · /agenda on · /agenda off")
        self.tg.mensaje(chat_id, self.agenda.resumen())

    # ---------- biblioteca ----------
    def _a_biblioteca(self, chat_id: int, texto: str, fotos: list[str]) -> None:
        tema = texto.partition(" ")[2].strip()
        nombre = biblioteca.nombre_de_tema(tema)
        if not nombre:
            self.tg.mensaje(chat_id, "Falta el tema: mandá las fotos con /biblioteca TEMA (por ejemplo /biblioteca "
                                     "SWAT o /biblioteca Discoteca).")
            return
        guardadas = repetidas = invalidas = 0
        for foto in fotos:
            try:
                ruta = biblioteca.agregar(self.s.cfg.biblioteca_dir, nombre, self.tg.bajar_archivo(foto))
            except (TelegramError, ValueError) as exc:  # ImagenInvalida es un ValueError
                log.info("foto para la biblioteca descartada: %s", exc)
                invalidas += 1
                continue
            guardadas += ruta is not None
            repetidas += ruta is None
        total = biblioteca.temas_del_editor(self.s.cfg.biblioteca_dir).get(nombre, 0)
        partes = [f"📚 «{nombre}»: guardé {guardadas} foto{'s' if guardadas != 1 else ''} (el tema tiene {total})."]
        if repetidas:
            partes.append(f"{repetidas} ya estaba{'n' if repetidas > 1 else ''}.")
        if invalidas:
            partes.append(f"{invalidas} no se pudo leer o no es una imagen.")
        partes.append(f"Entran en la rotación de los próximos reels; si la nota nombra «{nombre}», van primero.")
        self.tg.mensaje(chat_id, " ".join(partes))

    # ---------- clips de video del editor ----------
    def _clip_a_biblioteca(self, chat_id: int, texto: str, video: dict) -> None:
        """Un video del juego mandado con /biblioteca TEMA: se corta en tomas de video real (en segundo plano: el
        Qwen mira cada una)."""
        tema = biblioteca.nombre_de_tema(texto.partition(" ")[2]) if texto.startswith("/biblioteca") else ""
        entrada = self.s.cfg.tomas_dir / CARPETA_ENTRADA
        if not tema:
            self.tg.mensaje(chat_id, "Para sumar este clip a tus tomas de video, mandalo con el texto /biblioteca "
                                     "TEMA (por ejemplo /biblioteca Persecución). Los reels los armo con noticias.")
            return
        self.tg.mensaje(chat_id, f"🎞️ Recibí el clip para «{tema}». Lo corto en tomas y miro qué se ve en cada "
                                 "una (unos minutos)…")
        extension = Path(str(video.get("file_name", ""))).suffix.lower()

        def tarea() -> None:
            try:
                datos = self.tg.bajar_archivo(video["file_id"])
            except TelegramError as exc:
                self.tg.mensaje(chat_id, f"⚠️ No pude bajar el clip ({exc}). Telegram deja bajar hasta 20 MB: si "
                                         f"pesa más, copialo al servidor en {entrada}/{tema.replace(' ', '_')}/ y "
                                         "mandá /tomas.")
                return
            carpeta = entrada / tema.replace(" ", "_")
            carpeta.mkdir(parents=True, exist_ok=True)
            nombre = f"tg_{int(time.time())}_{secrets.token_hex(3)}" \
                     f"{extension if extension in ingesta.EXTENSIONES else '.mp4'}"
            (carpeta / nombre).write_bytes(datos)
            self.tg.mensaje(chat_id, self._informe_de_clips([(nombre, self.s.sumar_archivo(carpeta / nombre, tema))]))
        self._en_segundo_plano(chat_id, tarea, ejecutor=self._ejecutor_clips)

    def _informe_de_clips(self, hechos: list) -> str:
        lineas = []
        for archivo, resultado in hechos:
            if isinstance(resultado, str):
                lineas.append(f"⚠️ {archivo}: {resultado}")
                continue
            lineas.append(f"🎞️ {archivo}: {resultado.guardadas} toma{'s' if resultado.guardadas != 1 else ''} "
                          f"({resultado.segundos:.0f} s) en «{resultado.fuente.replace('_', ' ')}»"
                          + (f"; {resultado.descartadas} sin usar (oscuras o con texto)"
                             if resultado.descartadas else ""))
            if resultado.sin_mirar:  # no es que no sirvan: el Qwen no llego a mirarlas
                lineas.append(f"⏳ {resultado.sin_mirar} toma(s) de {archivo} quedaron sin mirar porque el Qwen no "
                              "respondió. El clip sigue en la entrada: mandá /tomas más tarde y sigo donde quedé.")
        return "\n".join(lineas or ["No había clips nuevos."]) + "\nLas tomas entran en los próximos reels."

    def _tomas(self, chat_id: int) -> None:
        """Cuantas tomas de video real hay y, si el editor copio videos a _entrada, los suma."""
        por_fuente = tomas.resumen(self.s.cfg.tomas_dir)
        total = sum(n for n, _ in por_fuente.values())
        lineas = [f"🎞️ Tomas de video real: {total} ({sum(s for _, s in por_fuente.values()):.0f} s)."]
        lineas += [f"• {fuente.replace('_', ' ')}: {n} ({s:.0f} s)" for fuente, (n, s) in sorted(por_fuente.items())]
        pendientes, copiando = self.s.clips_en_entrada()
        if pendientes:
            lineas.append(f"Hay {len(pendientes)} video(s) nuevos en la carpeta de entrada: los corto ahora (unos "
                          "minutos cada uno) y te aviso.")
        if copiando:
            lineas.append(f"{copiando} video(s) se están copiando todavía: mandá /tomas de nuevo en un minuto.")
        if not pendientes and not copiando:
            lineas.append("Para sumar: mandá un video del juego con /biblioteca TEMA.")
        self.tg.mensaje(chat_id, "\n".join(lineas))
        if pendientes:
            self._en_segundo_plano(chat_id, lambda: self.tg.mensaje(
                chat_id, self._informe_de_clips(self.s.sumar_entrada())), ejecutor=self._ejecutor_clips)

    def _resumen_biblioteca(self) -> str:
        oficiales = biblioteca.escanear(self.s.cfg.biblioteca_dir)
        propios = biblioteca.temas_del_editor(self.s.cfg.biblioteca_dir)
        lineas = [f"📚 Biblioteca: {len(oficiales)} imágenes en {len({biblioteca.tema(i) for i in oficiales})} temas."]
        if propios:
            lineas.append("Tus temas: " + ", ".join(f"{t} ({n})" for t, n in propios.items()))
        lineas.append("Para sumar: mandá una o varias fotos (álbum) con el texto /biblioteca TEMA.")
        return "\n".join(lineas)

    def _texto_en_cola(self, id_: str) -> str:
        if self.trabajador.politica == "inmediato":
            return f"🕒 {id_} en cola: arranco en menos de un minuto."
        return (f"🕒 {id_} en cola. Lo genero cuando el Qwen lleve {self.s.cfg.gpu.inactividad_min} min sin "
                "uso. /ya para apagarlo y generar ahora.")

    # ---------- comandos ----------
    def _comando(self, chat_id: int, texto: str) -> None:
        cmd, _, resto = texto.partition(" ")
        cmd = cmd.split("@")[0].lower()
        resto = resto.strip()
        try:
            manejador = {
                "/start": lambda: self.tg.mensaje(chat_id, AYUDA), "/ayuda": lambda: self.tg.mensaje(chat_id, AYUDA),
                "/estado": lambda: self.tg.mensaje(chat_id, self._estado()),
                "/ya": lambda: self._ya(chat_id), "/politica": lambda: self._politica(chat_id, resto),
                "/titular": lambda: self._editar(chat_id, titular=resto),
                "/gancho": lambda: self._usar_gancho(chat_id, resto),
                "/reescribir": lambda: self._reescribir(chat_id, resto),
                "/post": lambda: self._editar(chat_id, texto_post=resto),
                "/texto": lambda: self._editar_escena(chat_id, resto, "texto"),
                "/narracion": lambda: self._editar_escena(chat_id, resto, "narracion"),
                "/regenerar": lambda: self._regenerar(chat_id, resto),
                "/escena": lambda: self._escena(chat_id, resto),
                "/revoz": lambda: (self.s.rehacer_voz(resto),
                                   self.tg.mensaje(chat_id, f"🎙️ Rehago la voz de {resto} con "
                                                            f"'{voz.voz_activa(self.s.cfg.voz)}' (sin apagar "
                                                            "el Qwen).")),
                "/cancelar": lambda: (self.s.cancelar(resto), self.tg.mensaje(chat_id, f"🗑️ {resto} cancelado.")),
                "/voces": lambda: self._voces(chat_id), "/voz": lambda: self._elegir_voz(chat_id, resto),
                "/largo": lambda: self._cambiar_largo(chat_id, resto),
                "/biblioteca": lambda: self.tg.mensaje(chat_id, self._resumen_biblioteca()),
                "/tomas": lambda: self._tomas(chat_id),
                "/auto": lambda: self._auto(chat_id, resto),
                "/agenda": lambda: self._agenda(chat_id, resto),
                "/facebook": lambda: self.tg.mensaje(chat_id, self._facebook().resumen()),
                "/publicar": lambda: self._programar(chat_id, resto.split()[0] if resto else ""),
                "/despublicar": lambda: self._despublicar(chat_id, resto.strip()),
                "/voz_nueva": lambda: self.tg.mensaje(chat_id, "Mandá el audio (nota de voz) con /voz_nueva NOMBRE "
                                                               "como texto del mismo mensaje."),
            }.get(cmd)
            if manejador is None:
                self.tg.mensaje(chat_id, "No conozco ese comando.\n\n" + AYUDA)
            else:
                manejador()
        except (EdicionInvalida, voz.VozError, ValueError) as exc:
            self.tg.mensaje(chat_id, f"⚠️ {exc}")

    def _ultimo(self, chat_id: int) -> Proyecto:
        p = self.s.almacen.ultimo_esperando(chat_id)
        if p is None:
            raise EdicionInvalida("no hay ningún plan esperando aprobación")
        return p

    def _editar(self, chat_id: int, **cambios) -> None:
        if not next(iter(cambios.values())):
            raise ValueError("falta el texto nuevo")
        self._enviar_plan(chat_id, self.s.editar(self._ultimo(chat_id).id, **cambios))

    def _usar_gancho(self, chat_id: int, resto: str) -> None:
        if not resto.isdigit():
            raise ValueError("uso: /gancho N (el número de la lista \"Otros ganchos\")")
        self._enviar_plan(chat_id, self.s.usar_gancho(self._ultimo(chat_id).id, int(resto)))

    def _reescribir(self, chat_id: int, resto: str, opciones: dict | None = None) -> None:
        """`opciones` (voz/subtitulos/titular) se aplican al plan reescrito: reescribir una animacion le prende
        todo, y "otro guion sin subtítulos" tiene que quedar sin subtitulos."""
        id_ = resto.split()[0] if resto else self._ultimo(chat_id).id
        self.tg.mensaje(chat_id, f"✍️ Reescribo el guion de {id_} (3-10 min). Las imágenes y los clips quedan.")

        def tarea() -> None:
            p = self.s.reescribir(id_)
            if opciones:
                p = self.s.cambiar_opciones(id_, **opciones)
            self._mostrar_reescrito(chat_id, p)
        self._en_segundo_plano(chat_id, tarea)

    def _mostrar_reescrito(self, chat_id: int, p: Proyecto) -> None:
        self._enviar_plan(chat_id, p)
        if all(e.clip for e in p.plan.escenas):
            self.tg.mensaje(chat_id, "Los clips ya están: ✅ Generar rehace solo la voz y el montaje, sin apagar "
                                     "el Qwen.")

    def _editar_escena(self, chat_id: int, resto: str, campo: str) -> None:
        numero, _, nuevo = resto.partition(" ")
        if not numero.isdigit() or not nuevo.strip():
            raise ValueError(f"uso: /{campo} N TEXTO")
        self._enviar_plan(chat_id, self.s.editar(self._ultimo(chat_id).id, escena=int(numero), **{campo: nuevo}))

    def _regenerar(self, chat_id: int, resto: str) -> None:
        partes = resto.split()
        if len(partes) != 2 or not partes[1].isdigit():
            raise ValueError("uso: /regenerar ID N")
        self.s.regenerar_escena(partes[0], int(partes[1]))
        self.tg.mensaje(chat_id, self._texto_en_cola(partes[0]))

    def _ya(self, chat_id: int) -> None:
        self.trabajador.forzar()
        self.tg.mensaje(chat_id, "⚡ Apago el Qwen y genero en el próximo ciclo (≤30 s) si hay algo en cola.")

    def _politica(self, chat_id: int, valor: str) -> None:
        if valor not in ("inactivo", "inmediato"):
            raise ValueError("uso: /politica inactivo|inmediato")
        self.trabajador.politica = valor
        self.tg.mensaje(chat_id, f"Política de GPU: {valor}")

    def _estado(self) -> str:
        activos = self.s.almacen.listar((Estado.ESPERANDO_APROBACION, Estado.EN_COLA, Estado.GENERANDO,
                                         Estado.MONTANDO, Estado.PROGRAMADO))
        lineas = [self.trabajador.alerta] if self.trabajador.alerta else []
        lineas += [f"GPU: {self.trabajador.estado_turno} · política {self.trabajador.politica}",
                  f"Qwen sin uso hace {self.trabajador.monitor.minutos_inactivo():.0f} min "
                  f"({self.trabajador.monitor.estado})",
                  f"Voz activa: {voz.voz_activa(self.s.cfg.voz) or 'ninguna'}"]
        lineas += [f"• {p.id} {p.estado.value}: {p.plan.titular if p.plan else ''}" for p in activos] or ["Cola vacía."]
        return "\n".join(lineas)

    def _voces(self, chat_id: int) -> None:
        nombres = voz.voces_disponibles(self.s.cfg.voz)
        if not nombres:
            raise voz.VozError("todavía no hay voces: hay que correr `python -m vstudio voces` en el servidor")
        for nombre in nombres:
            ruta = next((self.s.cfg.voz.voces_dir / f"{nombre}{ext}" for ext in voz.EXTENSIONES
                         if (self.s.cfg.voz.voces_dir / f"{nombre}{ext}").exists()), None)
            if ruta is None:
                self.tg.mensaje(chat_id, f"/voz {nombre} (la voz que trae el modelo)")
            else:
                self.tg.audio(chat_id, ruta, f"/voz {nombre}")
        self.tg.mensaje(chat_id, f"Voz activa: {voz.voz_activa(self.s.cfg.voz)}. Elegí con /voz NOMBRE.")

    def _cambiar_largo(self, chat_id: int, valor: str) -> None:
        if not valor.isdigit() or int(valor) not in LARGOS:
            raise ValueError("uso: /largo " + "|".join(map(str, LARGOS)))
        self._guardar_largo(chat_id, int(valor))
        minutos = minutos_de_turno(self.s.n_escenas(int(valor)), len(self.s.cfg.gpu.comfy_puertos))
        costo = f" Cada uno deja el Qwen apagado ~{minutos} min."
        pendiente = self.s.almacen.ultimo_esperando(chat_id)
        if pendiente is None:
            self.tg.mensaje(chat_id, f"⏱ Los próximos reels van a durar ~{valor} s.{costo}")
            return
        self._cambiar_largo_de(chat_id, pendiente.id, int(valor))

    def _voz_nueva(self, chat_id: int, texto: str, file_id: str) -> None:
        nombre = texto.partition(" ")[2].strip().lower()
        try:
            segundos = voz.guardar_referencia(self.s.cfg.voz, nombre, self.tg.bajar_archivo(file_id))
        except voz.VozError as exc:
            self.tg.mensaje(chat_id, f"⚠️ {exc}")
            return
        self.tg.mensaje(chat_id, f"🎙️ Voz '{nombre}' guardada ({segundos:.0f} s). Activala con /voz {nombre}.")

    def _elegir_voz(self, chat_id: int, nombre: str) -> None:
        voz.elegir_voz(self.s.cfg.voz, nombre)
        self.tg.mensaje(chat_id, f"🎙️ Voz activa: {nombre}. Para cambiarla en un reel ya hecho: /revoz ID")
