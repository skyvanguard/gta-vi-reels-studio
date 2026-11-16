"""CLI de Video Studio.

  python -m vstudio bot                      servicio: bot de Telegram + cola de produccion
  python -m vstudio planificar --texto T [--foto F ...]   arma un plan sin Telegram (chat 0)
  python -m vstudio aprobar ID               pasa un plan a la cola
  python -m vstudio reescribir ID            otro guion con las mismas imagenes (los clips se reusan)
  python -m vstudio procesar [--forzar]      corre un ciclo de la cola (abre turno de GPU si corresponde)
  python -m vstudio voces | voz NOMBRE       crea las voces de marca / elige la activa
  python -m vstudio vincular                 genera un codigo nuevo para vincular otro usuario de Telegram
  python -m vstudio restaurar                devuelve el Qwen si quedo un turno de GPU abierto
  python -m vstudio estado                   lista los proyectos activos
  python -m vstudio tomas [VIDEO] [--fuente NOMBRE] [--tema T] [--sumar]
                                             corta un video (un trailer oficial) en tomas de video real para los
                                             reels; sin VIDEO, muestra cuantas tomas hay
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import voz
from .acceso import Acceso, asegurar_privado
from .almacen import Almacen
from .config import Config, cargar_config
from .gpu import restaurar_si_hace_falta
from .llm import ClienteLlm
from .modelos import ESTADOS_ACTIVOS, Estado
from .servicio import Servicio

log = logging.getLogger("vstudio")


def configurar_logs(cfg: Config) -> None:
    carpeta = cfg.datos.parent / "logs"
    carpeta.mkdir(parents=True, exist_ok=True)
    formato = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    archivo = RotatingFileHandler(carpeta / "vstudio.log", maxBytes=5 * 2**20, backupCount=5, encoding="utf-8")
    consola = logging.StreamHandler(sys.stderr)
    for h in (archivo, consola):
        h.setFormatter(formato)
    logging.basicConfig(level=logging.INFO, handlers=[archivo, consola])
    for ruidoso in ("httpx", "httpcore", "PIL", "trafilatura", "htmldate"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)


class NotificadorConsola:
    def mensaje(self, chat_id, texto, botones=None):
        print(f"[chat {chat_id}] {texto}")

    def foto(self, chat_id, ruta, texto, botones=None):
        print(f"[chat {chat_id}] (foto {ruta}) {texto}")

    def video(self, chat_id, ruta, texto, botones=None):
        print(f"[chat {chat_id}] (video {ruta}) {texto}")

    def audio(self, chat_id, ruta, texto):
        print(f"[chat {chat_id}] (audio {ruta}) {texto}")


def _servicio(cfg: Config, notificador) -> Servicio:
    return Servicio(cfg, Almacen(cfg.datos / "studio.db"), ClienteLlm(cfg.llm), notificador)


def cmd_bot(cfg: Config, _args) -> None:
    from .agenda import Agenda
    from .automatico import Automatico
    from .bot import Bot
    from .publicador import Publicador
    from .telegram import ClienteTelegram
    from .trabajador import Trabajador

    if not cfg.telegram.token_archivo.exists():
        sys.exit(f"falta el token del bot en {cfg.telegram.token_archivo} (crearlo con @BotFather)")
    for secreto in (cfg.telegram.token_archivo, cfg.telegram.usuarios_archivo, cfg.llm.clave_archivo,
                    cfg.facebook.token_archivo, cfg.facebook.pagina_archivo):
        if asegurar_privado(secreto):
            log.warning("permisos corregidos a 600 en %s (estaba legible por otros usuarios)", secreto)
    tg = ClienteTelegram(cfg.telegram.token_archivo.read_text(encoding="utf-8").strip())
    acceso = Acceso(cfg.telegram.usuarios_archivo, cfg.telegram.codigo_archivo)
    if acceso.asegurar_codigo_si_no_hay_usuarios():
        log.warning("sin usuarios vinculados: el codigo de vinculacion esta en %s (mandar /vincular CODIGO)",
                    cfg.telegram.codigo_archivo)
    servicio = _servicio(cfg, tg)
    trabajador = Trabajador(servicio)
    agenda = Agenda(servicio, trabajador.entregar_programado)  # los automaticos llegan repartidos en el dia
    trabajador.agenda = agenda  # antes de arrancar: el primer reel que se arme ya sabe a que hora sale
    trabajador.start()
    agenda.start()
    automatico = Automatico(servicio, acceso)  # las noticias del dia, a la hora configurada
    automatico.start()
    publicador = Publicador(servicio)  # programa en Facebook lo que pasa el control
    publicador.start()
    log.info("bot iniciado")
    Bot(tg, servicio, trabajador, acceso, cfg.datos / "telegram_offset", automatico, publicador, agenda).correr()


def cmd_planificar(cfg: Config, args) -> None:
    from .bot import resumen_plan

    fotos = [Path(f).read_bytes() for f in args.foto or []]
    servicio = _servicio(cfg, NotificadorConsola())
    p = servicio.crear(0, args.texto, fotos, segundos=args.segundos)
    print(resumen_plan(p, servicio.duracion_estimada(p)) if p.plan else f"ERROR {p.id}: {p.error}")


def cmd_procesar(cfg: Config, args) -> None:
    from .trabajador import Trabajador, bloqueo_de_proceso

    import time

    from .trabajador import INTERVALO_S

    servicio = _servicio(cfg, NotificadorConsola())
    trabajador = Trabajador(servicio)
    with bloqueo_de_proceso(cfg.datos / "trabajador.lock"):  # falla si el servicio ya esta procesando
        if args.forzar:
            trabajador.forzar()
        trabajador.ciclo()
        # --esperar: sigue ciclando (como el servicio, respetando la politica de inactividad) hasta vaciar la cola
        while args.esperar and servicio.almacen.listar((Estado.EN_COLA,)):
            log.info("cola pendiente; Qwen sin uso hace %.1f min (%s)", trabajador.monitor.minutos_inactivo(),
                     trabajador.monitor.estado)
            time.sleep(INTERVALO_S)
            trabajador.ciclo()


def cmd_facebook(cfg: Config, args) -> None:
    """Configura la pagina una sola vez: con el token de USUARIO de larga duracion (pedido por teclado sin
    mostrarlo, o el que quedo en secrets/facebook_usuario_token) guarda el token de la PAGINA (no vence) y el de
    usuario no queda en ningun lado."""
    import getpass

    from .acceso import escribir_privado
    from .facebook import ClienteFacebook, FacebookError, paginas_del_usuario

    fb = cfg.facebook
    if fb.token_usuario_archivo.exists():
        token_usuario = fb.token_usuario_archivo.read_text(encoding="utf-8").strip()
    else:
        token_usuario = getpass.getpass("Pegá el token de usuario de Meta (no se ve) y Enter: ").strip()
    if not token_usuario:
        sys.exit("no hay token")
    try:
        paginas = paginas_del_usuario(token_usuario)
    except FacebookError as exc:
        sys.exit(f"Meta rechazó el token: {exc}")
    finally:
        fb.token_usuario_archivo.unlink(missing_ok=True)
    if args.pagina:
        paginas = [p for p in paginas if args.pagina.lower() in p.nombre.lower() or p.id == args.pagina]
    if len(paginas) != 1:
        nombres = ", ".join(f"{p.nombre} ({p.id})" for p in paginas) or "ninguna con permiso de publicar"
        sys.exit(f"elegí la página con --pagina NOMBRE: {nombres}")
    pagina = paginas[0]
    try:  # antes de guardar nada: que el token de la pagina ande
        nombre = ClienteFacebook(pagina).nombre()
    except FacebookError as exc:
        sys.exit(f"Meta rechazó el token de la página: {exc}")
    escribir_privado(fb.token_archivo, pagina.token)
    escribir_privado(fb.pagina_archivo, json.dumps({"id": pagina.id, "nombre": pagina.nombre}, ensure_ascii=False))
    print(f"listo: {nombre} ({pagina.id}). El token de usuario no se guardó.")


def cmd_musica(cfg: Config, args) -> None:
    """Genera la biblioteca de musica de fondo (abre un turno de GPU: el Qwen queda apagado unos minutos)."""
    from . import musica
    from .gpu import TurnoGpu

    m = cfg.musica
    pedidos = musica.faltantes(cfg.montaje.musica_dir, args.por_estilo or m.por_estilo, 1000, m.segundos)
    if not pedidos:
        print("la biblioteca de música ya está completa")
        return
    activos = Almacen(cfg.datos / "studio.db").listar((Estado.GENERANDO, Estado.MONTANDO))
    if activos:
        sys.exit(f"hay reels generándose ({', '.join(p.id for p in activos)}): probá después")
    print(f"genero {len(pedidos)} tema(s)…", flush=True)
    with TurnoGpu(cfg.gpu, cfg.datos):
        salieron = musica.generar(m, pedidos)
    print(f"listo: {len(salieron)} de {len(pedidos)} en {cfg.montaje.musica_dir}")


def cmd_reescribir(cfg: Config, args) -> None:
    from .bot import resumen_plan

    servicio = _servicio(cfg, NotificadorConsola())
    p = servicio.reescribir(args.id)
    print(resumen_plan(p, servicio.duracion_estimada(p)))


def cmd_tomas(cfg: Config, args) -> None:
    """Corta un video en tomas y las suma a la biblioteca de video real (el Qwen mira cada una: tarda unos
    segundos por toma y espera si hay un turno de GPU)."""
    from . import ingesta, tomas

    if not args.video:
        por_fuente = tomas.resumen(cfg.tomas_dir)
        for fuente, (cantidad, segundos) in sorted(por_fuente.items()):
            print(f"{fuente}: {cantidad} tomas ({segundos:.0f} s)")
        print(f"total: {sum(n for n, _ in por_fuente.values())} tomas en {cfg.tomas_dir}")
        return
    video = Path(args.video)
    carpeta = cfg.tomas_dir / (ingesta.nombre_de_fuente(args.fuente or video.stem) or "video")
    if tomas.leer_indice(carpeta) and not args.sumar:
        sys.exit(f"ya hay tomas en {carpeta}: si es otro video de la misma fuente, agregá --sumar")
    servicio = _servicio(cfg, NotificadorConsola())
    tema = args.tema or ""
    try:
        r = ingesta.ingresar(video, carpeta, lambda cuadro: servicio.con_llm(ingesta.describir, cuadro, tema), tema,
                             max_segundos=args.max_minutos * 60, max_tomas=args.max_tomas)
    except ingesta.IngestaError as exc:
        sys.exit(f"no pude cortar {video}: {exc}")
    print(f"listo: {r.guardadas} tomas ({r.segundos:.0f} s) en {carpeta}; {r.descartadas} descartadas"
          + (f"; {r.sin_mirar} sin mirar (el Qwen no respondió): repetí el comando con --sumar" if r.sin_mirar else ""))


def cmd_estado(cfg: Config, _args) -> None:
    for p in Almacen(cfg.datos / "studio.db").listar((Estado.ESPERANDO_APROBACION, *ESTADOS_ACTIVOS, Estado.ERROR)):
        print(p.id, p.estado.value, p.plan.titular if p.plan else "", p.error)


def main() -> None:
    ap = argparse.ArgumentParser(prog="vstudio")
    ap.add_argument("--config", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bot")
    pl = sub.add_parser("planificar")
    pl.add_argument("--texto", required=True)
    pl.add_argument("--foto", action="append")
    pl.add_argument("--segundos", type=int, help="largo objetivo (por defecto segundos_por_defecto, 75)")
    sub.add_parser("aprobar").add_argument("id")
    sub.add_parser("revoz").add_argument("id")
    sub.add_parser("reescribir").add_argument("id")
    sub.add_parser("facebook").add_argument("--pagina", help="nombre o ID de la página (si administrás varias)")
    sub.add_parser("musica").add_argument("--por-estilo", type=int, help="temas por estilo (por defecto 4)")
    pr = sub.add_parser("procesar")
    pr.add_argument("--forzar", action="store_true")
    pr.add_argument("--esperar", action="store_true")
    sub.add_parser("voces")
    sub.add_parser("voz").add_argument("nombre")
    sub.add_parser("vincular")
    sub.add_parser("restaurar")
    sub.add_parser("estado")
    tm = sub.add_parser("tomas")
    tm.add_argument("video", nargs="?", help="archivo de video (sin esto, muestra el resumen)")
    tm.add_argument("--fuente", help="nombre de la carpeta (por defecto, el del archivo)")
    tm.add_argument("--tema", help="de que es el clip (se suma a las etiquetas de cada toma)")
    tm.add_argument("--sumar", action="store_true", help="agregar a una fuente que ya tiene tomas")
    tm.add_argument("--max-minutos", type=float, default=20.0,
                    help="largo maximo del video: uno mas largo se rechaza, no se corta a la mitad (por defecto 20)")
    tm.add_argument("--max-tomas", type=int, default=400,
                    help="tomas como maximo; si el video da mas, entran repartidas a lo largo (por defecto 400)")
    args = ap.parse_args()
    cfg = cargar_config(args.config)
    configurar_logs(cfg)
    acciones = {
        "bot": cmd_bot, "planificar": cmd_planificar, "procesar": cmd_procesar, "estado": cmd_estado,
        "aprobar": lambda c, a: print(_servicio(c, NotificadorConsola()).aprobar(a.id).estado.value),
        "revoz": lambda c, a: print(_servicio(c, NotificadorConsola()).rehacer_voz(a.id).estado.value),
        "reescribir": cmd_reescribir,
        "tomas": cmd_tomas,
        "facebook": cmd_facebook,
        "musica": cmd_musica,
        "voces": lambda c, a: print("\n".join(str(p) for p in voz.preparar_voces(c.voz))),
        "voz": lambda c, a: voz.elegir_voz(c.voz, a.nombre),
        "vincular": lambda c, a: (Acceso(c.telegram.usuarios_archivo, c.telegram.codigo_archivo).nuevo_codigo(),
                                  print(f"codigo nuevo en {c.telegram.codigo_archivo}")),
        "restaurar": lambda c, a: print("restaurado" if restaurar_si_hace_falta(c.gpu, c.datos) else "nada que hacer"),
    }
    acciones[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
