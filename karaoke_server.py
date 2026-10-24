"""
Servidor do karaoke: recebe pedidos pela rede e "digita" o codigo no programa do karaoke.

Instalar:  pip install flask flask-cors pyautogui pygetwindow waitress
Rodar:     python karaoke_server.py
Tunel:     cloudflared tunnel --url http://127.0.0.1:5000
           (use 127.0.0.1 e nao "localhost": evita falha de IPv6 no Windows)
           -> copie o https://xxxx.trycloudflare.com e cole em "api" no karaoke-config.json

Teste:     abra https://xxxx.trycloudflare.com/status  -> {"ok":true,...}
Log:       karaoke_server.log (ao lado deste arquivo) guarda tudo o que aconteceu.

Comandos do operador (precisam da ADMIN_KEY, header X-API-Key), POST /comando {"acao": "..."}:
  pausar_pedidos | retomar_pedidos | limpar_fila | cancelar | limpar | pausar |
  volume_mais | volume_menos | sair
"""
import hmac
import logging
import os
import queue
import re
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler

import pyautogui
from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

try:
    import pygetwindow as gw
except Exception:
    gw = None

# ---------------- CONFIGURACAO ----------------
# Dica: defina por variavel de ambiente para nao deixar a chave no arquivo.
PUBLIC_KEY = os.environ.get("KARAOKE_PUBLIC_KEY", "zezin2026")      # igual a "key" do karaoke-config.json (NAO e segredo)
ADMIN_KEY = os.environ.get("KARAOKE_ADMIN_KEY", "GUILHERME2026")    # SEGREDO: troque por algo longo e aleatorio
# Dominio EXATO do site, sem o caminho (/karaoke-do-romao nao entra aqui)
ORIGENS_PERMITIDAS = [r"^https://guilherme-lopesz\.github\.io$"]
JANELA_TITULO = ""            # parte do titulo da janela do karaoke. PREENCHA: vazio = digita onde estiver o foco!
INTERVALO_ENTRE_PEDIDOS = 1.5
INTERVALO_ENTRE_TECLAS = 0.08
TEMPO_PRIORIDADE = 3.0
MAX_FILA = 20                 # maximo de pedidos aguardando para serem digitados
COOLDOWN_POR_CLIENTE = 15     # segundos entre pedidos do mesmo celular (o site usa o mesmo valor em "cooldown")
JANELA_DUPLICADO = 600        # segundos em que a mesma musica nao pode ser pedida de novo (evita repeticao na fila)
LIMITE_POR_IP_POR_MIN = 90    # teto por rede/IP (alto de proposito: no wifi da festa todos compartilham o mesmo IP)
TENTATIVAS_FOCO = 3           # tentativas de trazer o karaoke para frente antes de desistir do pedido
FAILSAFE_CANTO_DO_MOUSE = False  # True = mouse no canto da tela aborta a digitacao (mas pode perder pedidos na festa)
PORTA = 5000
# ----------------------------------------------

pyautogui.PAUSE = INTERVALO_ENTRE_TECLAS
pyautogui.FAILSAFE = FAILSAFE_CANTO_DO_MOUSE

log = logging.getLogger("karaoke")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
_handlers = [logging.StreamHandler()]
try:
    _handlers.append(RotatingFileHandler("karaoke_server.log", maxBytes=500_000, backupCount=2, encoding="utf-8"))
except OSError:
    pass
for _h in _handlers:
    _h.setFormatter(_fmt)
    log.addHandler(_h)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2048  # um pedido tem poucos bytes; recusa lixo grande
CORS(app, origins=ORIGENS_PERMITIDAS, allow_headers=["Content-Type", "X-API-Key", "X-Client-Id"],
     methods=["GET", "POST", "OPTIONS"], max_age=600)

CODIGO_RE = re.compile(r"[0-9]{1,8}")        # [0-9] de proposito: \d e isdigit() aceitam digitos unicode ("²", "٣")
CID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")

