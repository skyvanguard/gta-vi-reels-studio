from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from tests.test_almacen_servicio_acceso import _proyecto
from vstudio import trabajador as trab_mod
from vstudio.acceso import Acceso
from vstudio.almacen import Almacen
from vstudio.bot import Bot
from vstudio.comfy import ResultadoClip
from vstudio.modelos import Estado
from vstudio.servicio import Servicio
from vstudio.telegram import ClienteTelegram, TelegramError

TOKEN = "123456:SECRETO-del-bot"


def test_errores_de_telegram_no_filtran_el_token():
    tg = ClienteTelegram(TOKEN)

    def falla(req):
        raise httpx.ConnectError(f"no se pudo conectar a {req.url}")
    tg._http = httpx.Client(transport=httpx.MockTransport(falla))
    with pytest.raises(TelegramError) as exc:
        tg.mensaje(1, "hola")
    assert "SECRETO" not in str(exc.value) and exc.value.__cause__ is None


class TgFalso:
    def __init__(self):
        self.enviados = []

    def mensaje(self, chat_id, texto, botones=None):
        self.enviados.append(("mensaje", chat_id, texto))

    def foto(self, chat_id, ruta, texto, botones=None):
        self.enviados.append(("foto", chat_id, texto))

    def video(self, chat_id, ruta, texto, botones=None):
        self.enviados.append(("video", chat_id, texto))

    def audio(self, chat_id, ruta, texto):
        self.enviados.append(("audio", chat_id, texto))

    def responder_boton(self, callback_id, texto=""):
        self.enviados.append(("boton", 0, texto))


class LlmQuieto:
    def metricas(self):
        return {"prompt_tokens_total": 1, "tokens_predicted_total": 1}

    def disponible(self):
        return True


@pytest.fixture
def entorno(cfg, tmp_path):
    tg = TgFalso()
    servicio = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), LlmQuieto(), tg)
    trabajador = trab_mod.Trabajador(servicio)
    acceso = Acceso(tmp_path / "s" / "u.json", tmp_path / "s" / "c")
    bot = Bot(tg, servicio, trabajador, acceso, tmp_path / "offset")
    return tg, servicio, trabajador, acceso, bot


def _msg(texto, usuario=7, chat=70, tipo="private"):
    return {"message": {"chat": {"id": chat, "type": tipo}, "from": {"id": usuario}, "text": texto}}


def test_bot_privado_y_vinculacion(entorno):
    tg, servicio, _, acceso, bot = entorno
    bot._manejar(_msg("hola"))
    assert tg.enviados == []  # a un desconocido no se le responde
    codigo = acceso.nuevo_codigo()
    bot._manejar(_msg(f"/vincular {codigo}", tipo="group"))
    assert tg.enviados == [] and not acceso.autorizado(7)  # en grupos no se atiende
    bot._manejar(_msg(f"/vincular {codigo}"))
    assert "Vinculado" in tg.enviados[-1][2] and acceso.autorizado(7)


def test_editar_por_comando_y_aprobar_por_boton(entorno):
    tg, servicio, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    servicio.almacen.guardar(_proyecto(chat=70))
    bot._manejar(_msg("/titular Nuevo gancho"))
    assert "🔥 Nuevo gancho" in tg.enviados[-1][2]
    bot._manejar(_msg("/texto 9 algo"))
    assert "⚠️" in tg.enviados[-1][2]
    bot._manejar({"callback_query": {"id": "q", "from": {"id": 7}, "message": {"chat": {"id": 70}}, "data": "ok:abc"}})
    assert servicio.almacen.obtener("abc").estado == Estado.EN_COLA
    bot._manejar({"callback_query": {"id": "q", "from": {"id": 99}, "message": {"chat": {"id": 70}}, "data": "no:abc"}})
    assert servicio.almacen.obtener("abc").estado == Estado.EN_COLA  # un extrano no puede cancelar


