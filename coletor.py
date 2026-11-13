"""Radar de preços: lê canais públicos do Telegram, guarda os posts e monta dados/indice.json para o site.

Uso:  python coletor.py
Dependências:  pip install requests beautifulsoup4
"""
import base64
import hashlib
import hmac
import html
import json
import re
import statistics
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import os

import requests
from bs4 import BeautifulSoup

RAIZ = Path(__file__).parent
ARQ_CANAIS = RAIZ / "canais.txt"
PASTA_DADOS = RAIZ / "dados"
PASTA_POSTS = PASTA_DADOS / "posts"
ARQ_INDICE = PASTA_DADOS / "indice.json"
ARQ_ANTIGO = PASTA_DADOS / "ofertas.json"
FUSO = ZoneInfo("America/Sao_Paulo")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
ARQ_ESTADO = PASTA_DADOS / "estado.json"
PASTA_ALERTAS = RAIZ / "alertas"
JANELA_DUPLICATA = timedelta(hours=6)
MAX_LINKS_POR_RODADA = 80
# Histórico: na primeira vez, o coletor volta no passado de cada canal até esta data, aos poucos.
HISTORICO_DESDE = "2026-08-01"
PAGINAS_ANTIGAS_POR_RODADA = 25

# ---------------------------------------------------------------- lojas e links
ENCURTADORES = {"tidd.ly", "bit.ly", "s.shopee.com.br", "amzn.to", "tinyurl.com", "cutt.ly", "is.gd",
                "magazineluiza.onelink.me", "s.click.aliexpress.com", "mercadolivre.com"}
NAO_E_PRODUTO = {"t.me", "telegram.me", "chat.whatsapp.com", "whatsapp.com", "wa.me", "instagram.com",
                 "youtube.com", "youtu.be", "linktr.ee", "tiktok.com"}
LOJAS = {"kabum": "KaBuM!", "shopee": "Shopee", "amazon": "Amazon", "mercadolivre": "Mercado Livre",
         "mercado livre": "Mercado Livre", "magazineluiza": "Magalu", "magalu": "Magalu",
         "aliexpress": "AliExpress", "pichau": "Pichau", "terabyte": "Terabyte", "casasbahia": "Casas Bahia",
         "americanas": "Americanas", "fastshop": "Fast Shop", "girafa": "Girafa", "carrefour": "Carrefour"}

# ---------------------------------------------------------------- leitura do texto
RE_PRECO = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?)")
RE_ANTES_IGNORA = re.compile(
    r"(\d+\s*x\s*(de\s*)?|cupom\s+de\s*(at[eé]\s*)?|desconto\s+de\s*|economi\w*\s*(de\s*)?|cashback\s*(de\s*)?"
    r"|frete\s*(gr[aá]tis\s*)?(acima\s+de\s*)?|acima\s+de\s*|m[ií]nimo\s*(de\s*)?|ganhe\s*|off\s+de\s*|at[eé]\s*"
    r"|parcelas?\s*(de\s*)?)$", re.I)
RE_DEPOIS_IGNORA = re.compile(r"^\s*(off\b|de\s+desconto|de\s+cashback|em\s+cashback|de\s+volta|de\s+economia)", re.I)
RE_POR = re.compile(r"\bpor\s*:?\s*$", re.I)
RE_CUPOM = re.compile(r"(?i:cupom)\s*:?\s*([A-Z0-9][A-Z0-9_\-]{3,24})(?![a-z])")
RE_CUPOM_DEPOIS = re.compile(r"(?i:cupom)[^:\n]{1,30}:\s*([A-Z0-9][A-Z0-9_\-]{3,24})(?![a-z])")
RE_URL = re.compile(r"https?://[^\s<>\"')]+")
RE_LINK_PRODUTO = re.compile(r"(?i)link\s+d[oa]\s+produto\s*:?\s*(https?://[^\s<>\"')]+)")
RE_CHAMADA = re.compile(
    r"(?i)\b(corre+|rapaziada|galera|pessoal|aten[cç][aã]o|vcs|voc[eê]s|bom dia|boa tarde|boa noite|urgente"
    r"|aproveit\w*|olha s[oó]|acabou de|chegou|n[aã]o perca|an[uú]ncio|live)\b")
RE_PREFIXO_LOJA = re.compile(r"(?i)^(kabum!?|terabyte|pichau|amazon|shopee|mercado livre|magalu|aliexpress)\s*[-–:|]\s*")


def slug(texto):
    s = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def limpar_nome(linha):
    linha = re.sub(r"^[^\w\"']+|[^\w\"')+]+$", "", linha, flags=re.UNICODE).strip()
    return RE_PREFIXO_LOJA.sub("", linha).strip()


def eh_chamada(linha):
    """Frase de chamada ou anúncio, e não o nome de um produto."""
    letras = [c for c in linha if c.isalpha()]
    gritando = len(letras) >= 15 and sum(c.isupper() for c in letras) / len(letras) > 0.7
    return bool(RE_CHAMADA.search(linha)) or (gritando and not re.search(r"\d", linha))


def numero(txt):
    return float(txt.replace(".", "").replace(",", "."))


def escolher_preco(texto):
    """Acha o preço da oferta, ignorando parcelas, cashback, valor do cupom e o preço riscado."""
    candidatos = []
    for m in RE_PRECO.finditer(texto):
        antes = texto[max(0, m.start() - 30): m.start()]
        antes = antes.split("\n")[-1]
        depois = texto[m.end(): m.end() + 25]
        if RE_ANTES_IGNORA.search(antes) or RE_DEPOIS_IGNORA.search(depois):
            continue
        valor = numero(m.group(1))
        if valor >= 1:
            candidatos.append((m, valor, bool(RE_POR.search(antes))))
    if not candidatos:
        return None
    com_por = [c for c in candidatos if c[2]]
    return (com_por or candidatos)[0][:2]


RE_CAPACIDADE = re.compile(r"(?<![\d.,])(\d{1,4})\s*(GB|TB)\b", re.I)


def capacidades(texto):
    """Capacidades citadas (ex.: {'250gb', '500gb', '1tb'})."""
    return {f"{int(n)}{u.lower()}" for n, u in RE_CAPACIDADE.findall(texto)}


def precos_por_versao(texto):
    """Quando o post dá o preço de cada versão numa linha ('500GB: R$ 199,90'), devolve {'500gb': 199.9}."""
    saida = {}
    for linha in texto.splitlines():
        caps = capacidades(linha)
        if len(caps) == 1 and "R$" in linha:
            achado = escolher_preco(linha)
            if achado:
                saida[next(iter(caps))] = achado[1]
    return saida


def interpretar_post(texto):
    """Recebe o texto de um post e devolve nome, preço, pagamento, cupom e link (ou None se não for oferta)."""
    escolhido = escolher_preco(texto)
    if not escolhido:
        return None
    m_preco, preco = escolhido

    nome = ""
    for linha in texto[: m_preco.start()].splitlines():
        if "http" in linha or "R$" in linha:
            continue
        candidato = limpar_nome(linha)
        if len(candidato) >= 8 and not eh_chamada(candidato):
            nome = candidato[:160]
            break
    if not nome:
        return None

    linha_preco = texto[texto.rfind("\n", 0, m_preco.start()) + 1:]
    linha_preco = linha_preco.split("\n")[0].lower()
    if "pix" in linha_preco:
        pagamento = "pix"
    elif "vista" in linha_preco or "boleto" in linha_preco:
        pagamento = "à vista"
    elif re.search(r"\d+\s*x", linha_preco) or "cart" in linha_preco:
        pagamento = "cartão"
    else:
        pagamento = ""

    m_cupom = RE_CUPOM_DEPOIS.search(texto) or RE_CUPOM.search(texto)
    cupom = m_cupom.group(1) if m_cupom else ""

    m_link = RE_LINK_PRODUTO.search(texto)
    link = m_link.group(1) if m_link else ""
    if not link:
        for url in RE_URL.findall(texto):
            if urlparse(url).netloc.lower().removeprefix("www.") not in NAO_E_PRODUTO:
                link = url
                break
    return {"nome": nome, "preco": preco, "pagamento": pagamento, "cupom": cupom, "link": link.rstrip(".,;"),
            "versoes": precos_por_versao(texto) if len(capacidades(nome)) > 1 else {}}


