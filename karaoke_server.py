"""
Servidor do karaoke (versao compativel com o site novo: nome, senha da festa, fila, regras).

Instalar:   pip install flask flask-cors pyautogui pygetwindow
Rodar:      python karaoke_server.py
Expor:      cloudflared tunnel --url http://localhost:5000
            -> cole o https://xxxx.trycloudflare.com em "api" no karaoke-config.json

karaoke-config.json (ao lado do index.html) deve ter EXATAMENTE estes nomes:
    {"api": "https://xxxx.trycloudflare.com", "key": "zezin2026", "cooldown": 15}

Teste rapido: abra https://xxxx.trycloudflare.com/status  -> {"ok":true,...}
"""
import hmac
import threading
import time
from collections import deque
from datetime import datetime
from urllib.parse import unquote

import pyautogui
from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    import pygetwindow as gw
except Exception:
    gw = None

# ---------------- CONFIGURACAO ----------------
PUBLIC_KEY = "zezin2026"          # igual ao "key" do karaoke-config.json
ADMIN_KEY = "GUILHERME2026"       # NUNCA colocar no site
SENHA_FESTA = "zezin2026" # senha que os convidados digitam (pergunte ao Ze)
ORIGENS_PERMITIDAS = [r"^https://guilherme-lopesz\.github\.io$"]
JANELA_TITULO = ""                # parte do titulo da janela do karaoke (vazio = nao forca foco)
INTERVALO_ENTRE_PEDIDOS = 1.5
INTERVALO_ENTRE_TECLAS = 0.35    # pausa entre cada numero (se ainda errar, aumente: 0.5)
TEMPO_TECLA_PRESSIONADA = 0.08   # quanto tempo cada tecla fica apertada
PAUSA_ANTES_DO_ENTER = 0.6       # espera depois do ultimo numero, antes do Enter
TEMPO_PRIORIDADE = 3.0

# Regras (o site le estes valores em /fila e mostra no "Como funciona")
MAX_FILA = 10                     # pedidos aguardando ao mesmo tempo
MAX_PESSOA = 3                    # pedidos aguardando por pessoa/aparelho
COOLDOWN_POR_CLIENTE = 15         # segundos entre pedidos do mesmo aparelho
REPETIR_MINUTOS = 30              # mesma musica so pode ser pedida de novo apos isso
MUSICAS_BLOQUEADAS = set()        # ex.: {"12345", "67890"}
MAX_TENTATIVAS_SENHA = 5          # senhas erradas por minuto, por IP
# ----------------------------------------------

pyautogui.PAUSE = 0.05
app = Flask(__name__)
CORS(
    app,
    origins=ORIGENS_PERMITIDAS,
    allow_headers=["Content-Type", "X-API-Key", "X-Cliente", "X-Senha"],
    methods=["GET", "POST", "OPTIONS"],
)

lock = threading.Lock()
fila = deque()                # itens aguardando: dict(codigo, nome, quem, hora, cliente, tipo)
enviados = deque(maxlen=10)   # ultimos enviados
comandos_pendentes = deque()  # comandos de teclado do admin
ultimo_pedido = {}            # cliente -> timestamp
recentes = {}                 # codigo -> timestamp do ultimo pedido
tentativas = {}               # ip -> lista de timestamps de senha errada
bloqueados = set()            # ids de aparelho bloqueados
estado = {"aberto": True, "pausado": False}


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


def digitar(codigo):
    """Digita numero por numero, devagar, segurando cada tecla um instante."""
    for ch in str(codigo):
        pyautogui.keyDown(ch)
        time.sleep(TEMPO_TECLA_PRESSIONADA)
        pyautogui.keyUp(ch)
        time.sleep(INTERVALO_ENTRE_TECLAS)
    time.sleep(PAUSA_ANTES_DO_ENTER)


def enviar_musica(codigo):
    digitar(codigo)
    pyautogui.press("enter")


def enviar_prioridade(codigo):
    digitar(codigo)
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
        item = None
        cmd = None
        with lock:
            if comandos_pendentes:
                cmd = comandos_pendentes.popleft()
            elif fila and not estado["pausado"]:
                item = fila.popleft()
        if cmd is None and item is None:
            time.sleep(0.3)
            continue
        try:
            focar_janela()
            if cmd is not None:
                COMANDOS[cmd]()
                print("OK: comando", cmd)
            else:
                if item["tipo"] == "prioridade":
                    enviar_prioridade(item["codigo"])
                else:
                    enviar_musica(item["codigo"])
                with lock:
                    enviados.appendleft(item)
                print("OK:", item["tipo"], item["codigo"], item["quem"])
        except Exception as e:
            print("Erro ao processar", cmd or item, e)
        time.sleep(INTERVALO_ENTRE_PEDIDOS)


def chave():
    return request.headers.get("X-API-Key", "")


def ip_cliente():
    return request.headers.get("CF-Connecting-IP", request.remote_addr)


def id_cliente():
    c = request.headers.get("X-Cliente", "").strip()[:40]
    return c or ip_cliente()


def senha_ok(senha):
    return hmac.compare_digest(senha.encode(), SENHA_FESTA.encode())


def publico(item):
    return {k: item[k] for k in ("codigo", "nome", "quem", "hora")}


def regras():
    return {
        "cooldown": COOLDOWN_POR_CLIENTE,
        "max_pessoa": MAX_PESSOA,
        "repetir": REPETIR_MINUTOS,
        "max_fila": MAX_FILA,
    }


