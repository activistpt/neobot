#!/usr/bin/env python3
"""
Extrai as webcams do webcamtaxi.com para webcams.json.

Uso: python gerar_webcams.py [saida.json]

Para cada categoria (do menu do site), percorre as paginas de listagem
e recolhe as webcams (paginas individuais) com titulo, pais (do URL),
categoria e thumb. Cada pagina de webcam embute um stream live do
YouTube (iframe youtube.com/embed/<ID>) — o video e visto clicando no
link (preview do Telegram reproduz o live).

Saida: webcams.json
{
  "categorias": {"beach": {"nome": "Praias", "webcams": [...]}, ...},
}
cada webcam: {"titulo", "url", "yt", "pais", "thumb"}
"""

import json
import re
import sys
import time
import urllib.request

BASE = "https://www.webcamtaxi.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# Categoria -> rotulo em PT (slugs validados no menu do site)
CATEGORIAS = {
    "beach": "Praias",
    "sea": "Mar",
    "ocean": "Oceano",
    "island": "Ilhas",
    "lake": "Lagos",
    "river": "Rios",
    "surf": "Surf",
    "nature": "Natureza",
    "wildlife": "Vida Selvagem",
    "zoo": "Zoos",
    "aquarium": "Aquarios",
    "fishing": "Pesca",
    "boats": "Barcos",
    "marina": "Marinas",
    "harbour": "Portos",
    "cruise-ships": "Cruzeiros",
    "bridge": "Pontes",
    "lighthouse": "Farois",
    "city": "Cidades",
    "square": "Pracas",
    "traffic": "Trafego",
    "airport": "Aeroportos",
    "trains": "Comboios",
    "ski": "Neve e Esqui",
    "volcanoes": "Vulcoes",
    "space": "Espaco",
    "astronomy": "Astronomia",
    "storm-watch": "Tempestades",
    "earthquakes": "Terramotos",
    "monument": "Monumentos",
    "restaurant": "Restaurantes",
    "bar": "Bares",
    "pool": "Piscinas",
    "hotels-resorts": "Hoteis e Resorts",
    "sports": "Desporto",
    "most-viewed-cams": "Populares",
    "latest-webcams": "Recentes",
    "live-events-worldwide": "Eventos ao Vivo",
    "world-news-weather": "Meteo Mundial",
}

# Paginas que nao sao webcams
IGNORAR = re.compile(
    r"/en/(about-us|advertise|contacts|privacy|terms|map|webcams|4k-cameras|"
    r"artificial-intelligence|airport-tranfers-worldwide|[a-z-]*-live-webcams)\.html$"
)


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def links_de_categoria(categoria: str) -> list[dict]:
    """Percorre a categoria e as suas paginas (?start=N, Joomla) deduplicando."""
    html = _get(f"{BASE}/en/{categoria}.html")
    encontrados: dict[str, dict] = {}
    paginas = sorted(set(int(n) for n in re.findall(
        rf"{re.escape(categoria)}\.html\?start=(\d+)", html
    )))
    for start in [0] + paginas:
        if start > 0:
            try:
                html = _get(f"{BASE}/en/{categoria}.html?start={start}")
            except Exception:
                break
        antes = len(encontrados)
        for url, info in extrair_da_pagina(html, categoria).items():
            encontrados.setdefault(url, info)
        if len(encontrados) == antes and start > 0:
            break  # pagina vazia/repetida: fim
        time.sleep(0.2)
    return list(encontrados.values())


def extrair_da_pagina(html: str, categoria: str) -> dict[str, dict]:
    """Extrai webcams (paginas de pelo menos 2 niveis) do HTML de listagem."""
    resultado: dict[str, dict] = {}
    for m in re.finditer(
        r'<a[^>]+href=(?:"([^"]+)"|([^\s>]+))[^>]*>(.*?)</a>', html, re.S
    ):
        href, _alt, titulo = m.group(1), m.group(2), m.group(3)
        caminho = (href or _alt or "").rstrip('"\'').strip()
        if caminho.startswith("/"):
            caminho = BASE + caminho
        if not caminho.startswith(BASE):
            continue
        caminho = caminho[len(BASE):]
        if not caminho.endswith(".html") or caminho == f"/en/{categoria}.html":
            continue
        if IGNORAR.match(caminho):
            continue
        # webcams tem sempre 4 niveis: /en/pais/cidade/cam.html
        # (3 niveis = paginas de cidade/regiao, nao sao webcams)
        partes = caminho.strip("/").split("/")
        if len(partes) != 4 or partes[0] != "en":
            continue
        if caminho in resultado:
            continue
        texto = re.sub(r"<[^>]+>", " ", titulo or "")
        texto = re.sub(r"\s+", " ", texto).strip()
        resultado[caminho] = {
            "titulo": texto or caminho.rsplit("/", 1)[-1].replace(".html", "").replace("-", " ").title(),
            "url": BASE + caminho,
            "pais": partes[1].replace("-", " ").title() if len(partes) >= 2 else "",
            "cidade": partes[2].replace("-", " ").title() if len(partes) >= 3 else "",
            "thumb": "",
        }
    # thumbs: <a href=/en/p/c/cam.html> <img ... data-src=/images/template/thumbs/x.jpg>
    for cam, thumb in re.findall(
        r'href=(/en/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+\.html)>\s*<img[^>]*?data-src=([^ >]+?\.jpg)',
        html,
    ):
        if cam in resultado and not resultado[cam]["thumb"]:
            resultado[cam]["thumb"] = BASE + thumb
    return resultado


def main() -> None:
    saida = sys.argv[1] if len(sys.argv) > 1 else "webcams.json"
    dados: dict[str, dict] = {}
    total = 0
    for slug, nome in CATEGORIAS.items():
        try:
            webcams = links_de_categoria(slug)
        except Exception as exc:
            print(f"  !! {slug}: {exc}")
            webcams = []
        if not webcams:
            print(f"  - {nome} ({slug}): vazia, ignorada")
            continue
        dados[slug] = {"nome": nome, "webcams": webcams}
        total += len(webcams)
        print(f"  {nome} ({slug}): {len(webcams)} webcams")
    with open(saida, "w", encoding="utf-8") as fh:
        json.dump(dados, fh, ensure_ascii=False, indent=1)
    print(f"\nTotal: {total} webcams em {len(dados)} categorias -> {saida}")


if __name__ == "__main__":
    main()