# ---------------------------------------------------------------- categorias e modelos
CATEGORIAS = [
    ("Notebooks", r"\b(notebook|laptop|macbook|chromebook)\b"),
    ("PCs montados", r"\b(pc gamer|computador|desktop|mini pc|pc completo)\b"),
    ("Placas de vídeo", r"\b(placa de v[ií]deo|rtx\s?\d{4}|gtx\s?\d{4}|rx\s?\d{4}|arc\s?[ab]\d{3})"),
    ("Processadores", r"\b(processador|ryzen\s?[3579]|core\s?i[3579]|core\s?ultra|i[3579]-\d{4,5})"),
    ("Placas-mãe", r"\b(placa[- ]m[ãa]e|motherboard)\b"),
    ("SSD e HDs", r"\b(ssd|nvme|hd externo|hd interno|disco r[ií]gido|hdd)\b"),
    ("Memória RAM", r"\b(mem[oó]ria ram|mem[oó]ria|ddr4|ddr5)\b"),
    ("Fontes", r"\b(fonte)\b"),
    ("Gabinetes", r"\b(gabinete)\b"),
    ("Coolers", r"\b(water ?cooler|air ?cooler|cooler|ventoinha|fan)\b"),
    ("Monitores", r"\b(monitor)\b"),
    ("Setup e acessórios", r"\b(mousepad|mouse pad|cadeira gamer|mesa gamer|webcam|microfone|hub usb|suporte)\b"),
    ("Mouses", r"\b(mouse)\b"),
    ("Teclados", r"\b(teclado)\b"),
    ("Headsets e fones", r"\b(headset|headphone|fone|earbuds?|airpods)\b"),
    ("Controles", r"\b(controle|gamepad|joystick|dualsense)\b"),
    ("Consoles", r"\b(playstation|ps5|ps4|xbox series|nintendo switch|switch 2|console)\b"),
    ("TVs", r"\b(smart ?tv|tv|televis[aã]o|tv box)\b"),
    ("Celulares, tablets e relógios", r"\b(celular|smartphone|iphone|galaxy|redmi|poco|motorola|tablet|ipad|smartwatch|rel[oó]gio inteligente|apple watch|galaxy watch|amazfit|mi band|smart ?band|kindle|e-reader)\b"),
    ("Câmeras e casa inteligente", r"\b(c[aâ]mera|webcam|gopro|drone|echo dot|echo pop|alexa|l[aâ]mpada inteligente|tomada inteligente|fechadura digital|campainha inteligente|projetor|starlink)\b"),
    ("Setup e acessórios", r"\b(caixa de som|soundbar|roteador|cabo|adaptador|lumin[aá]ria|fita led|power ?bank|carregador|nobreak|estabilizador|pendrive|cart[aã]o de mem[oó]ria|impressora)\b"),
    ("Ferramentas", r"\b(furadeira|parafusadeira|serra (circular|tico-tico|m[aá]rmore)|serra el[eé]trica|esmerilhadeira|lixadeira|plaina|tupia|jogo de chaves|chave de (fenda|boca|catraca|impacto)|alicate|martelo|trena|n[ií]vel a laser|kit de ferramentas|maleta de ferramentas|caixa de ferramentas|jogo de brocas|broca|compressor de ar|lavadora de alta press[aã]o|soprador|mult[ií]metro|ferro de solda|soquete|torqu[ií]metro|morsa|pistola de cola)\b"),
    ("Eletrodomésticos", r"\b(geladeira|refrigerador|freezer|lavadora|m[aá]quina de lavar|lava e seca|air ?fryer|fritadeira|micro-?ondas|aspirador|rob[oô] aspirador|ventilador|ar[- ]condicionado|cafeteira|liquidificador|fog[aã]o|cooktop|forno|panela el[eé]trica|purificador|secador|chapinha|barbeador|lava-?lou[cç]as|batedeira|mixer|processador de alimentos|sanduicheira|torradeira|chaleira el[eé]trica|ferro de passar|bebedouro|adega|climatizador|aquecedor)\b"),
    ("Móveis", r"\b(sof[aá]|poltrona|puff|cadeiras?|mesa de (jantar|centro|escrit[oó]rio|cabeceira)|rack|painel para tv|estante|guarda-?roupas?|c[oô]moda|criado-?mudo|cama box|box ba[uú]|colch[aã]o|beliche|cabeceira|escrivaninha|banqueta|sapateira|arm[aá]rio|gaveteiro|penteadeira|nicho|prateleira|aparador|bicama)\b"),
    ("Cama, mesa e banho", r"\b(jogo de cama|len[cç]ol|edredom|cobertor|manta|travesseiro|toalhas?|roup[aã]o|fronha|colcha|cobre-?leito|jogo americano|toalha de mesa)\b"),
    ("Casa e cozinha", r"\b(jogo de panelas|panelas?|frigideira|ca[cç]arola|talheres|faqueiro|facas?|jogo de pratos|pratos|copos?|ta[cç]as?|x[ií]caras?|canecas?|garrafa t[eé]rmica|garrafa|squeeze|potes?|organizador|lixeira|varal|tapete|cortina|vaso|espelho|escorredor|t[aá]bua de corte|assadeira|forma de bolo|utens[ií]lios|marmita|cooler t[eé]rmico|caixa t[eé]rmica|jarra|chaleira|cuscuzeira|espremedor|abridor|descanso de panela|tramontina|stanley|tupperware|electrolux ergo)\b"),
    ("Jogos e software", r"\b(jogo (para|de) (ps5|ps4|xbox|nintendo|switch|pc)|game pass|gift ?card|cart[aã]o presente (playstation|xbox|nintendo|steam)|licen[cç]a|windows \d+|office 365|microsoft 365|steam|psn|xbox live)\b"),
]


def categorizar(nome):
    """A categoria é a da palavra-chave que aparece primeiro no nome ('Gabinete ... sem fonte' é gabinete).
    Notebook e PC montado vencem sempre, porque citam placa de vídeo e processador no nome."""
    baixo = nome.lower()
    achados = []
    for ordem, (categoria, padrao) in enumerate(CATEGORIAS):
        m = re.search(padrao, baixo)
        if m:
            acessorio = re.search(r"\b(para|p/|pra|de|compat[ií]vel com)\s+(o\s+)?(notebook|laptop|pc|computador|desktop)", baixo)
            if categoria in ("Notebooks", "PCs montados") and not acessorio:
                return categoria
            achados.append((m.start() - (100 if categoria == "Eletrodomésticos" and "processador de alimentos" in baixo else 0), ordem, categoria))
    return min(achados)[2] if achados else "Outros"