@app.get("/")
def raiz():
    return "Servidor do karaoke no ar.", 200


@app.get("/status")
def status():
    return jsonify(ok=True, na_fila=len(fila))


@app.get("/fila")
def ver_fila():
    if chave() not in (PUBLIC_KEY, ADMIN_KEY):
        return jsonify(erro="chave"), 401
    with lock:
        return jsonify(
            regras=regras(),
            aberto=estado["aberto"],
            pausado=estado["pausado"],
            fila=[publico(i) for i in fila],
            enviados=[publico(i) for i in enviados],
        )


@app.post("/musica")
def musica():
    k = chave()
    if k not in (PUBLIC_KEY, ADMIN_KEY):
        return jsonify(erro="chave"), 401
    dados = request.get_json(silent=True) or {}
    codigo = str(dados.get("codigo", "")).strip()
    if not codigo.isdigit() or len(codigo) > 8:
        return jsonify(erro="codigo_invalido"), 400
    nome_musica = str(dados.get("nome", "")).strip()[:80]
    quem = str(dados.get("quem", "")).strip()[:20]

    eh_admin = k == ADMIN_KEY
    cliente = id_cliente()
    agora = time.time()

    with lock:
        if not eh_admin:
            ip = ip_cliente()

            # senha da festa (com limite de tentativas erradas)
            erradas = [t for t in tentativas.get(ip, []) if agora - t < 60]
            tentativas[ip] = erradas
            if len(erradas) >= MAX_TENTATIVAS_SENHA:
                return jsonify(erro="muitas_tentativas"), 429
            senha = unquote(request.headers.get("X-Senha", ""))
            if not senha_ok(senha):
                erradas.append(agora)
                return jsonify(erro="senha"), 401

            if cliente in bloqueados:
                return jsonify(erro="bloqueado"), 403
            if not estado["aberto"]:
                return jsonify(erro="fechado"), 403
            if not quem:
                return jsonify(erro="nome"), 400
            if codigo in MUSICAS_BLOQUEADAS:
                return jsonify(erro="musica_bloqueada"), 403

            ultimo = recentes.get(codigo, 0)
            if agora - ultimo < REPETIR_MINUTOS * 60:
                return jsonify(erro="repetida"), 409
            if sum(1 for i in fila if i["cliente"] == cliente) >= MAX_PESSOA:
                return jsonify(erro="limite"), 429
            espera = COOLDOWN_POR_CLIENTE - (agora - ultimo_pedido.get(cliente, 0))
            if espera > 0:
                return jsonify(erro="aguarde", espere=int(espera) + 1), 429
            if len(fila) >= MAX_FILA:
                return jsonify(erro="fila_cheia"), 503

            ultimo_pedido[cliente] = agora
            recentes[codigo] = agora
            # limpeza para os dicionarios nao crescerem sem parar
            for d in (ultimo_pedido, recentes):
                for antigo in [x for x, t in d.items() if agora - t > 7200]:
                    del d[antigo]

        tipo = "prioridade" if (eh_admin and dados.get("prioridade")) else "musica"
        fila.append({
            "codigo": codigo,
            "nome": nome_musica,
            "quem": quem or ("Admin" if eh_admin else ""),
            "hora": datetime.now().strftime("%H:%M"),
            "cliente": cliente,
            "tipo": tipo,
        })
        return jsonify(ok=True, posicao=len(fila))


@app.post("/comando")
def comando():
    """Teclas no programa do karaoke (so admin)."""
    if chave() != ADMIN_KEY:
        return jsonify(erro="nao autorizado"), 401
    dados = request.get_json(silent=True) or {}
    acao = dados.get("acao")
    if acao not in COMANDOS:
        return jsonify(erro="acao invalida", validas=list(COMANDOS)), 400
    with lock:
        comandos_pendentes.append(acao)
    return jsonify(ok=True)


@app.post("/admin")
def admin():
    """Controle da fila (so admin).
    acao: abrir | fechar | pausar_fila | retomar_fila | limpar_fila
          | remover (posicao: 1,2,...) | bloquear / desbloquear (posicao ou cliente)
    """
    if chave() != ADMIN_KEY:
        return jsonify(erro="nao autorizado"), 401
    dados = request.get_json(silent=True) or {}
    acao = dados.get("acao")
    with lock:
        if acao == "abrir":
            estado["aberto"] = True
        elif acao == "fechar":
            estado["aberto"] = False
        elif acao == "pausar_fila":
            estado["pausado"] = True
        elif acao == "retomar_fila":
            estado["pausado"] = False
        elif acao == "limpar_fila":
            fila.clear()
        elif acao in ("remover", "bloquear", "desbloquear"):
            cliente = dados.get("cliente")
            pos = dados.get("posicao")
            if pos is not None:
                try:
                    cliente_pos = list(fila)[int(pos) - 1]
                except (ValueError, IndexError):
                    return jsonify(erro="posicao invalida"), 400
                if acao == "remover":
                    fila.remove(cliente_pos)
                cliente = cliente or cliente_pos["cliente"]
            if acao == "bloquear" and cliente:
                bloqueados.add(cliente)
            elif acao == "desbloquear" and cliente:
                bloqueados.discard(cliente)
        else:
            return jsonify(erro="acao invalida"), 400
        return jsonify(ok=True, **estado, na_fila=len(fila), bloqueados=len(bloqueados))


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, threaded=True)
