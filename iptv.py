"""
NEOBOT — Módulo IPTV (fontes por página web)

O utilizador confirmou que só as fontes com link por página web funcionam
bem (tipo SportOnline). Por isso este módulo agrega apenas:

1. Rebel Pirate TV (rebel.m3u local, gerado de canais.json) — streams
   diretos .m3u8 + páginas web marcadas #WEB
2. SportOnline (25 canais desportivos PT/BR/mundiais por página web,
   com iframe embutido que funciona em browser)
3. TV Garden (IPTV mundial por país — página web com player embutido)

Não há tokens nem links que expiram: tudo é clicável e estável.
"""

import asyncio
import logging
import os
import time

import httpx

logger = logging.getLogger("neobot.iptv")

# --- Configuração -----------------------------------------------------------

# Segunda fonte local: lista do Rebel Pirate TV (gerada por gerar_rebel_m3u.py)
_FICHEIRO_REBEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rebel.m3u")

# Base do SportOnline (páginas de canal com iframe; funciona em browser)
SZO_BASE = "https://sportzonline.click/channels"

# TV Garden: IPTV mundial por página de país (player web embutido)
TV_GARDEN_BASE = "https://tv.garden"

# Descarregar a playlist na verificação de validade no máximo uma vez por hora.
CACHE_TTL = 3600

# Validar stream = GET de 2 KB; se não responder dentro deste tempo, morto.
STREAM_TIMEOUT = 10.0

# Para não martelar os servidores com milhares de pedidos de validação.
MAX_CONCURRENT_CHECKS = 12

# Canais desportivos do SportOnline (pt/br/hd — verificados no site)
SZO_CANAIS = [
    ("SPORT TV 1", "pt/sporttv1.php"),
    ("SPORT TV 2", "pt/sporttv2.php"),
    ("SPORT TV 3", "pt/sporttv3.php"),
    ("SPORT TV 4", "pt/sporttv4.php"),
    ("SPORT TV 5", "pt/sporttv5.php"),
    ("ELEVEN SPORTS 1", "pt/eleven1.php"),
    ("ELEVEN SPORTS 2", "pt/eleven2.php"),
    ("ELEVEN SPORTS 3", "pt/eleven3.php"),
    ("Brasil HD 1", "bra/br1.php"),
    ("Brasil HD 2", "bra/br2.php"),
    ("Brasil HD 3", "bra/br3.php"),
    ("Brasil HD 4", "bra/br4.php"),
    ("Brasil HD 5", "bra/br5.php"),
    ("Brasil HD 6", "bra/br6.php"),
    ("HD Mundial 1", "hd/hd1.php"),
    ("HD Mundial 2", "hd/hd2.php"),
    ("HD Mundial 3", "hd/hd3.php"),
    ("HD Mundial 4", "hd/hd4.php"),
    ("HD Mundial 5", "hd/hd5.php"),
    ("HD Mundial 6", "hd/hd6.php"),
    ("HD Mundial 7", "hd/hd7.php"),
    ("HD Mundial 8", "hd/hd8.php"),
    ("HD Mundial 9", "hd/hd9.php"),
    ("HD Mundial 10", "hd/hd10.php"),
    ("HD Mundial 11", "hd/hd11.php"),
]

# Países do TV Garden (IPTV mundial por página web)
TV_GARDEN_PAISES = [
    ("pt", "Portugal"),
    ("br", "Brasil"),
    ("gb", "Reino Unido"),
    ("us", "EUA"),
    ("fr", "França"),
    ("de", "Alemanha"),
    ("it", "Itália"),
    ("es", "Espanha"),
    ("nl", "Holanda"),
    ("ar", "Argentina"),
    ("mx", "México"),
    ("jp", "Japão"),
    ("in", "Índia"),
    ("ru", "Rússia"),
    ("tr", "Turquia"),
    ("pl", "Polónia"),
    ("se", "Suécia"),
    ("ca", "Canadá"),
    ("au", "Austrália"),
    ("cn", "China"),
]


def _attr(attrs: str, nome: str) -> str:
    """Valor de um atributo #EXTINF (tvg-id="..." ou group-title="...")."""
    import re

    m = re.search(rf'{nome}="([^"]*)"', attrs)
    if m:
        return m.group(1).strip()
    m = re.search(rf"{nome}=(\S+)", attrs)
    return m.group(1).strip() if m else ""


class Canal:
    """Uma entrada da lista: stream direto (.m3u8) e/ou página web."""

    __slots__ = ("nome", "grupo", "tvg_id", "tvg_name", "logo", "url", "web", "fonte")

    def __init__(
        self,
        nome: str,
        grupo: str,
        tvg_id: str = "",
        tvg_name: str = "",
        logo: str = "",
        url: str = "",
        web: str | None = None,
        fonte: str = "rebel",
    ):
        self.nome = nome
        self.grupo = grupo
        self.tvg_id = tvg_id
        self.tvg_name = tvg_name
        self.logo = logo
        self.url = url  # stream direto (pode ser vazio em entradas só-web)
        self.web = web  # página web do canal
        self.fonte = fonte  # rebel | web


class PlaylistVazia(Exception):
    """Nenhuma fonte disponível (rebel.m3u ausente, por exemplo)."""


