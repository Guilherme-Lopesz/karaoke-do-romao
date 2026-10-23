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
import hmac
from urllib.parse import unquote
import threading
import time

import pyautogui
from flask import Flask, Response, jsonify, request
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
INTERVALO_ENTRE_TECLAS = 0.25      # pausa entre um digito e o proximo (aumente se ainda perder digitos)
TEMPO_TECLA_PRESSIONADA = 0.08    # quanto tempo cada tecla fica apertada
ESPERA_ANTES_DE_DIGITAR = 0.6     # espera apos focar a janela, antes do primeiro digito
TEMPO_PRIORIDADE = 3.0
MAX_FILA = 15                 # maximo de pedidos aguardando
SENHA_FESTA = "zezin63"         # senha que o convidado digita no site (so quem sabe consegue pedir)
MAX_POR_PESSOA = 3            # pedidos aguardando por pessoa ao mesmo tempo
REPETIR_MIN = 30              # minutos ate a mesma musica poder ser pedida de novo
COOLDOWN_POR_CLIENTE = 15     # segundos entre pedidos do mesmo celular (o site usa o mesmo valor em "cooldown")
# ----------------------------------------------

pyautogui.PAUSE = 0.1
app = Flask(__name__)
CORS(app, origins=ORIGENS_PERMITIDAS, allow_headers=["Content-Type", "X-API-Key", "X-Senha", "X-Cliente"],
     methods=["GET", "POST", "OPTIONS"])
lock = threading.RLock()
cond = threading.Condition(lock)
pedidos = []                       # historico + fila (status: aguardando, digitando, enviado, cancelado, erro)
estado = {"aberto": True, "pausado": False, "seq": 0}
bloq_codigos, bloq_clientes = set(), set()
ultimo, falhas = {}, {}


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
    """Digita digito a digito, segurando cada tecla, para o karaoke nao perder nenhum."""
    time.sleep(ESPERA_ANTES_DE_DIGITAR)
    for c in str(codigo):
        pyautogui.keyDown(c)
        time.sleep(TEMPO_TECLA_PRESSIONADA)
        pyautogui.keyUp(c)
        time.sleep(INTERVALO_ENTRE_TECLAS)


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


def novo_pedido(tipo, valor, nome="", cli="", prio=False):
    estado["seq"] += 1
    p = dict(id=estado["seq"], tipo=tipo, codigo=str(valor), nome=nome, cliente=cli, prio=prio,
             status="aguardando", t=time.time())
    pedidos.append(p)
    if len(pedidos) > 300:  # descarta os mais antigos ja finalizados
        pedidos[:] = [x for x in pedidos[:-300] if x["status"] in ("aguardando", "digitando")] + pedidos[-300:]
    cond.notify_all()
    return p


def worker():
    while True:
        with cond:
            while True:
                espera = [p for p in pedidos if p["status"] == "aguardando"]
                if espera and not estado["pausado"]:
                    p = min(espera, key=lambda x: (not x["prio"], x["id"]))
                    p["status"] = "digitando"
                    break
                cond.wait(1)
        try:
            focar_janela()
            if p["tipo"] == "musica":
                enviar_musica(p["codigo"])
            elif p["tipo"] == "prioridade":
                enviar_prioridade(p["codigo"])
            else:
                COMANDOS[p["codigo"]]()
            p["status"] = "enviado"
            print("OK:", p["tipo"], p["codigo"], p["nome"])
        except Exception as e:
            p["status"] = "erro"
            print("Erro ao processar", p["tipo"], p["codigo"], e)
        time.sleep(INTERVALO_ENTRE_PEDIDOS)


def chave():
    return request.headers.get("X-API-Key")


def ip_cliente():
    return request.headers.get("CF-Connecting-IP", request.remote_addr)


def cliente():
    c = "".join(ch for ch in (request.headers.get("X-Cliente") or "")[:40] if ch.isalnum() or ch in "-_")
    return c or ip_cliente()


def verificar_senha():
    """None se a senha esta certa; senao a resposta de erro. Limita tentativas (5 por minuto)."""
    ip, agora = ip_cliente(), time.time()
    with lock:
        f = [t for t in falhas.get(ip, []) if agora - t < 60]
        falhas[ip] = f
        if len(f) >= 5:
            return jsonify(erro="muitas_tentativas"), 429
        enviada = unquote(request.headers.get("X-Senha") or "").encode("utf-8")
        if not hmac.compare_digest(enviada, SENHA_FESTA.encode("utf-8")):
            f.append(agora)
            return jsonify(erro="senha"), 401
    return None


def hora(t):
    return time.strftime("%H:%M", time.localtime(t))


def publico(p):
    return dict(id=p["id"], codigo=p["codigo"], nome=p["nome"], status=p["status"], hora=hora(p["t"]))