fila = queue.Queue()
lock = threading.Lock()
worker_lock = threading.Lock()
pendentes = {}            # codigo -> quantas copias aguardando/em andamento
recentes = {}             # codigo -> instante do ultimo pedido aceito
ultimo_por_cliente = {}   # cliente -> instante do ultimo pedido aceito
hits_ip = {}              # ip -> instantes das ultimas tentativas
falhas_auth = {}          # ip -> instantes de chaves erradas
pausado = False
worker_thread = None
_limpeza_prox = 0.0


class FocoErro(Exception):
    pass


def agora():
    return time.monotonic()


def erro(msg, motivo, status, **extra):
    return jsonify(erro=msg, motivo=motivo, **extra), status


# ---------------- digitacao ----------------
def garantir_foco():
    """Traz o karaoke para frente e CONFIRMA. Se nao der, nao digita (evita escrever em outro programa)."""
    if not JANELA_TITULO:
        return
    if gw is None:
        raise FocoErro("pygetwindow nao esta instalado")
    janelas = gw.getWindowsWithTitle(JANELA_TITULO)
    if not janelas:
        raise FocoErro(f"janela com '{JANELA_TITULO}' no titulo nao encontrada")
    try:
        if janelas[0].isMinimized:
            janelas[0].restore()
        janelas[0].activate()
    except Exception:
        pass  # o pygetwindow as vezes acusa erro mesmo ativando; a checagem abaixo e que vale
    time.sleep(0.3)
    ativa = gw.getActiveWindow()
    if not ativa or JANELA_TITULO.lower() not in (ativa.title or "").lower():
        raise FocoErro("o karaoke nao ficou em primeiro plano")


def enviar_musica(codigo):
    pyautogui.write(str(codigo), interval=INTERVALO_ENTRE_TECLAS)
    pyautogui.press("enter")


def enviar_prioridade(codigo):
    pyautogui.write(str(codigo), interval=INTERVALO_ENTRE_TECLAS)
    pyautogui.keyDown("enter")
    try:
        time.sleep(TEMPO_PRIORIDADE)
    finally:
        pyautogui.keyUp("enter")  # garante que a tecla nunca fique presa


COMANDOS = {
    "cancelar": lambda: pyautogui.press("q"),
    "limpar": lambda: pyautogui.press("u"),
    "pausar": lambda: pyautogui.press("space"),
    "volume_mais": lambda: pyautogui.press("m"),
    "volume_menos": lambda: pyautogui.press("n"),
    "sair": lambda: pyautogui.press("esc"),
}


def processar(tipo, valor):
    for tentativa in range(1, TENTATIVAS_FOCO + 1):
        try:
            garantir_foco()
            break
        except FocoErro as e:
            log.warning("Foco falhou (%d/%d): %s", tentativa, TENTATIVAS_FOCO, e)
            if tentativa == TENTATIVAS_FOCO:
                raise
            time.sleep(3)
    if tipo == "musica":
        enviar_musica(valor)
    elif tipo == "prioridade":
        enviar_prioridade(valor)
    elif tipo == "comando":
        COMANDOS[valor]()


def liberar(tipo, valor, falhou):
    if tipo == "comando":
        return
    with lock:
        n = pendentes.get(valor, 0) - 1
        if n > 0:
            pendentes[valor] = n
        else:
            pendentes.pop(valor, None)
        if falhou:
            recentes.pop(valor, None)  # pedido nao foi digitado: a pessoa pode tentar de novo


def worker():
    while True:
        try:
            tipo, valor = fila.get()
            falhou = True
            try:
                processar(tipo, valor)
                falhou = False
                log.info("OK: %s %s", tipo, valor)
            except Exception as e:
                log.error("PEDIDO PERDIDO: %s %s (%s: %s)", tipo, valor, type(e).__name__, e)
            finally:
                liberar(tipo, valor, falhou)
                fila.task_done()
            time.sleep(INTERVALO_ENTRE_PEDIDOS)
        except Exception:
            log.exception("Erro inesperado no digitador (segue funcionando)")
            time.sleep(1)