def modelo_canonico(nome, categoria):
    """Para placas de vídeo e processadores, devolve o modelo padronizado (ex.: 'RX 9060 XT 16GB')."""
    if categoria == "Placas de vídeo":
        gb = re.search(r"(\d{1,2})\s*GB\b", nome, re.I)
        mem = f" {gb.group(1)}GB" if gb else ""
        m = re.search(r"\b(RTX|GTX)\s*(\d{4})\s*(Ti\s*Super|Ti|Super)?\b", nome, re.I)
        if m:
            extra = f" {' '.join(w.capitalize() for w in m.group(3).split())}" if m.group(3) else ""
            return f"{m.group(1).upper()} {m.group(2)}{extra.replace('Ti', 'Ti')}{mem}"
        m = re.search(r"\bRX\s*(\d{4})\s*(XTX|XT|GRE)?\b", nome, re.I)
        if m:
            return f"RX {m.group(1)}{' ' + m.group(2).upper() if m.group(2) else ''}{mem}"
        m = re.search(r"\bArc\s*([AB]\d{3})\b", nome, re.I)
        if m:
            return f"Arc {m.group(1).upper()}{mem}"
    if categoria == "Processadores":
        m = re.search(r"\bRyzen\s*([3579])\s*(\d{4})\s*(X3D|XT|X|GE|G|F)?\b", nome, re.I)
        if m:
            return f"Ryzen {m.group(1)} {m.group(2)}{(m.group(3) or '').upper()}"
        m = re.search(r"\bCore\s*Ultra\s*([3579])\s*(\d{3})\s*(KF|K|F)?\b", nome, re.I)
        if m:
            return f"Core Ultra {m.group(1)} {m.group(2)}{(m.group(3) or '').upper()}"
        m = re.search(r"\bi([3579])[-\s]?(\d{4,5})\s*(KF|KS|K|F|T)?\b", nome, re.I)
        if m:
            return f"Core i{m.group(1)}-{m.group(2)}{(m.group(3) or '').upper()}"
    return None


SINONIMOS = {"black": "preto", "white": "branco", "tower": "torre", "pink": "rosa", "blue": "azul",
             "red": "vermelho", "grey": "cinza", "gray": "cinza", "green": "verde", "silver": "prata"}
PALAVRAS_VAZIAS = set(("de da do das dos com sem e para p por em a o the and with kabum terabyte pichau amazon "
                       "shopee magalu aliexpress mercado livre oferta promocao valor original oficial novo "
                       "lacrado frete gratis envio full pix cupom").split())


RE_PADRAO = re.compile(r"^(gen\d|\d|ii|iii|iv)$")


def tokens(nome):
    """Palavras do nome para comparar produtos. 'SATA 3', 'SATA III', 'Gen3' e 'PCIe 4' não mudam o produto."""
    saida = set()
    for t in slug(nome).split("-"):
        t = re.sub(r"^(sata|pcie)(\d|iii|ii)$", r"\1", t)
        if t and t not in PALAVRAS_VAZIAS and not RE_PADRAO.match(t):
            saida.add(SINONIMOS.get(t, t))
    return saida


def mesmo_produto(a, b):
    """Compara dois conjuntos de palavras: os códigos com número precisam bater e o resto quase todo."""
    ca = {t for t in a if re.fullmatch(r"\d+(gb|tb)", t)}
    cb = {t for t in b if re.fullmatch(r"\d+(gb|tb)", t)}
    if ca and cb and ca != cb:
        return False  # 500GB não se junta com "250GB 500GB 1TB", nem 512GB com 1TB
    da = {t for t in a if any(c.isdigit() for c in t)}
    db = {t for t in b if any(c.isdigit() for c in t)}
    if da != db and not (da and db and (da <= db or db <= da) and len(da & db) >= 2):
        return False
    menor = min(len(a), len(b))
    if not menor:
        return False
    return len(a & b) / menor >= (0.7 if da else 0.85)


# ---------------------------------------------------------------- coleta
def ler_canal(canal, html=None, antes=None):
    """Baixa a página pública do canal (t.me/s/canal) e devolve os posts com texto e data.
    Com 'antes', traz os 20 posts anteriores a esse número de post."""
    if html is None:
        url = f"https://t.me/s/{canal}" + (f"?before={antes}" if antes else "")
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        html = r.text
    sopa = BeautifulSoup(html, "html.parser")
    posts = []
    for msg in sopa.select("div.tgme_widget_message[data-post]"):
        corpo = msg.select_one("div.tgme_widget_message_text")
        hora = msg.select_one("time[datetime]")
        if not corpo or not hora:
            continue
        for riscado in corpo.find_all(["s", "del", "strike"]):
            riscado.decompose()
        for br in corpo.find_all("br"):
            br.replace_with("\n")
        posts.append({"post": msg["data-post"], "data": hora["datetime"], "texto": corpo.get_text()})
    return posts


def tapar_buracos(canal, recentes, todos, e, numero_post, paginas_max=15):
    """A página pública mostra só os 20 posts mais novos. Se o canal postou mais que isso entre duas coletas
    (ou uma coleta atrasou), sobra um buraco na numeração. Aqui o coletor volta para buscar o que faltou
    nos últimos 3 dias."""
    limite = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    conhecidos = {int(pid.split("/")[1]) for pid, p in todos.items() if p["canal"] == canal and p["data"] >= limite}
    conhecidos |= {numero_post(p) for p in recentes}
    if len(conhecidos) < 2:
        return []
    vistos = set(e.get("buracos_vistos", []))
    ordem = sorted(conhecidos)
    buracos = [(a, b) for a, b in zip(ordem, ordem[1:]) if b - a > 3 and f"{a}-{b}" not in vistos]
    achados, paginas = [], 0
    for a, b in reversed(buracos):  # do mais novo para o mais antigo
        antes = b
        while antes > a + 1 and paginas < paginas_max:
            pagina = ler_canal(canal, antes=antes)
            paginas += 1
            if not pagina:
                break
            achados += [p for p in pagina if numero_post(p) not in conhecidos]
            antes = min(map(numero_post, pagina))
            time.sleep(0.4)
        if antes <= a + 1:
            vistos.add(f"{a}-{b}")
        if paginas >= paginas_max:
            break
    e["buracos_vistos"] = sorted(vistos, key=lambda x: int(x.split("-")[0]))[-60:]
    return achados


def resolver_link(link):
    """Abre o link encurtado e devolve (endereço final, nome da loja)."""
    final = link
    try:
        r = requests.get(link, headers=UA, timeout=12, allow_redirects=True, stream=True)
        final = r.url
        r.close()
    except requests.RequestException:
        pass
    u = urlparse(final)
    dominio = u.netloc.lower().removeprefix("www.")
    loja = next((nome for chave, nome in LOJAS.items() if chave in dominio), "")
    if dominio in ENCURTADORES:
        return "", loja
    return f"{dominio}{u.path}".rstrip("/"), loja


def loja_pelo_texto(texto):
    baixo = texto.lower()
    return next((nome for chave, nome in LOJAS.items() if chave in baixo), "")


def arquivo_do_mes(data_iso):
    return PASTA_POSTS / f"{data_iso[:7]}.json"


def carregar_posts():
    todos = {}
    for arq in sorted(PASTA_POSTS.glob("*.json")):
        todos.update(json.loads(arq.read_text(encoding="utf-8")))
    return todos


def salvar_posts(todos):
    por_mes = defaultdict(dict)
    for pid, p in todos.items():
        por_mes[arquivo_do_mes(p["data"])][pid] = p
    PASTA_POSTS.mkdir(parents=True, exist_ok=True)
    for arq, posts in por_mes.items():
        arq.write_text(json.dumps(posts, ensure_ascii=False, sort_keys=True, indent=0), encoding="utf-8")


