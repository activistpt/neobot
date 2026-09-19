"""
NEOBOT — Módulo IPTV (playlist RKDY)
Descarrega o M3U com um token de utilizador, faz parse dos canais,
mantém cache com expiração e valida streams individuais.

O worker (Cloudflare Pages) só entrega o M3U a players IPTV — a pedidos de
browser responde com redirect para o canal Telegram. Por isso usamos um
User-Agent de player.

O token expira a cada 7 dias (renovar via bot do canal @rkdyhelp1_bot).
Pode ser definido por ordem de prioridade: .env (NEOBOT_IPTV_TOKEN) >
ficheiro iptv_token.txt > constante abaixo.
"""

import asyncio
import base64
import json
import logging
import os
import re
import time

import httpx

logger = logging.getLogger("neobot.iptv")

# --- Configuração -----------------------------------------------------------

BASE = "https://playlistgen-rkdyiptv.pages.dev/api/rkdyiptv/playlist.m3u"

# Token de referência (pessoal, expira a cada 7 dias).
IPTV_TOKEN = "ae9561fa5f62af7c5a8e02df10221570e1abb886bf4d2709"

_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iptv_token.txt")

# O worker bloqueia browsers; players passam.
UA_PLAYER = "VLC/3.0.20 LibVLC/3.0.20"

# Descarregar a playlist no máximo uma vez por hora (o M3U pede refresh=1380s).
CACHE_TTL = 3600

# Validar stream = GET de 2 KB; se não responder dentro deste tempo, morto.
STREAM_TIMEOUT = 10.0

# Para não martelar o servidor final com milhares de pedidos de validação.
MAX_CONCURRENT_CHECKS = 12

# Metadados no parâmetro d= (base64url -> JSON: i=canal, p=hash token,
# e=expiração ms, s=sessão, h=hash canal).
_RE_D = re.compile(r"[?&]d=([A-Za-z0-9_-]+)")


def _token_de_ficheiro() -> str:
    """Token guardado manualmente em iptv_token.txt (1.ª linha não vazia)."""
    try:
        with open(_TOKEN_FILE, encoding="utf-8") as fh:
            for linha in fh:
                linha = linha.strip()
                if linha and not linha.startswith("#"):
                    return linha
    except OSError:
        pass
    return ""


def _attr(attrs: str, nome: str) -> str:
    """Valor de um atributo #EXTINF (tvg-id="..." ou group-title="...")."""
    m = re.search(rf'{nome}="([^"]*)"', attrs)
    if m:
        return m.group(1).strip()
    m = re.search(rf"{nome}=(\S+)", attrs)
    return m.group(1).strip() if m else ""


class Canal:
    """Um canal do M3U."""

    __slots__ = ("nome", "grupo", "tvg_id", "tvg_name", "logo", "url", "url_direto", "web")

    def __init__(
        self,
        nome: str,
        grupo: str,
        tvg_id: str,
        tvg_name: str,
        logo: str,
        url: str,
        web: str | None = None,
    ):
        self.nome = nome
        self.grupo = grupo
        self.tvg_id = tvg_id
        self.tvg_name = tvg_name
        self.logo = logo
        self.url = url
        self.url_direto: str | None = None  # preenchido por resolver()
        self.web = web  # página web do canal (Rebel Pirate TV), se existir


class TokenExpirado(Exception):
    """A playlist veio vazia / não-M3U — tipicamente token expirado ou revogado."""