def garantir_worker():
    """Se a thread que digita tivesse morrido, os pedidos ficariam 'aceitos' e nunca digitados. Isso a revive."""
    global worker_thread
    with worker_lock:
        if worker_thread is None or not worker_thread.is_alive():
            worker_thread = threading.Thread(target=worker, daemon=True, name="digitador")
            worker_thread.start()


# ---------------- seguranca / limites ----------------
def ip_cliente():
    ra = request.remote_addr or "?"
    if ra in ("127.0.0.1", "::1"):  # so confia no cabecalho do Cloudflare quando a conexao vem do proprio tunel
        return (request.headers.get("CF-Connecting-IP") or ra)[:64]
    return ra


def papel():
    k = request.headers.get("X-API-Key", "").encode("utf-8", "ignore")
    if hmac.compare_digest(k, ADMIN_KEY.encode()):
        return "admin"
    if hmac.compare_digest(k, PUBLIC_KEY.encode()):
        return "publico"
    return None


def _recentes_na_janela(d, t, janela):
    while d and t - d[0] > janela:
        d.popleft()
    return len(d)


def limpar_antigos(t):
    """Evita que os dicionarios cresçam sem parar. Chamar com 'lock' em maos."""
    global _limpeza_prox
    if t < _limpeza_prox:
        return
    _limpeza_prox = t + 60
    for d, validade in ((ultimo_por_cliente, 3600), (recentes, JANELA_DUPLICADO)):
        for k in [k for k, v in d.items() if t - v > validade]:
            del d[k]
    for d in (hits_ip, falhas_auth):
        for k in [k for k, v in d.items() if not v or t - v[-1] > 600]:
            del d[k]


def autenticar():
    """Retorna (papel, resposta_de_erro). Quem erra a chave muitas vezes fica bloqueado por 5 min (anti forca-bruta)."""
    ip, t = ip_cliente(), agora()
    with lock:
        limpar_antigos(t)
        if _recentes_na_janela(falhas_auth.setdefault(ip, deque()), t, 300) >= 20:
            return None, erro("muitas tentativas", "bloqueado", 429, espere=300)
    p = papel()
    if p is None:
        with lock:
            falhas_auth.setdefault(ip, deque()).append(t)
        log.warning("Chave invalida vinda de %s", ip)
        return None, erro("nao autorizado", "nao_autorizado", 401)
    return p, None


# ---------------- rotas ----------------
@app.errorhandler(Exception)
def tratar_erro(e):
    if isinstance(e, HTTPException):
        return erro(e.description or e.name, f"http_{e.code}", e.code)
    log.exception("Erro nao tratado em %s", request.path)
    return erro("erro interno", "interno", 500)


@app.get("/")
def raiz():
    return "Servidor do karaoke no ar. Os pedidos chegam em /musica.", 200


@app.get("/status")
def status():
    # Sem chave: serve so para voce testar se o tunel esta funcionando.
    garantir_worker()
    return jsonify(ok=True, na_fila=fila.qsize(), pausado=pausado, digitador=worker_thread.is_alive())


