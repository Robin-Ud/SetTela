#!/usr/bin/env python3
"""settela — janelinha GTK pra espelhar, ligar/desligar e posicionar monitores no Hyprland.

Tudo via `hyprctl keyword monitor`, igual à função `settela` do ~/.bashrc — nada vai
pra config do Hyprland. Em vez disso, cada conjunto de monitores (pela descrição, ex.:
note + projetor da facul) lembra a última configuração confirmada em `pessoal/lembrados.json`,
e o vigia reaplica quando esse conjunto volta a ser plugado.
Puro Python + PyGObject (GTK4).

Uso:
    settela.py            abre a janela
    settela.py --vigiar   vigia hotplug e reaplica o lembrado (exec-once do Hyprland)
    settela.py --test     roda os testes da lógica (não chama hyprctl)
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent
CONFIGS = DIR / "configs.json"
LEMBRADOS = DIR / "pessoal" / "lembrados.json"

RAIO_GRUDAR = 80        # distância (px lógicos) em que o monitor arrastado gruda no alinhamento
SEGUNDOS_CONFIRMAR = 15  # tempo pra confirmar antes de reverter sozinho
TAMANHO_JANELA = (700, 600)  # tamanho da janela principal (igual à windowrule do Hyprland)


def ler_configs() -> dict:
    cfg = {"principal": "eDP-1", "escala_padrao": 1.25}
    try:
        cfg.update(json.loads(CONFIGS.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


# ========================= Lógica pura (testável sem display) =========================

def parse_modo(modo: str) -> tuple[int, int, float]:
    """'1920x1200@60.00Hz' → (1920, 1200, 60.0)"""
    res, _, hz = modo.removesuffix("Hz").partition("@")
    w, h = res.split("x")
    return int(w), int(h), float(hz or 60)


def ler_monitores(texto_json: str) -> list[dict]:
    """Converte a saída de `hyprctl monitors all -j` no estado usado pela janela."""
    brutos = json.loads(texto_json)
    id_para_nome = {str(m["id"]): m["name"] for m in brutos}
    estado = []
    for m in brutos:
        atual = f'{m["width"]}x{m["height"]}@{m["refreshRate"]:.2f}Hz'
        modos = list(m.get("availableModes") or [])
        if modos:
            # casa o modo atual com o disponível de mesma resolução e Hz mais próximo
            mesma_res = [x for x in modos if parse_modo(x)[:2] == (m["width"], m["height"])]
            if mesma_res:
                atual = min(mesma_res, key=lambda x: abs(parse_modo(x)[2] - m["refreshRate"]))
            elif atual not in modos:
                modos.insert(0, atual)
        else:
            modos = [atual]
        espelho = str(m.get("mirrorOf", "none"))
        espelho = None if espelho in ("none", "", "-1") else id_para_nome.get(espelho, espelho)
        estado.append({
            "nome": m["name"],
            "desc": m.get("description", ""),
            "modos": modos,
            "modo": atual,
            "x": m["x"],
            "y": m["y"],
            "escala": float(m["scale"]),
            "transform": int(m.get("transform", 0)),
            "ligado": not m.get("disabled", False),
            "espelho": espelho,
        })
    return estado


def fmt_escala(e: float) -> str:
    return f"{round(e, 4):g}"


def regra(m: dict) -> str:
    """Monta o valor de `hyprctl keyword monitor` pra um monitor."""
    if not m["ligado"]:
        return f'{m["nome"]},disable'
    r = f'{m["nome"]},{m["modo"].removesuffix("Hz")},{m["x"]}x{m["y"]},{fmt_escala(m["escala"])}'
    if m["transform"]:
        r += f',transform,{m["transform"]}'
    if m["espelho"]:
        r += f',mirror,{m["espelho"]}'
    return r


def regras_em_ordem(estado: list[dict]) -> list[str]:
    """Liga os normais primeiro, depois os espelhos e só no fim desliga —
    assim nunca fica um instante sem tela."""
    normais = [m for m in estado if m["ligado"] and not m["espelho"]]
    espelhos = [m for m in estado if m["ligado"] and m["espelho"]]
    desligados = [m for m in estado if not m["ligado"]]
    return [regra(m) for m in normais + espelhos + desligados]


def retangulo(m: dict) -> tuple[int, int, int, int]:
    """Retângulo lógico (x, y, w, h): pixels ÷ escala, girado se tiver transform 90/270."""
    w, h, _ = parse_modo(m["modo"])
    if m["transform"] % 2 == 1:
        w, h = h, w
    return m["x"], m["y"], round(w / m["escala"]), round(h / m["escala"])


def no_layout(estado: list[dict]) -> list[dict]:
    """Monitores que ocupam espaço na disposição (ligados e sem espelhar)."""
    return [m for m in estado if m["ligado"] and not m["espelho"]]


def sobrepoe(a, b) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _alinhar(v: float, alvos: list[float]) -> float:
    perto = min(alvos, key=lambda a: abs(a - v))
    return perto if abs(perto - v) <= RAIO_GRUDAR else v


def grudar(x: float, y: float, w: int, h: int, outros: list[tuple]) -> tuple[int, int]:
    """Posição mais próxima de (x, y) encostada na borda de algum vizinho, sem sobrepor
    ninguém, e alinhada (topo/base/centro) quando estiver perto."""
    if not outros:
        return 0, 0
    cands = []
    for ox, oy, ow, oh in outros:
        for cx in (ox - w, ox + ow):   # à esquerda / à direita do vizinho
            cy = min(max(y, oy - h + 1), oy + oh - 1)
            cands.append((cx, _alinhar(cy, [oy, oy + oh - h, oy + (oh - h) / 2])))
        for cy in (oy - h, oy + oh):   # acima / abaixo do vizinho
            cx = min(max(x, ox - w + 1), ox + ow - 1)
            cands.append((_alinhar(cx, [ox, ox + ow - w, ox + (ow - w) / 2]), cy))
    livres = [c for c in cands if not any(sobrepoe((c[0], c[1], w, h), o) for o in outros)]
    cx, cy = min(livres or cands, key=lambda c: (c[0] - x) ** 2 + (c[1] - y) ** 2)
    return round(cx), round(cy)


def encaixar(estado: list[dict], nome: str) -> None:
    """Reposiciona `nome` grudado nos outros (usado depois de ligar ou mudar tamanho)."""
    m = next(m for m in estado if m["nome"] == nome)
    outros = [retangulo(o) for o in no_layout(estado) if o["nome"] != nome]
    x, y, w, h = retangulo(m)
    if outros and (any(sobrepoe((x, y, w, h), o) for o in outros) or not _encostado((x, y, w, h), outros)):
        m["x"], m["y"] = grudar(x, y, w, h, outros)


def _encostado(r, outros) -> bool:
    x, y, w, h = r
    folgado = (x - 1, y - 1, w + 2, h + 2)
    return any(sobrepoe(folgado, o) for o in outros)


def normalizar(estado: list[dict]) -> None:
    """Desloca a disposição pra o canto de cima à esquerda ficar em 0x0."""
    ativos = no_layout(estado)
    if not ativos:
        return
    dx = min(m["x"] for m in ativos)
    dy = min(m["y"] for m in ativos)
    for m in estado:
        m["x"] -= dx
        m["y"] -= dy


def validar(estado: list[dict]) -> str | None:
    """Mensagem de erro, ou None se dá pra aplicar."""
    if not no_layout(estado):
        return "Pelo menos um monitor precisa ficar ligado sem espelhar."
    por_nome = {m["nome"]: m for m in estado}
    for m in estado:
        if not (m["ligado"] and m["espelho"]):
            continue
        alvo = por_nome.get(m["espelho"])
        if m["espelho"] == m["nome"]:
            return f'{m["nome"]} não pode espelhar ele mesmo.'
        if not alvo or not alvo["ligado"]:
            return f'{m["nome"]} espelha {m["espelho"]}, que está desligado.'
        if alvo["espelho"]:
            return f'{m["nome"]} espelha {alvo["nome"]}, que já é espelho de outro.'
    ativos = no_layout(estado)
    for i, a in enumerate(ativos):
        for b in ativos[i + 1:]:
            if sobrepoe(retangulo(a), retangulo(b)):
                return f'{a["nome"]} e {b["nome"]} estão sobrepostos.'
    return None


def _enfileirar(estado: list[dict], nomes: list[str]) -> None:
    """Coloca os monitores `nomes` lado a lado, da esquerda pra direita, a partir de 0x0."""
    x = 0
    for m in estado:
        if m["nome"] in nomes:
            m["x"], m["y"] = x, 0
            x += retangulo(m)[2]


def preset(estado: list[dict], qual: str, principal: str,
           escala_padrao: float | None = None) -> tuple[list[dict] | None, str | None]:
    """Os atalhos do topo (mesma ideia do `settela` do bashrc). Devolve (novo_estado, erro).
    `escala_padrao` volta o principal pra escala da config (o `settela normal` deixa em 1)."""
    novo = copy.deepcopy(estado)
    externos = [m["nome"] for m in novo if m["nome"] != principal]
    if qual != "so_notebook" and not externos:
        return None, "Nenhum monitor externo conectado."
    if not any(m["nome"] == principal for m in novo):
        return None, f"Monitor principal {principal} não encontrado (veja configs.json)."
    for m in novo:
        eh_principal = m["nome"] == principal
        m["espelho"] = None
        if eh_principal and escala_padrao:
            m["escala"] = escala_padrao
        if qual == "espelhar":
            m["ligado"] = True
            m["espelho"] = None if eh_principal else principal
        elif qual == "estender":
            m["ligado"] = True
        elif qual == "so_notebook":
            m["ligado"] = eh_principal
        elif qual == "so_externo":
            m["ligado"] = not eh_principal
    ordem = [principal] + externos if qual in ("estender", "espelhar", "so_notebook") else externos
    _enfileirar(novo, ordem)
    return novo, None


# ========================= Memória por conjunto de monitores =========================

def chave(m: dict) -> str:
    """Identidade estável do monitor: a descrição (EDID). O nome (HDMI-A-1) muda de porta pra porta."""
    return m["desc"] or m["nome"]


def conjunto(estado: list[dict]) -> str:
    """Chave do conjunto conectado (ligados ou não) — ex.: 'LG Display ... + BenQ ...'."""
    return " + ".join(sorted(chave(m) for m in estado))


def lembrar(dados: dict, estado: list[dict]) -> dict:
    """Guarda `estado` como o lembrado do seu conjunto. Espelho é salvo pela chave do alvo."""
    por_nome = {m["nome"]: chave(m) for m in estado}
    novo = dict(dados)
    novo[conjunto(estado)] = {
        chave(m): {"ligado": m["ligado"], "modo": m["modo"], "escala": m["escala"],
                   "x": m["x"], "y": m["y"], "transform": m["transform"],
                   "espelho": por_nome.get(m["espelho"]) if m["espelho"] else None}
        for m in estado
    }
    return novo


def recordar(dados: dict, estado: list[dict]) -> list[dict] | None:
    """O estado lembrado pro conjunto atual (traduzido pros nomes de agora), ou None."""
    salvo = dados.get(conjunto(estado))
    if not salvo:
        return None
    por_chave = {chave(m): m["nome"] for m in estado}
    novo = copy.deepcopy(estado)
    for m in novo:
        s = salvo.get(chave(m))
        if not s:
            return None
        m.update(ligado=s["ligado"], escala=s["escala"], x=s["x"], y=s["y"], transform=s["transform"],
                 espelho=por_chave.get(s["espelho"]) if s["espelho"] else None)
        if s["modo"] in m["modos"]:
            m["modo"] = s["modo"]
    return novo if validar(novo) is None else None


def ler_lembrados() -> dict:
    try:
        return json.loads(LEMBRADOS.read_text())
    except (OSError, ValueError):
        return {}


def gravar_lembrados(dados: dict) -> None:
    LEMBRADOS.parent.mkdir(exist_ok=True)
    tmp = LEMBRADOS.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(LEMBRADOS)


# ========================= hyprctl =========================

def hyprctl_monitores() -> list[dict]:
    saida = subprocess.run(["hyprctl", "monitors", "all", "-j"], capture_output=True, text=True, check=True)
    return ler_monitores(saida.stdout)


def aplicar(estado: list[dict]) -> str | None:
    """Manda todas as regras num `hyprctl --batch` só. Devolve erro ou None."""
    lote = " ; ".join(f"keyword monitor {r}" for r in regras_em_ordem(estado))
    try:
        saida = subprocess.run(["hyprctl", "--batch", lote], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"hyprctl falhou: {e}"
    erros = [l for l in saida.stdout.splitlines() if l.strip() and l.strip() != "ok"]
    return "; ".join(erros) or None


def comandos_centralizar(clientes: list[dict], monitores: list[dict], classe: str = "rud.settela") -> list[str]:
    """Dispatches que centralizam as janelas `classe` no monitor em que estão (e cabem nele).
    Mudar a escala muda o tamanho lógico da tela e o Hyprland deixa a janela flutuante onde
    estava — ela sai do centro e pode até ficar fora da tela."""
    telas = {m["id"]: m for m in monitores if not m.get("disabled")}
    if not telas:
        return []
    cmds = []
    for c in clientes:
        if c.get("class") != classe:
            continue
        m = telas.get(c.get("monitor")) or next(iter(telas.values()))
        mw, mh = m["width"] / m["scale"], m["height"] / m["scale"]
        if m.get("transform", 0) % 2 == 1:
            mw, mh = mh, mw
        # desconta a área reservada (waybar etc.), igual ao `center` do Hyprland
        res_esq, res_topo, res_dir, res_base = (m.get("reserved") or [0, 0, 0, 0])[:4]
        mw -= res_esq + res_dir
        mh -= res_topo + res_base
        # a principal volta pro tamanho cheio quando couber; o diálogo mantém o dele
        ideal = TAMANHO_JANELA if c.get("title") == "SetTela" else c["size"]
        w = min(ideal[0], int(mw * 0.9))
        h = min(ideal[1], int(mh * 0.9))
        addr = c["address"]
        if (w, h) != tuple(c["size"]):
            cmds.append(f"dispatch resizewindowpixel exact {w} {h},address:{addr}")
        x = round(m["x"] + res_esq + (mw - w) / 2)
        y = round(m["y"] + res_topo + (mh - h) / 2)
        cmds.append(f"dispatch movewindowpixel exact {x} {y},address:{addr}")
    return cmds


def centralizar_janelas() -> bool:
    """Recentraliza as janelas do SetTela. Serve de callback do GLib (devolve False)."""
    try:
        clientes = json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True).stdout)
        monitores = json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True).stdout)
    except (OSError, ValueError):
        return False
    cmds = comandos_centralizar(clientes, monitores)
    if cmds:
        # desligar a tela em que a janela estava leva o workspace dela pra outra tela, mas
        # não o mostra — o focuswindow troca pro workspace certo pra janela não "sumir"
        cmds.append(r"dispatch focuswindow class:^(rud\.settela)$")
        subprocess.run(["hyprctl", "--batch", " ; ".join(cmds)], capture_output=True)
    return False


# ========================= Vigia de hotplug =========================

def notificar(texto: str) -> None:
    subprocess.run(["notify-send", "-t", "4000", "SetTela", texto], capture_output=True)


def reagir(ultimo: str | None, cfg: dict) -> str | None:
    """Chamado a cada evento de monitor. Só age quando o CONJUNTO muda (ligar/desligar
    um monitor já conectado não muda o conjunto, então não briga com a janela)."""
    try:
        estado = hyprctl_monitores()
    except (OSError, subprocess.CalledProcessError):
        return ultimo
    atual = conjunto(estado)
    # desplugou o único monitor ligado (ex.: "Só externo" e tirou o cabo) → religa o note
    if not no_layout(estado):
        novo, _ = preset(estado, "so_notebook", cfg["principal"], cfg["escala_padrao"])
        if novo:
            aplicar(novo)
            notificar("Nenhuma tela ligada — religuei o notebook.")
        return atual
    if atual == ultimo:
        return ultimo
    novo = recordar(ler_lembrados(), estado)
    if novo and regras_em_ordem(novo) != regras_em_ordem(estado):
        erro = aplicar(novo)
        notificar(f"hyprctl: {erro}" if erro else "Configuração lembrada aplicada (Super+P pra mudar).")
    return atual


def vigiar() -> int:
    trava = open(Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "settela-vigia.lock", "w")
    try:
        fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("settela: o vigia já está rodando", file=sys.stderr)
        return 1
    cfg = ler_configs()
    caminho = Path(os.environ["XDG_RUNTIME_DIR"]) / "hypr" / os.environ["HYPRLAND_INSTANCE_SIGNATURE"] / ".socket2.sock"
    ultimo = reagir(None, cfg)  # ao subir, já aplica o lembrado do que estiver plugado
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(caminho))
        resto = b""
        while bloco := sock.recv(4096):
            resto += bloco
            *linhas, resto = resto.split(b"\n")
            if any(l.startswith(b"configreloaded") for l in linhas):
                ultimo = None  # reload volta pra regra da config → reaplica o lembrado
            if ultimo is None or any(l.startswith((b"monitoradded", b"monitorremoved")) for l in linhas):
                time.sleep(1.0)  # dá tempo do Hyprland terminar de configurar a saída
                ultimo = reagir(ultimo, cfg)
    return 0


# ========================= Janela GTK =========================

def main() -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk

    cfg = ler_configs()
    principal = cfg["principal"]

    class Janela(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="SetTela")
            self.set_default_size(680, 560)
            self.estado: list[dict] = []
            self.original: list[dict] = []   # como estava ao abrir (botão Reverter)
            self.anterior: list[dict] = []   # antes do último Aplicar (revert automático)
            self.montando = False            # evita loop de sinais ao preencher os widgets
            self.arrastando = None           # (nome, x0, y0) durante o arraste
            self.vista = (1.0, 0.0, 0.0)     # (fator, offset_x, offset_y) usados no desenho
            self.vista_fixa = None

            caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            for lado in ("top", "bottom", "start", "end"):
                getattr(caixa, f"set_margin_{lado}")(12)
            self.set_child(caixa)

            topo = Gtk.Box(spacing=6, homogeneous=True)
            for rotulo, qual in (("Espelhar", "espelhar"), ("Estender", "estender"),
                                 ("Só notebook", "so_notebook"), ("Só externo", "so_externo")):
                b = Gtk.Button(label=rotulo)
                b.connect("clicked", self.on_preset, qual)
                topo.append(b)
            caixa.append(topo)

            self.area = Gtk.DrawingArea(content_height=230, hexpand=True)
            self.area.set_draw_func(self.desenhar)
            arraste = Gtk.GestureDrag()
            arraste.connect("drag-begin", self.on_drag_begin)
            arraste.connect("drag-update", self.on_drag_update)
            arraste.connect("drag-end", self.on_drag_end)
            self.area.add_controller(arraste)
            moldura = Gtk.Frame(child=self.area)
            caixa.append(moldura)

            self.grade = Gtk.Grid(column_spacing=10, row_spacing=6)
            caixa.append(self.grade)

            self.status = Gtk.Label(xalign=0, wrap=True, vexpand=True, valign=Gtk.Align.END)
            caixa.append(self.status)

            # "Manter?" fica na própria janela: um diálogo separado abria no meio da troca de
            # monitores e o Hyprland o jogava num workspace invisível (ex.: "Só externo")
            self.barra = Gtk.Box(spacing=6, visible=False)
            self.barra.add_css_class("card")
            self.texto_confirmar = Gtk.Label(xalign=0, hexpand=True, margin_start=8)
            b_nao = Gtk.Button(label="Reverter")
            b_nao.connect("clicked", lambda *_: self.fechar_confirmacao(False))
            b_sim = Gtk.Button(label="Manter")
            b_sim.add_css_class("suggested-action")
            b_sim.connect("clicked", lambda *_: self.fechar_confirmacao(True))
            for w in (self.texto_confirmar, b_nao, b_sim):
                self.barra.append(w)
            caixa.append(self.barra)
            self.b_manter = b_sim
            self.fonte_confirmar = 0
            self.restante = 0

            rodape = Gtk.Box(spacing=6)
            b_atualizar = Gtk.Button(label="⟳ Atualizar")
            b_atualizar.connect("clicked", lambda *_: self.recarregar())
            b_reverter = Gtk.Button(label="Reverter")
            b_reverter.connect("clicked", self.on_reverter)
            b_esquecer = Gtk.Button(label="Esquecer")
            b_esquecer.set_tooltip_text("Apaga a configuração lembrada deste conjunto de monitores")
            b_esquecer.connect("clicked", self.on_esquecer)
            espaco = Gtk.Box(hexpand=True)
            b_aplicar = Gtk.Button(label="Aplicar")
            b_aplicar.add_css_class("suggested-action")
            b_aplicar.connect("clicked", lambda *_: self.aplicar_com_confirmacao(self.estado))
            for w in (b_atualizar, b_reverter, b_esquecer, espaco, b_aplicar):
                rodape.append(w)
            caixa.append(rodape)
            self.travaveis = (topo, moldura, self.grade, rodape)  # desligados enquanto confirma
            self.connect("close-request", self.on_fechar_janela)

            self.recarregar()
            self.original = copy.deepcopy(self.estado)
            if conjunto(self.estado) in ler_lembrados():
                self.avisar("Este conjunto de monitores tem configuração lembrada.", erro=False)

        # ---------- estado ----------

        def recentralizar(self):
            GLib.timeout_add(300, centralizar_janelas)
            GLib.timeout_add(900, centralizar_janelas)

        def recarregar(self):
            try:
                self.estado = hyprctl_monitores()
            except (OSError, subprocess.CalledProcessError) as e:
                self.avisar(f"Não consegui ler os monitores: {e}")
                return False
            self.montar_linhas()
            self.area.queue_draw()
            return False  # pra servir de callback do GLib.timeout_add

        def mudou(self, nome=None):
            if nome:
                encaixar(self.estado, nome)
            normalizar(self.estado)
            self.area.queue_draw()
            erro = validar(self.estado)
            self.avisar(erro or "Alterações pendentes — clique em Aplicar.", erro is not None)

        def avisar(self, texto, erro=True):
            cor = "#e5534b" if erro else "#57ab5a"
            self.status.set_markup(f'<span foreground="{cor}">{GLib.markup_escape_text(texto)}</span>')

        # ---------- linhas de cada monitor ----------

        def montar_linhas(self):
            self.montando = True
            while (filho := self.grade.get_first_child()) is not None:
                self.grade.remove(filho)
            for col, titulo in enumerate(("Monitor", "Ligado", "Modo", "Escala", "Espelhar de")):
                l = Gtk.Label(xalign=0)
                l.set_markup(f"<b>{titulo}</b>")
                self.grade.attach(l, col, 0, 1, 1)
            nomes = [m["nome"] for m in self.estado]
            for lin, m in enumerate(self.estado, start=1):
                rotulo = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
                rotulo.set_markup(f'<b>{m["nome"]}</b>\n<small>{GLib.markup_escape_text(m["desc"])}</small>')
                rotulo.set_tooltip_text(m["desc"])
                self.grade.attach(rotulo, 0, lin, 1, 1)

                sw = Gtk.Switch(active=m["ligado"], valign=Gtk.Align.CENTER)
                sw.connect("notify::active", self.on_ligado, m["nome"])
                self.grade.attach(sw, 1, lin, 1, 1)

                dd_modo = Gtk.DropDown.new_from_strings(m["modos"])
                dd_modo.set_selected(m["modos"].index(m["modo"]))
                dd_modo.connect("notify::selected", self.on_modo, m["nome"])
                self.grade.attach(dd_modo, 2, lin, 1, 1)

                spin = Gtk.SpinButton.new_with_range(0.5, 3.0, 0.25)
                spin.set_digits(2)
                spin.set_value(m["escala"])
                spin.connect("value-changed", self.on_escala, m["nome"])
                self.grade.attach(spin, 3, lin, 1, 1)

                opcoes = ["—"] + [n for n in nomes if n != m["nome"]]
                dd_esp = Gtk.DropDown.new_from_strings(opcoes)
                dd_esp.set_selected(opcoes.index(m["espelho"]) if m["espelho"] in opcoes else 0)
                dd_esp.connect("notify::selected", self.on_espelho, m["nome"], opcoes)
                self.grade.attach(dd_esp, 4, lin, 1, 1)
            self.montando = False

        def mon(self, nome):
            return next(m for m in self.estado if m["nome"] == nome)

        def on_ligado(self, sw, _param, nome):
            if self.montando:
                return
            self.mon(nome)["ligado"] = sw.get_active()
            self.mudou(nome if sw.get_active() else None)

        def on_modo(self, dd, _param, nome):
            if self.montando:
                return
            m = self.mon(nome)
            m["modo"] = m["modos"][dd.get_selected()]
            self.mudou(nome)

        def on_escala(self, spin, nome):
            if self.montando:
                return
            self.mon(nome)["escala"] = spin.get_value()
            self.mudou(nome)

        def on_espelho(self, dd, _param, nome, opcoes):
            if self.montando:
                return
            escolha = opcoes[dd.get_selected()]
            self.mon(nome)["espelho"] = None if escolha == "—" else escolha
            self.mudou(nome if escolha == "—" else None)

        # ---------- atalhos e aplicar ----------

        def on_preset(self, _botao, qual):
            novo, erro = preset(self.estado, qual, principal, cfg["escala_padrao"])
            if erro:
                self.avisar(erro)
                return
            self.aplicar_com_confirmacao(novo)

        def on_esquecer(self, _botao):
            dados = ler_lembrados()
            if dados.pop(conjunto(self.estado), None) is None:
                self.avisar("Nada lembrado pra este conjunto.", erro=False)
                return
            gravar_lembrados(dados)
            self.avisar("Esqueci a configuração deste conjunto.", erro=False)

        def on_reverter(self, _botao):
            erro = aplicar(self.original)
            self.avisar(erro or "Voltou pra como estava ao abrir.", erro is not None)
            self.recentralizar()
            GLib.timeout_add(500, self.recarregar)

        def aplicar_com_confirmacao(self, novo):
            erro = validar(novo)
            if erro:
                self.avisar(erro)
                return
            self.anterior = hyprctl_monitores()
            erro = aplicar(novo)
            if erro:
                self.avisar(f"hyprctl: {erro}")
                aplicar(self.anterior)
                return
            self.estado = copy.deepcopy(novo)
            self.montar_linhas()
            self.area.queue_draw()
            self.confirmar()

        def confirmar(self):
            for w in self.travaveis:
                w.set_sensitive(False)
            self.restante = SEGUNDOS_CONFIRMAR
            self.texto_confirmar.set_text(f"Manter esta configuração? Reverte sozinho em {self.restante} s.")
            self.barra.set_visible(True)
            self.b_manter.grab_focus()
            self.fonte_confirmar = GLib.timeout_add_seconds(1, self.tique)
            self.recentralizar()

        def tique(self):
            self.restante -= 1
            if self.restante <= 0:
                self.fonte_confirmar = 0
                self.fechar_confirmacao(False)
                return False
            self.texto_confirmar.set_text(f"Manter esta configuração? Reverte sozinho em {self.restante} s.")
            return True

        def fechar_confirmacao(self, manter):
            if self.fonte_confirmar:
                GLib.source_remove(self.fonte_confirmar)
                self.fonte_confirmar = 0
            self.barra.set_visible(False)
            for w in self.travaveis:
                w.set_sensitive(True)
            if manter:
                gravar_lembrados(lembrar(ler_lembrados(), self.estado))
                self.avisar("Configuração aplicada e lembrada pra este conjunto de monitores.", erro=False)
            else:
                aplicar(self.anterior)
                self.avisar("Revertido pra configuração anterior.", erro=False)
                self.recentralizar()
            GLib.timeout_add(500, self.recarregar)

        def on_fechar_janela(self, _janela):
            # fechar a janela com a confirmação pendente = não confirmou → reverte
            if self.barra.get_visible():
                self.fechar_confirmacao(False)
            return False

        # ---------- desenho e arraste ----------

        def calcular_vista(self, largura, altura):
            rets = [retangulo(m) for m in no_layout(self.estado)]
            if not rets:
                return 1.0, 0.0, 0.0
            x0 = min(r[0] for r in rets)
            y0 = min(r[1] for r in rets)
            x1 = max(r[0] + r[2] for r in rets)
            y1 = max(r[1] + r[3] for r in rets)
            margem = 24
            fator = min((largura - 2 * margem) / (x1 - x0), (altura - 2 * margem) / (y1 - y0)) * 0.8
            ox = (largura - (x1 - x0) * fator) / 2 - x0 * fator
            oy = (altura - (y1 - y0) * fator) / 2 - y0 * fator
            return fator, ox, oy

        def desenhar(self, _area, cr, largura, altura):
            self.vista = self.vista_fixa or self.calcular_vista(largura, altura)
            fator, ox, oy = self.vista
            espelhos = {}
            for m in self.estado:
                if m["ligado"] and m["espelho"]:
                    espelhos.setdefault(m["espelho"], []).append(m["nome"])
            for m in no_layout(self.estado):
                x, y, w, h = retangulo(m)
                rx, ry, rw, rh = ox + x * fator, oy + y * fator, w * fator, h * fator
                cor = (0.26, 0.47, 0.80) if m["nome"] == principal else (0.35, 0.60, 0.40)
                if self.arrastando and self.arrastando[0] == m["nome"]:
                    cor = tuple(min(1, c + 0.15) for c in cor)
                cr.set_source_rgba(*cor, 0.9)
                cr.rectangle(rx + 1, ry + 1, rw - 2, rh - 2)
                cr.fill_preserve()
                cr.set_source_rgb(1, 1, 1)
                cr.set_line_width(1.5)
                cr.stroke()
                linhas = [m["nome"], f'{m["modo"].removesuffix("Hz")} ×{fmt_escala(m["escala"])}']
                if m["nome"] in espelhos:
                    linhas.append("espelhado em " + ", ".join(espelhos[m["nome"]]))
                cr.select_font_face("Sans", 0, 1)
                cr.set_font_size(13)
                for i, t in enumerate(linhas):
                    ext = cr.text_extents(t)
                    cr.move_to(rx + (rw - ext.width) / 2, ry + rh / 2 + (i - (len(linhas) - 1) / 2) * 17 + 5)
                    cr.show_text(t)
                    cr.set_font_size(11)

        def on_drag_begin(self, _g, px, py):
            fator, ox, oy = self.vista
            for m in reversed(no_layout(self.estado)):
                x, y, w, h = retangulo(m)
                if ox + x * fator <= px <= ox + (x + w) * fator and oy + y * fator <= py <= oy + (y + h) * fator:
                    self.arrastando = (m["nome"], x, y)
                    self.vista_fixa = self.vista
                    return
            self.arrastando = None

        def on_drag_update(self, _g, dx, dy):
            if not self.arrastando:
                return
            nome, x0, y0 = self.arrastando
            m = self.mon(nome)
            fator = self.vista_fixa[0]
            _, _, w, h = retangulo(m)
            outros = [retangulo(o) for o in no_layout(self.estado) if o["nome"] != nome]
            m["x"], m["y"] = grudar(x0 + dx / fator, y0 + dy / fator, w, h, outros)
            self.area.queue_draw()

        def on_drag_end(self, _g, _dx, _dy):
            if not self.arrastando:
                return
            self.arrastando = None
            self.vista_fixa = None
            self.mudou()

    app = Gtk.Application(application_id="rud.settela")

    def ativar(app):
        janela = app.get_active_window() or Janela(app)
        janela.present()

    app.connect("activate", ativar)
    return app.run([sys.argv[0]])


# ========================= Testes =========================

def _mon_json(id_, nome, w, h, x=0, y=0, escala=1.0, desligado=False, espelho="none", modos=None):
    return {"id": id_, "name": nome, "description": f"desc {nome}", "width": w, "height": h,
            "refreshRate": 60.001, "x": x, "y": y, "scale": escala, "transform": 0,
            "disabled": desligado, "mirrorOf": espelho,
            "availableModes": modos if modos is not None else [f"{w}x{h}@60.00Hz", f"{w}x{h}@59.94Hz"]}


def testes() -> int:
    ok = 0

    def checa(cond, msg):
        nonlocal ok
        if not cond:
            raise AssertionError(msg)
        ok += 1

    # 1 monitor, igual ao note hoje
    so_note = json.dumps([_mon_json(0, "eDP-1", 1920, 1200, escala=1.25)])
    e = ler_monitores(so_note)
    checa(e[0]["modo"] == "1920x1200@60.00Hz", "modo atual casado com availableModes")
    checa(regra(e[0]) == "eDP-1,1920x1200@60.00,0x0,1.25", f"regra simples: {regra(e[0])}")
    checa(retangulo(e[0]) == (0, 0, 1536, 960), "tamanho lógico = px ÷ escala")
    checa(validar(e) is None, "1 monitor ligado é válido")
    checa(preset(e, "espelhar", "eDP-1")[1] == "Nenhum monitor externo conectado.", "sem externo não espelha")
    checa(preset(e, "so_notebook", "eDP-1")[1] is None, "só notebook funciona sem externo")
    e1 = copy.deepcopy(e)
    e1[0]["escala"] = 1.0                 # como o `settela normal` deixa
    checa(preset(e1, "so_notebook", "eDP-1", 1.25)[0][0]["escala"] == 1.25, "preset volta escala padrão")

    # 2 monitores, externo espelhando o note (mirrorOf vem como id)
    dois = json.dumps([_mon_json(0, "eDP-1", 1920, 1200, escala=1.25),
                       _mon_json(1, "HDMI-A-1", 1920, 1080, x=1536, espelho="0")])
    e = ler_monitores(dois)
    checa(e[1]["espelho"] == "eDP-1", "mirrorOf id → nome")
    checa(regra(e[1]) == "HDMI-A-1,1920x1080@60.00,1536x0,1,mirror,eDP-1", f"regra espelho: {regra(e[1])}")
    checa(regras_em_ordem(e)[0].startswith("eDP-1,"), "monitor normal vem antes do espelho")

    # presets
    novo, _ = preset(e, "estender", "eDP-1")
    checa(not novo[1]["espelho"] and (novo[1]["x"], novo[1]["y"]) == (1536, 0), "estender põe externo à direita")
    checa(validar(novo) is None, "estender é válido")
    novo, _ = preset(e, "so_externo", "eDP-1")
    checa(not novo[0]["ligado"] and novo[1]["ligado"] and (novo[1]["x"], novo[1]["y"]) == (0, 0), "só externo")
    checa(regras_em_ordem(novo) == ["HDMI-A-1,1920x1080@60.00,0x0,1", "eDP-1,disable"], "desliga por último")
    novo, _ = preset(e, "espelhar", "eDP-1")
    checa(novo[1]["espelho"] == "eDP-1" and novo[0]["espelho"] is None, "espelhar")
    novo, _ = preset(e, "so_notebook", "eDP-1")
    checa(novo[0]["ligado"] and not novo[1]["ligado"], "só notebook")

    # validação
    tudo_off = copy.deepcopy(e)
    for m in tudo_off:
        m["ligado"] = False
    checa(validar(tudo_off) is not None, "recusa tudo desligado")
    alvo_off = copy.deepcopy(e)
    alvo_off[0]["ligado"] = False
    checa(validar(alvo_off) is not None, "recusa espelhar monitor desligado")
    so_espelhos = copy.deepcopy(e)
    so_espelhos[0]["espelho"] = "HDMI-A-1"
    checa(validar(so_espelhos) is not None, "recusa espelho em cadeia / nenhum sobrando")
    sobreposto, _ = preset(e, "estender", "eDP-1")
    sobreposto[1]["x"] = 100
    checa("sobrepostos" in (validar(sobreposto) or ""), "recusa sobreposição")

    # desligado e sem availableModes (monitor headless)
    e = ler_monitores(json.dumps([_mon_json(0, "eDP-1", 1920, 1200),
                                  _mon_json(1, "HEADLESS-2", 1920, 1080, desligado=True, modos=[])]))
    checa(e[1]["modos"] == ["1920x1080@60.00Hz"] and not e[1]["ligado"], "headless desligado")
    checa(regra(e[1]) == "HEADLESS-2,disable", "regra disable")

    # grudar: solto perto da borda direita, um pouco abaixo → encosta e alinha o topo
    note = (0, 0, 1536, 960)
    checa(grudar(1600, 40, 1920, 1080, [note]) == (1536, 0), "gruda à direita e alinha topo")
    checa(grudar(-2000, 500, 1920, 1080, [note]) == (-1920, 500), "gruda à esquerda sem alinhar longe")
    checa(grudar(300, 1000, 1920, 1080, [note]) == (300, 960), "gruda embaixo")
    checa(not sobrepoe((*grudar(200, 200, 1920, 1080, [note]), 1920, 1080), note), "nunca sobrepõe")

    # encaixar + normalizar depois de mudar a escala
    e = ler_monitores(dois)
    e[1]["espelho"] = None
    e[1]["x"] = 1000                      # sobreposto de propósito
    encaixar(e, "HDMI-A-1")
    normalizar(e)
    checa(validar(e) is None, "encaixar resolve sobreposição")

    # centralizar: note em 1.25 (1536x960 lógico) e externo à direita em escala 2
    mons = [{"id": 0, "width": 1920, "height": 1200, "scale": 1.25, "x": 0, "y": 0, "transform": 0,
             "reserved": [0, 30, 0, 0]},
            {"id": 1, "width": 1920, "height": 1080, "scale": 2.0, "x": 1536, "y": 0, "transform": 0}]
    cli = [{"class": "rud.settela", "address": "0xa", "size": [700, 600], "monitor": 0},
           {"class": "firefox", "address": "0xb", "size": [700, 600], "monitor": 0},
           {"class": "rud.settela", "address": "0xc", "size": [700, 600], "monitor": 1}]
    cmds = comandos_centralizar(cli, mons)
    encolhida = [{"class": "rud.settela", "title": "SetTela", "address": "0xd", "size": [700, 478], "monitor": 0}]
    checa(comandos_centralizar(encolhida, mons)[0] == "dispatch resizewindowpixel exact 700 600,address:0xd",
          "principal volta pro tamanho cheio quando cabe")
    checa(cmds[0] == "dispatch movewindowpixel exact 418 195,address:0xa", f"centraliza no note: {cmds[0]}")
    checa(not any("0xb" in c for c in cmds), "não mexe em janela de outro app")
    checa(cmds[1] == "dispatch resizewindowpixel exact 700 486,address:0xc", f"encolhe pra caber: {cmds[1]}")
    checa(cmds[2] == "dispatch movewindowpixel exact 1666 27,address:0xc", f"centraliza no externo: {cmds[2]}")
    mons[1]["disabled"] = True
    checa(comandos_centralizar(cli[2:], mons)[-1].endswith("address:0xc") and "exact 418 195" in comandos_centralizar(cli[2:], mons)[-1],
          "monitor desligado → vai pro que sobrou")

    # memória por conjunto: projetor espelhando, depois volta com outro nome de porta
    e = ler_monitores(json.dumps([_mon_json(0, "eDP-1", 1920, 1200, escala=1.25),
                                  _mon_json(1, "HDMI-A-1", 1920, 1080, x=1536)]))
    e[1]["desc"] = "Projetor Facul"
    espelhado, _ = preset(e, "espelhar", "eDP-1")
    dados = lembrar({}, espelhado)
    checa(list(dados) == ["Projetor Facul + desc eDP-1"], f"chave do conjunto: {list(dados)}")
    checa(dados["Projetor Facul + desc eDP-1"]["Projetor Facul"]["espelho"] == "desc eDP-1", "espelho salvo pela chave")
    outra_porta = copy.deepcopy(e)
    outra_porta[1]["nome"] = "DP-2"
    volta = recordar(dados, outra_porta)
    checa(volta and volta[1]["nome"] == "DP-2" and volta[1]["espelho"] == "eDP-1", "recorda em outra porta")
    checa(recordar(dados, e[:1]) is None, "conjunto diferente → nada")
    json.dumps(dados)  # tem que ser serializável

    print(f"ok — {ok} testes passaram")
    return 0


if __name__ == "__main__":
    if "--test" in sys.argv[1:]:
        sys.exit(testes())
    if "--vigiar" in sys.argv[1:]:
        sys.exit(vigiar())
    sys.exit(main())
