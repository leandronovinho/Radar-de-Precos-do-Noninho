"""Radar de preços: lê canais públicos do Telegram, guarda os posts e monta dados/indice.json para o site.

Uso:  python coletor.py
Dependências:  pip install requests beautifulsoup4
"""
import json
import re
import statistics
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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
JANELA_DUPLICATA = timedelta(hours=6)
MAX_LINKS_POR_RODADA = 120

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
    return {"nome": nome, "preco": preco, "pagamento": pagamento, "cupom": cupom, "link": link.rstrip(".,;")}


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
    ("Setup e acessórios", r"\b(mousepad|mouse pad|cadeira|mesa gamer|webcam|microfone|hub usb|suporte)\b"),
    ("Mouses", r"\b(mouse)\b"),
    ("Teclados", r"\b(teclado)\b"),
    ("Headsets e fones", r"\b(headset|headphone|fone|earbuds?|airpods)\b"),
    ("Controles", r"\b(controle|gamepad|joystick|dualsense)\b"),
    ("Consoles", r"\b(playstation|ps5|ps4|xbox series|nintendo switch|switch 2|console)\b"),
    ("TVs", r"\b(smart ?tv|tv|televis[aã]o)\b"),
    ("Celulares, tablets e relógios", r"\b(celular|smartphone|iphone|galaxy|redmi|poco|motorola|tablet|ipad|smartwatch|rel[oó]gio|watch)\b"),
    ("Setup e acessórios", r"\b(caixa de som|soundbar|roteador|cabo|adaptador|lumin[aá]ria|fita led)\b"),
]


def categorizar(nome):
    """A categoria é a da palavra-chave que aparece primeiro no nome ('Gabinete ... sem fonte' é gabinete).
    Notebook e PC montado vencem sempre, porque citam placa de vídeo e processador no nome."""
    baixo = nome.lower()
    achados = []
    for ordem, (categoria, padrao) in enumerate(CATEGORIAS):
        m = re.search(padrao, baixo)
        if m:
            if categoria in ("Notebooks", "PCs montados"):
                return categoria
            achados.append((m.start(), ordem, categoria))
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


def tokens(nome):
    return {SINONIMOS.get(t, t) for t in slug(nome).split("-") if t and t not in PALAVRAS_VAZIAS}


def mesmo_produto(a, b):
    """Compara dois conjuntos de palavras: os códigos com número precisam bater e o resto quase todo."""
    da = {t for t in a if any(c.isdigit() for c in t)}
    db = {t for t in b if any(c.isdigit() for c in t)}
    if da != db and not (da and db and (da <= db or db <= da) and len(da & db) >= 2):
        return False
    menor = min(len(a), len(b))
    if not menor:
        return False
    return len(a & b) / menor >= (0.7 if da else 0.85)


# ---------------------------------------------------------------- coleta
def ler_canal(canal, html=None):
    """Baixa a página pública do canal (t.me/s/canal) e devolve os posts com texto e data."""
    if html is None:
        r = requests.get(f"https://t.me/s/{canal}", headers=UA, timeout=20)
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
    """Guarda os posts que ainda não existem e descobre a loja de cada um. Devolve quantos entraram."""
    entraram = 0
    for post in novos:
        if post["post"] in todos:
            continue
        info = interpretar_post(post["texto"])
        todos[post["post"]] = {"canal": post["post"].split("/")[0], "data": post["data"], "texto": post["texto"],
                               "link": info["link"] if info else "", "url": "", "loja": "",
                               "resolvido": not (info and info["link"])}
        entraram += 1
    pendentes = [p for p in todos.values() if not p["resolvido"]][:MAX_LINKS_POR_RODADA]
    for p in pendentes:
        p["url"], p["loja"] = resolver(p["link"])
        p["resolvido"] = True
    return entraram


# ---------------------------------------------------------------- índice para o site
def montar_indice(todos, canais):
    grupos = {}            # chave -> {"nome", "cat", "ofertas": [...], "tokens": [...]}
    por_categoria = defaultdict(list)

    for pid, p in sorted(todos.items(), key=lambda kv: kv[1]["data"]):
        info = interpretar_post(p["texto"])
        if not info:
            continue
        cat = categorizar(info["nome"])
        modelo = modelo_canonico(info["nome"], cat)
        if modelo:
            chave = "m:" + slug(modelo)
            grupos.setdefault(chave, {"nome": modelo, "cat": cat, "ofertas": [], "tokens": [], "nomes": []})
        else:
            tk = tokens(info["nome"])
            chave = next((c for c in por_categoria[cat]
                          if any(mesmo_produto(tk, outro) for outro in grupos[c]["tokens"])), None)
            if not chave:
                chave = f"f:{len(grupos)}"
                grupos[chave] = {"nome": "", "cat": cat, "ofertas": [], "tokens": [], "nomes": []}
                por_categoria[cat].append(chave)
            grupos[chave]["tokens"].append(tk)
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
                             "canal": p["canal"], "canais": {p["canal"]}, "posts": 1})

    produtos, usados = [], set()
    for chave, g in grupos.items():
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
        linha = lambda o: [round(o["preco"], 2), o["loja"], o["cupom"], o["pag"], o["link"], o["canal"]]
        ult = validas[-1]
        produtos.append({
            "id": pid, "nome": nome, "cat": g["cat"], "posts": sum(o["posts"] for o in validas),
            "dias": [[dia] + linha(o) for dia, o in sorted(por_dia.items())],
            "ult": [ult["quando"].astimezone(timezone.utc).isoformat(timespec="minutes")] + linha(ult),
        })
    produtos.sort(key=lambda p: p["ult"][0], reverse=True)
    return {"versao": 2, "atualizado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "canais": canais, "produtos": produtos}


def main():
    canais = [l.strip() for l in ARQ_CANAIS.read_text(encoding="utf-8").splitlines()
              if l.strip() and not l.strip().startswith("#")]
    todos = carregar_posts()
    novos = []
    for canal in canais:
        try:
            posts = ler_canal(canal)
            print(f"{canal}: {len(posts)} posts lidos")
            if not posts:
                print("  aviso: nenhum post. O canal é público e o nome está certo?")
            novos += posts
        except requests.RequestException as erro:
            print(f"{canal}: falhou ({erro})", file=sys.stderr)
    entraram = guardar_novos(todos, novos)
    salvar_posts(todos)
    indice = montar_indice(todos, canais)
    ARQ_INDICE.write_text(json.dumps(indice, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if ARQ_ANTIGO.exists():
        ARQ_ANTIGO.unlink()  # formato da primeira versão, substituído por indice.json
    print(f"{entraram} posts novos guardados, {len(todos)} no total, {len(indice['produtos'])} produtos no site")


if __name__ == "__main__":
    main()