class TurnoFalso:
    abiertos = 0

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        TurnoFalso.abiertos += 1
        return self

    def __exit__(self, *exc):
        return False


def test_cola_respeta_la_politica_y_entrega(cfg, monkeypatch):
    workflow = Path(__file__).resolve().parents[1] / "workflows/wan/wan22_i2v_14b.api.json"
    cfg = replace(cfg, video=replace(cfg.video, workflow=workflow))
    tg = TgFalso()
    servicio = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), LlmQuieto(), tg)
    trabajador = trab_mod.Trabajador(servicio)
    carpeta = cfg.proyectos / "abc"
    carpeta.mkdir(parents=True)
    p = _proyecto(estado=Estado.EN_COLA, chat=70)
    p = p.con(plan=replace(p.plan, escenas=tuple(replace(e, clip="") for e in p.plan.escenas)))
    servicio.almacen.guardar(p)
    monkeypatch.setattr(trab_mod, "TurnoGpu", TurnoFalso)
    TurnoFalso.abiertos = 0
    pedidos = []

    def clip_falso(servidor, gpu, comfy_dir, plantilla, pedido, destino, timeout_s):
        pedidos.append((servidor, pedido.cuadros))
        destino.write_bytes(b"mp4")
        return ResultadoClip(ruta=destino, segundos=1.0, vram_pico_mib=100, prompt_id="x", workflow={})

    def reel_falso(escenas, titular, carpeta_, salida, *a, **k):
        salida.write_bytes(b"reel")
        return {"duracion_s": 14.4, "bytes": 4}
    monkeypatch.setattr(trab_mod.comfy, "generar_clip", clip_falso)
    monkeypatch.setattr(trab_mod.montaje, "armar_reel", reel_falso)

    trabajador.ciclo()  # politica "inactivo" y el Qwen recien muestreado: no abre turno
    assert TurnoFalso.abiertos == 0
    trabajador.forzar()
    trabajador.ciclo()
    final = servicio.almacen.obtener("abc")
    assert TurnoFalso.abiertos == 1 and final.estado == Estado.ENTREGADO
    assert all(e.clip for e in final.plan.escenas)
    assert len(pedidos) == 3 and all(c == 81 for _, c in pedidos)
    assert {s for s, _ in pedidos} <= {"http://127.0.0.1:8188", "http://127.0.0.1:8189"}
    assert any(tipo == "video" for tipo, _, _ in tg.enviados)


def test_escena_que_falla_pasa_a_error_sin_bucle_de_turnos(cfg, monkeypatch):
    workflow = Path(__file__).resolve().parents[1] / "workflows/wan/wan22_i2v_14b.api.json"
    cfg = replace(cfg, video=replace(cfg.video, workflow=workflow))
    tg = TgFalso()
    servicio = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), LlmQuieto(), tg)
    trabajador = trab_mod.Trabajador(servicio)
    (cfg.proyectos / "abc").mkdir(parents=True)
    p = _proyecto(estado=Estado.EN_COLA, chat=70)
    servicio.almacen.guardar(p.con(plan=replace(p.plan, escenas=tuple(replace(e, clip="") for e in p.plan.escenas))))
    monkeypatch.setattr(trab_mod, "TurnoGpu", TurnoFalso)
    TurnoFalso.abiertos = 0

    def clip_roto(*a, **k):
        raise KeyError("prompt_id")  # una excepcion "rara": antes mataba el hilo en silencio
    monkeypatch.setattr(trab_mod.comfy, "generar_clip", clip_roto)
    trabajador.forzar()
    trabajador.ciclo()
    final = servicio.almacen.obtener("abc")
    assert final.estado == Estado.ERROR and all(e.error for e in final.plan.escenas)
    trabajador.forzar()
    trabajador.ciclo()
    assert TurnoFalso.abiertos == 1  # no vuelve a apagar el Qwen por un proyecto en error


