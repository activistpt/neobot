#!/usr/bin/env python3
"""
Gera o rebel.m3u a partir do canais.json do projeto Rebel Pirate TV
(rebel-pirate-tv.vercel.app — 700 e tal canais).

Uso: python3 gerar_rebel_m3u.py [pasta-do-projeto] [saida.m3u]
Por omissão procura o projeto no Desktop e escreve rebel.m3u ao lado deste script.

Regras de conversão:
- Só entram URLs jogáveis: .m3u8/.mpd diretos e /api/proxy?url=... (decodificados)
- Páginas .php (SportOnline etc.), RTP Play, dailymotion e outros sites vão
  para o campo "web:" da linha EXTINF (o bot mostra o link como página)
"""

import json
import os
import sys
import urllib.parse

# Nomes de categoria em PT -> etiqueta do grupo no M3U
GRUPOS = {
    0: "Todos",
    1: "Desportos",
    2: "Infantil",
    3: "Documentários",
    4: "Filmes e Séries",
    5: "Notícias",
    6: "Abertos",
    7: "Variedades",
    8: "A Casa do Patrão",
    9: "Portugal",
    10: "Brasil",
    11: "Música",
    12: "Internacional",
    13: "Culinária",
    14: "Desporto Motor",
    15: "Humor",
    16: "SportOnline",
    17: "Desporto",
}

DEFAULT_PROJECT = os.path.join(
    os.path.expanduser("~"), "Desktop", "rebel-pirate-tv", "rebel-pirate-tv-clone-main"
)


def _decodificar(url: str) -> str | None:
    """Devolve o URL real por trás de /api/proxy?url=...; None se não for jogável."""
    if url.startswith("/api/proxy?url="):
        alvo = urllib.parse.parse_qs(url).get("url", [""])[0]
        return alvo or None
    return url


def _limpar(url: str) -> str:
    """Remove barras duplicadas acidentais (http://x//y -> http://x/y)."""
    if "://" in url:
        esquema, _, resto = url.partition("://")
        return esquema + "://" + resto.replace("//", "/")
    return url


def carregar_canais(pasta_projeto: str) -> list[dict]:
    with open(os.path.join(pasta_projeto, "canais.json"), encoding="utf-8") as fh:
        dados = json.load(fh)
    return dados.get("channels", [])


def construir_m3u(canais: list[dict]) -> str:
    linhas = ["#EXTM3U"]
    n_stream = n_web = 0
    for ch in canais:
        nome = (ch.get("name") or "").strip()
        url = (ch.get("url") or "").strip()
        if not nome or not url:
            continue
        logo = (ch.get("image") or "").strip()
        cats = ch.get("categories") or []
        grupo = GRUPOS.get(cats[0], "Variedades") if cats else "Variedades"

        jogavel = _decodificar(url)
        if jogavel and (".m3u8" in jogavel or ".mpd" in jogavel):
            attrs = f'tvg-name="{nome}"'
            if logo:
                attrs += f' tvg-logo="{logo}"'
            linhas.append(f'#EXTINF:-1 {attrs} group-title="{grupo}",{nome}')
            linhas.append(_limpar(jogavel))
            n_stream += 1
        else:
            # Entrada web: mantém no M3U como meta, sem URL de stream
            attrs = f'tvg-name="{nome}"'
            if logo:
                attrs += f' tvg-logo="{logo}"'
            linhas.append(f'#EXTINF:-1 {attrs} group-title="{grupo} · web",{nome}')
            linhas.append(f"#WEB {url}")
            n_web += 1
    print(f"gerar_rebel_m3u: {n_stream} streams + {n_web} web = {n_stream + n_web} entradas")
    return "\n".join(linhas) + "\n"


def main() -> None:
    pasta = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROJECT
    saida = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "rebel.m3u")
    if not os.path.isfile(os.path.join(pasta, "canais.json")):
        print(f"ERRO: canais.json não encontrado em {pasta}", file=sys.stderr)
        raise SystemExit(1)
    canais = carregar_canais(pasta)
    with open(saida, "w", encoding="utf-8") as fh:
        fh.write(construir_m3u(canais))
    print(f"escrito: {saida}")


if __name__ == "__main__":
    main()