class Playlist:
    """Playlist M3U em cache com expiração."""

    def __init__(self, token: str | None = None):
        # Lido em runtime (depois do _load_env do bot) para respeitar o .env.
        self.token = (
            token
            or os.environ.get("NEOBOT_IPTV_TOKEN", "").strip()
            or _token_de_ficheiro()
            or IPTV_TOKEN
        )
        self._canais: list[Canal] | None = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()
        # Segunda fonte local: lista do Rebel Pirate TV (gerada por gerar_rebel_m3u.py)
        self._ficheiro_local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rebel.m3u")
        self._rebel: list[Canal] | None = None

    # -- download + parse ----------------------------------------------------

    async def _fetch(self) -> list[Canal]:
        url = f"{BASE}?token={self.token}"
        async with httpx.AsyncClient(
            timeout=60, headers={"User-Agent": UA_PLAYER}, follow_redirects=True
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            texto = resp.text
        canais = self._parse(texto)
        if not canais:
            raise TokenExpirado(
                "Playlist vazia ou inválida — o token expirou/foi revogado. "
                "Renova em @rkdyhelp1_bot e atualiza o .env (NEOBOT_IPTV_TOKEN) ou iptv_token.txt."
            )
        logger.info("IPTV: %s canais descarregados", len(canais))
        return canais

    def _parse(self, texto: str) -> list[Canal]:
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
                    )
                )
                atual = None

        for raw in texto.splitlines():
            linha = raw.strip()
            if not linha:
                continue
            if linha.startswith("#EXTINF"):
                _flush()  # entrada anterior sem URL = canal só-web (Rebel)
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

    # -- API pública ----------------------------------------------------------

    def _carregar_local(self) -> list[Canal]:
        """Canais do rebel.m3u (lista do Rebel Pirate TV), carregados uma vez."""
        if self._rebel is None:
            try:
                with open(self._ficheiro_local, encoding="utf-8") as fh:
                    self._rebel = self._parse(fh.read())
                logger.info("IPTV: %s canais locais (Rebel) carregados", len(self._rebel))
            except OSError:
                self._rebel = []
        return self._rebel

    async def canais(self) -> list[Canal]:
        """Canais RKDY + Rebel (local), com cache de 1 hora.

        Se a playlist remota falhar (token expirado, rede), os canais locais
        do Rebel continuam disponíveis — o bot nunca fica sem lista.
        """
        async with self._lock:
            agora = time.monotonic()
            if self._canais is not None and (agora - self._fetched_at) < CACHE_TTL:
                return self._canais
            try:
                remotos = await self._fetch()
            except Exception as e:
                logger.warning("IPTV: remota falhou (%s); a usar só a lista local", e)
                remotos = []
            locais = self._carregar_local()
            self._canais = remotos + locais
            self._fetched_at = agora
            if not self._canais:
                raise TokenExpirado(
                    "Playlist vazia ou inválida — o token expirou/foi revogado. "
                    "Renova em @rkdyhelp1_bot e atualiza o .env (NEOBOT_IPTV_TOKEN) ou iptv_token.txt."
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
        """Diagnóstico: nº canais/categorias e expiração do token (codificada nos links)."""
        canais = await self.canais()
        expira_ms: int | None = None
        n_rkdy = n_rebel = 0
        for c in canais:
            if "action=stream" in c.url:
                n_rkdy += 1
                if expira_ms is None:
                    m = _RE_D.search(c.url)
                    if m:
                        try:
                            b64 = m.group(1) + "=" * (-len(m.group(1)) % 4)
                            info = json.loads(base64.urlsafe_b64decode(b64))
                            expira_ms = int(info.get("e", 0)) or None
                        except Exception:
                            pass
            else:
                n_rebel += 1
        return {
            "canais": len(canais),
            "rkdy": n_rkdy,
            "rebel": n_rebel,
            "grupos": len(await self.grupos()),
            "token_expira_ms": expira_ms,
            "token_valido": bool(expira_ms and expira_ms > time.time() * 1000),
        }

    async def validar_stream(self, url: str) -> bool:
        """Segue o redirect do link action=stream e confirma resposta 2xx."""
        try:
            async with httpx.AsyncClient(
                timeout=STREAM_TIMEOUT, headers={"User-Agent": UA_PLAYER}, follow_redirects=True
            ) as client:
                resp = await client.get(url, headers={"Range": "bytes=0-2047"})
                return resp.status_code in (200, 206)
        except Exception:
            return False

    async def resolver(self, url: str) -> str | None:
        """Resolve um link action=stream para o URL direto da fonte original.

        O worker responde 302 para o stream real; esse destino final não tem a
        restrição "só funciona em apps IPTV" — abre em qualquer player/browser.
        O token do redirect é por-pedido, por isso resolver fresco a cada uso.
        """
        if not url or "action=stream" not in url:
            return None
        try:
            async with httpx.AsyncClient(
                timeout=STREAM_TIMEOUT, headers={"User-Agent": UA_PLAYER}, follow_redirects=False
            ) as client:
                resp = await client.get(url)
                loc = resp.headers.get("location", "").strip()
                return loc or None
        except Exception:
            return None

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