@app.get("/")
def raiz():
    return "Servidor do karaoke no ar. Os pedidos chegam em /musica.", 200


@app.get("/status")
def status():
    return jsonify(ok=True, aberto=estado["aberto"])


@app.post("/entrar")
def entrar():
    if chave() not in (PUBLIC_KEY, ADMIN_KEY):
        return jsonify(erro="nao autorizado"), 401
    return verificar_senha() or jsonify(ok=True)


@app.get("/fila")
def ver_fila():
    if chave() not in (PUBLIC_KEY, ADMIN_KEY):
        return jsonify(erro="nao autorizado"), 401
    with lock:
        musicas = [p for p in pedidos if p["tipo"] != "comando"]
        return jsonify(
            aberto=estado["aberto"], pausado=estado["pausado"],
            regras=dict(cooldown=COOLDOWN_POR_CLIENTE, max_pessoa=MAX_POR_PESSOA, repetir=REPETIR_MIN, max_fila=MAX_FILA),
            fila=[publico(p) for p in musicas if p["status"] in ("aguardando", "digitando")],
            enviados=[publico(p) for p in musicas if p["status"] == "enviado"][-8:][::-1])


@app.post("/musica")
def musica():
    k = chave()
    if k not in (PUBLIC_KEY, ADMIN_KEY):
        return jsonify(erro="nao autorizado"), 401
    dados = request.get_json(silent=True) or {}
    codigo = str(dados.get("codigo", "")).strip()
    if not codigo.isdigit() or len(codigo) > 8:
        return jsonify(erro="codigo invalido"), 400
    nome = str(dados.get("nome", "")).strip()[:80]
    eh_admin = k == ADMIN_KEY
    cli = cliente()
    if not eh_admin:
        if cli in bloq_clientes:
            return jsonify(erro="bloqueado"), 403
        erro = verificar_senha()
        if erro:
            return erro
        if not estado["aberto"]:
            return jsonify(erro="fechado"), 403
        if codigo in bloq_codigos:
            return jsonify(erro="musica_bloqueada"), 403
    with lock:
        if not eh_admin:
            agora = time.time()
            espera = [p for p in pedidos if p["status"] == "aguardando" and p["tipo"] != "comando"]
            if len(espera) >= MAX_FILA:
                return jsonify(erro="fila_cheia"), 503
            if sum(p["cliente"] == cli for p in espera) >= MAX_POR_PESSOA:
                return jsonify(erro="limite"), 429
            if any(p["codigo"] == codigo and p["tipo"] != "comando" and p["status"] in ("aguardando", "digitando", "enviado")
                   and agora - p["t"] < REPETIR_MIN * 60 for p in pedidos):
                return jsonify(erro="repetida"), 409
            falta = COOLDOWN_POR_CLIENTE - (agora - ultimo.get(cli, 0))
            if falta > 0:
                return jsonify(erro="aguarde", espere=int(falta) + 1), 429
            ultimo[cli] = agora
        prio = eh_admin and bool(dados.get("prioridade"))
        p = novo_pedido("prioridade" if prio else "musica", codigo, nome, cli, prio)
        pos = sum(1 for x in pedidos if x["status"] == "aguardando" and x["id"] <= p["id"] and x["tipo"] != "comando")
        return jsonify(ok=True, id=p["id"], posicao=pos)


@app.post("/comando")
def comando():
    if chave() != ADMIN_KEY:
        return jsonify(erro="nao autorizado"), 401
    acao = (request.get_json(silent=True) or {}).get("acao")
    if acao not in COMANDOS:
        return jsonify(erro="acao invalida", validas=list(COMANDOS)), 400
    with lock:
        novo_pedido("comando", acao, acao, "admin", True)
    return jsonify(ok=True)


# ---------------- PAINEL DO ADMIN (abra http://localhost:5000/admin no notebook) ----------------
@app.get("/admin")
def painel():
    return Response(PAINEL, mimetype="text/html")


@app.get("/admin/estado")
def admin_estado():
    if chave() != ADMIN_KEY:
        return jsonify(erro="nao autorizado"), 401
    with lock:
        return jsonify(aberto=estado["aberto"], pausado=estado["pausado"],
                       pedidos=[dict(publico(p), cliente=p["cliente"], tipo=p["tipo"]) for p in pedidos[-80:]],
                       bloq_codigos=sorted(bloq_codigos), bloq_clientes=sorted(bloq_clientes))