def guardar_novos(todos, novos, resolver=resolver_link):
    """Guarda os posts que ainda não existem e descobre a loja dos recentes. Devolve quantos entraram.
    Posts com mais de 3 dias (do histórico) não têm o link aberto, para a coleta não demorar."""
    entraram = 0
    recente = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    for post in novos:
        if post["post"] in todos or post["data"] < HISTORICO_DESDE:
            continue
        info = interpretar_post(post["texto"])
        todos[post["post"]] = {"canal": post["post"].split("/")[0], "data": post["data"], "texto": post["texto"],
                               "link": info["link"] if info else "", "url": "", "loja": "",
                               "resolvido": not (info and info["link"]) or post["data"] < recente}
        entraram += 1
    pendentes = [p for p in todos.values() if not p["resolvido"]][:MAX_LINKS_POR_RODADA]
    for p in pendentes:
        p["url"], p["loja"] = resolver(p["link"])
        p["resolvido"] = True
    return entraram


# ---------------------------------------------------------------- índice para o site
def montar_indice(todos, canais):
    grupos = {}            # chave -> {"nome", "cat", "ofertas": [...], "tokens": set de frozensets}
    indice_palavras = defaultdict(lambda: defaultdict(set))   # categoria -> palavra -> chaves
    frequencia = defaultdict(lambda: defaultdict(int))
    ja_visto = {}

    for pid, p in sorted(todos.items(), key=lambda kv: kv[1]["data"]):
        info = interpretar_post(p["texto"])
        if not info:
            continue
        cat = categorizar(info["nome"])
        modelo = modelo_canonico(info["nome"], cat)
        if modelo:
            chave = "m:" + slug(modelo)
            grupos.setdefault(chave, {"nome": modelo, "cat": cat, "ofertas": [], "tokens": set(), "nomes": []})
        else:
            tk = frozenset(tokens(info["nome"]))
            if not tk:
                continue
            idx, freq = indice_palavras[cat], frequencia[cat]
            chave = ja_visto.get((cat, tk))
            if not chave:
                com_numero = [t for t in tk if any(c.isdigit() for c in t)]
                chaves_de = sorted(com_numero or tk, key=lambda t: freq[t])[:2]
                candidatos = set().union(*(idx[t] for t in chaves_de))
                chave = next((c for c in sorted(candidatos)
                              if any(mesmo_produto(tk, outro) for outro in grupos[c]["tokens"])), None)
            if not chave:
                chave = f"f:{len(grupos)}"
                grupos[chave] = {"nome": "", "cat": cat, "ofertas": [], "tokens": set(), "nomes": []}
            ja_visto[(cat, tk)] = chave
            if tk not in grupos[chave]["tokens"]:
                grupos[chave]["tokens"].add(tk)
                for t in tk:
                    idx[t].add(chave)
                    freq[t] += 1
        g = grupos[chave]
        g["nomes"].append(info["nome"])
        quando = datetime.fromisoformat(p["data"])
        loja = p.get("loja") or loja_pelo_texto(p["texto"])

        repetida = next((o for o in reversed(g["ofertas"])
                         if o["preco"] == info["preco"] and o["cupom"] == info["cupom"]
                         and quando - o["quando"] <= JANELA_DUPLICATA), None)
        if repetida:
            repetida["canais"].add(p["canal"])
            repetida["posts"] += 1
            repetida["loja"] = repetida["loja"] or loja
            continue
        g["ofertas"].append({"quando": quando, "preco": info["preco"], "cupom": info["cupom"],
                             "pag": info["pagamento"], "link": info["link"], "loja": loja,
                             "canal": p["canal"], "canais": {p["canal"]}, "posts": 1, "post": pid, "versoes": info["versoes"]})

    produtos, usados = [], set()
    for chave, g in grupos.items():
        if g["cat"] == "Outros":
            continue  # o site mostra tecnologia, eletrônicos, eletrodomésticos e casa
        ofertas = g["ofertas"]
        precos = [o["preco"] for o in ofertas]
        mediana = statistics.median(precos)
        validas = [o for o in ofertas if len(ofertas) < 3 or o["preco"] >= 0.3 * mediana]
        if not validas:
            continue
        nome = g["nome"] or min((n for n in g["nomes"] if len(n) >= 15), key=len, default=g["nomes"][0])
        base = slug(nome)[:48].strip("-") or "produto"
        pid, n = base, 2
        while pid in usados:
            pid, n = f"{base}-{n}", n + 1
        usados.add(pid)

        por_dia = {}
        for o in validas:
            dia = o["quando"].astimezone(FUSO).date().isoformat()
            if dia not in por_dia or o["preco"] < por_dia[dia]["preco"]:
                por_dia[dia] = o
        linha = lambda o: [round(o["preco"], 2), o["loja"], o["cupom"], o["pag"], o["link"], o["canal"], o.get("post", "")]
        ultima = lambda o: linha(o) + ([o["versoes"]] if o.get("versoes") else [])
        ult = validas[-1]
        produtos.append({
            "id": pid, "nome": nome, "cat": g["cat"], "posts": sum(o["posts"] for o in validas),
            "dias": [[dia] + linha(o) + [o["quando"].astimezone(FUSO).strftime("%H:%M")] for dia, o in sorted(por_dia.items())],
            "ult": [ult["quando"].astimezone(timezone.utc).isoformat(timespec="minutes")] + ultima(ult),
        })
    produtos.sort(key=lambda p: p["ult"][0], reverse=True)
    return {"versao": 2, "alertas": [{k: a[k] for k in ("arquivo", "produto", "nome", "busca", "top", "maximo")} for a in ler_alertas()], "atualizado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "canais": canais, "produtos": produtos}


# ---------------------------------------------------------------- alertas no Telegram
def ler_alertas():
    """Cada arquivo em alertas/ é um alerta: linhas 'chave: valor' com produto, nome, avisar_quando e preco_maximo."""
    alertas = []
    for arq in sorted(PASTA_ALERTAS.glob("*.txt")) if PASTA_ALERTAS.exists() else []:
        campos = {}
        for linha in arq.read_text(encoding="utf-8").splitlines():
            if ":" in linha and not linha.strip().startswith("#"):
                k, v = linha.split(":", 1)
                campos[k.strip().lower()] = v.strip()
        maximo = re.sub(r"[^\d,.]", "", campos.get("preco_maximo", ""))
        try:
            maximo = numero(maximo) if maximo else None
        except ValueError:
            maximo = None
        alertas.append({"arquivo": arq.name, "produto": campos.get("produto", "" if campos.get("busca") else arq.stem), "nome": campos.get("nome", ""),
                        "busca": campos.get("busca", ""),
                        "top": "top" in campos.get("avisar_quando", "").lower(), "maximo": maximo})
    return alertas


def veredito(prod, agora):
    """Mesma regra do site: top, compensa, razoavel, caro ou sem (pouco histórico)."""
    precos = [d[1] for d in prod["dias"]]
    if len(precos) < 3:
        return "sem", 0.0
    u = prod["ult"][1]
    limite = (agora - timedelta(days=60)).astimezone(FUSO).date().isoformat()
    recentes = [d[1] for d in prod["dias"] if d[0] >= limite]
    med = statistics.median(recentes if len(recentes) >= 3 else precos)
    menor = min(precos)
    alvo = None
    if len(precos) >= 5:
        o = sorted(precos)
        i = (len(o) - 1) * 0.25
        b = int(i)
        alvo = round((o[b] + (o[min(b + 1, len(o) - 1)] - o[b]) * (i - b)) / 10) * 10
    queda = 1 - u / med
    if u <= menor * 1.02 or queda >= 0.15:
        return "top", queda
    if (alvo and u <= alvo) or queda >= 0.05:
        return "compensa", queda
    if u <= med * 1.04:
        return "razoavel", queda
    return "caro", queda


def termos_busca(texto):
    """Mesma regra da busca do site: sem acento, sem espaços e sem o 's' do plural."""
    saida = []
    for t in re.split(r"[\s,]+", unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()):
        t = re.sub(r"[^a-z0-9]", "", t)
        if t:
            saida.append(t[:-1] if len(t) > 4 and t.endswith("s") else t)
    return saida


def casa_busca(ts, prod):
    """Todas as palavras da busca no nome; números precisam bater inteiros (500 casa com 500GB, não com 3500MB/s)."""
    junto = texto_busca(prod)
    espaco = re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", prod["nome"]).encode("ascii", "ignore").decode().lower())
    return all(re.search(rf"(?<![0-9]){t}(?![0-9])", espaco) if t.isdigit() else t in junto for t in ts)


def texto_busca(prod):
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", prod["nome"] + " " + prod["cat"]).encode("ascii", "ignore").decode().lower())


def reais_txt(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def telegram(token, metodo, **dados):
    r = requests.post(f"https://api.telegram.org/bot{token}/{metodo}", json=dados, timeout=20)
    r.raise_for_status()
    return r.json()


ARQ_PESSOAS = PASTA_DADOS / "pessoas.bin"
MAX_ALERTAS_PESSOA = 10
PREFIXOS_MODELO = {"rtx", "gtx", "rx", "ryzen", "i3", "i5", "i7", "i9", "ultra", "core", "galaxy", "iphone", "redmi", "moto",
                   "ps", "xbox", "arc", "a", "s", "tv", "monitor", "gen"}
AJUDA = ("Para pesquisar, escreva o nome do produto (ex.: ak400).\n\nPara ser avisado, mande o produto e o preço máximo, assim:\n\n"
         "ak400 até 150\nrx 9060 xt até 2500\nfonte 650w até 300\n\n"
         
         "Os botões 🔥 Ofertas do dia, 📉 Maiores quedas, 🔎 Pesquisar e ⚖️ Comparar abrem o radar na hora, aqui dentro do Telegram.\n\n"
         "Já as mensagens escritas, 🔔 Meus alertas e 📬 Resumo diário eu leio a cada 10 minutos, então a resposta pode demorar um pouquinho.")


def _chaves(token):
    base = hashlib.sha256(("radar-de-precos|" + token).encode()).digest()
    return hashlib.sha256(base + b"cifra").digest(), hashlib.sha256(base + b"selo").digest()


def _fluxo(chave, nonce, n):
    saida, i = bytearray(), 0
    while len(saida) < n:
        saida += hashlib.sha256(chave + nonce + i.to_bytes(8, "big")).digest()
        i += 1
    return bytes(saida[:n])


def guardar_pessoas(pessoas, token):
    """Grava a lista de pessoas e alertas cifrada: o projeto é público, mas ninguém sem a chave do robô consegue ler."""
    ce, cm = _chaves(token)
    dados = json.dumps(pessoas, ensure_ascii=False).encode()
    nonce = os.urandom(16)
    cifra = bytes(a ^ b for a, b in zip(dados, _fluxo(ce, nonce, len(dados))))
    selo = hmac.new(cm, nonce + cifra, hashlib.sha256).digest()
    ARQ_PESSOAS.write_bytes(base64.b64encode(nonce + selo + cifra))


def ler_pessoas(token):
    if not ARQ_PESSOAS.exists():
        return {}
    bruto = base64.b64decode(ARQ_PESSOAS.read_bytes())
    nonce, selo, cifra = bruto[:16], bruto[16:48], bruto[48:]
    ce, cm = _chaves(token)
    if not hmac.compare_digest(selo, hmac.new(cm, nonce + cifra, hashlib.sha256).digest()):
        print("pessoas: a chave do robô mudou, a lista antiga foi guardada à parte e começa uma nova")
        ARQ_PESSOAS.rename(ARQ_PESSOAS.with_suffix(".antigo.bin"))
        return {}
    return json.loads(bytes(a ^ b for a, b in zip(cifra, _fluxo(ce, nonce, len(cifra)))))


def entender_pedido(texto):
    """'ak400 até 150' -> ('ak400', 150.0). Sem preço -> (texto, None), que vira alerta de Promoção top."""
    texto = re.sub(r"\s+", " ", texto.strip())
    m = re.search(r"^(.*?)\s*(?:at[eé]|max(?:imo)?|r\$|por)\s*:?\s*r?\$?\s*(\d[\d.,]*)\s*(?:reais)?\s*$", texto, re.I)
    if m and m.group(1).strip():
        return m.group(1).strip(), numero(m.group(2).rstrip(".,"))
    partes = texto.split(" ")
    if len(partes) >= 2 and re.fullmatch(r"\d{2,6}(?:[.,]\d{1,2})?", partes[-1]):
        valor = numero(partes[-1])
        if valor >= 50 and partes[-2].lower() not in PREFIXOS_MODELO:
            return " ".join(partes[:-1]), valor
    return texto, None


def ler_inicio(carga, por_id):
    """Botão do site: /start b_ak400_deepcool_p150 (busca) ou /start i_<produto>_top."""
    partes = carga.split("_")
    if len(partes) < 3 or partes[0] not in ("b", "i"):
        return None
    fim = partes[-1]
    top, maximo = fim == "top", (float(fim[1:]) if re.fullmatch(r"p\d+", fim) else None)
    if not top and maximo is None:
        return None
    if partes[0] == "b":
        return {"busca": " ".join(partes[1:-1]), "produto": "", "nome": "", "top": top, "maximo": maximo}
    prod = por_id.get("_".join(partes[1:-1]))
    return {"busca": "", "produto": prod["id"], "nome": prod["nome"], "top": top, "maximo": maximo} if prod else None


def descrever(a):
    alvo = f"\"{a['busca']}\"" if a["busca"] else a["nome"]
    regra = f"até {reais_txt(a['maximo'])}" if a["maximo"] else "quando entrar em Promoção top"
    return f"{alvo} {regra}"


def candidatos_do_alerta(a, indice, por_id):
    if a["busca"]:
        ts = termos_busca(a["busca"])
        return [p for p in indice["produtos"] if ts and casa_busca(ts, p)]
    prod = por_id.get(a["produto"])
    if not prod and a["nome"]:  # o produto pode ter mudado de nome no radar
        tk = tokens(a["nome"])
        prod = next((p for p in indice["produtos"] if mesmo_produto(tk, tokens(p["nome"]))), None)
    return [prod] if prod else []


BOTOES = {"ofertas": "🔥 Ofertas do dia", "quedas": "📉 Maiores quedas", "vale": "🏆 Vale a pena agora",
          "pesquisar": "🔎 Pesquisar", "comparar": "⚖️ Comparar",
          "alertas": "🔔 Meus alertas", "resumo": "📬 Resumo diário", "ajuda": "❓ Ajuda"}
ICONE_V = {"top": "🟢", "compensa": "🟩", "razoavel": "🟡", "caro": "🔴", "sem": "⚪"}
NOME_V = {"top": "Promoção top", "compensa": "Compensa", "razoavel": "Preço razoável", "caro": "Não compensa", "sem": "Acompanhando"}
ORDEM_CATS = ["Placas de vídeo", "Processadores", "Placas-mãe", "Memória RAM", "SSD e HDs", "Fontes", "Gabinetes", "Coolers",
              "Monitores", "Mouses", "Teclados", "Headsets e fones", "Controles", "Setup e acessórios", "Notebooks", "PCs montados",
              "Consoles", "TVs", "Celulares, tablets e relógios", "Câmeras e casa inteligente", "Eletrodomésticos", "Móveis",
              "Casa e cozinha", "Cama, mesa e banho", "Ferramentas", "Jogos e software"]


def teclado(site):
    """Botões fixos do robô. Os que abrem o site respondem na hora; os outros, na próxima coleta."""
    app = lambda texto, aba: {"text": texto, "web_app": {"url": f"{site}#{aba}"}} if site else {"text": texto}
    return {"keyboard": [[app(BOTOES["ofertas"], "top"), app(BOTOES["quedas"], "quedas")],
                         [app(BOTOES["pesquisar"], "ofertas"), app(BOTOES["comparar"], "comparar")],
                         [{"text": BOTOES["alertas"]}, {"text": BOTOES["resumo"]}, {"text": BOTOES["ajuda"]}]],
            "resize_keyboard": True, "is_persistent": True, "input_field_placeholder": "Produto, ou produto até preço"}


def h(t):
    return html.escape(str(t), quote=False)


def links_oferta(p):
    """Links da última oferta: a página do produto na loja e o post original no canal, com os cupons."""
    link, post = p["ult"][5], p["ult"][7] if len(p["ult"]) > 7 else ""
    partes = []
    if p["ult"][3]:
        partes.append(f"🎟 <code>{h(p['ult'][3])}</code>")
    if link:
        partes.append(f'<a href="{h(link)}">🛒 Comprar</a>')
    if post:
        partes.append(f'<a href="https://t.me/{h(post)}">📣 Post no canal</a>')
    return "     " + " · ".join(partes) if partes else ""


def linha_produto(p, agora, site, extra=""):
    v, queda = veredito(p, agora)
    nome = f'<a href="{site}#p-{p["id"]}">{h(p["nome"])}</a>' if site else h(p["nome"])
    quando = datetime.fromisoformat(p["ult"][0]).astimezone(FUSO).strftime("%d/%m às %H:%M")
    partes = [reais_txt(p["ult"][1])]
    if v != "sem":
        partes.append(f"{'−' if queda >= 0 else '+'}{abs(queda):.0%} vs mediana".replace(".", ","))
    partes += [x for x in (p["ult"][2], quando, extra) if x]
    links = links_oferta(p)
    return f"{ICONE_V[v]} {nome}\n     {' · '.join(partes)}" + (f"\n{links}" if links else "")


def ofertas_do_dia(indice, agora, site, limite=10):
    recentes = []
    for p in indice["produtos"]:
        if agora - datetime.fromisoformat(p["ult"][0]) <= timedelta(hours=24):
            v, queda = veredito(p, agora)
            if v == "top":
                recentes.append((queda, p))
    recentes.sort(key=lambda x: -x[0])
    if not recentes:
        return "Nenhuma Promoção top nas últimas 24 horas. Volte mais tarde: os canais são lidos a cada 10 minutos."
    return ("🔥 <b>Ofertas do dia</b> (Promoção top nas últimas 24 horas)\n\n"
            + "\n\n".join(linha_produto(p, agora, site) for _, p in recentes[:limite]))


def maiores_quedas(indice, agora, site, limite=10):
    hoje = (agora - timedelta(hours=36)).astimezone(FUSO).date().isoformat()
    lista = []
    for p in indice["produtos"]:
        dias = p["dias"]
        for i in range(max(3, len(dias) - 3), len(dias)):
            if dias[i][0] < hoje:
                continue
            antes = [d[1] for d in dias[:i]]
            q = 1 - dias[i][1] / statistics.median(antes)
            if q >= 0.1:
                lista.append((q, p, dias[i]))
    lista.sort(key=lambda x: -x[0])
    if not lista:
        return "Nenhuma queda forte nas últimas 36 horas."
    linhas = []
    for q, p, d in lista[:limite]:
        nome = f'<a href="{site}#p-{p["id"]}">{h(p["nome"])}</a>' if site else h(p["nome"])
        tipo = "⚡ possível erro de preço" if q >= 0.4 else "📉"
        hora = f" · {d[0][8:10]}/{d[0][5:7]}" + (f" às {d[8]}" if len(d) > 8 else "")
        linhas.append(f"{tipo} {nome}\n     {reais_txt(d[1])} · −{q:.0%} da mediana{' · ' + h(d[2]) if d[2] else ''}{hora}")
    return "📉 <b>Maiores quedas</b> (últimas 36 horas)\n\n" + "\n\n".join(linhas)


def vale_a_pena(indice, agora, site):
    melhores = {}
    for p in indice["produtos"]:
        if agora - datetime.fromisoformat(p["ult"][0]) > timedelta(days=3):
            continue
        v, queda = veredito(p, agora)
        if v in ("top", "compensa") and (p["cat"] not in melhores or queda > melhores[p["cat"]][0]):
            melhores[p["cat"]] = (queda, p)
    if not melhores:
        return "Nada em bom preço nos últimos 3 dias."
    linhas = [f"<b>{h(c)}</b>\n{linha_produto(melhores[c][1], agora, site)}" for c in ORDEM_CATS if c in melhores]
    return "🏆 <b>Vale a pena agora</b>: o melhor preço de cada categoria nos últimos 3 dias\n\n" + "\n\n".join(linhas)


def pesquisar(texto, indice, agora, site):
    ts = termos_busca(texto)
    achados = [p for p in indice["produtos"] if ts and casa_busca(ts, p)]
    if not achados:
        return (f"Não achei \"{h(texto)}\" no radar. Tente menos palavras, como só o modelo (ex.: ak400, 9060, 7800x3d).", None)
    modelo = lambda p: 0 if re.match(r"(?i)^(rtx|gtx|rx|arc|ryzen|core)", p["nome"]) else 1
    achados.sort(key=lambda p: (modelo(p), p["ult"][0]), reverse=False)
    achados = sorted(achados, key=lambda p: (modelo(p), -datetime.fromisoformat(p["ult"][0]).timestamp()))
    corpo = "\n\n".join(linha_produto(p, agora, site) for p in achados[:6])
    texto_fim = f"\n\n…e mais {len(achados) - 6} no site." if len(achados) > 6 else ""
    msg = f"🔎 <b>{h(texto)}</b>: {len(achados)} {'produto' if len(achados) == 1 else 'produtos'}\n\n{corpo}{texto_fim}\n\n" \
          f"Para ser avisado de um preço, mande por exemplo: <code>{h(texto)} até 300</code>"
    chave = "_".join(ts)[:55]
    botoes = [[{"text": "🔔 Avisar quando entrar em Promoção top", "callback_data": "a|" + chave}]]
    if site:
        botoes.append([{"text": "🌐 Ver todos no site", "url": f"{site}#b-{'-'.join(ts)[:60]}"}])
    return msg, {"inline_keyboard": botoes}


def lista_alertas(pessoa):
    if not pessoa["alertas"]:
        return "Você ainda não tem alertas.\n\n" + AJUDA, None
    linhas = [f"{i}. {h(descrever(a))}" for i, a in enumerate(pessoa["alertas"], 1)]
    botoes = [[{"text": f"🗑 Apagar {i}", "callback_data": "d|" + a.setdefault("id", os.urandom(3).hex())}]
              for i, a in enumerate(pessoa["alertas"], 1)]
    return "🔔 <b>Seus alertas</b>\n\n" + "\n".join(linhas) + "\n\nToque para apagar, ou mande: parar 1.", {"inline_keyboard": botoes}


def criar_alerta(pessoa, novo, indice, por_id):
    if len(pessoa["alertas"]) >= MAX_ALERTAS_PESSOA:
        return f"Você já tem {MAX_ALERTAS_PESSOA} alertas, o máximo. Apague um em 🔔 Meus alertas."
    novo["id"] = os.urandom(3).hex()
    pessoa["alertas"].append(novo)
    achados = candidatos_do_alerta(novo, indice, por_id)
    resposta = f"✅ Alerta criado: {h(descrever(novo))}.\n\n"
    if achados:
        mais_barato = min(achados, key=lambda p: p["ult"][1])
        resposta += (f"Hoje o radar acompanha {len(achados)} {'produto' if len(achados) == 1 else 'produtos'} assim. "
                     f"O último preço mais baixo foi {reais_txt(mais_barato['ult'][1])} ({h(mais_barato['nome'])}).\n\n")
    else:
        resposta += "Ainda não vi nenhum produto com esse nome nos canais, mas deixei o alerta ligado.\n\n"
    return resposta + "Te aviso aqui quando aparecer uma oferta assim."


def responder_mensagens(token, tg, pessoas, indice, por_id):
    """Lê o que as pessoas mandaram ao robô desde a última coleta e responde, com o menu de botões."""
    site, agora = tg.get("site", ""), datetime.now(timezone.utc)
    resp = telegram(token, "getUpdates", offset=tg.get("offset", 0), timeout=0, allowed_updates=["message", "callback_query"])
    def enviar(chat, texto, botoes=None):
        """Manda a resposta; textos longos (o Telegram aceita até 4096 letras) vão em partes."""
        pedacos, atual = [], ""
        for bloco in texto.split("\n\n"):
            if atual and len(atual) + len(bloco) + 2 > 3900:
                pedacos.append(atual)
                atual = bloco
            else:
                atual = f"{atual}\n\n{bloco}" if atual else bloco
        pedacos.append(atual)
        for i, pedaco in enumerate(pedacos):
            ultimo = i == len(pedacos) - 1
            telegram(token, "sendMessage", chat_id=chat, text=pedaco, parse_mode="HTML", disable_web_page_preview=True,
                     reply_markup=(botoes or teclado(site)) if ultimo else teclado(site))
    for upd in resp.get("result", []):
        tg["offset"] = upd["update_id"] + 1
        try:
            if "callback_query" in upd:
                cq = upd["callback_query"]
                cid, dado = str(cq["from"]["id"]), cq.get("data", "")
                pessoa = pessoas.setdefault(cid, {"nome": cq["from"].get("first_name", ""), "alertas": [], "avisos": {}})
                try:
                    telegram(token, "answerCallbackQuery", callback_query_id=cq["id"])
                except requests.RequestException:
                    pass  # resposta tardia ao toque no botão: o Telegram pode recusar, não faz mal
                if dado.startswith("a|"):
                    enviar(int(cid), criar_alerta(pessoa, {"busca": dado[2:].replace("_", " "), "produto": "", "nome": "",
                                                           "top": True, "maximo": None}, indice, por_id))
                elif dado.startswith("d|"):
                    antes = len(pessoa["alertas"])
                    pessoa["alertas"] = [a for a in pessoa["alertas"] if a.get("id") != dado[2:]]
                    enviar(int(cid), "🗑 Alerta apagado." if len(pessoa["alertas"]) < antes else "Esse alerta já tinha sido apagado.")
                continue

            msg = upd.get("message") or {}
            chat, texto = msg.get("chat", {}), (msg.get("text") or "").strip()
            if chat.get("type") != "private" or not texto:
                continue
            cid = str(chat["id"])
            if not tg.get("chat"):
                tg["chat"] = chat["id"]  # o primeiro a falar com o robô é o dono, que recebe os alertas criados pelo GitHub
            pessoa = pessoas.setdefault(cid, {"nome": msg.get("from", {}).get("first_name", ""), "alertas": [], "avisos": {}})
            baixo = texto.lower()
            if baixo.startswith("/start"):
                carga = texto[6:].strip()
                novo = ler_inicio(carga, por_id) if carga else None
                if novo:
                    enviar(chat["id"], criar_alerta(pessoa, novo, indice, por_id))
                else:
                    enviar(chat["id"], f"Oi, {h(pessoa['nome'] or 'tudo bem')}! 👋 Eu sou o Radar de Preços.\n\n" + AJUDA)
            elif texto == BOTOES["ofertas"] or baixo in ("/ofertas", "ofertas"):
                enviar(chat["id"], ofertas_do_dia(indice, agora, site))
            elif texto == BOTOES["quedas"] or baixo in ("/quedas", "quedas"):
                enviar(chat["id"], maiores_quedas(indice, agora, site))
            elif texto == BOTOES["vale"] or baixo in ("/vale", "vale a pena"):
                enviar(chat["id"], vale_a_pena(indice, agora, site))
            elif texto == BOTOES["alertas"] or baixo in ("/meusalertas", "meus alertas", "alertas", "/alertas"):
                enviar(chat["id"], *lista_alertas(pessoa))
            elif texto == BOTOES["resumo"] or baixo in ("/resumo", "resumo"):
                pessoa["resumo"] = not pessoa.get("resumo")  # liga ou desliga
                if pessoa["resumo"]:
                    pessoa["resumo_dia"] = agora.astimezone(FUSO).date().isoformat()  # o de hoje já vai agora
                    enviar(chat["id"], "📬 Resumo diário ligado! A partir de amanhã, toda manhã eu te mando as melhores ofertas. "
                           "As de agora estão aqui:\n\n" + ofertas_do_dia(indice, agora, site))
                else:
                    enviar(chat["id"], "📭 Resumo diário desligado. Para ligar de novo, toque em 📬 Resumo diário.")
            elif texto == BOTOES["ajuda"] or baixo in ("/ajuda", "ajuda", "/help", "oi", "olá", "ola"):
                enviar(chat["id"], AJUDA)
            elif re.match(r"^/?(parar|apagar|remover|excluir)\b", baixo):
                n = re.search(r"\d+", baixo)
                if n and 1 <= int(n.group()) <= len(pessoa["alertas"]):
                    tirado = pessoa["alertas"].pop(int(n.group()) - 1)
                    enviar(chat["id"], f"🗑 Apaguei o alerta: {h(descrever(tirado))}.")
                else:
                    enviar(chat["id"], *lista_alertas(pessoa))
            else:
                busca, maximo = entender_pedido(texto)
                if maximo:
                    enviar(chat["id"], criar_alerta(pessoa, {"busca": busca, "produto": "", "nome": "", "top": False, "maximo": maximo},
                                                    indice, por_id))
                else:
                    enviar(chat["id"], *pesquisar(texto, indice, agora, site))
        except Exception as erro:  # uma mensagem estranha não pode travar o robô nem a coleta
            print(f"robô: falha ao responder ({type(erro).__name__}: {erro})", file=sys.stderr)

    # resumo diário: a partir das 9h, uma vez por dia
    hoje = agora.astimezone(FUSO)
    if hoje.hour >= 9:
        corpo = None
        for cid, pessoa in pessoas.items():
            if pessoa.get("resumo") and pessoa.get("resumo_dia") != hoje.date().isoformat():
                corpo = corpo or ofertas_do_dia(indice, agora, site, limite=8)
                try:
                    enviar(int(cid), "☀️ Bom dia! " + corpo)
                    pessoa["resumo_dia"] = hoje.date().isoformat()
                except requests.RequestException as erro:
                    print(f"robô: falha no resumo ({erro})", file=sys.stderr)


def verificar_alertas(indice, estado):
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    alertas = ler_alertas()
    if not token:
        if alertas:
            print("alertas: falta o segredo TELEGRAM_TOKEN no GitHub, nenhum aviso enviado")
        return
    tg = estado.setdefault("_telegram", {})
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    tg["site"] = site = f"https://{repo.split('/')[0].lower()}.github.io/{repo.split('/')[1]}/" if "/" in repo else ""
    por_id = {p["id"]: p for p in indice["produtos"]}
    pessoas = ler_pessoas(token)
    try:
        if tg.get("config") != 2:
            tg["robo"] = telegram(token, "getMe")["result"]["username"]
            telegram(token, "setMyCommands", commands=[{"command": "ofertas", "description": "Ofertas do dia"},
                                                       {"command": "quedas", "description": "Maiores quedas"},
                                                       {"command": "vale", "description": "O que vale a pena agora"},
                                                       {"command": "meusalertas", "description": "Ver os seus alertas"},
                                                       {"command": "ajuda", "description": "Como usar"}])
            if site:
                telegram(token, "setChatMenuButton", menu_button={"type": "web_app", "text": "Abrir Radar", "web_app": {"url": site}})
            telegram(token, "setMyDescription", description="Te aviso no Telegram quando um produto de tecnologia ficar barato. "
                     "Toque em Iniciar e me mande o produto e o preço, por exemplo: ak400 até 150.")
            telegram(token, "setMyShortDescription", short_description="Alertas de preço de tecnologia, eletrônicos, eletrodomésticos e casa.")
            tg["config"] = 2
        responder_mensagens(token, tg, pessoas, indice, por_id)
    except (requests.RequestException, KeyError) as erro:
        print(f"alertas: falha ao falar com o Telegram ({erro})", file=sys.stderr)
    indice["robo"] = tg.get("robo", "")

    agora = datetime.now(timezone.utc)
    if tg.get("chat"):  # alertas do dono, criados como arquivo no GitHub
        avisos = estado.setdefault("_avisos", {})
        for a in alertas:
            for prod in candidatos_do_alerta(a, indice, por_id):
                avisar(a, prod, a["arquivo"], agora, site, avisos, token, tg["chat"])
    for cid, pessoa in pessoas.items():
        for i, a in enumerate(pessoa["alertas"]):
            base = f"{a['busca'] or a['produto']}|{a['maximo']}|{a['top']}"
            for prod in candidatos_do_alerta(a, indice, por_id):
                avisar(a, prod, base, agora, site, pessoa["avisos"], token, int(cid))
    guardar_pessoas(pessoas, token)
    print(f"alertas: {len(pessoas)} pessoas, {sum(len(p['alertas']) for p in pessoas.values())} alertas pelo Telegram, {len(alertas)} pelo GitHub")


def avisar(a, prod, base, agora, site, avisos, token, chat):
    """Manda a mensagem de um produto se ele bater a regra do alerta e ainda não tiver sido avisado."""
    quando = datetime.fromisoformat(prod["ult"][0])
    if agora - quando > timedelta(hours=3):
        return  # só oferta nova
    preco, loja, cupom, _, link = prod["ult"][1:6]
    versao = ""
    caps_nome = capacidades(prod["nome"])
    if len(caps_nome) > 1 and a["busca"]:
        pedida = [c for c in caps_nome if any(re.fullmatch(rf"{t}(gb|tb)?", c) for t in termos_busca(a["busca"]))]
        if pedida:
            versoes = prod["ult"][8] if len(prod["ult"]) > 8 else {}
            em_gb = lambda c: int(c[:-2]) * (1000 if c.endswith("tb") else 1)
            if pedida[0] in versoes:
                preco, versao = versoes[pedida[0]], pedida[0].upper()
            elif pedida[0] == min(caps_nome, key=em_gb):
                versao = f"{pedida[0].upper()} (o anúncio tem várias versões; o preço postado costuma ser o da menor)"
            else:
                return  # várias versões, um preço só, e a pedida não é a menor: o preço não é dela, não avisa
    v, queda = veredito(prod, agora)
    if not ((a["top"] and v == "top") or (a["maximo"] and preco <= a["maximo"])):
        return
    chave = base + "|" + prod["id"]
    ant = avisos.get(chave, {})
    if ant.get("quando") == prod["ult"][0]:
        return
    if ant and preco >= ant.get("preco", 0) and agora - datetime.fromisoformat(ant["enviado"]) < timedelta(hours=24):
        return
    postado = quando.astimezone(FUSO).strftime("%d/%m às %H:%M")
    partes = ["🔥 PROMOÇÃO TOP" if v == "top" else "🔔 PREÇO DO SEU ALERTA", prod["nome"], f"🕒 Postado em {postado}", "",
              f"💰 {reais_txt(preco)}" + (f" na versão {versao}" if versao else "")
              + (f" ({queda:.0%} abaixo da mediana)".replace(".", ",") if queda > 0.01 and not versao else "")]
    if cupom:
        partes.append(f"🎟 Cupom: {cupom}")
    if loja:
        partes.append(f"🏬 {loja}")
    if link:
        partes.append(f"🛒 Comprar: {link}")
    if len(prod["ult"]) > 7 and prod["ult"][7]:
        partes.append(f"📣 Post no canal (com os cupons): https://t.me/{prod['ult'][7]}")
    if site:
        partes.append(f"📈 Histórico: {site}#p-{prod['id']}")
    partes.append("⚠️ Confira o preço e a versão na loja antes de pagar: o valor pode mudar depois do post.")
    if a["busca"]:
        partes.append(f"(alerta: \"{a['busca']}\")")
    try:
        telegram(token, "sendMessage", chat_id=chat, text="\n".join(partes))
        avisos[chave] = {"quando": prod["ult"][0], "preco": preco, "enviado": agora.isoformat()}
        print(f"alertas: aviso enviado para {prod['nome']} ({reais_txt(preco)})")
    except Exception as erro:
        print(f"alertas: falha ao enviar aviso ({type(erro).__name__}: {erro})", file=sys.stderr)

def main():
    canais = [l.strip() for l in ARQ_CANAIS.read_text(encoding="utf-8").splitlines()
              if l.strip() and not l.strip().startswith("#")]
    todos = carregar_posts()
    estado = json.loads(ARQ_ESTADO.read_text(encoding="utf-8")) if ARQ_ESTADO.exists() else {}
    novos = []
    numero_post = lambda p: int(p["post"].split("/")[1])
    for canal in canais:
        try:
            posts = ler_canal(canal)
            if not posts:
                print(f"{canal}: nenhum post. O canal é público e o nome está certo?")
                continue
            novos += posts
            e = estado.setdefault(canal, {"antes": min(map(numero_post, posts)), "completo": False})
            buracos = tapar_buracos(canal, posts, todos, e, numero_post)
            novos += buracos
            antigos = 0
            for _ in range(0 if e["completo"] else PAGINAS_ANTIGAS_POR_RODADA):
                pagina = ler_canal(canal, antes=e["antes"])
                if not pagina:
                    e["completo"] = True
                    break
                novos += pagina
                antigos += len(pagina)
                e["antes"] = min(map(numero_post, pagina))
                if min(p["data"] for p in pagina) < HISTORICO_DESDE:
                    e["completo"] = True
                    break
                time.sleep(0.4)
            situacao = "histórico completo" if e["completo"] else "ainda buscando o histórico"
            print(f"{canal}: {len(posts)} recentes, {len(buracos)} recuperados, {antigos} antigos ({situacao})")
        except requests.RequestException as erro:
            print(f"{canal}: falhou ({erro})", file=sys.stderr)
    entraram = guardar_novos(todos, novos)
    salvar_posts(todos)
    indice = montar_indice(todos, canais)
    try:
        verificar_alertas(indice, estado)
    except Exception as erro:  # problema no robô ou nos alertas não impede o site de atualizar
        print(f"alertas: erro inesperado ({type(erro).__name__}: {erro}), o site foi atualizado mesmo assim", file=sys.stderr)
    ARQ_ESTADO.write_text(json.dumps(estado, indent=1), encoding="utf-8")
    ARQ_INDICE.write_text(json.dumps(indice, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if ARQ_ANTIGO.exists():
        ARQ_ANTIGO.unlink()  # formato da primeira versão, substituído por indice.json
    print(f"{entraram} posts novos guardados, {len(todos)} no total, {len(indice['produtos'])} produtos no site")


if __name__ == "__main__":
    main()