@app.post("/musica")
def musica():
    garantir_worker()
    p, e = autenticar()
    if e:
        return e
    dados = request.get_json(silent=True)
    codigo = dados.get("codigo") if isinstance(dados, dict) else None
    if isinstance(codigo, int) and not isinstance(codigo, bool):
        codigo = str(codigo)
    codigo = codigo.strip() if isinstance(codigo, str) else ""
    if not CODIGO_RE.fullmatch(codigo):
        return erro("codigo invalido", "invalido", 400)

    admin = p == "admin"
    tipo = "prioridade" if (admin and dados.get("prioridade")) else "musica"
    ip, t = ip_cliente(), agora()
    with lock:
        if not admin:
            if pausado:
                return erro("pedidos pausados", "pausado", 503)
            h = hits_ip.setdefault(ip, deque())
            if _recentes_na_janela(h, t, 60) >= LIMITE_POR_IP_POR_MIN:
                return erro("muitos pedidos desta rede", "aguarde", 429, espere=30)
            h.append(t)
            # Identifica o CELULAR (id gerado pelo site), nao o IP: no wifi da festa todos teriam o mesmo IP.
            cid = request.headers.get("X-Client-Id", "")
            chave = "c:" + cid if CID_RE.fullmatch(cid) else "ip:" + ip
            espera = COOLDOWN_POR_CLIENTE - (t - ultimo_por_cliente.get(chave, float("-inf")))
            if espera > 0:
                return erro("aguarde", "aguarde", 429, espere=int(espera) + 1)
            if fila.qsize() >= MAX_FILA:
                return erro("fila cheia, tente em instantes", "fila_cheia", 503)
            if codigo in pendentes or t - recentes.get(codigo, float("-inf")) < JANELA_DUPLICADO:
                return erro("essa musica ja foi pedida ha pouco", "duplicado", 409)
            ultimo_por_cliente[chave] = t
        pendentes[codigo] = pendentes.get(codigo, 0) + 1
        recentes[codigo] = t
        fila.put((tipo, codigo))
        log.info("Aceito: %s %s (de %s)", tipo, codigo, ip)
        return jsonify(ok=True, na_fila=fila.qsize())


@app.post("/comando")
def comando():
    global pausado
    garantir_worker()
    p, e = autenticar()
    if e:
        return e
    if p != "admin":
        return erro("nao autorizado", "nao_autorizado", 401)
    dados = request.get_json(silent=True)
    acao = dados.get("acao") if isinstance(dados, dict) else None
    if acao == "pausar_pedidos":
        pausado = True
        log.warning("Pedidos pelo site PAUSADOS")
        return jsonify(ok=True, pausado=True)
    if acao == "retomar_pedidos":
        pausado = False
        log.warning("Pedidos pelo site RETOMADOS")
        return jsonify(ok=True, pausado=False)
    if acao == "limpar_fila":
        with lock:
            while True:
                try:
                    fila.get_nowait()
                    fila.task_done()
                except queue.Empty:
                    break
            pendentes.clear()
            recentes.clear()
        log.warning("Fila de digitacao LIMPA")
        return jsonify(ok=True)
    if not isinstance(acao, str) or acao not in COMANDOS:
        validas = list(COMANDOS) + ["pausar_pedidos", "retomar_pedidos", "limpar_fila"]
        return erro("acao invalida", "invalido", 400, validas=validas)
    fila.put(("comando", acao))
    return jsonify(ok=True)


def avisos_iniciais():
    if len(ADMIN_KEY) < 12 or ADMIN_KEY.lower() == ADMIN_KEY or ADMIN_KEY == PUBLIC_KEY:
        log.warning("ADMIN_KEY fraca/curta. Gere uma: python -c \"import secrets;print(secrets.token_urlsafe(16))\"")
    if not JANELA_TITULO:
        log.warning("JANELA_TITULO vazio: os codigos serao digitados na janela que estiver em foco!")
    elif gw is None:
        log.warning("pygetwindow indisponivel: nao sera possivel focar o karaoke (pedidos vao falhar).")
    if not FAILSAFE_CANTO_DO_MOUSE:
        log.info("Failsafe do mouse desligado. Para parar: feche esta janela ou use /comando pausar_pedidos.")


if __name__ == "__main__":
    avisos_iniciais()
    garantir_worker()
    log.info("Servidor em http://127.0.0.1:%d (so aceita conexoes do proprio PC, via tunel)", PORTA)
    try:
        from waitress import serve  # servidor de producao: aguenta conexoes lentas/quebradas sem travar
        serve(app, host="127.0.0.1", port=PORTA, threads=8)
    except ImportError:
        log.warning("waitress nao instalado (pip install waitress); usando o servidor de desenvolvimento do Flask.")
        app.run(host="127.0.0.1", port=PORTA, threaded=True)