@app.post("/admin/acao")
def admin_acao():
    if chave() != ADMIN_KEY:
        return jsonify(erro="nao autorizado"), 401
    d = request.get_json(silent=True) or {}
    a, v = d.get("acao"), str(d.get("valor", ""))
    with lock:
        if a in ("abrir", "fechar"):
            estado["aberto"] = a == "abrir"
        elif a in ("pausar", "retomar"):
            estado["pausado"] = a == "pausar"
            cond.notify_all()
        elif a == "limpar":
            for p in pedidos:
                if p["status"] == "aguardando":
                    p["status"] = "cancelado"
        elif a == "cancelar":
            for p in pedidos:
                if str(p["id"]) == v and p["status"] == "aguardando":
                    p["status"] = "cancelado"
        elif a == "bloquear":
            bloq_codigos.add(v)
        elif a == "desbloquear":
            bloq_codigos.discard(v)
        elif a == "bloquear_cliente":
            bloq_clientes.add(v)
        elif a == "desbloquear_cliente":
            bloq_clientes.discard(v)
        else:
            return jsonify(erro="acao invalida"), 400
    return jsonify(ok=True)


PAINEL = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Painel do karaokê</title>
<style>body{font:16px system-ui;background:#1a0f08;color:#f6e7c6;max-width:760px;margin:auto;padding:12px}
button{font:inherit;padding:8px 12px;margin:3px;border-radius:8px;border:0;background:#e9b949;color:#2b1a0a;cursor:pointer}
button.r{background:#c0392b;color:#fff}input{font:inherit;padding:8px;border-radius:8px;border:1px solid #6b4a1f;background:#24140b;color:#f6e7c6}
li{padding:7px 0;border-bottom:1px solid #6b4a1f;list-style:none}ul{padding:0}small{color:#c9ae80}h2{margin:20px 0 6px}#st{font-weight:700}</style>
<h1>🎤 Painel do karaokê</h1>
<div id=login><input id=k type=password placeholder="ADMIN_KEY"> <button onclick="entrar()">Entrar</button></div>
<div id=app hidden>
<p id=st></p>
<button onclick="a('abrir')">Abrir pedidos</button><button class=r onclick="a('fechar')">Fechar pedidos</button>
<button onclick="a('pausar')">Pausar digitação</button><button onclick="a('retomar')">Retomar</button>
<button class=r onclick="a('limpar')">Limpar fila</button>
<h2>Controle do karaokê</h2>
<button onclick="c('pausar')">Pausar/Tocar</button><button onclick="c('cancelar')">Cancelar música</button><button onclick="c('limpar')">Limpar lista</button>
<button onclick="c('volume_mais')">Volume +</button><button onclick="c('volume_menos')">Volume −</button>
<h2>Fila</h2><ul id=f></ul><h2>Histórico</h2><ul id=h></ul>
<h2>Bloqueios</h2><input id=b placeholder="código da música"><button onclick="a('bloquear',b.value);b.value=''">Bloquear música</button>
<ul id=bq></ul></div>
<script>
let K=sessionStorage.getItem('k')||'';
const E=s=>String(s).replace(/[&<>"']/g,c=>'&#'+c.charCodeAt(0)+';');
async function api(p,b){try{const r=await fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json','X-API-Key':K},body:b?JSON.stringify(b):undefined});return r.ok?r.json():null}catch(e){return null}}
const li=p=>`<li><b>${E(p.codigo)}</b> ${E(p.nome)} <small>${p.status} · ${p.hora} · ${E(p.cliente)}</small>
${p.status=='aguardando'?`<button class=r onclick="a('cancelar','${p.id}')">Cancelar</button>`:''}${p.tipo!='comando'?`<button onclick="a('bloquear_cliente','${E(p.cliente)}')">Bloquear pessoa</button>`:''}</li>`;
async function ver(){const d=await api('/admin/estado');if(!d)return false;login.hidden=true;app.hidden=false;
st.textContent=(d.aberto?'✅ Pedidos ABERTOS':'⛔ Pedidos FECHADOS')+' · '+(d.pausado?'⏸ digitação PAUSADA':'▶ digitando normalmente');
const ativo=p=>p.status=='aguardando'||p.status=='digitando';
f.innerHTML=d.pedidos.filter(ativo).map(li).join('')||'<li><small>fila vazia</small></li>';
h.innerHTML=d.pedidos.filter(p=>!ativo(p)).reverse().slice(0,30).map(li).join('');
bq.innerHTML=d.bloq_codigos.map(x=>`<li>Música ${E(x)} <button onclick="a('desbloquear','${E(x)}')">Desbloquear</button></li>`).join('')+
d.bloq_clientes.map(x=>`<li>Pessoa ${E(x)} <button onclick="a('desbloquear_cliente','${E(x)}')">Desbloquear</button></li>`).join('');return true}
async function a(acao,valor){await api('/admin/acao',{acao,valor});ver()}
async function c(acao){await api('/comando',{acao});ver()}
async function entrar(){K=k.value;sessionStorage.setItem('k',K);if(!await ver())alert('Chave incorreta')}
ver();setInterval(()=>{if(!app.hidden)ver()},3000);
</script>"""

if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    print("Painel do admin: http://localhost:5000/admin")
    app.run(host="0.0.0.0", port=5000, threaded=True)
