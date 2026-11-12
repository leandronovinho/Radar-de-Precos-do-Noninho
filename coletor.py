"""Radar de preços: lê canais públicos do Telegram e grava as ofertas em dados/ofertas.json.

Uso:  python coletor.py
Dependências:  pip install requests beautifulsoup4
"""
import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

RAIZ = Path(__file__).parent
ARQ_CANAIS = RAIZ / "canais.txt"
ARQ_DADOS = RAIZ / "dados" / "ofertas.json"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
JANELA_DUPLICATA = timedelta(hours=6)

ENCURTADORES = {"tidd.ly", "bit.ly", "s.shopee.com.br", "amzn.to", "mercadolivre.com", "tinyurl.com",
                "cutt.ly", "is.gd", "magazineluiza.onelink.me", "s.click.aliexpress.com"}
NAO_E_PRODUTO = {"t.me", "telegram.me", "chat.whatsapp.com", "whatsapp.com", "wa.me",
                 "instagram.com", "youtube.com", "youtu.be"}
LOJAS = {"kabum": "KaBuM!", "shopee": "Shopee", "amazon": "Amazon", "mercadolivre": "Mercado Livre",
         "magazineluiza": "Magalu", "magalu": "Magalu", "aliexpress": "AliExpress", "pichau": "Pichau",
         "terabyteshop": "Terabyte", "casasbahia": "Casas Bahia", "americanas": "Americanas",
         "fastshop": "Fast Shop", "girafa": "Girafa", "carrefour": "Carrefour"}

RE_PRECO = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+(?:,\d{2})?)")
RE_CUPOM = re.compile(r"(?i:cupom)\s*:?\s*([A-Z0-9][A-Z0-9_\-]{3,24})(?![a-z])")
RE_URL = re.compile(r"https?://[^\s<>\"')]+")
RE_LINK_PRODUTO = re.compile(r"(?i)link\s+d[oa]\s+produto\s*:?\s*(https?://[^\s<>\"')]+)")


def slug(texto):
    """Nome sem acento, minúsculo, só letras e números: serve para comparar produtos."""
    s = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def limpar_nome(linha):
    """Tira emojis e símbolos do começo e do fim da linha do título."""
    return re.sub(r"^[^\w\"']+|[^\w\"')+]+$", "", linha, flags=re.UNICODE).strip()


def interpretar_post(texto):
    """Recebe o texto de um post e devolve nome, preço, pagamento, cupom e link (ou None)."""
    m_preco = RE_PRECO.search(texto)
    if not m_preco:
        return None
    preco = float(m_preco.group(1).replace(".", "").replace(",", "."))
    if preco <= 0:
        return None

    nome = ""
    for linha in texto.splitlines():
        if "R$" in linha or "http" in linha:
            continue
        candidato = limpar_nome(linha)
        if len(candidato) >= 8:
            nome = candidato
            break
    if not nome:
        return None
    # títulos quebrados em duas linhas: junta a continuação até chegar ao preço
    antes_do_preco = texto[: m_preco.start()]
    partes = [limpar_nome(l) for l in antes_do_preco.splitlines() if limpar_nome(l)]
    if partes and partes[0] == nome and len(partes) > 1:
        nome = " ".join(partes)[:160]

    trecho = texto[m_preco.end(): m_preco.end() + 60].lower()
    if "pix" in trecho:
        pagamento = "pix"
    elif "vista" in trecho or "boleto" in trecho:
        pagamento = "à vista"
    elif re.search(r"\d+\s*x", trecho) or "cart" in trecho:
        pagamento = "cartão"
    else:
        pagamento = ""

    m_cupom = RE_CUPOM.search(texto)
    cupom = m_cupom.group(1) if m_cupom else ""

    m_link = RE_LINK_PRODUTO.search(texto)
    link = m_link.group(1) if m_link else ""
    if not link:
        for url in RE_URL.findall(texto):
            if urlparse(url).netloc.lower().removeprefix("www.") not in NAO_E_PRODUTO:
                link = url
    link = link.rstrip(".,;")
    return {"nome": nome, "preco": preco, "pagamento": pagamento, "cupom": cupom, "link": link}


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
        for br in corpo.find_all("br"):
            br.replace_with("\n")
        posts.append({"post": msg["data-post"], "data": hora["datetime"], "texto": corpo.get_text()})
    return posts


def resolver_link(link):
    """Abre o link encurtado e devolve (endereço final sem parâmetros, nome da loja)."""
    if not link:
        return "", ""
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
    if dominio in ENCURTADORES or not u.path.strip("/"):
        return "", loja  # não deu para chegar na página do produto
    return f"{dominio}{u.path}".rstrip("/"), loja


def carregar():
    if ARQ_DADOS.exists():
        return json.loads(ARQ_DADOS.read_text(encoding="utf-8"))
    return {"atualizado": "", "ofertas": []}


def juntar(dados, novos, resolver=resolver_link):
    """Acrescenta os posts novos, sem repetir a mesma oferta vinda de canais diferentes."""
    ofertas = dados["ofertas"]
    vistos = {p for o in ofertas for p in o["posts"]}
    chave_por_url = {o["url"]: o["produto"] for o in ofertas if o.get("url")}
    chave_por_nome = {slug(o["nome"]): o["produto"] for o in ofertas}
    adicionadas = 0

    for post in sorted(novos, key=lambda p: p["data"]):
        if post["post"] in vistos:
            continue
        info = interpretar_post(post["texto"])
        if not info:
            continue
        url, loja = resolver(info["link"])
        nome_slug = slug(info["nome"])
        produto = chave_por_url.get(url) or chave_por_nome.get(nome_slug) or nome_slug
        if url:
            chave_por_url[url] = produto
        chave_por_nome[nome_slug] = produto
        canal = post["post"].split("/")[0]
        quando = datetime.fromisoformat(post["data"])

        repetida = next(
            (o for o in reversed(ofertas)
             if o["produto"] == produto and o["preco"] == info["preco"] and o["cupom"] == info["cupom"]
             and abs(quando - datetime.fromisoformat(o["data"])) <= JANELA_DUPLICATA),
            None,
        )
        if repetida:
            repetida["posts"].append(post["post"])
            if canal not in repetida["canais"]:
                repetida["canais"].append(canal)
            if not repetida["loja"] and loja:
                repetida["loja"] = loja
        else:
            ofertas.append({
                "produto": produto, "nome": info["nome"], "preco": info["preco"],
                "pagamento": info["pagamento"], "cupom": info["cupom"], "loja": loja,
                "link": info["link"], "url": url, "data": post["data"],
                "canais": [canal], "posts": [post["post"]],
            })
            adicionadas += 1
        vistos.add(post["post"])

    ofertas.sort(key=lambda o: o["data"])
    dados["atualizado"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return adicionadas


def main():
    canais = [l.strip() for l in ARQ_CANAIS.read_text(encoding="utf-8").splitlines()
              if l.strip() and not l.strip().startswith("#")]
    dados = carregar()
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
    adicionadas = juntar(dados, novos)
    ARQ_DADOS.parent.mkdir(exist_ok=True)
    ARQ_DADOS.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{adicionadas} ofertas novas, {len(dados['ofertas'])} no total")


if __name__ == "__main__":
    main()