def test_rehacer_voz_no_espera_al_qwen_ni_abre_turno(cfg, monkeypatch):
    tg = TgFalso()
    servicio = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), LlmQuieto(), tg)
    trabajador = trab_mod.Trabajador(servicio)
    carpeta = cfg.proyectos / "abc"
    carpeta.mkdir(parents=True)
    p = _proyecto(estado=Estado.ENTREGADO, chat=70)
    for e in p.plan.escenas:
        (carpeta / e.clip).write_bytes(b"mp4")  # los clips ya existen
    servicio.almacen.guardar(p)
    monkeypatch.setattr(trab_mod, "TurnoGpu", TurnoFalso)
    TurnoFalso.abiertos = 0
    montajes = []

    def reel_falso(escenas, titular, carpeta_, salida, *a, **k):
        montajes.append(salida)
        salida.write_bytes(b"reel")
        return {"duracion_s": 60.0, "bytes": 4}
    monkeypatch.setattr(trab_mod.montaje, "armar_reel", reel_falso)
    servicio.rehacer_voz("abc")
    assert servicio.almacen.obtener("abc").estado == Estado.EN_COLA
    trabajador.ciclo()  # el Qwen "en uso" (recien muestreado) y politica inactivo: igual se procesa
    assert TurnoFalso.abiertos == 0 and len(montajes) == 1
    assert servicio.almacen.obtener("abc").estado == Estado.ENTREGADO


def test_reconciliar_reencola_lo_que_quedo_a_mitad(entorno):
    _, servicio, trabajador, _, _ = entorno
    servicio.almacen.guardar(_proyecto(estado=Estado.GENERANDO, id_="g1"))
    servicio.almacen.guardar(_proyecto(estado=Estado.MONTANDO, id_="m1"))
    servicio.almacen.guardar(_proyecto(estado=Estado.PLANIFICANDO, id_="p1"))
    trabajador.reconciliar()
    assert servicio.almacen.obtener("g1").estado == Estado.EN_COLA
    assert servicio.almacen.obtener("m1").estado == Estado.EN_COLA
    assert servicio.almacen.obtener("p1").estado == Estado.ERROR


@pytest.mark.parametrize("falla", [False, True])
def test_entregado_recien_cuando_telegram_lo_recibio(cfg, monkeypatch, falla):
    """Un reinicio corto la subida de un reel ya marcado ENTREGADO y nadie lo reenvio. Durante la
    subida tiene que seguir en MONTANDO (reconciliar lo reencola si se corta)."""
    servicio = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), LlmQuieto(), None)
    estados_al_subir = []

    class TgQueMira(TgFalso):
        def video(self, chat_id, ruta, texto, botones=None):
            estados_al_subir.append(servicio.almacen.obtener("abc").estado)
            if falla:
                raise TelegramError("Server disconnected without sending a response")

    servicio.notificador = TgQueMira()
    carpeta = cfg.proyectos / "abc"
    carpeta.mkdir(parents=True)
    for i in (1, 2, 3):
        (carpeta / f"clip_{i}.mp4").write_bytes(b"mp4")
    servicio.almacen.guardar(_proyecto(estado=Estado.GENERANDO, chat=70))
    monkeypatch.setattr(trab_mod.montaje, "armar_reel",
                        lambda escenas, titular, carpeta_, salida, *a, **k: {"duracion_s": 70.3, "bytes": 4})
    monkeypatch.setattr(trab_mod.time, "sleep", lambda s: None)
    trab_mod.Trabajador(servicio)._montar_y_entregar("abc")
    final = servicio.almacen.obtener("abc")
    assert set(estados_al_subir) == {Estado.MONTANDO}
    assert final.estado == Estado.ENTREGADO and final.reel == "reel_abc.mp4"
    assert ("no se pudo enviar" in final.error) is falla
    assert len(estados_al_subir) == (trab_mod.REINTENTOS_ENVIO if falla else 1)


