"""
Servidor que recebe pedidos de musica pela rede e "digita" no programa do karaoke.

Instalar:   pip install flask flask-cors pyautogui pygetwindow
Rodar:      python karaoke_server.py
Expor com HTTPS (necessario p/ o site no github.io):
            cloudflared tunnel --url http://localhost:5000
            -> copie o endereco https://xxxx.trycloudflare.com que aparecer e cole em
               "api" no arquivo karaoke-config.json (fica ao lado do index.html no site).

Teste rapido: abra  https://xxxx.trycloudflare.com/status  no navegador. Deve mostrar {"ok":true,...}
"""
import queue
import threading
import time

import pyautogui
from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    import pygetwindow as gw
except Exception:
    gw = None

# ---------------- CONFIGURACAO ----------------
PUBLIC_KEY = "zezin2026"   # a mesma "key" do karaoke-config.json (so permite /musica)
ADMIN_KEY = "GUILHERME2026"      # NUNCA colocar no site (libera /comando e prioridade)
# Dominio EXATO do site, sem o caminho (/karaoke-do-romao nao entra aqui)
ORIGENS_PERMITIDAS = [r"^https://guilherme-lopesz\.github\.io$"]
JANELA_TITULO = ""            # parte do titulo da janela do karaoke (vazio = nao forca foco)
INTERVALO_ENTRE_PEDIDOS = 1.5
INTERVALO_ENTRE_TECLAS = 0.08
TEMPO_PRIORIDADE = 3.0
MAX_FILA = 15                 # maximo de pedidos aguardando
COOLDOWN_POR_CLIENTE = 15     # segundos entre pedidos do mesmo celular (o site usa o mesmo valor em "cooldown")
# ----------------------------------------------

pyautogui.PAUSE = INTERVALO_ENTRE_TECLAS
app = Flask(__name__)
CORS(app, origins=ORIGENS_PERMITIDAS, allow_headers=["Content-Type", "X-API-Key"], methods=["GET", "POST", "OPTIONS"])
fila = queue.Queue()
ultimo_pedido = {}
lock = threading.Lock()


def focar_janela():
    if not JANELA_TITULO or gw is None:
        return
    janelas = gw.getWindowsWithTitle(JANELA_TITULO)
    if janelas:
        try:
            if janelas[0].isMinimized:
                janelas[0].restore()
            janelas[0].activate()
            time.sleep(0.2)
        except Exception:
            pass


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


def worker():
    while True:
        tipo, valor = fila.get()
        try:
            focar_janela()
            if tipo == "musica":
                enviar_musica(valor)
            elif tipo == "prioridade":
                enviar_prioridade(valor)
            elif tipo == "comando":
                COMANDOS[valor]()
            print("OK:", tipo, valor)
        except Exception as e:
            print("Erro ao processar", tipo, valor, e)
        time.sleep(INTERVALO_ENTRE_PEDIDOS)
        fila.task_done()


def chave():
    return request.headers.get("X-API-Key")


def ip_cliente():
    return request.headers.get("CF-Connecting-IP", request.remote_addr)


@app.get("/")
def raiz():
    return "Servidor do karaoke no ar. Os pedidos chegam em /musica.", 200


@app.get("/status")
def status():
    # Sem chave: serve so para voce testar se o tunel esta funcionando.
    return jsonify(ok=True, na_fila=fila.qsize())


@app.post("/musica")
def musica():
    k = chave()
    if k not in (PUBLIC_KEY, ADMIN_KEY):
        return jsonify(erro="nao autorizado"), 401
    dados = request.get_json(silent=True) or {}
    codigo = str(dados.get("codigo", "")).strip()
    if not codigo.isdigit() or len(codigo) > 8:
        return jsonify(erro="codigo invalido"), 400

    eh_admin = k == ADMIN_KEY
    with lock:
        if not eh_admin:
            if fila.qsize() >= MAX_FILA:
                return jsonify(erro="fila cheia, tente em instantes"), 503
            ip = ip_cliente()
            agora = time.time()
            espera = COOLDOWN_POR_CLIENTE - (agora - ultimo_pedido.get(ip, 0))
            if espera > 0:
                return jsonify(erro="aguarde", espere=int(espera) + 1), 429
            ultimo_pedido[ip] = agora
            # limpa registros antigos para o dicionario nao crescer sem parar
            for antigo in [i for i, t in ultimo_pedido.items() if agora - t > 3600]:
                del ultimo_pedido[antigo]

        tipo = "prioridade" if (eh_admin and dados.get("prioridade")) else "musica"
        fila.put((tipo, codigo))
        return jsonify(ok=True, na_fila=fila.qsize())


@app.post("/comando")
def comando():
    if chave() != ADMIN_KEY:
        return jsonify(erro="nao autorizado"), 401
    dados = request.get_json(silent=True) or {}
    acao = dados.get("acao")
    if acao not in COMANDOS:
        return jsonify(erro="acao invalida", validas=list(COMANDOS)), 400
    fila.put(("comando", acao))
    return jsonify(ok=True)


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, threaded=True)