class Playlist:
    """Agregador de fontes por página web, com cache em memória."""

    def __init__(self):
        self._canais: list[Canal] | None = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    # -- parse do M3U (Rebel) -------------------------------------------------

    def _parse(self, texto: str, fonte: str = "rebel") -> list[Canal]:
        canais: list[Canal] = []
        atual: dict | None = None

        def _flush() -> None:
            nonlocal atual
            if atual is not None:
                canais.append(
                    Canal(
                        nome=atual["nome"],
                        grupo=atual["grupo"],
                        tvg_id=atual["tvg_id"],
                        tvg_name=atual["tvg_name"],
                        logo=atual["logo"],
                        url=atual.get("url", ""),
                        web=atual.get("web"),
                        fonte=fonte,
                    )
                )
                atual = None

        for raw in texto.splitlines():
            linha = raw.strip()
            if not linha:
                continue
            if linha.startswith("#EXTINF"):
                _flush()  # entrada anterior sem URL = canal só-web
                attrs, _, display = linha.partition(",")
                atual = {
                    "nome": display.strip(),
                    "grupo": _attr(attrs, "group-title"),
                    "tvg_id": _attr(attrs, "tvg-id"),
                    "tvg_name": _attr(attrs, "tvg-name"),
                    "logo": _attr(attrs, "tvg-logo"),
                    "web": None,
                }
            elif linha.startswith("#WEB ") and atual is not None:
                atual["web"] = linha[5:].strip()
            elif not linha.startswith("#") and atual is not None:
                atual["url"] = linha
                _flush()
        _flush()
        return canais

    # -- fontes ----------------------------------------------------------------

    def _carregar_rebel(self) -> list[Canal]:
        try:
            with open(_FICHEIRO_REBEL, encoding="utf-8") as fh:
                dados = self._parse(fh.read(), fonte="rebel")
            logger.info("IPTV: %s canais locais (Rebel) carregados", len(dados))
            return dados
        except OSError:
            logger.warning("IPTV: rebel.m3u não encontrado em %s", _FICHEIRO_REBEL)
            return []

    def _fontes_web(self) -> list[Canal]:
        """Entradas web estáticas: SportOnline + TV Garden (verificadas a funcionar)."""
        saida: list[Canal] = []
        for nome, caminho in SZO_CANAIS:
            saida.append(
                Canal(
                    nome=nome,
                    grupo="WEB | SportOnline",
                    url="",
                    web=f"{SZO_BASE}/{caminho}",
                    fonte="web",
                )
            )
        for codigo, pais in TV_GARDEN_PAISES:
            saida.append(
                Canal(
                    nome=f"TV Garden — {pais}",
                    grupo="WEB | TV Garden",
                    url="",
                    web=f"{TV_GARDEN_BASE}/{codigo}",
                    fonte="web",
                )
            )
        return saida

    # -- API pública ------------------------------------------------------------

    async def canais(self) -> list[Canal]:
        """Rebel + fontes web, com cache de 1 hora em memória."""
        async with self._lock:
            agora = time.monotonic()
            if self._canais is not None and (agora - self._fetched_at) < CACHE_TTL:
                return self._canais
            self._canais = self._fontes_web() + self._carregar_rebel()
            self._fetched_at = agora
            if not self._canais:
                raise PlaylistVazia(
                    "Nenhuma fonte disponível — falta o rebel.m3u. "
                    "Gera-o com: python neobot/gerar_rebel_m3u.py"
                )
            return self._canais

    async def grupos(self) -> list[tuple[str, int]]:
        """Lista de (categoria, nº de canais), ordenada alfabeticamente."""
        contagem: dict[str, int] = {}
        for c in await self.canais():
            g = c.grupo or "SEM GRUPO"
            contagem[g] = contagem.get(g, 0) + 1
        return sorted(contagem.items())

    async def procurar(self, termo: str, limite: int = 25) -> list[Canal]:
        """Procura canais por nome (case-insensitive)."""
        termo_l = termo.lower().strip()
        if not termo_l:
            return []
        return [c for c in await self.canais() if termo_l in c.nome.lower()][:limite]

    async def do_grupo(self, grupo: str, limite: int = 60) -> list[Canal]:
        """Canais de uma categoria específica."""
        return [c for c in await self.canais() if c.grupo == grupo][:limite]

    async def estado(self) -> dict:
        """Diagnóstico: nº de canais por tipo/fonte e nº de categorias."""
        canais = await self.canais()
        n_stream = sum(1 for c in canais if c.url)
        n_web = sum(1 for c in canais if not c.url and c.web)
        return {
            "canais": len(canais),
            "streams": n_stream,
            "web": n_web,
            "szo": sum(1 for c in canais if c.grupo == "WEB | SportOnline"),
            "garden": sum(1 for c in canais if c.grupo == "WEB | TV Garden"),
            "grupos": len(await self.grupos()),
        }

    async def validar_stream(self, url: str) -> bool:
        """Valida um stream direto: GET de 2 KB tem de responder 2xx."""
        try:
            async with httpx.AsyncClient(
                timeout=STREAM_TIMEOUT, follow_redirects=True
            ) as client:
                resp = await client.get(url, headers={"Range": "bytes=0-2047"})
                return resp.status_code in (200, 206)
        except Exception:
            return False

    async def validar_grupo(self, grupo: str, limite: int = 30) -> list[tuple[Canal, bool]]:
        """Valida até `limite` streams de uma categoria em paralelo (só canais com stream)."""
        canais = [c for c in await self.do_grupo(grupo, limite=limite * 2) if c.url][:limite]
        sem = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

        async def _um(c: Canal) -> tuple[Canal, bool]:
            async with sem:
                return (c, await self.validar_stream(c.url))

        return list(await asyncio.gather(*[_um(c) for c in canais]))


# --- Singleton ---------------------------------------------------------------

_playlist: Playlist | None = None


def get_playlist() -> Playlist:
    global _playlist
    if _playlist is None:
        _playlist = Playlist()
    return _playlist