def test_con_la_cpu_ocupada_la_voz_va_en_la_gpu_dentro_del_turno(cfg, monkeypatch):
    """Con otro trabajo llenando la CPU, 3 frases tardaban 17 min. La voz de lo que igual usa la GPU
    se hace al abrir el turno (Qwen apagado), antes de los clips."""
    workflow = Path(__file__).resolve().parents[1] / "workflows/wan/wan22_i2v_14b.api.json"
    cfg = replace(cfg, video=replace(cfg.video, workflow=workflow))
    servicio = Servicio(cfg, Almacen(cfg.datos / "db.sqlite"), LlmQuieto(), TgFalso())
    trabajador = trab_mod.Trabajador(servicio)
    (cfg.proyectos / "abc").mkdir(parents=True)
    p = _proyecto(estado=Estado.EN_COLA, chat=70)
    servicio.almacen.guardar(p.con(plan=replace(p.plan, escenas=tuple(replace(e, clip="") for e in p.plan.escenas))))
    eventos = []

    class Turno(TurnoFalso):
        def __enter__(self):
            eventos.append("turno")
            return self

    def clip_falso(servidor, gpu, comfy_dir, plantilla, pedido, destino, timeout_s):
        eventos.append("clip")
        destino.write_bytes(b"mp4")
        return ResultadoClip(ruta=destino, segundos=1.0, vram_pico_mib=100, prompt_id="x", workflow={})
    monkeypatch.setattr(trab_mod, "TurnoGpu", Turno)
    monkeypatch.setattr(trab_mod.comfy, "generar_clip", clip_falso)
    monkeypatch.setattr(trab_mod.montaje, "armar_reel", lambda *a, **k: {"duracion_s": 30.0, "bytes": 4})
    monkeypatch.setattr(trabajador, "_cpu_ocupada", lambda: True)
    monkeypatch.setattr(trabajador, "_preparar_voz", lambda p, en_gpu=False: eventos.append(f"voz gpu={en_gpu}") or p)
    trabajador.forzar()
    trabajador.ciclo()
    assert eventos.index("turno") < eventos.index("voz gpu=True") < eventos.index("clip")
    assert "voz gpu=False" in eventos  # el respaldo al montar (no hay audios): en la CPU


def test_si_la_voz_en_gpu_falla_no_se_apaga_la_voz(cfg, monkeypatch):
    servicio = Servicio(replace(cfg, voz=replace(cfg.voz, motor="chatterbox")), Almacen(cfg.datos / "db.sqlite"),
                        LlmQuieto(), TgFalso())
    trabajador = trab_mod.Trabajador(servicio)
    (cfg.proyectos / "abc").mkdir(parents=True)
    servicio.almacen.guardar(_proyecto(estado=Estado.GENERANDO, chat=70))
    usados = []

    def sintetizar(cfg_voz, textos, carpeta, **k):
        usados.append(cfg_voz.dispositivo)
        raise trab_mod.voz.VozError("CUDA out of memory")
    monkeypatch.setattr(trab_mod.voz, "sintetizar", sintetizar)
    p = trabajador._preparar_voz(servicio.almacen.obtener("abc"), en_gpu=True)
    assert usados == ["cuda"] and p.con_voz and servicio.almacen.obtener("abc").con_voz


def test_whisper_recibe_el_dispositivo(cfg, monkeypatch, tmp_path):
    from vstudio import subtitulos
    comandos = []

    class Res:
        returncode, stdout, stderr = 1, "", "x"
    monkeypatch.setattr(subtitulos.subprocess, "run", lambda cmd, **k: comandos.append(cmd) or Res())
    sub = replace(cfg.subtitulos, dispositivo="cuda", python=tmp_path / "py", script=tmp_path / "s.py")
    (tmp_path / "py").write_text("")
    (tmp_path / "s.py").write_text("")
    with pytest.raises(subtitulos.SubtitulosError):
        subtitulos.oir(sub, [tmp_path / "a.wav"], tmp_path)
    assert comandos and comandos[0][comandos[0].index("--dispositivo") + 1] == "cuda"
