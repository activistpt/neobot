"""
NEOBOT — Assistente multiusos para Telegram
Comandos: /start, /ajuda, /hora, /data, /piada, /dado, /moeda, /escolhe, /ask, /google, /news
"""

import asyncio
import atexit
import html
import json
import ipaddress
import logging
import os
import random
import re
import socket
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

import httpx

try:  # optional dependency for /phone
    import phonenumbers
    from phonenumbers import carrier as _pn_carrier
    from phonenumbers import geocoder as _pn_geocoder
    from phonenumbers import timezone as _pn_timezone
except ImportError:
    phonenumbers = None
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


def _load_env() -> None:
    """Minimal .env loader so NEOBOT_TOKEN can live in a file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("neobot")

# --- single-instance lock ---
_INSTANCE_LOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".neobot.lock")
_lock_fd = None


def _acquire_instance_lock() -> None:
    """O_EXCL lock so a second `py bot.py` fails fast instead of Telegram 409-ing."""
    global _lock_fd
    try:
        _lock_fd = os.open(_INSTANCE_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(_lock_fd, str(os.getpid()).encode())
        atexit.register(_release_instance_lock)
    except FileExistsError:
        try:
            with open(_INSTANCE_LOCK, encoding="utf-8") as fh:
                other = fh.read().strip()
        except OSError:
            other = "?"
        logger.error("NEOBOT já está a correr (PID %s). Se for um processo morto, apague %s", other, _INSTANCE_LOCK)
        raise SystemExit(1)


def _release_instance_lock() -> None:
    global _lock_fd
    if _lock_fd is not None:
        try:
            os.close(_lock_fd)
        except OSError:
            pass
        try:
            os.remove(_INSTANCE_LOCK)
        except OSError:
            pass
        _lock_fd = None


# --- Groq configuration (shared by /ask, /google and /news) ---
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_HEADERS = {
    "Content-Type": "application/json",
    # Groq's Cloudflare blocks Python's default UA (error 1010); curl's UA passes.
    "User-Agent": "curl/8.9.1",
}
GROQ_SYSTEM_PROMPT = os.environ.get(
    "GROQ_SYSTEM_PROMPT",
    "You are NEOBOT, a friendly assistant inside a Telegram group. "
    "Answer questions concisely (default under 120 words unless the user asks for detail). "
    "Use plain text only — no markdown, no HTML tags like <b> or **bold**. "
    "If you are unsure, say so honestly. Reply in the language the question was asked in.",
)
GOOGLE_SYSTEM_PROMPT = os.environ.get(
    "GOOGLE_SYSTEM_PROMPT",
    "You are NEOBOT's web-research mode. You are given web search results. "
    "Answer the user's question using the results, citing sources as bare URLs in parentheses. "
    "Be concise (under 150 words unless asked for detail). Plain text only — no markdown, no HTML. "
    "If the results do not answer the question, say so. Reply in the language of the question.",
)
NEWS_SYSTEM_PROMPT = os.environ.get(
    "NEWS_SYSTEM_PROMPT",
    "You are NEOBOT's news summarizer. You are given recent web search results about a topic. "
    "Summarize the latest news in up to 6 short bullet points, each starting with '• '. "
    "Include dates when available. Plain text only — no markdown, no HTML. "
    "Reply in the language of the topic (default: Portuguese of Portugal).",
)
# Simple per-user rate limit shared by /ask, /google and /news: max 5 per 60 seconds.
ASK_RATE_LIMIT = int(os.environ.get("ASK_RATE_LIMIT", "5"))
ASK_RATE_WINDOW = int(os.environ.get("ASK_RATE_WINDOW", "60"))
_ask_history: dict[int, list[float]] = {}


def _groq_key() -> str:
    return os.environ.get("GROQ_API_KEY", "").strip()


async def _groq_chat(model: str, system: str, user: str, max_tokens: int = 700) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.6,
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {_groq_key()}", **GROQ_HEADERS}
    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.post(GROQ_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


PIADAS = [
    # --- filosofia + informática ---
    "Descartes entra num café. O empregado pergunta: — Um café? Ele responde: — Não penso que sim... e desaparece. 💭",
    "Sócrates era o pior cliente de suporte: a cada resposta dizia \"mas como sabes tu isso?\" — e no fim o técnico deixou de saber se o rato estava ligado. 🏛️🖱️",
    "Gödel programou um sistema capaz de provar que ele próprio tinha bugs. Chamaram-lhe Windows. 🐛",
    "Turing e Gödel entram num bar. Um diz: — Este bar não consegue provar a própria consistência. O outro bebe mesmo assim. 🍺",
    "— Quantos filósofos são precisos para trocar uma lâmpada? — Nenhum. Primeiro têm de provar que a lâmpada existe. 💡",
    "A 1.ª lei da informática filosófica: se um programa funciona, ninguém sabe porquê. Se não funciona, também ninguém sabe porquê. 🤔",
    "Se uma árvore cai na floresta e não há ninguém por perto, faz som? Se um servidor cai às 3h da manhã e não tem monitorização, deu downtime? 🌲🖥️",
    "O gato de Schrödinger está na pasta Downloads: está e não está infetado até abrires o zip. 🐱📦",
    "Zenão do Eletó conheceu a rede da empresa: a ligação mostrava 100%, mas o ecrã nunca chegava a abrir. 🏹",
    "O mantra do programador: \"funciona na minha máquina\". O mantra do filósofo: \"a minha máquina funciona?\". 🖥️",
    "Porque é que os filósofos fazem péssimos sysadmins? Porque respondem a todos os tickets com \"depende\". 🎫",
    "Há 10 tipos de pessoas no mundo: as que entendem binário e as que ainda decidem se o copo está meio cheio ou meio vazio. 🥛",
    "Em que é que um packet suspenso pensa quando atravessa o router? Nada — só reencaminha o seu vazio existencial. 📡",
    "— O que é a consciência? — perguntou o aluno. — Um processo em execução sem logs de origem — respondeu o root. 🌳",
    "Porque é que a Cloud engana os filósofos? Porque promete existir em todo o lado sem estar em lado nenhum. ☁️",
    "O platonismo aplicado às bases de dados: os teus dados são apenas sombras imperfeitas do modelo relacional perfeito. 🗿",
    "— Dizes que a realidade é uma simulação. Prova. — 403 Forbidden. 🚫",
    # --- hackers / piratas informáticos ---
    "— Quantos hackers são precisos para trocar uma lâmpada? — Nenhum. Entram pela rede elétrica e trocam-na remotamente. 💡🔓",
    "Um hacker entra num bar, ignora o firewall, e sai com a base de dados das bebidas. 🍸🏴‍☠️",
    "O que é um pirata informático existencial? Alguém que crackeia um programa e depois passa horas a perguntar-se: \"mas afinal, o que é a propriedade?\" 🏴‍☠️💭",
    "O firewall disse ao hacker: — Tu não vais passar! O hacker respondeu: — Já passei. Isso foi a tua memória cache a reproduzir o passado. 🧱",
    "Porque é que os hackers adoram Sócrates? Porque \"só sei que nada sei\" é a melhor desculpa depois de apagarem o disco errado. 💾",
    "Em Matrix ninguém sabe se a pílula é vermelha ou azul. Na informática ninguém sabe se é problema do router ou do utilizador. Spoiler: é sempre do utilizador. 🕶️🔌",
    "O responsável de segurança pergunta ao interno: — Qual é a palavra-passe mais forte? O interno: — \"errado\", porque ninguém nunca a escreve. 🔐",
    "Um hacker filósofo deixou de fazer phishing: chegou à conclusão de que já ninguém sabe o que é verdade nem no inbox. 🎣",
    "— Quem vigia os vigilantes? — O antivírus. E quem vigia o antivírus? — As atualizações. E quem vigia as atualizações? — Ninguém, é aí que começa tudo. 👀",
]


def teclado_menu():
    """Retorna o texto do menu principal."""
    return (
        "🤖 *NEOBOT* — o teu assistente!\n\n"
        "Comandos disponíveis:\n"
        "▪️ /hora — que horas são\n"
        "▪️ /data — que dia é hoje\n"
        "▪️ /piada — piadas filosóficas, de informática e de hackers 🏴‍☠️\n"
        "▪️ /dado — lança um dado (1-6)\n"
        "▪️ /moeda — cara ou coroa\n"
        "▪️ /escolhe — escolhe por ti (ex: `/escolhe pizza sushi burger`)\n"
        "▪️ /ask — pergunta algo à IA (ex: `/ask quem escreveu Dom Casmurro?`)\n"
        "▪️ /google — pesquisa na internet (ex: `/google últimas notícias do GPL`)\n"
        "▪️ /news — notícias da atualidade (ex: `/news tecnologia`)\n"
        "▪️ /wiki — pesquisa na Wikipédia (ex: `/wiki Portugal`)\n"
        "▪️ /image — gera uma imagem (ex: `/image gato astronauta`)\n"
        "▪️ /audio — gera um clipe de voz com a frase que você escrever (ex: `/audio bem-vindos ao grupo`)\n"
        "▪️ /meteo — meteorologia (ex: `/meteo Lisboa`)\n"
        "▪️ /youtube — pesquisa no YouTube (ex: `/youtube tutorial python`)\n"
        "▪️ /crypto — preços de crypto (ex: `/crypto btc`)\n"
        "▪️ /cinema — filmes em cartaz\n"
        "▪️ /estreias — estreias da semana\n"
        "▪️ /imdb — info de filmes (ex: `/imdb Matrix`)\n"
        "▪️ /play — link de streaming a partir de um URL do IMDb\n"
        "▪️ /ipinfo — info de um IP/domínio\n"
        "▪️ /ipscan — portas abertas de um IP\n"
        "▪️ /iplookup — DNS inverso de um IP\n"
        "▪️ /phone — info de um número de telefone\n"
        "▪️ /torrent — procura torrents (ex: `/torrent ubuntu 22.04`)\n"
        "▪️ /download — descarrega vídeo de um link (YouTube, TikTok…)\n"
        "▪️ /mp3 — extrai o áudio de um link (ex: `/mp3 <url-youtube>`)\n"
        "▪️ /radio — rádios portuguesas + rádio parceira HellGate 🌟\n"
        "▪️ /music — gera uma música original com a tua descrição 🎼 (ex: `/music balada sobre Coimbra`)\n"
        "▪️ /streamhub — sites de streaming: filmes, séries e IPTV 📺 (ex: `/streamhub filmes`)\n"
        "▪️ /video — gera vídeo a partir de texto ou anima uma imagem 🎬 (ex: `/video um dragão a voar`)\n"
        "▪️ /avatar — avatar falante: responde a uma foto com `/avatar olá!` 🗣\n"
        "▪️ /ajuda — mostra esta mensagem\n\n"
        "Escolhe um botão abaixo ou escreve um comando! 👇"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Olá, *{update.effective_user.first_name}*! 👋\n\n{teclado_menu()}",
        parse_mode="Markdown",
    )


async def neobot_mention(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde com uma saudação quando alguém escreve 'neobot' numa mensagem de grupo."""
    if not update.message or not update.message.text:
        return
    texto = update.message.text.lower()
    if "neobot" not in texto:
        return
    user = update.effective_user
    nome = user.first_name if user else "amigo"
    saudacao = (
        f"👋 Olá, *{nome}*! Eu sou o *NEOBOT*, o teu assistente multiusos! 🤖\n\n"
        f"Escreve */ajuda* para veres todos os comandos que eu tenho para ti! 💫"
    )
    try:
        await update.message.reply_text(saudacao, parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text(saudacao)


async def cmd_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(teclado_menu(), parse_mode="Markdown")


async def cmd_hora(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agora = datetime.now()
    await update.message.reply_text(f"🕐 Agora são *{agora.strftime('%H:%M:%S')}*", parse_mode="Markdown")


async def cmd_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    dias = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]
    meses = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
             "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
    hoje = datetime.now()
    texto = f"📅 Hoje é *{dias[hoje.weekday()]}*, {hoje.day} de {meses[hoje.month - 1]} de {hoje.year}"
    await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_piada(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(random.choice(PIADAS))


async def cmd_dado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = random.randint(1, 6)
    dados = ["⚀", "⚁", "⚂", "⚃", "⚄", "⚅"]
    await update.message.reply_text(f"🎲 Lançaste o dado... *{n}*! {dados[n - 1]}", parse_mode="Markdown")


async def cmd_moeda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    resultado = random.choice(["Cara 🪙", "Coroa 🌙"])
    await update.message.reply_text(f"🪙 A moeda caiu... *{resultado}*!", parse_mode="Markdown")


async def cmd_escolhe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Dá-me as opções! Exemplo:\n`/escolhe pizza sushi burger`",
            parse_mode="Markdown",
        )
        return
    escolha = random.choice(context.args)
    await update.message.reply_text(f"🤔 Eu escolho... *{escolha}*!", parse_mode="Markdown")


def _extract_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Query = command args, or the replied-to message text."""
    if context.args:
        return " ".join(context.args).strip()
    replied = update.message.reply_to_message
    if replied and replied.text:
        return replied.text.strip()
    return None


def _rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    window = _ask_history.setdefault(user_id, [])
    window[:] = [t for t in window if now - t < ASK_RATE_WINDOW]
    if len(window) >= ASK_RATE_LIMIT:
        return True
    window.append(now)
    return False


def _strip_markup(text: str) -> str:
    """Remove markdown/HTML the model leaks so Telegram HTML never shows raw tags."""
    text = re.sub(r"<[^>]+>", "", text)          # <b>bold</b> -> bold
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)  # **bold** -> bold
    text = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", text)  # *em* -> em
    text = re.sub(r"__([^_]+)__", r"\1", text)   # __bold__ -> bold
    text = re.sub(r"`([^`]+)`", r"\1", text)     # `code` -> code
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)  # headings
    return html.escape(text)


async def _ddg_search(query: str, limit: int = 5) -> list[tuple[str, str, str]]:
    """DuckDuckGo HTML search fallback (POST avoids the anti-bot 202 challenge).

    Returns (title, url, snippet) triples.
    """
    async with httpx.AsyncClient(
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        follow_redirects=True,
    ) as client:
        resp = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
        resp.raise_for_status()
    results: list[tuple[str, str, str]] = []
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
        resp.text,
        re.S,
    ):
        url, title, snippet = m.group(1), m.group(2), m.group(3)
        if url.startswith("//duckduckgo.com/l/?uddg="):
            url = urllib.parse.unquote(url.split("uddg=")[1].split("&")[0])
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()  # noqa: E731
        results.append((clean(title), url, clean(snippet)))
        if len(results) >= limit:
            break
    return results


async def cmd_google(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    author = user.first_name if user else "Alguém"

    query = _extract_question(update, context)
    if not query:
        await update.message.reply_text(
            "Como usar:\n`/google últimas notícias do GPL`\n\n"
            "Ou responde (reply) a uma mensagem com `/google` para eu pesquisar sobre ela.",
            parse_mode="Markdown",
        )
        return

    if user and _rate_limited(user.id):
        await update.message.reply_text(
            "⏳ Muitas pesquisas seguidas! Espera um pouco e tenta de novo."
        )
        return

    thinking = await update.message.reply_text("🌐 A pesquisar na internet...")
    try:
        results = await _ddg_search(query)
        if not results:
            await thinking.edit_text("🔍 Não encontrei resultados úteis. Tenta reformular a pesquisa.")
            return
        listing = "\n".join(
            f"{i}. {t} — {u}\n   {s}" for i, (t, u, s) in enumerate(results, 1)
        )
        answer = "Principais resultados:\n\n" + listing
    except Exception:
        logger.exception("Falha na pesquisa /google")
        await thinking.edit_text("❌ A pesquisa falhou agora. Tenta outra vez em instantes.")
        return

    answer = _strip_markup(answer)
    if len(answer) > 4000:
        answer = answer[:3990] + "…"
    header = f"🌐 *{author}* pesquisou:\n❝{query[:180]}❞\n\n"
    try:
        await thinking.edit_text(header + answer, parse_mode=ParseMode.HTML)
    except Exception:
        await thinking.edit_text(f"🌐 {author} pesquisou:\n❝{query[:180]}❞\n\n{answer}")


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    author = user.first_name if user else "Alguém"

    if not _groq_key():
        await update.message.reply_text(
            "🧠 A minha IA ainda não está configurada — o admin precisa de adicionar "
            "`GROQ_API_KEY` ao .env. Volta em breve!",
            parse_mode="Markdown",
        )
        return

    question = _extract_question(update, context)
    if not question:
        await update.message.reply_text(
            "Como usar:\n`/ask qual é a capital do Japão?`\n\n"
            "Ou responde (reply) a uma mensagem com `/ask` para eu responder a ela.",
            parse_mode="Markdown",
        )
        return

    if user and _rate_limited(user.id):
        await update.message.reply_text(
            "⏳ Muitas perguntas seguidas! Espera um pouco e tenta de novo."
        )
        return

    thinking = await update.message.reply_text("🧠 A pensar...")
    try:
        answer = await _groq_chat(
            GROQ_MODEL,
            GROQ_SYSTEM_PROMPT,
            f"{author} pergunta: {question}",
        )
    except httpx.HTTPStatusError as exc:
        logger.warning("Groq HTTP %s: %s", exc.response.status_code, exc.response.text[:200])
        await thinking.edit_text(
            "😅 A IA está sobrecarregada ou indisponível neste momento. Tenta outra vez daqui a pouco."
        )
        return
    except Exception:
        logger.exception("Falha ao contactar a Groq")
        await thinking.edit_text("❌ Não consegui falar com a IA agora. Tenta outra vez em instantes.")
        return

    # Strip any markup the model leaks, then escape for Telegram HTML.
    answer = _strip_markup(answer)
    # Telegram HTML messages cap at 4096 chars.
    if len(answer) > 4000:
        answer = answer[:3990] + "…"
    header = f"💬 *{author}* perguntou:\n❝{question[:180]}❞\n\n"
    try:
        await thinking.edit_text(header + answer, parse_mode=ParseMode.HTML)
    except Exception:
        await thinking.edit_text(f"💬 {author} perguntou:\n❝{question[:180]}❞\n\n{answer}")


# --- /news: recent news by category ---

NEWS_CATEGORIES = {
    "politica": "política",
    "tecnologia": "tecnologia",
    "desporto": "desporto",
    "economia": "economia",
    "mundo": "internacional mundo",
    "cultura": "cultura e entretenimento",
    "ciencia": "ciência",
    "saude": "saúde",
}

NEWS_FALLBACK_HTML = (
    "📰 Como usar:\n`/news <categoria ou tema>`\n\n"
    "Categorias disponíveis:\n"
    "▪️ /news politica\n"
    "▪️ /news tecnologia\n"
    "▪️ /news desporto\n"
    "▪️ /news economia\n"
    "▪️ /news mundo\n"
    "▪️ /news cultura\n"
    "▪️ /news ciencia\n"
    "▪️ /news saude\n\n"
    "Também podes pedir qualquer tema: `/news fifa mundial 2026`"
)


async def _google_news_rss(query: str, limit: int = 8) -> list[tuple[str, str, str]]:
    """Google News RSS — real article titles with publication dates (HTTP 200, no anti-bot)."""
    from urllib.parse import quote
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=pt-PT&gl=PT&ceid=PT:pt-150"
    async with httpx.AsyncClient(
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    items: list[tuple[str, str, str]] = []
    for m in re.finditer(
        r"<item><title>(.*?)</title><link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>",
        resp.text,
        re.S,
    ):
        clean = lambda s: html.unescape(re.sub(r"<!\\[CDATA\\[|]]>|<[^>]+>", "", s)).strip()  # noqa: E731
        items.append((clean(m.group(1)), m.group(2), clean(m.group(3))))
        if len(items) >= limit:
            break
    return items

async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    author = user.first_name if user else "Alguém"

    topic = _extract_question(update, context)
    if not topic:
        await update.message.reply_text(NEWS_FALLBACK_HTML, parse_mode="Markdown")
        return

    # Map known category aliases (also match accented input like "política").
    normalized = topic.lower().strip()
    for alias, label in NEWS_CATEGORIES.items():
        if normalized == alias or normalized.rstrip("s") == alias.rstrip("s") or normalized == label:
            topic = f"notícias {label} hoje Portugal"
            break

    if user and _rate_limited(user.id):
        await update.message.reply_text(
            "⏳ Muitas pesquisas seguidas! Espera um pouco e tenta de novo."
        )
        return

    thinking = await update.message.reply_text("📰 A procurar as notícias mais recentes...")
    hoje_str = datetime.now().strftime("%d/%m/%Y")
    try:
        results = await _google_news_rss(topic)
        if results:
            listing = "\n".join(
                f"• {t} — {d}" for t, _, d in results
            )
            user_content = (
                f"Data de hoje: {hoje_str}. Notícias recentes sobre: {topic}\n\n"
                f"Manchetes com datas de publicação:\n{listing}\n\n"
                "Resume as notícias mais recentes e relevantes em bullets."
            )
        else:
            results = await _ddg_search(f"{topic} notícias {hoje_str}")
            if results:
                listing = "\n".join(
                    f"• {t}: {d} ({u})" for t, u, d in results
                )
                user_content = (
                    f"Data de hoje: {hoje_str}. Notícias recentes sobre: {topic}\n\n"
                    f"Resultados da pesquisa:\n{listing}\n\n"
                    "Resume as notícias mais recentes e relevantes em bullets. "
                    "Se os resultados não tiverem notícias concretas, diz o que encontraste."
                )
            else:
                # No search results — let the model answer from its own knowledge, flagged as such.
                user_content = f"Quais são as notícias mais recentes sobre: {topic}? (sem resultados de pesquisa disponíveis — responde com cautela)"
    except Exception:
        logger.exception("Falha na pesquisa /news")
        results = []
        user_content = f"Quais são as notícias mais recentes sobre: {topic}?"

    if not _groq_key():
        # No Groq key: fall back to raw DDG results list.
        if results:
            listing = "\n".join(
                f"{i}. {t} — {u}\n   {s}" for i, (t, u, s) in enumerate(results, 1)
            )
            answer = "Principais notícias encontradas:\n\n" + listing
        else:
            await thinking.edit_text("🔍 Não encontrei notícias agora. Tenta outra vez em instantes.")
            return
    else:
        try:
            answer = await _groq_chat(GROQ_MODEL, NEWS_SYSTEM_PROMPT, user_content, max_tokens=800)
        except httpx.HTTPStatusError as exc:
            logger.warning("Groq HTTP %s: %s", exc.response.status_code, exc.response.text[:200])
            # Fall back to raw results if the AI fails.
            if results:
                answer = "Principais notícias encontradas:\n\n" + "\n".join(
                    f"{i}. {t} — {u}\n   {s}" for i, (t, u, s) in enumerate(results, 1)
                )
            else:
                await thinking.edit_text("❌ Não consegui resumir as notícias agora. Tenta outra vez em instantes.")
                return
        except Exception:
            logger.exception("Falha ao contactar a Groq para /news")
            if results:
                answer = "Principais notícias encontradas:\n\n" + "\n".join(
                    f"{i}. {t} — {u}\n   {s}" for i, (t, u, s) in enumerate(results, 1)
                )
            else:
                await thinking.edit_text("❌ Não consegui resumir as notícias agora. Tenta outra vez em instantes.")
                return

    answer = _strip_markup(answer)
    if len(answer) > 4000:
        answer = answer[:3990] + "…"
    header = f"📰 *{author}* pediu notícias de:\n❝{topic[:180]}❞\n\n"
    try:
        await thinking.edit_text(header + answer, parse_mode=ParseMode.HTML)
    except Exception:
        await thinking.edit_text(f"📰 {author} pediu notícias de:\n❝{topic[:180]}❞\n\n{answer}")


# --- comandos extra: wiki, img, meteo, youtube, crypto, cinema, estreias, imdb, play, ipinfo, ipscan, iplookup, phone ---

UA_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
}


def _usage(text: str) -> str:
    return text


async def _http_get(url: str, *, timeout: float = 20, headers: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout, headers=headers or UA_BROWSER, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp


# --- /wiki ---

async def cmd_wiki(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    termo = " ".join(context.args).strip()
    if not termo:
        await update.message.reply_text("Como usar:\n`/wiki Portugal`", parse_mode="Markdown")
        return
    thinking = await update.message.reply_text("📚 A procurar na Wikipédia...")
    try:
        resp = await _http_get(
            "https://pt.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(termo.replace(" ", "_"))
        )
        data = resp.json()
    except httpx.HTTPStatusError:
        data = None
    except Exception:
        logger.exception("Falha /wiki")
        await thinking.edit_text("❌ Não consegui falar com a Wikipédia agora. Tenta outra vez.")
        return

    if not data or data.get("type") == "https://mediawiki.org/wiki/HyperSwitch/errors/not_found":
        # Fallback: search API for close matches.
        try:
            resp = await _http_get(
                "https://pt.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=5&srsearch="
                + urllib.parse.quote(termo)
            )
            hits = resp.json().get("query", {}).get("search", [])
        except Exception:
            hits = []
        if hits:
            listing = "\n".join(
                f"▪️ {html.escape(h['title'])}" for h in hits
            )
            await thinking.edit_text(
                f"🔍 Não encontrei uma página exata para “{html.escape(termo)}”. Talvez te refiras a:\n\n{listing}",
                parse_mode=ParseMode.HTML,
            )
        else:
            await thinking.edit_text(f"🔍 Não encontrei nada na Wikipédia sobre “{termo}”.")
        return

    title = html.escape(data.get("title", termo))
    extract = html.escape(data.get("extract", "")[:900])
    url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
    answer = f"📚 *{title}*\n\n{extract}"
    if url:
        answer += f"\n\n{url}"
    try:
        await thinking.edit_text(answer, parse_mode=ParseMode.HTML)
    except Exception:
        await thinking.edit_text(answer)


# --- /img (Z-Image via Pollinations — modelo da zimage.run) ---

async def cmd_img(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text(            "Como usar:\n`/image gato astronauta a flutuar no espaço`", parse_mode="Markdown")
        return
    if user and _rate_limited(user.id):
        await update.message.reply_text("⏳ Muitas imagens seguidas! Espera um pouco.")
        return
    thinking = await update.message.reply_text("🎨 A gerar a imagem...")
    url = (
        "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
        + f"?width=1024&height=1024&nologo=true&model=z-image&seed={random.randint(1, 999999)}"
    )
    try:
        resp = await _http_get(url, timeout=120)
        image_bytes = resp.content
    except Exception:
        logger.exception("Falha /img")
        await thinking.edit_text("❌ A geração de imagens falhou agora. Tenta outra vez em instantes.")
        return
    caption = f"🎨 {html.escape(prompt[:200])}"
    try:
        await update.message.reply_photo(photo=image_bytes, caption=caption, parse_mode=ParseMode.HTML)
        await thinking.delete()
    except Exception:
        logger.exception("Falha ao enviar foto /img")
        await thinking.edit_text(f"🎨 A imagem foi gerada, mas não consegui enviá-la. Vê aqui:\n{url}")


# --- /audio (TTS via Google Translate, gratuito, sem chaves) ---

async def cmd_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(
            "Como usar:\n`/audio frase que queres ouvir`\n\n"
            "Exemplo: `/audio bem-vindos ao grupo, aqui falamos de futebol`",
            parse_mode="Markdown",
        )
        return
    if user and _rate_limited(user.id):
        await update.message.reply_text("⏳ Muitos pedidos seguidos! Espera um pouco.")
        return
    if len(text) > 1500:
        await update.message.reply_text("✂️ Texto demasiado longo — máximo de 1500 caracteres de cada vez.")
        return

    thinking = await update.message.reply_text("🔊 A gerar a mensagem de voz...")

    # Deteção de idioma: cirílico → ru, caso contrário pt
    lang = "ru" if re.search(r"[а-яА-ЯёЁ]", text) else "pt"

    # Google TTS принимает максимум ~200 символов — режем на куски по границе слов
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= 190:
            chunks.append(rest)
            break
        cut = rest.rfind(" ", 0, 190)
        if cut < 40:
            cut = 190
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()

    audio = bytearray()
    try:
        async with httpx.AsyncClient(timeout=30, headers=UA_BROWSER, follow_redirects=True) as client:
            for chunk in chunks:
                resp = await client.get(
                    "https://translate.google.com/translate_tts",
                    params={"ie": "UTF-8", "q": chunk, "tl": lang, "client": "tw-ob", "total": len(chunks), "idx": chunks.index(chunk), "voice": "male"},
                )
                resp.raise_for_status()
                if resp.content[:2] not in (b"\xff\xf3", b"\xff\xf2") and resp.content[:3] != b"ID3":
                    raise ValueError("Не аудио в ответе")
                audio.extend(resp.content)
    except Exception:
        logger.exception("Falha /audio")
        await thinking.edit_text("❌ Não consegui gerar a voz agora. Tenta outra vez num instante.")
        return

    caption = f"🎙 {html.escape(text[:150])}"
    try:
        await update.message.reply_voice(voice=bytes(audio), caption=caption, parse_mode=ParseMode.HTML)
        await thinking.delete()
    except Exception:
        logger.exception("Falha ao enviar voz /audio")
        await thinking.edit_text("❌ A voz foi gerada, mas o Telegram não aceitou o ficheiro. Tenta algo mais curto.")


# --- /meteo (Open-Meteo) ---

_WMO = {
    0: ("céu limpo", "☀️"), 1: ("sobretudo limpo", "🌤️"), 2: ("parcialmente nublado", "⛅"),
    3: ("encoberto", "☁️"), 45: ("nevoeiro", "🌫️"), 48: ("nevoeiro com geada", "🌫️"),
    51: ("chuvisco leve", "🌦️"), 53: ("chuvisco", "🌦️"), 55: ("chuvisco forte", "🌧️"),
    61: ("chuva fraca", "🌧️"), 63: ("chuva", "🌧️"), 65: ("chuva forte", "🌧️"),
    71: ("neve fraca", "🌨️"), 73: ("neve", "🌨️"), 75: ("neve forte", "❄️"),
    80: ("aguaceiros fracos", "🌦️"), 81: ("aguaceiros", "🌧️"), 82: ("aguaceiros violentos", "⛈️"),
    95: ("trovoada", "⛈️"), 96: ("trovoada com granizo", "⛈️"), 99: ("trovoada forte com granizo", "⛈️"),
}


def _wmo_label(code: int) -> str:
    desc, emoji = _WMO.get(code, ("tempo desconhecido", "🌡️"))
    return f"{emoji} {desc}"


async def cmd_meteo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cidade = " ".join(context.args).strip()
    if not cidade:
        await update.message.reply_text("Como usar:\n`/meteo Lisboa`", parse_mode="Markdown")
        return
    thinking = await update.message.reply_text(f"🌤️ A ver o tempo em {cidade}...")
    try:
        geo = (
            await _http_get(
                "https://geocoding-api.open-meteo.com/v1/search?count=1&language=pt&name=" + urllib.parse.quote(cidade)
            )
        ).json()
        results = geo.get("results") or []
        if not results:
            await thinking.edit_text(f"🔍 Não encontrei a cidade “{html.escape(cidade)}”.")
            return
        place = results[0]
        forecast = (
            await _http_get(
                "https://api.open-meteo.com/v1/forecast?timezone=auto&forecast_days=3"
                f"&latitude={place['latitude']}&longitude={place['longitude']}"
                "&current=temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code"
                "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
            )
        ).json()
    except Exception:
        logger.exception("Falha /meteo")
        await thinking.edit_text("❌ Não consegui obter a meteorologia agora. Tenta outra vez.")
        return

    cur = forecast["current"]
    daily = forecast["daily"]
    nome = place.get("name", cidade)
    pais = place.get("country", "")
    lines = [
        f"🌍 *{html.escape(nome)}*{', ' + html.escape(pais) if pais else ''}",
        f"{html.escape(_wmo_label(int(cur['weather_code'])))}",
        f"🌡️ {cur['temperature_2m']}°C (sente-se {cur['apparent_temperature']}°C)",
        f"💧 Humidade: {cur['relative_humidity_2m']}%  ·  💨 Vento: {cur['wind_speed_10m']} km/h",
        "",
    ]
    for i in range(len(daily["time"])):
        dia = daily["time"][i]
        rotulo = "Hoje" if i == 0 else ("Amanhã" if i == 1 else dia)
        lines.append(
            f"▪️ *{rotulo}*: {daily['temperature_2m_min'][i]}–{daily['temperature_2m_max'][i]}°C, "
            f"{daily['precipitation_probability_max'][i]}% chuva — "
            f"{html.escape(_wmo_label(int(daily['weather_code'][i])))}"
        )
    try:
        await thinking.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception:
        await thinking.edit_text("\n".join(lines))


# --- /youtube ---

async def cmd_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Como usar:\n`/youtube tutorial python`", parse_mode="Markdown")
        return
    thinking = await update.message.reply_text("📺 A procurar no YouTube...")
    try:
        results = await _ddg_search(f"site:youtube.com {query}", limit=5)
    except Exception:
        logger.exception("Falha /youtube")
        results = []
    if not results:
        link = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
        await thinking.edit_text(f"🔍 Sem resultados diretos. Pesquisa manual:\n{link}")
        return
    listing = "\n".join(f"▪️ {html.escape(t)}\n   {u}" for t, _, u in results)
    try:
        await thinking.edit_text(f"📺 Resultados para “{html.escape(query)}”:\n\n{listing}", parse_mode=ParseMode.HTML)
    except Exception:
        await thinking.edit_text(f"📺 Resultados para “{query}”:\n\n{listing}")


# --- /crypto (CoinGecko) ---

_CRYPTO_IDS = {
    "btc": "bitcoin", "bitcoin": "bitcoin",
    "eth": "ethereum", "ethereum": "ethereum",
    "sol": "solana", "solana": "solana",
    "ada": "cardano", "cardano": "cardano",
    "doge": "dogecoin", "dogecoin": "dogecoin",
    "xrp": "ripple", "ripple": "ripple",
    "bnb": "binancecoin", "bnb coin": "binancecoin",
    "dot": "polkadot", "polkadot": "polkadot",
    "link": "chainlink", "chainlink": "chainlink",
    "ltc": "litecoin", "litecoin": "litecoin",
    "trx": "tron", "tron": "tron",
    "avax": "avalanche-2", "avalanche": "avalanche-2",
    "usdt": "tether", "tether": "tether",
    "usdc": "usd-coin",
}


def _fmt_price(price: float) -> str:
    if price >= 1000:
        return f"${price:,.0f}"
    if price >= 1:
        return f"${price:,.2f}"
    return f"${price:.6f}"


async def cmd_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    moeda = " ".join(context.args).strip().lower()
    thinking = await update.message.reply_text("🪙 A consultar os preços...")
    try:
        if moeda:
            coin_id = _CRYPTO_IDS.get(moeda)
            if not coin_id:
                await thinking.edit_text(
                    f"🤔 Não conheço “{html.escape(moeda)}”. Tenta: btc, eth, sol, ada, doge, xrp, bnb, dot, link, ltc, trx, avax.",
                    parse_mode=ParseMode.HTML,
                )
                return
            data = (
                await _http_get(
                    f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd,eur&include_24hr_change=true"
                )
            ).json()
            entry = data.get(coin_id, {})
            if not entry:
                await thinking.edit_text("❌ Não consegui obter o preço agora.")
                return
            change = entry.get("usd_24h_change")
            tendencia = f" ({'📈 +' if (change or 0) >= 0 else '📉 '}{change:.1f}% 24h)" if change is not None else ""
            texto = (
                f"🪙 *{html.escape(moeda.upper())}*\n"
                f"USD: {_fmt_price(entry.get('usd', 0))}{tendencia}\n"
                f"EUR: {_fmt_price(entry.get('eur', 0))}"
            )
        else:
            data = (
                await _http_get(
                    "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=5&page=1"
                )
            ).json()
            lines = ["🪙 *Top 5 por capitalização:*", ""]
            for coin in data[:5]:
                change = coin.get("price_change_percentage_24h") or 0
                seta = "📈" if change >= 0 else "📉"
                lines.append(
                    f"▪️ {html.escape(coin['name'])} ({coin['symbol'].upper()}): "
                    f"{_fmt_price(coin['current_price'])} {seta} {change:+.1f}%"
                )
            texto = "\n".join(lines)
    except Exception:
        logger.exception("Falha /crypto")
        await thinking.edit_text("❌ Não consegui obter os preços agora. Tenta outra vez.")
        return
    try:
        await thinking.edit_text(texto, parse_mode=ParseMode.HTML)
    except Exception:
        await thinking.edit_text(texto)


# --- /cinema e /estreias ---

async def _list_web_results(query: str, limit: int) -> list[tuple[str, str, str]]:
    try:
        return await _ddg_search(query, limit=limit)
    except Exception:
        logger.exception("Falha na pesquisa web")
        return []


async def _send_result_listing(update, thinking, header: str, results, empty_msg: str) -> None:
    if not results:
        await thinking.edit_text(empty_msg)
        return
    listing = "\n".join(
        f"▪️ {html.escape(t)}\n   {u}" for t, _, u in results
    )
    try:
        await thinking.edit_text(header + "\n\n" + listing, parse_mode=ParseMode.HTML)
    except Exception:
        await thinking.edit_text(header + "\n\n" + listing)


async def cmd_cinema(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    thinking = await update.message.reply_text("🎬 A procurar filmes em cartaz...")
    try:
        resp = await _http_get("https://filmspot.pt/filmes/", timeout=30)
        html_src = resp.text
    except Exception:
        logger.exception("Falha /cinema")
        html_src = ""
    results: list[tuple[str, str, str]] = []
    # filmspot.pt/filmes: <H2><a href="/filme/..."><span>TITULO</span> <span class="tituloOriginal"><span>ORIG</span></span></a></h2>
    for m in re.finditer(
        r'<h2><a href="(/filme/[^"]+)"[^>]*>(.*?)</a></h2>\s*<p class="zsmall">([^<]*)</p>',
        html_src,
        re.I | re.S,
    ):
        url, inner, info = m.groups()
        sp = re.search(
            r'<span>(.*?)</span>(?:\s*<span class="tituloOriginal"><span>(.*?)</span></span>)?',
            inner,
            re.S,
        )
        if not sp:
            continue
        titulo = html.unescape(sp.group(1)).strip()
        original = html.unescape(sp.group(2)).strip() if sp.group(2) else ""
        # titles may contain stray markup like "</span> / <span>" — normalise
        titulo = re.sub(r"</?span[^>]*>", "", titulo)
        titulo = re.sub(r"\s{2,}", " ", titulo).strip()
        titulo = html.unescape(titulo)
        titulo = titulo + (f" ({original})" if original and original != titulo else "")
        info = html.unescape(info).strip()
        results.append((titulo, f"https://filmspot.pt{url}", info))
        if len(results) >= 25:
            break
    if not results:
        # Fallback: pesquisa web
        results = await _list_web_results("filmes em cartaz cinema Portugal esta semana", limit=25)
    listing = "\n".join(f"🎬 {html.escape(t)}\n   {u}\n   {html.escape(i)}" for t, u, i in results)
    texto = f"🎬 *Filmes em cartaz (filmspot.pt):*\n\n{listing}" if results else "🔍 Não encontrei a lista agora. Tenta outra vez em instantes."
    try:
        await thinking.edit_text(texto, parse_mode=ParseMode.HTML)
    except Exception:
        await thinking.edit_text(texto)


async def cmd_estreias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    thinking = await update.message.reply_text("🆕 A procurar as estreias da semana...")
    try:
        resp = await _http_get("https://filmspot.pt/estreias/", timeout=30)
        html_src = resp.text
    except Exception:
        logger.exception("Falha /estreias")
        html_src = ""

    results: list[tuple[str, str, str]] = []
    # filmspot.pt/estreias: date headers (<h2 class="estreiasH2">27 de agosto de 2026</h2>)
    # followed by film entries whose titles are <H3><a ...><span>TITULO</span> ...
    entry_pat = re.compile(
        r'<h2 class="estreiasH2[^"]*"[^>]*>(.*?)</h2>'
        r'|<h3><a href="(/filme/[^"]+)"[^>]*>(.*?)</a></h3>\s*<p class="zsmall">([^<]*)</p>',
        re.I | re.S,
    )
    current_date: str | None = None
    for m in entry_pat.finditer(html_src):
        if m.group(1) is not None:
            current_date = html.unescape(m.group(1)).strip()
            continue
        inner = m.group(3)
        sp = re.search(
            r'<span>(.*?)</span>(?:\s*<span class="tituloOriginal"><span>(.*?)</span></span>)?',
            inner,
            re.S,
        )
        if not sp:
            continue
        titulo = html.unescape(sp.group(1)).strip()
        original = html.unescape(sp.group(2)).strip() if sp.group(2) else ""
        # titles may contain stray markup like "</span> / <span>" — normalise
        titulo = re.sub(r"</?span[^>]*>", "", titulo)
        titulo = re.sub(r"\s+/\s+", " / ", titulo).strip()
        titulo = re.sub(r"\s{2,}", " ", titulo)
        titulo = html.unescape(titulo)
        titulo = titulo + (f" ({original})" if original and original != titulo else "")
        info = html.unescape(m.group(4)).strip()
        date_part = f" — estreia: {current_date}" if current_date else ""
        results.append((f"{titulo}{date_part}", f"https://filmspot.pt{m.group(2)}", info))
        if len(results) >= 10:
            break
    if not results:
        results = await _list_web_results("estreias cinema esta semana Portugal", limit=10)
    listing = "\n".join(f"🆕 {html.escape(t)}\n   {u}\n   {html.escape(i)}" for t, u, i in results)
    texto = f"🆕 *Estreias da semana (filmspot.pt):*\n\n{listing}" if results else "🔍 Não encontrei as estreias agora. Tenta outra vez em instantes."
    try:
        await thinking.edit_text(texto, parse_mode=ParseMode.HTML)
    except Exception:
        await thinking.edit_text(texto)


# --- /imdb e /play ---

async def cmd_imdb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Como usar:\n`/imdb Matrix`", parse_mode="Markdown")
        return
    thinking = await update.message.reply_text("🎞️ A consultar o IMDb...")
    try:
        resp = await _http_get(
            "https://v3.sg.media-imdb.com/suggestion/x/" + urllib.parse.quote(query.lower()) + ".json"
        )
        entries = resp.json().get("d", [])
    except Exception:
        logger.exception("Falha /imdb")
        await thinking.edit_text("❌ Não consegui falar com o IMDb agora. Tenta outra vez.")
        return
    match = next((e for e in entries if e.get("id", "").startswith("tt")), None)
    if not match:
        await thinking.edit_text(f"🔍 Não encontrei “{html.escape(query)}” no IMDb.", parse_mode=ParseMode.HTML)
        return
    tipo = html.escape(str(match.get("q", "")))
    ano = match.get("y", "")
    elenco = html.escape(str(match.get("s", "")))
    titulo = html.escape(str(match.get("l", query)))
    url = f"https://www.imdb.com/title/{match['id']}/"
    texto = f"🎞️ *{titulo}*"
    if ano:
        texto += f" ({ano})"
    if tipo:
        texto += f" — {tipo}"
    if elenco:
        texto += f"\n👥 {elenco}"
    texto += f"\n\n{url}"
    try:
        await thinking.edit_text(texto, parse_mode=ParseMode.HTML)
    except Exception:
        await thinking.edit_text(texto)


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    alvo = " ".join(context.args).strip()
    m = re.search(r"tt\d{5,}", alvo)
    if not m:
        await update.message.reply_text(
            "Como usar:\n`/play https://www.imdb.com/title/tt0111161/`\n\n"
            "Cola o URL do IMDb do filme e eu gero o link de streaming.",
            parse_mode="Markdown",
        )
        return
    tt = m.group(0)
    await update.message.reply_text(
        f"▶️ Link de streaming para *{tt}*:\nhttps://imdb.su/title/{tt}/",
        parse_mode="Markdown",
    )


# --- /ipinfo, /ipscan, /iplookup ---

async def _resolve_host(alvo: str) -> str | None:
    try:
        ipaddress.ip_address(alvo)
        return alvo
    except ValueError:
        pass
    try:
        return await asyncio.to_thread(socket.gethostbyname, alvo)
    except OSError:
        return None


async def cmd_ipinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    alvo = " ".join(context.args).strip()
    if not alvo:
        await update.message.reply_text("Como usar:\n`/ipinfo 8.8.8.8` ou `/ipinfo exemplo.com`", parse_mode="Markdown")
        return
    thinking = await update.message.reply_text("🛰️ A consultar o IP...")
    ip = await _resolve_host(alvo)
    if not ip:
        await thinking.edit_text(f"🔍 Não consegui resolver “{html.escape(alvo)}”.", parse_mode=ParseMode.HTML)
        return
    try:
        data = (
            await _http_get(
                f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,zip,lat,lon,timezone,isp,org,as,reverse,query"
            )
        ).json()
    except Exception:
        logger.exception("Falha /ipinfo")
        await thinking.edit_text("❌ Não consegui consultar a base de dados de IPs agora.")
        return
    if data.get("status") != "success":
        await thinking.edit_text(f"🔍 Sem informação para {ip} (IP privado ou reservado?).")
        return
    texto = (
        f"🛰️ *{html.escape(str(data.get('query', ip)))}*\n"
        f"📍 {html.escape(str(data.get('city', '?')))}, {html.escape(str(data.get('regionName', '?')))}, "
        f"{html.escape(str(data.get('country', '?')))} {html.escape(str(data.get('zip', '')))}\n"
        f"🌐 Coordenadas: {data.get('lat', '?')}, {data.get('lon', '?')}\n"
        f"🗺️ <a href=\"https://www.google.com/maps?q={data.get('lat', '')},{data.get('lon', '')}\">Ver no Google Maps</a>\n"
        f"👁️ <a href=\"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={data.get('lat', '')},{data.get('lon', '')}\">Street View</a>\n"
        f"🕓 Fuso: {html.escape(str(data.get('timezone', '?')))}\n"
        f"🏢 ISP: {html.escape(str(data.get('isp', '?')))}\n"
        f"⚙️ Org: {html.escape(str(data.get('org', '?')))}\n"
        f"🔧 AS: {html.escape(str(data.get('as', '?')))}"
    )
    if data.get("reverse"):
        texto += f"\n↩️ DNS inverso: {html.escape(str(data['reverse']))}"
    if alvo != ip:
        texto = f"🔗 {html.escape(alvo)} → {ip}\n\n" + texto
    try:
        await thinking.edit_text(texto, parse_mode=ParseMode.HTML)
    except Exception:
        await thinking.edit_text(texto)


_COMMON_PORTS = [
    21, 22, 23, 25, 53, 80, 110, 143, 443, 465, 587, 993, 995,
    1433, 1723, 3306, 3389, 5432, 5900, 6379, 8080, 8443, 9200, 27017, 25565,
]


async def _check_port(ip: str, port: int) -> int | None:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=1.5)
        writer.close()
        return port
    except Exception:
        return None


async def cmd_ipscan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    alvo = " ".join(context.args).strip()
    if not alvo:
        await update.message.reply_text("Como usar:\n`/ipscan 8.8.8.8`", parse_mode="Markdown")
        return
    ip = await _resolve_host(alvo)
    if not ip:
        await update.message.reply_text(f"🔍 Não consegui resolver “{alvo}”.")
        return
    thinking = await update.message.reply_text(
        f"🔎 A analisar {len(_COMMON_PORTS)} portas comuns em {ip}... (pode demorar ~10s)"
    )
    abertas = await asyncio.gather(*(_check_port(ip, p) for p in _COMMON_PORTS))
    abertas = [p for p in abertas if p is not None]
    if abertas:
        lista = ", ".join(str(p) for p in sorted(abertas))
        texto = f"🔓 Portas abertas em {ip}:\n{lista}"
    else:
        texto = f"🔒 Nenhuma das portas comuns está aberta em {ip}."
    texto += "\n\n⚠️ Usa apenas em sistemas que te pertencem ou com autorização."
    await thinking.edit_text(texto)


async def cmd_iplookup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    alvo = " ".join(context.args).strip()
    if not alvo:
        await update.message.reply_text("Como usar:\n`/iplookup 8.8.8.8`", parse_mode="Markdown")
        return
    ip = await _resolve_host(alvo)
    if not ip:
        await update.message.reply_text(f"🔍 Não consegui resolver “{alvo}”.")
        return
    try:
        hostname, aliases, _ = await asyncio.to_thread(socket.gethostbyaddr, ip)
    except OSError:
        await update.message.reply_text(f"🔍 Sem DNS inverso para {ip}.")
        return
    extras = "\n".join(f"▪️ {html.escape(a)}" for a in aliases[:5] if a != hostname)
    texto = f"↩️ DNS inverso de {ip}:\n*{html.escape(hostname)}*"
    if extras:
        texto += "\n\n" + extras
    try:
        await update.message.reply_text(texto, parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text(texto)


# --- /torrent (TPB via apibay.org + 1337x.pro + predb.me) ---

async def _tpb_search(query: str, limit: int = 5) -> list[tuple[str, str, str]]:
    """ThePirateBay: a página de pesquisa é JS, mas o site expõe a API oficial apibay.org.
    Devolve (titulo, magnet_link, "seeders | leechers | tamanho")."""
    resp = await _http_get(f"https://apibay.org/q.php?q={urllib.parse.quote(query)}&cat=0", timeout=20)
    data = resp.json()
    if isinstance(data, dict):  # {"No results"}
        return []

    def _human_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
            n /= 1024
        return f"{n:.1f} TB"

    results: list[tuple[str, str, str]] = []
    for t in data:
        try:
            tamanho = _human_size(int(t.get("size", 0)))
        except (TypeError, ValueError):
            tamanho = "?"
        magnet = (
            f"magnet:?xt=urn:btih:{t['info_hash']}"
            f"&dn={urllib.parse.quote(t['name'])}"
            "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
            "&tr=udp%3A%2F%2Ftracker.coppersurfer.tk%3A6969%2Fannounce"
            "&tr=udp%3A%2F%2Ftracker.leechers-paradise.org%3A6969"
        )
        results.append(
            (
                t["name"],
                magnet,
                f"👤 {t.get('username', '?')} | 🌱 {t.get('seeders', '?')} | 📦 {tamanho}",
            )
        )
        if len(results) >= limit:
            break
    return results


async def _1337x_search(query: str, limit: int = 5) -> list[tuple[str, str, str]]:
    """1337x.pro: tabela de resultados da pesquisa; magnet fica na página de cada torrent."""
    resp = await _http_get(
        "https://1337x.pro/search/?q=" + urllib.parse.quote(query),
        timeout=20,
    )
    rows: list[tuple[str, str, str, str]] = []  # (url, title, seeders, size)
    for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", resp.text, re.S):
        row = m.group(1)
        ent = re.search(r'<a href="(https://1337x\.pro/torrent/[^"]+)"[^>]*id-text="([^"]*)"', row)
        if not ent:
            continue
        url, title = ent.groups()
        se = re.search(r'<td class="coll-2[^"]*">([^<]*)</td>', row)
        size = re.search(r'<td class="coll-4[^"]*">\s*([^<]+?)\s*</td>', row)
        rows.append((url, html.unescape(title).strip(), se.group(1).strip() if se else "?", size.group(1).strip() if size else "?"))
        if len(rows) >= limit:
            break
    results: list[tuple[str, str, str]] = []
    for url, title, se, size in rows:
        try:
            resp_t = await _http_get(url, timeout=15)
            mg = re.search(r'href="(magnet:\?[^"]+)"', resp_t.text)
        except Exception:
            mg = None
        magnet = html.unescape(mg.group(1)) if mg else url  # fallback: link da página
        results.append((title, magnet, f"🌱 {se} | 📦 {size}"))
    return results


async def cmd_torrent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Como usar:\n`/torrent ubuntu 22.04`", parse_mode="Markdown")
        return
    if user and _rate_limited(user.id):
        await update.message.reply_text("⏳ Muitas pesquisas seguidas! Espera um pouco.")
        return
    thinking = await update.message.reply_text("🧲 A procurar nos sites de torrents...")

    tpb: list[tuple[str, str, str]] = []
    l337: list[tuple[str, str, str]] = []
    try:
        tpb = await _tpb_search(query, limit=5)
    except Exception:
        logger.exception("Falha /torrent TPB")
    try:
        l337 = await _1337x_search(query, limit=3)
    except Exception:
        logger.exception("Falha /torrent 1337x")

    if not tpb and not l337:
        await thinking.edit_text(
            f"🔍 Não encontrei resultados para “{html.escape(query)}”.\n\n"
            f"🔎 Pesquisa manual:\n"
            f"▪️ https://thepiratebay.org/search/{urllib.parse.quote(query)}/1/99/0\n"
            f"▪️ https://1337x.pro/search/?q={urllib.parse.quote(query)}\n"
            f"▪️ https://predb.me/?search={urllib.parse.quote(query)}",
            parse_mode=ParseMode.HTML,
        )
        return

    sections: list[str] = []
    if tpb:
        listing = "\n\n".join(
            f"🧲 {html.escape(t)}\n   🌱 {i}\n   {u}"
            for t, u, i in tpb
        )
        sections.append(f"<b>☠️ ThePirateBay:</b>\n\n{listing}")
    if l337:
        listing = "\n\n".join(
            f"🧲 {html.escape(t)}\n   🌱 {i}\n   {u[:120]}{'…' if len(u) > 120 else ''}"
            for t, u, i in l337
        )
        sections.append(f"<b>🌀 1337x:</b>\n\n{listing}")
    predb_link = f"🔎 predb.me: https://predb.me/?search={urllib.parse.quote(query)}"
    texto = (
        f"🧲 <b>Resultados para “{html.escape(query)}”:</b>\n\n"
        + "\n\n".join(sections)
        + f"\n\n{predb_link}"
    )
    if len(texto) > 4000:
        texto = texto[:3990] + "…"
    try:
        await thinking.edit_text(texto, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await thinking.edit_text(texto, disable_web_page_preview=True)


# --- /download e /mp3 (yt-dlp) ---

TG_FILE_LIMIT = 49 * 1024 * 1024  # bots: 50 MB por ficheiro


def _run_ytdlp(opts: dict) -> dict:
    """Bloqueante — corre em thread via asyncio.to_thread.

    Motor Seal (JunkFood02/Seal): yt-dlp nightly com aria2c como downloader
    externo e runtime JS (deno/node) para os desafios n-sig do YouTube.
    """
    import yt_dlp
    import shutil
    if shutil.which("aria2c"):
        opts.setdefault("external_downloader", {"default": "aria2c"})
        opts.setdefault("external_downloader_args", {"aria2c": ["-c", "-x", "8", "-s", "8", "--console-log-level=warn"]})
    if shutil.which("deno"):
        opts.setdefault("js_runtimes", {"deno": {}})
    elif shutil.which("node"):
        opts.setdefault("js_runtimes", {"node": {}})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(opts["urls"], download=True)
        return info or {}


def _mvtools_download(url: str, dest_dir: str) -> tuple[str, str] | None:
    """Descarrega ficheiros diretos via mv.tools (url-file-downloader).

    O servidor deles busca a URL com aria2 e devolve um link temporário.
    Fluxo: POST /api/url-downloads {url} → poll GET /api/url-downloads/{id}
    até status COMPLETED → GET /api/url-downloads/{id}/file
    Devolve (caminho, nome_do_ficheiro) ou None.
    """
    if not url.startswith(("http://", "https://")):
        return None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Referer": "https://mv.tools/tools/url-file-downloader",
        "Origin": "https://mv.tools",
        "Content-Type": "application/json",
    }

    def _req(method: str, u: str, body: dict | None = None) -> dict:
        import urllib.request
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(u, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    try:
        task = _req("POST", "https://mv.tools/api/url-downloads", {"url": url})
    except Exception:
        return None
    task_id = task.get("id")
    if not task_id:
        return None

    # poll até COMPLETED/FAILED (máx ~90s)
    status: dict = {}
    for _ in range(30):
        time.sleep(3)
        try:
            status = _req("GET", f"https://mv.tools/api/url-downloads/{task_id}")
        except Exception:
            return None
        if status.get("status") in ("COMPLETED", "FAILED"):
            break
    if status.get("status") != "COMPLETED":
        return None

    file_url = status.get("downloadUrl")
    if not file_url:
        return None
    nome = status.get("fileName") or "ficheiro"
    # sanidade: sem extensão conhecida → não é um ficheiro direto útil
    dest = os.path.join(dest_dir, re.sub(r"[^\w.\- ]", "_", nome)[:80] or "ficheiro")

    import urllib.request
    req = urllib.request.Request(
        "https://mv.tools" + file_url,
        headers={"User-Agent": headers["User-Agent"], "Referer": "https://mv.tools/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as fh:
            total = 0
            while True:
                bloco = resp.read(65536)
                if not bloco:
                    break
                total += len(bloco)
                if total > TG_FILE_LIMIT:
                    fh.close()
                    os.remove(dest)
                    raise ValueError("mv.tools: ficheiro excede o limite do Telegram")
                fh.write(bloco)
    except Exception:
        if os.path.exists(dest):
            try:
                os.remove(dest)
            except OSError:
                pass
        return None
    if total < 100:
        os.remove(dest)
        return None
    return dest, nome


_YT_VID_RE = re.compile(r"(?:youtu\.be/|youtube\.com/(?:embed/|live/|shorts/)|[?&]v=)([A-Za-z0-9_-]{11})")


def _yt_video_id(url: str) -> str | None:
    m = _YT_VID_RE.search(url)
    return m.group(1) if m else None


def _y2mate_mp3_download(url: str, dest_dir: str, fmt: str = "mp3") -> tuple[str, str] | None:
    """Descarrega MP3 ou MP4 via y2mate.gs (API eta.etacloud.org).

    Fluxo (extraído do y2mate.js do site):
      auth → init (Bearer key) → convert?v=ID&f=FMT (segue redirects)
      → poll progressURL até progress==3 → download do downloadURL
    Devolve (caminho_do_ficheiro, titulo) ou None em caso de falha.
    """
    vid = _yt_video_id(url)
    if not vid:
        return None

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Referer": "https://y2mate.gs/",
        "Origin": "https://y2mate.gs",  # sem Origin o init devolve 403
        "Accept": "application/json",
    }

    # A API passou a exigir api_key no auth — o site injeta-a no HTML (var apiKey='...')
    api_key = "e4b503d6ae10c35b1d3ee822c807d2f5"
    try:
        import urllib.request as _ur
        _req = _ur.Request("https://y2mate.gs/", headers={"User-Agent": headers["User-Agent"]})
        with _ur.urlopen(_req, timeout=20) as _resp:
            _home = _resp.read().decode("utf-8", errors="replace")
        _mk = re.search(r"apiKey\s*=\s*['\"]([0-9a-f]{32})['\"]", _home)
        if _mk:
            api_key = _mk.group(1)
    except Exception:
        pass  # usa a chave conhecida como fallback

    def _get_json(u: str, extra: dict | None = None) -> dict:
        import urllib.request
        h = dict(headers)
        if extra:
            h.update(extra)
        req = urllib.request.Request(u, headers=h)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    now = int(time.time() * 1000)
    # 1. auth (agora exige api_key — injetada no HTML do site)
    auth = _get_json(f"https://eta.etacloud.org/api/v1/auth?api_key={api_key}&_={now}")
    key = auth.get("key")
    if not key:
        return None
    # 2. init
    init = _get_json(f"https://eta.etacloud.org/api/v1/init?_={int(time.time() * 1000)}", {"Authorization": f"Bearer {key}"})
    convert_url = init.get("convertURL")
    if not convert_url:
        return None
    # 3. convert (segue redirects — o 1.º pedido pode devolver redirect==1)
    step: dict = {}
    u = convert_url
    for _ in range(3):
        step = _get_json(f"{u}&v={vid}&f={fmt}&_={int(time.time() * 1000)}")
        if step.get("redirect") == 1 and step.get("redirectURL"):
            u = step["redirectURL"]
            continue
        break
    if step.get("error"):
        return None
    purl = step.get("progressURL")
    durl = step.get("downloadURL")
    titulo = str(step.get("title") or vid)
    if not durl:
        return None
    # 4. poll progress até 3 (máx ~120s — conversões "frias" demoram mais)
    if purl:
        for _ in range(30):
            time.sleep(4)
            p = _get_json(f"{purl}&_={int(time.time() * 1000)}")
            if p.get("progress") == 3:
                durl = p.get("downloadURL") or durl
                titulo = str(p.get("title") or titulo)
                break
            if p.get("error"):
                return None
        else:
            return None
    # 5. download (usa downloadURL do convert com v/f/r como o site faz)
    import urllib.request
    final_url = f"{durl}&v={vid}&f={fmt}&r=y2mate.gs"
    req = urllib.request.Request(final_url, headers={
        "User-Agent": headers["User-Agent"],
        "Referer": "https://y2mate.gs/",
    })
    dest = os.path.join(dest_dir, f"y2mate.{fmt}")
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
        total = 0
        while True:
            bloco = resp.read(65536)
            if not bloco:
                break
            total += len(bloco)
            if total > TG_FILE_LIMIT:
                fh.close()
                os.remove(dest)
                raise ValueError("y2mate: ficheiro excede o limite do Telegram")
            fh.write(bloco)
    if total < 10_000:  # menos de 10 KB não é um ficheiro válido
        os.remove(dest)
        return None
    return dest, titulo


def _loader_mp3_download(url: str, dest_dir: str, fmt: str = "mp3") -> tuple[str, str] | None:
    """Descarrega áudio/vídeo do YouTube via loader.to (API video-download-api.com).

    Fluxo: download.php?format=&url=&api= → progress_url (poll até progress==1000)
    → download_url. É uma 2.ª fonte independente do y2mate para o /mp3.
    Devolve (caminho, titulo) ou None em caso de falha.
    """
    import urllib.parse
    import urllib.request

    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    api_key = "dfcb6d76f2f6a9894gjkege8a4ab232222"
    h = {"User-Agent": ua, "Accept": "application/json", "Referer": "https://loader.to/"}

    def _gj(u: str) -> dict:
        req = urllib.request.Request(u, headers=h)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    q = urllib.parse.quote(url, safe="")
    try:
        r = _gj(f"https://loader.to/ajax/download.php?format={fmt}&url={q}&api={api_key}")
    except Exception:
        return None
    if not r.get("id"):
        return None
    purl = str(r.get("progress_url") or f"https://p.oceansaver.in/ajax/progress.php?id={r['id']}")
    sep = "&" if "?" in purl else "?"
    durl, titulo = "", str(r.get("title") or "")
    for _ in range(30):  # máx ~120s
        time.sleep(4)
        try:
            p = _gj(f"{purl}{sep}_={int(time.time() * 1000)}")
        except Exception:
            continue  # falha transitória de rede/DNS — tenta de novo
        if p.get("progress") == 1000:
            durl = str(p.get("download_url") or "")
            titulo = str(p.get("title") or titulo)
            break
    if not durl:
        return None

    req = urllib.request.Request(durl, headers={"User-Agent": ua, "Referer": "https://loader.to/"})
    dest = os.path.join(dest_dir, f"loader.{fmt}")
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
        total = 0
        while True:
            bloco = resp.read(65536)
            if not bloco:
                break
            total += len(bloco)
            if total > TG_FILE_LIMIT:
                fh.close()
                os.remove(dest)
                raise ValueError("loader: ficheiro excede o limite do Telegram")
            fh.write(bloco)
    if total < 10_000:
        os.remove(dest)
        return None
    return dest, (titulo or "áudio")


def _cobalt_download(url: str, dest_dir: str, audio_only: bool = False) -> tuple[str, str] | None:
    """Descarrega media via cobalt (instância comunitária co.otomir23.me).

    Multi-plataforma: TikTok, X/Twitter, Instagram, SoundCloud, Bluesky, etc.
    Fluxo: POST {url, downloadMode} → tunnel/redirect → download do media.
    Devolve (caminho, titulo) ou None em caso de falha.
    """
    import urllib.request as _ur

    api = "https://co.otomir23.me/"
    body = json.dumps(
        {"url": url, "filenameStyle": "basic", "downloadMode": "audio" if audio_only else "auto"}
    ).encode("utf-8")
    try:
        req = _ur.Request(
            api,
            data=body,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with _ur.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    if data.get("status") not in ("tunnel", "redirect", "local") or not data.get("url"):
        return None

    media_url = data["url"]
    filename = re.sub(r"[^A-Za-z0-9._ -]", "_", str(data.get("filename") or "cobalt_media"))[:80] or "cobalt_media"
    dest = os.path.join(dest_dir, filename)
    total = 0
    try:
        req2 = _ur.Request(media_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with _ur.urlopen(req2, timeout=180) as resp, open(dest, "wb") as fh:
            while True:
                bloco = resp.read(65536)
                if not bloco:
                    break
                total += len(bloco)
                if total > TG_FILE_LIMIT:
                    fh.close()
                    os.remove(dest)
                    raise ValueError("cobalt: ficheiro excede o limite do Telegram")
                fh.write(bloco)
    except ValueError:
        raise
    except Exception:
        try:
            os.remove(dest)
        except OSError:
            pass
        return None
    if total < 10_000:
        try:
            os.remove(dest)
        except OSError:
            pass
        return None
    titulo = filename.rsplit(".", 1)[0].replace("_", " ").strip() or "media"
    return dest, titulo


async def _download_and_send(
    update: Update,
    url: str,
    audio_only: bool,
    thinking_msg,
) -> None:
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="neobot_dl_")
    if audio_only:
        opts = {
            "urls": url,
            "format": "bestaudio/best",
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ],
            "outtmpl": os.path.join(tmpdir, "%(title).80s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": TG_FILE_LIMIT,
            "socket_timeout": 30,
        }
    else:
        opts = {
            "urls": url,
            # vídeo <=720p para caber no limite de 50MB na maioria dos casos
            # YouTube já não serve ficheiros combinados — o ffmpeg junta vídeo+áudio
            "format": "bv*[height<=720][filesize<50M]+ba/b[height<=720]/b[filesize<50M]/b",
            "outtmpl": os.path.join(tmpdir, "%(title).80s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": TG_FILE_LIMIT,
            "socket_timeout": 30,
        }

    try:
        info = await asyncio.to_thread(_run_ytdlp, opts)
    except Exception as exc:
        logger.exception("Falha /download yt-dlp")
        msg = "❌ Não consegui descarregar deste link."
        if "max-filesize" in str(exc).lower() or "larger" in str(exc).lower():
            msg = "❌ O ficheiro é demasiado grande (limite do Telegram: 50 MB para bots). Tenta /mp3 para só o áudio."
        await thinking_msg.edit_text(msg)
        return

    # encontrar o ficheiro descarregado
    import glob as _glob
    files = _glob.glob(os.path.join(tmpdir, "*"))
    if not files:
        await thinking_msg.edit_text("❌ O download falhou — nenhum ficheiro foi criado.")
        return
    ficheiro = max(files, key=os.path.getsize)
    tamanho = os.path.getsize(ficheiro)
    if tamanho > TG_FILE_LIMIT:
        os.remove(ficheiro)
        await thinking_msg.edit_text(
            "❌ O ficheiro excede 50 MB (limite do Telegram para bots). "
            + ("Tenta `/mp3 <url>` para só o áudio." if not audio_only else "")
        )
        return

    titulo = html.escape(str(info.get("title", "media")))[:120]
    dur = info.get("duration")
    dur_txt = f" · {int(dur // 60)}:{int(dur % 60):02d}" if dur else ""
    plataforma = info.get("extractor_key", "?")

    try:
        if audio_only:
            await update.message.reply_audio(
                audio=open(ficheiro, "rb"),
                title=str(info.get("title", "audio"))[:60],
                performer=str(info.get("uploader", ""))[:60] or None,
                caption=f"🎵 {titulo}{dur_txt}",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_video(
                video=open(ficheiro, "rb"),
                caption=f"🎬 {titulo} · {plataforma}{dur_txt}",
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
            )
        await thinking_msg.delete()
    except Exception:
        logger.exception("Falha ao enviar /download")
        await thinking_msg.edit_text("❌ O download terminou, mas o envio falhou (ficheiro demasiado grande?).")
    finally:
        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = " ".join(context.args).strip()
    if not url or not url.startswith("http"):
        await update.message.reply_text(
            "Como usar:\n`/download https://youtube.com/watch?v=...`\n\n"
            "Funciona com YouTube, TikTok, Instagram, Twitter/X, Vimeo, etc.\n"
            "💡 Para só o áudio (música): `/mp3 <url>`",
            parse_mode="Markdown",
        )
        return
    user = update.effective_user
    if user and _rate_limited(user.id):
        await update.message.reply_text("⏳ Muitos downloads seguidos! Espera um pouco.")
        return
    thinking = await update.message.reply_text("⬇️ A descarregar... (pode demorar)")

    # 1.º tentativa: y2mate.gs (rápido e fiável para YouTube — MP4 ≤ 50 MB)
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="neobot_y2v_")
    try:
        resultado = await asyncio.to_thread(_y2mate_mp3_download, url, tmpdir, "mp4")
    except Exception:
        logger.exception("Falha y2mate.gs em /download")
        resultado = None

    if resultado:
        ficheiro, titulo_y2m = resultado
        if os.path.getsize(ficheiro) > TG_FILE_LIMIT:
            try:
                os.remove(ficheiro)
                os.rmdir(tmpdir)
            except OSError:
                pass
            await thinking.edit_text(
                "❌ O vídeo excede o limite de 50 MB do Telegram para bots. Tenta /mp3 para só o áudio."
            )
            return
        titulo = html.escape(titulo_y2m[:120])
        try:
            await update.message.reply_video(
                video=open(ficheiro, "rb"),
                caption=f"🎬 {titulo}",
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
            )
            await thinking.delete()
        except Exception:
            logger.exception("Falha ao enviar vídeo y2mate")
            await thinking.edit_text("❌ O vídeo foi descarregado, mas o envio falhou.")
        finally:
            try:
                os.remove(ficheiro)
                os.rmdir(tmpdir)
            except OSError:
                pass
        return

    # 2.ª tentativa: loader.to (fonte independente para YouTube — MP4 720p)
    try:
        await thinking.edit_text("🔁 A tentar outra fonte de vídeo...")
    except Exception:
        pass
    import tempfile as _tfl
    tmpdir_ld = _tfl.mkdtemp(prefix="neobot_ld_")
    try:
        resultado = await asyncio.to_thread(_loader_mp3_download, url, tmpdir_ld, "720")
    except Exception:
        logger.exception("Falha loader.to em /download")
        resultado = None
    if resultado:
        ficheiro, titulo_ld = resultado
        if os.path.getsize(ficheiro) > TG_FILE_LIMIT:
            try:
                os.remove(ficheiro)
                os.rmdir(tmpdir_ld)
            except OSError:
                pass
            await thinking.edit_text(
                "❌ O vídeo excede o limite de 50 MB do Telegram para bots. Tenta /mp3 para só o áudio."
            )
            return
        titulo = html.escape(titulo_ld[:120])
        try:
            await update.message.reply_video(
                video=open(ficheiro, "rb"),
                caption=f"🎬 {titulo}",
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
            )
            await thinking.delete()
        except Exception:
            logger.exception("Falha ao enviar vídeo loader.to")
            await thinking.edit_text("❌ O vídeo foi descarregado, mas o envio falhou.")
        finally:
            try:
                os.remove(ficheiro)
                os.rmdir(tmpdir_ld)
            except OSError:
                pass
        return

    # 3.ª tentativa: cobalt (TikTok, X/Twitter, Instagram, SoundCloud, Bluesky, etc.)
    import tempfile as _tf
    tmpdir2 = _tf.mkdtemp(prefix="neobot_cb_")
    try:
        resultado = await asyncio.to_thread(_cobalt_download, url, tmpdir2, False)
    except Exception:
        logger.exception("Falha cobalt em /download")
        resultado = None

    if resultado:
        ficheiro, titulo_cb = resultado
        if os.path.getsize(ficheiro) > TG_FILE_LIMIT:
            try:
                os.remove(ficheiro)
                os.rmdir(tmpdir2)
            except OSError:
                pass
            await thinking.edit_text(
                "❌ O ficheiro excede o limite de 50 MB do Telegram para bots. Tenta /mp3 para só o áudio."
            )
            return
        titulo = html.escape(titulo_cb[:120])
        try:
            if ficheiro.lower().endswith(".mp3"):
                await update.message.reply_audio(
                    audio=open(ficheiro, "rb"),
                    title=titulo_cb[:60],
                    caption=f"🎵 {titulo}",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_video(
                    video=open(ficheiro, "rb"),
                    caption=f"🎬 {titulo}",
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                )
            await thinking.delete()
        except Exception:
            logger.exception("Falha ao enviar ficheiro cobalt")
            await thinking.edit_text("❌ O ficheiro foi descarregado, mas o envio falhou.")
        finally:
            try:
                os.remove(ficheiro)
                os.rmdir(tmpdir2)
            except OSError:
                pass
        return

    # 3.ª tentativa: yt-dlp (YouTube, TikTok, Instagram, X, Vimeo e links diretos)
    await thinking.edit_text("🔁 A tentar pelo yt-dlp...")
    await _download_and_send(update, url, audio_only=False, thinking_msg=thinking)


async def cmd_mp3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = " ".join(context.args).strip()
    if not url or not url.startswith("http"):
        await update.message.reply_text(
            "Como usar:\n`/mp3 https://youtube.com/watch?v=...`\n\n"
            "Extrai o áudio em MP3 de vídeos do YouTube, TikTok, etc.",
            parse_mode="Markdown",
        )
        return
    user = update.effective_user
    if user and _rate_limited(user.id):
        await update.message.reply_text("⏳ Muitos downloads seguidos! Espera um pouco.")
        return
    thinking = await update.message.reply_text("🎵 A extrair o áudio via y2mate... (pode demorar)")

    # 1.º tentativa: y2mate.gs (rápido, sem conversão local)
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="neobot_y2m_")
    try:
        resultado = await asyncio.to_thread(_y2mate_mp3_download, url, tmpdir)
    except Exception:
        logger.exception("Falha y2mate.gs em /mp3")
        resultado = None

    if resultado:
        ficheiro, titulo_y2m = resultado
        titulo = html.escape(titulo_y2m[:120])
        try:
            await update.message.reply_audio(
                audio=open(ficheiro, "rb"),
                title=titulo_y2m[:60],
                caption=f"🎵 {titulo}",
                parse_mode=ParseMode.HTML,
            )
            await thinking.delete()
        except Exception:
            logger.exception("Falha ao enviar áudio y2mate")
            await thinking.edit_text("❌ O MP3 foi gerado, mas o envio falhou.")
        finally:
            try:
                os.remove(ficheiro)
                os.rmdir(tmpdir)
            except OSError:
                pass
        return

    # 2.ª tentativa: loader.to (fonte independente, robusta para YouTube)
    try:
        await thinking.edit_text("🔁 A tentar outra fonte de áudio...")
    except Exception:
        pass
    tmpdir2 = tempfile.mkdtemp(prefix="neobot_loader_")
    try:
        resultado = await asyncio.to_thread(_loader_mp3_download, url, tmpdir2)
    except Exception:
        logger.exception("Falha loader.to em /mp3")
        resultado = None
    if resultado:
        ficheiro, titulo_ld = resultado
        titulo = html.escape(titulo_ld[:120])
        try:
            await update.message.reply_audio(
                audio=open(ficheiro, "rb"),
                title=titulo_ld[:60],
                caption=f"🎵 {titulo}",
                parse_mode=ParseMode.HTML,
            )
            await thinking.delete()
        except Exception:
            logger.exception("Falha ao enviar áudio loader.to")
            await thinking.edit_text("❌ O MP3 foi gerado, mas o envio falhou.")
        finally:
            try:
                os.remove(ficheiro)
                os.rmdir(tmpdir2)
            except OSError:
                pass
        return

    # 3.ª tentativa: cobalt áudio (TikTok, SoundCloud, X, etc.)
    import tempfile as _tf2
    tmpdir3 = _tf2.mkdtemp(prefix="neobot_cba_")
    try:
        resultado = await asyncio.to_thread(_cobalt_download, url, tmpdir3, True)
    except Exception:
        logger.exception("Falha cobalt em /mp3")
        resultado = None
    if resultado:
        ficheiro, titulo_cb = resultado
        titulo = html.escape(titulo_cb[:120])
        try:
            await update.message.reply_audio(
                audio=open(ficheiro, "rb"),
                title=titulo_cb[:60],
                caption=f"🎵 {titulo}",
                parse_mode=ParseMode.HTML,
            )
            await thinking.delete()
        except Exception:
            logger.exception("Falha ao enviar áudio cobalt")
            await thinking.edit_text("❌ O áudio foi descarregado, mas o envio falhou.")
        finally:
            try:
                os.remove(ficheiro)
                os.rmdir(tmpdir3)
            except OSError:
                pass
        return

    # fallback: yt-dlp (funciona para mais plataformas, mas mais lento)
    await thinking.edit_text("🔁 y2mate não conseguiu — a tentar pelo yt-dlp...")
    await _download_and_send(update, url, audio_only=True, thinking_msg=thinking)


# --- /music (ACE-Step: geração de música com letras personalizadas) ---

ACE_SPACE = "ACE-Step/Ace-Step-v1.5"
ACE_TOKEN = os.environ.get("HF_TOKEN", "").strip()

# 1 pedido por utilizador a cada 10 min — a quota ZeroGPU é limitada
_music_last: dict[int, float] = {}


def _ace_client():
    """Cria um gradio_client para o Space do ACE-Step (com HF token se houver)."""
    from gradio_client import Client
    return Client(ACE_SPACE, token=ACE_TOKEN or None, verbose=False)


def _ace_generate(prompt: str, lyrics: str, lang: str) -> str:
    """Bloqueante — chama o /generation_wrapper do ACE-Step em modo custom.

    Devolve o URL do MP3 gerado (sample 1) ou levanta exceção.
    """
    c = _ace_client()
    r = c.predict(
        selected_model="acestep-v15-turbo",       # turbo = mais rápido (menos GPU)
        generation_mode="custom",                  # letra fornecida por nós (via Groq)
        simple_query_input=None,
        simple_vocal_language="unknown",
        param_4=prompt,                            # caption: estilo/instrumentação
        param_5=lyrics,                            # letra com tags [verse]/[chorus]
        param_6=0, param_7="", param_8="",
        param_9=lang,                              # idioma vocal (pt/en/…)
        param_10=8,                                # inference steps (turbo)
        param_11=7.0,                              # guidance scale
        param_12=True,                             # seed aleatória
        param_13=-1, param_14=None,
        param_15=-1,                               # duração: automática
        param_16=1,                                # batch=1 (poupa quota GPU)
        param_17=None, param_18=None,
        param_19=0.0, param_20=-1,
        param_23="text2music",
        param_30="mp3",
        param_32=False,                            # sem "thinking" da LM (poupa GPU)
        param_37=False, param_38=False, param_39=False,
        param_47=None,
        api_name="/generation_wrapper",
    )
    # r é um NamedTuple; a 1.ª componente é o Sample 1 (gradio FileData)
    sample1 = r[0]
    url = getattr(sample1, "url", None) or getattr(sample1, "path", None)
    if not url:
        raise RuntimeError("ACE-Step não devolveu áudio")
    return url


async def cmd_music(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tema = " ".join(context.args).strip()
    if not tema:
        await update.message.reply_text(
            "Como usar:\n`/music <tema ou descrição da música>`\n\n"
            "Exemplos:\n"
            "· `/music uma balada sobre saudade de Lisboa`\n"
            "· `/music rap engraçado sobre exames`\n\n"
            "🤖 A letra é escrita pela IA (Groq) e a música gerada pelo "
            "ACE-Step (modelo aberto estilo Suno). Pode demorar 1-2 minutos.",
            parse_mode="Markdown",
        )
        return
    user = update.effective_user
    if user:
        agora = time.time()
        ultimo = _music_last.get(user.id, 0)
        if agora - ultimo < 600:
            falta = int(600 - (agora - ultimo))
            await update.message.reply_text(
                f"⏳ A geração de música usa muita GPU — espera {falta // 60}m{falta % 60:02d}s e tenta outra vez."
            )
            return
        _music_last[user.id] = agora

    thinking = await update.message.reply_text(
        "🎼 A escrever a letra com a IA... (depois gero a música — pode demorar 1-2 min)"
    )

    # 1) Groq escreve a letra personalizada em formato ACE-Step
    system = (
        "Escreve a letra completa de uma canção original em português de Portugal. "
        "Usa EXATAMENTE este formato com tags em inglês e versos curtos (2-4 linhas cada): "
        "[intro]\n[verse]\n...\n[chorus]\n...\n[verse]\n...\n[chorus]\n...\n[bridge]\n...\n[chorus]\n[outro]\n "
        "A letra deve ter no máximo 24 linhas no total. Não incluas nenhum outro texto, títulos ou comentários."
    )
    user_prompt = (
        f"Tema/pedido: {tema}\n\n"
        "Responde também, na 1.ª linha antes da letra, uma descrição curta do estilo musical "
        "em inglês no formato exato: STYLE: <descrição em inglês com género, ritmo e instrumentos>"
    )
    try:
        raw = await _groq_chat(GROQ_MODEL, system, user_prompt, max_tokens=700)
    except Exception:
        logger.exception("Groq falhou em /music")
        await thinking.edit_text("❌ Não consegui escrever a letra (erro da IA de texto). Tenta outra vez.")
        return

    style = "upbeat pop song with synth and energetic drums, catchy female vocals"
    letra = raw
    m = re.search(r"STYLE:\s*(.+)", raw)
    if m:
        style = m.group(1).strip()[:200]
        letra = raw[m.end():].strip()
    letra = re.sub(r"\n{3,}", "\n\n", letra).strip()
    if not letra:
        await thinking.edit_text("❌ A letra saiu vazia — tenta reformular o tema.")
        return

    await thinking.edit_text(
        f"🎵 Letra pronta ({len(letra.splitlines())} linhas)!\n🎚 Estilo: {style[:100]}\n\n"
        "🎹 A gerar a música no ACE-Step... (1-2 minutos)"
    )

    # 2) ACE-Step gera o áudio (bloqueante → thread)
    import tempfile as _tfd
    tmpdir = _tfd.mkdtemp(prefix="neobot_music_")
    try:
        audio_url = await asyncio.to_thread(_ace_generate, style, letra, "pt")
    except Exception as exc:
        logger.exception("ACE-Step falhou em /music")
        if "quota" in str(exc).lower() or "GPU" in str(exc):
            await thinking.edit_text(
                "⏳ GPU gratuita esgotada — a gerar uma versão instrumental na CPU do servidor "
                "(MusicGen, pode demorar até 5 min)..."
            )
            mg = await asyncio.to_thread(
                _musicgen_fallback, f"{style}. Instrumental, sem voz. Tema: {tema}", tmpdir, 15
            )
            if mg:
                try:
                    await update.message.reply_audio(
                        audio=open(mg, "rb"),
                        title=tema[:60],
                        performer="NEOBOT · MusicGen",
                        caption=(
                            f"🎼 {html.escape(tema[:60])}\n🎚 {html.escape(style[:100])}\n\n"
                            "🤖 Versão instrumental (fallback CPU). A versão cantada volta quando a GPU gratuita repor."
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                    await thinking.delete()
                except Exception:
                    logger.exception("Falha ao enviar fallback /music")
                    await thinking.edit_text("❌ A música instrumental foi gerada mas o envio falhou.")
                finally:
                    try:
                        os.remove(mg)
                    except OSError:
                        pass
                return
            await thinking.edit_text(
                "⏳ A quota de GPU esgotou-se e o fallback instrumental também não conseguiu correr. "
                "Tenta outra vez mais tarde (a quota repõe-se em ~24h)."
            )
        else:
            await thinking.edit_text("❌ A geração de música falhou. Tenta outra vez dentro de alguns minutos.")
        return

    # 3) descarregar e enviar
    import urllib.request as _ur
    dest = os.path.join(tmpdir, "neobot_music.mp3")
    try:
        req = _ur.Request(audio_url, headers={"User-Agent": "Mozilla/5.0"})
        with _ur.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
            fh.write(resp.read())
        if os.path.getsize(dest) > TG_FILE_LIMIT:
            await thinking.edit_text(
                f"✅ Música gerada, mas o ficheiro excede 50 MB. Ouve aqui: {audio_url}"
            )
            return
        titulo = html.escape(tema[:60])
        await update.message.reply_audio(
            audio=open(dest, "rb"),
            title=tema[:60],
            performer="NEOBOT · ACE-Step",
            caption=f"🎼 {titulo}\n🎚 {html.escape(style[:100])}",
            parse_mode=ParseMode.HTML,
        )
        await thinking.delete()
    except Exception:
        logger.exception("Falha ao enviar /music")
        await thinking.edit_text(f"✅ Música gerada, mas o envio falhou. Ouve aqui: {audio_url}")
    finally:
        for f in [dest]:
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


# --- /video (texto→vídeo e imagem→vídeo via LTX-Video + avatar via EchoMimic) ---

VIDEO_NEGATIVE = "worst quality, inconsistent motion, blurry, jittery, distorted"


def _musicgen_fallback(prompt: str, out_dir: str, segundos: int = 15) -> str | None:
    """Fallback do /music: MusicGen (Meta) a correr NA CPU do servidor — sem ZeroGPU,
    sem quota. Gera música instrumental. Tenta os dois nomes do pacote (audiocraft
    antigo / supra novo). Devolve o caminho do áudio ou None."""
    import subprocess as sp
    import shutil as _sh
    py = _sh.which("python") or "python"
    candidatos = [
        [py, "-m", "audiocraft.generate", "--model", "facebook/musicgen-small",
         "--texts", prompt, "--duration", str(segundos), "--output-dir", out_dir],
        [py, "-m", "supra.engine.cli", "/generate", "--texts", prompt,
         "--duration", str(segundos), "--output-dir", out_dir],
    ]
    env = {**os.environ, "PYTORCH_JIT": "0"}
    for cmd in candidatos:
        try:
            r = sp.run(cmd, capture_output=True, text=True, timeout=540, env=env)
        except (sp.TimeoutExpired, FileNotFoundError, OSError):
            continue
        if r.returncode != 0:
            logger.debug("MusicGen cmd falhou (%s): %s", cmd[2], r.stderr[-300:] if r.stderr else "?")
            continue
        for root, _dirs, files in os.walk(out_dir):
            for f in files:
                if f.lower().endswith((".mp3", ".wav", ".flac")):
                    return os.path.join(root, f)
    logger.error("MusicGen fallback não produziu áudio")
    return None


def _kenburns_video(image_path: str, out_path: str, segundos: int = 8, size: str = "720x720") -> bool:
    """Fallback do /video: efeito Ken Burns (zoom lento cinematográfico) com ffmpeg —
    CPU pura, sem quota nenhuma. A imagem 'ganha vida' com movimento de câmara."""
    import subprocess as sp
    w, h = size.split("x")
    frames = segundos * 24
    try:
        r = sp.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-loop", "1", "-i", image_path,
             "-vf", (
                 f"scale={int(w)*2}:-2,"
                 f"zoompan=z='min(zoom+0.0013,1.3)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                 f"d={frames}:s={size}:fps=24"
             ),
             "-t", str(segundos),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
             "-movflags", "+faststart",
             out_path],
            capture_output=True, text=True, timeout=240,
        )
        ok = r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000
        if not ok:
            logger.error("Ken Burns falhou: %s", r.stderr[-300:] if r.stderr else "?")
        return ok
    except (sp.TimeoutExpired, FileNotFoundError, OSError):
        logger.exception("Ken Burns: ffmpeg indisponível ou timeout")
        return False


def _hf_token_from_env() -> str | None:
    tok = os.environ.get("HF_TOKEN")
    if tok:
        return tok
    try:
        env = open(".env", encoding="utf-8").read()
    except OSError:
        return None
    for line in env.splitlines():
        if line.startswith("HF_TOKEN="):
            return line.split("=", 1)[1].strip() or None
    return None


def _download_video_payload(url: str, timeout_s: int = 90) -> bytes | None:
    """Descarrega o vídeo gerado (url do FileData do Gradio)."""
    try:
        resp = httpx.get(url, timeout=timeout_s, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return resp.content
    except Exception:
        logger.exception("Falha ao descarregar vídeo gerado: %s", url)
        return None


def _run_ltx(
    prompt: str,
    image_path: str | None = None,
    duration: float = 3.0,
    height: float = 512,
    width: float = 704,
) -> tuple[bytes, str] | None:
    """Gera vídeo com o Space LTX-Video destilado (texto→vídeo ou imagem→vídeo).
    Devolve (bytes_do_video, url) ou None."""
    try:
        from gradio_client import Client, handle_file
    except ImportError:
        logger.error("gradio_client não instalado")
        return None
    tok = _hf_token_from_env()
    client = Client("Lightricks/ltx-video-distilled", token=tok, verbose=False)
    api = "/image_to_video" if image_path else "/text_to_video"
    common = dict(
        prompt=prompt,
        negative_prompt=VIDEO_NEGATIVE,
        height_ui=height,
        width_ui=width,
        mode="image-to-video" if image_path else "text-to-video",
        duration_ui=duration,
        ui_frames_to_use=9,
        seed_ui=42,
        randomize_seed=True,
        ui_guidance_scale=3.0,
        improve_texture_flag=False,
    )
    if image_path:
        common["input_image_filepath"] = handle_file(image_path)
    result = client.predict(**common, api_name=api)
    video_url = result[0]["video"]["url"] if isinstance(result, tuple) else result["video"]["url"]
    payload = _download_video_payload(video_url)
    if not payload:
        return None
    return payload, video_url


def _run_echo_avatar(image_path: str, audio_path: str) -> tuple[bytes, str] | None:
    """Avatar falante: EchoMimic anima a imagem com o áudio (lip-sync)."""
    try:
        from gradio_client import Client, handle_file
    except ImportError:
        return None
    tok = _hf_token_from_env()
    client = Client("fffiloni/EchoMimic", token=tok, verbose=False)
    result = client.predict(
        uploaded_img=handle_file(image_path),
        uploaded_audio=handle_file(audio_path),
        width=512, height=512, length=1200, seed=420,
        facemask_dilation_ratio=0.1, facecrop_dilation_ratio=0.5,
        context_frames=12, context_overlap=3, cfg=2.5, steps=30,
        sample_rate=16000, fps=24, device="cuda",
        api_name="/generate_video",
    )
    video_url = result[0]["video"]["url"] if isinstance(result, tuple) else result["video"]["url"]
    payload = _download_video_payload(video_url)
    if not payload:
        return None
    return payload, video_url


def _google_tts_mp3(text: str, lang: str = "pt") -> bytes | None:
    """Google TTS (o mesmo do /audio) — para a voz do avatar."""
    try:
        resp = httpx.get(
            "https://translate.google.com/translate_tts",
            params={"ie": "UTF-8", "q": text[:190], "tl": lang, "client": "tw-ob"},
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        if resp.content[:3] != b"ID3" and resp.content[:2] not in (b"\xff\xf3", b"\xff\xf2"):
            return None
        return resp.content
    except Exception:
        logger.exception("TTS falhou em /video")
        return None


def _video_quota_msg(exc: Exception) -> str | None:
    """Detecta erro de quota ZeroGPU para mensagem amigável."""
    s = str(exc)
    if "ZeroGPU quota" in s or "GPU duration" in s or "quota" in s.lower():
        m = re.search(r"Try again in ([\dh :]+)", s)
        quando = m.group(1).strip() if m else "~21 horas"
        return f"⏳ A quota gratuita de GPU esgotou-se por agora. Repõe-se em {quando} — tenta depois!"
    return None


async def cmd_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gera vídeo: texto→vídeo, imagem→vídeo (responder a uma imagem) ou avatar falante."""
    user = update.effective_user
    args = list(context.args or [])
    reply_img = None
    if update.message and update.message.reply_to_message and update.message.reply_to_message.photo:
        try:
            f = await update.message.reply_to_message.photo[-1].get_file()
            p = tempfile.mkstemp(suffix=".jpg")[1]
            await f.download_to_drive(p)
            reply_img = p
        except Exception:
            logger.exception("Falha ao obter imagem respondida")
    sub = (args[0].lower() if args else "")
    texto = " ".join(args[1:]) if sub in ("texto", "t", "avatar", "a") and len(args) > 1 else " ".join(args)

    if sub == "avatar" or sub == "a":
        if not texto:
            await update.message.reply_text(
                "Como usar o avatar falante:\n"
                "· Responde a uma foto com: `/avatar olá a todos!`\n"
                "· `/avatar [descrição do retrato] frase` — gera o retrato e anima"
            )
            return
        thinking = await update.message.reply_text("🧑‍🎤 A preparar o avatar (retrato + voz + animação)...")
        img_path = reply_img
        if not img_path:
            desc = "professional portrait of a friendly person, natural lighting"
            try:
                u = (
                    "https://image.pollinations.ai/prompt/" + urllib.parse.quote(
                        f"{desc}, realistic portrait photo, front facing, neutral expression, head and shoulders"
                    )
                    + f"?width=512&height=512&nologo=true&model=z-image&seed={random.randint(1, 999999)}"
                )
                resp = await _http_get(u, timeout=120)
                fd, img_path = tempfile.mkstemp(suffix=".jpg")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(resp.content)
            except Exception:
                logger.exception("Falha ao gerar retrato /video avatar")
                await thinking.edit_text("❌ Não consegui gerar o retrato do avatar. Tenta outra vez.")
                return
        audio = _google_tts_mp3(texto)
        if not audio:
            await thinking.edit_text("❌ Não consegui gerar a voz do avatar. Tenta outra vez.")
            return
        fd, audio_path = tempfile.mkstemp(suffix=".mp3")
        with os.fdopen(fd, "wb") as fh:
            fh.write(audio)
        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(None, _run_echo_avatar, img_path, audio_path)
        except Exception as exc:
            logger.exception("EchoMimic falhou em /video")
            if _video_quota_msg(exc):
                # Fallback sem GPU: Ken Burns sobre o retrato + a voz como nota de voz
                await thinking.edit_text(
                    "⏳ GPU gratuita esgotada — a gerar versão alternativa: "
                    "retrato com movimento cinematográfico + a voz separada (CPU)..."
                )
                kb = os.path.join(tempfile.gettempdir(), f"neobot_kb_{random.randint(1,999999)}.mp4")
                sent_any = False
                if _kenburns_video(img_path, kb, 8):
                    try:
                        await update.message.reply_video(
                            video=open(kb, "rb"),
                            caption=f"🗣 Avatar (modo CPU)\n💬 {html.escape(texto[:150])}",
                            parse_mode=ParseMode.HTML,
                        )
                        sent_any = True
                    except Exception:
                        logger.exception("Falha ao enviar Ken Burns avatar")
                try:
                    await update.message.reply_voice(voice=audio, caption=f"🎙 {html.escape(texto[:120])}")
                    sent_any = True
                except Exception:
                    logger.exception("Falha ao enviar voz avatar")
                try:
                    os.remove(kb)
                except OSError:
                    pass
                if sent_any:
                    await thinking.delete()
                    return
                await thinking.edit_text("❌ Não consegui gerar a versão alternativa agora. Tenta mais tarde.")
                return
            await thinking.edit_text("❌ A animação do avatar falhou agora. Tenta outra vez em instantes.")
            return
        finally:
            for tmp in (audio_path,):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if not res:
            await thinking.edit_text("❌ A animação do avatar falhou agora. Tenta outra vez em instantes.")
            return
        payload, url = res
        caption = f"🗣 Avatar falante\n💬 {html.escape(texto[:150])}"
        try:
            await update.message.reply_video(video=payload, caption=caption, parse_mode=ParseMode.HTML)
            await thinking.delete()
        except Exception:
            logger.exception("Falha ao enviar avatar")
            await thinking.edit_text(f"🎬 O avatar foi gerado mas excede o limite do Telegram. Vê aqui:\n{url}")
        return

    # --- texto→vídeo / imagem→vídeo (LTX) ---
    if not texto:
        await update.message.reply_text(
            "Como usar:\n"
            "· `/video <descrição>` — gera vídeo a partir de texto\n"
            "· Responde a uma imagem com `/video faz-la mover-se` — anima a imagem\n"
            "· `/avatar <frase>` — avatar falante (responde a uma foto ou descreve o retrato)"
        )
        return
    if user and _rate_limited(user.id):
        await update.message.reply_text("⏳ Muitos pedidos seguidos! Espera um pouco.")
        return
    thinking = await update.message.reply_text(
        "🎬 A gerar o vídeo (1–3 min, a IA está a renderizar)..." if not reply_img
        else "🎬 A animar a imagem (1–3 min)..."
    )
    img_for_ltx = reply_img
    if not img_for_ltx and re.match(r"^https?://\S+\.(jpe?g|png|webp)(\?|$)", texto, re.I):
        try:
            resp = await _http_get(texto, timeout=60)
            fd, img_for_ltx = tempfile.mkstemp(suffix=".jpg")
            with os.fdopen(fd, "wb") as fh:
                fh.write(resp.content)
            texto = "A cena da imagem ganha vida com movimento suave"
        except Exception:
            logger.exception("Falha ao baixar imagem-URL em /video")
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, _run_ltx, texto, img_for_ltx, 3.0, 512.0, 704.0)
    except Exception as exc:
        logger.exception("LTX falhou em /video")
        if _video_quota_msg(exc):
            # Fallback sem GPU: Ken Burns (zoom cinematográfico) sobre uma imagem
            await thinking.edit_text(
                "⏳ GPU gratuita esgotada — a gerar vídeo alternativo na CPU "
                "(imagem gerada por IA + movimento de câmara)...")
            kb_img = img_for_ltx
            kb_tmp = None
            if not kb_img:
                try:
                    u = (
                        "https://image.pollinations.ai/prompt/" + urllib.parse.quote(texto[:300])
                        + f"?width=1024&height=1024&nologo=true&model=z-image&seed={random.randint(1, 999999)}"
                    )
                    resp = await _http_get(u, timeout=120)
                    fd, kb_tmp = tempfile.mkstemp(suffix=".jpg")
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(resp.content)
                    kb_img = kb_tmp
                except Exception:
                    logger.exception("Falha ao gerar imagem p/ Ken Burns")
            if kb_img:
                kb = os.path.join(tempfile.gettempdir(), f"neobot_kb_{random.randint(1,999999)}.mp4")
                if _kenburns_video(kb_img, kb, 8):
                    try:
                        await update.message.reply_video(
                            video=open(kb, "rb"),
                            caption=(
                                f"🎬 {html.escape(texto[:120])}\n"
                                "🤖 Modo CPU: imagem IA + movimento de câmara (Ken Burns). "
                                "O vídeo gerado por difusão volta quando a GPU gratuita repor."
                            ),
                            parse_mode=ParseMode.HTML,
                        )
                        await thinking.delete()
                    except Exception:
                        logger.exception("Falha ao enviar Ken Burns")
                        await thinking.edit_text("❌ O vídeo alternativo foi gerado mas o envio falhou.")
                    finally:
                        try:
                            os.remove(kb)
                        except OSError:
                            pass
                    return
            await thinking.edit_text(
                "⏳ A quota de GPU esgotou-se e o modo alternativo também falhou. Tenta mais tarde "
                "(repõe em ~8h) — ou usa /image entretanto."
            )
            return
        await thinking.edit_text("❌ A geração de vídeo falhou agora. Tenta outra vez em instantes.")
        return
    finally:
        if img_for_ltx and img_for_ltx != reply_img:
            try:
                os.remove(img_for_ltx)
            except OSError:
                pass
    if not res:
        await thinking.edit_text("❌ A geração de vídeo falhou agora. Tenta outra vez em instantes.")
        return
    payload, url = res
    modo = "🎞 Imagem → Vídeo" if img_for_ltx else "✍️ Texto → Vídeo"
    caption = f"{modo} · LTX-Video\n💬 {html.escape(texto[:150])}"
    try:
        await update.message.reply_video(video=payload, caption=caption, parse_mode=ParseMode.HTML)
        await thinking.delete()
    except Exception:
        logger.exception("Falha ao enviar vídeo")
        await thinking.edit_text(f"🎬 O vídeo foi gerado mas excede o limite do Telegram. Vê aqui:\n{url}")


# --- /radio ---

RADIO_ESTACOES = [
    # (nome, stream_url)
    ("Rádio Comercial", "https://stream-icy.bauermedia.pt/comercial.mp3"),
    ("TSF Rádio Notícias", "https://directo.tsf.pt/tsfdirecto.mp3"),
    ("RFM", "https://23603.live.streamtheworld.com/RFMAAC.aac"),
    ("Cidade FM", "https://stream-icy.bauermedia.pt/cidade.mp3"),
    ("M80 Rádio", "https://stream-icy.bauermedia.pt/m80.mp3"),
    ("Smooth FM", "https://stream-icy.bauermedia.pt/smooth.aac"),
    ("Antena 1 (RTP)", "http://streaming-live-app.rtp.pt/liveradio/antena180a/playlist.m3u8"),
    ("Rádio Observador", "http://195.23.85.126:8455/listen.pls?sid=1"),
    ("Radar", "http://proic1.redeaudio.com/radar_aac"),
    ("Rádio Amália (Fado)", "http://centova.radio.com.pt:9496/;"),
]


async def cmd_radio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    linhas = [
        "📻 <b>Rádios Portuguesas</b> — toca no navegador:",
        "",
        "🌟 <b>Em destaque — Rádio parceira HellGate:</b>",
        "🎛 <a href=\"https://hellgate-radio.duckdns.org/radio\">HellGate Radio — OUVIR</a>",
        "🌐 Página da rádio: https://hellgate-radio.duckdns.org",
        "",
        "🎧 <b>Outras rádios:</b>",
    ]
    for nome, url in RADIO_ESTACOES:
        linhas.append(f"▸ {html.escape(nome)} — {url}")
    linhas += [
        "",
        "💡 Toca num link para abrir o stream (funciona no VLC, browser, etc.).",
    ]
    await update.message.reply_text("\n".join(linhas), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# --- /streamhub (hub de sites de streaming: filmes, séries e canais IPTV) ---

STREAMHUB_HUBS = [
    ("NetFly", "https://netflyapp.com/pt"),
    ("Lokke", "https://lokke.app/download"),
    ("FMHY — a maior lista de streaming", "https://fmhy.net/video"),
]

STREAMHUB_FILMES = [
    ("MegaFlix", "https://megaflix.store/"),
    ("MegaTuga", "https://megatuga.io/"),
    ("StreamGoblin", "https://streamgoblin.com/"),
    ("Cinegram", "https://cinegram.net/"),
    ("WarezTuga", "https://wareztuga.io/"),
    ("Tugaflix", "https://tugaflix.site/"),
    ("TugaStream", "https://tugastream.top/"),
    ("Mirana TV", "https://mirana.tv/"),
    ("VisionCine", "https://visioncine.stream/"),
    ("FMovies", "https://www.fmovies.gd/"),
    ("Cineby", "https://www.cineby.gd/"),
    ("BrocoFlix", "https://brocoflix.xyz/"),
    ("BitCine", "https://www.bitcine.app/"),
    ("CineHD", "https://cinehd.cc/"),
    ("TopFilmeOnline", "https://topfilmeonline.org/"),
    ("StreamIMDB", "https://streamimdb.ru/"),
    ("Encontrei", "https://encontrei.info/"),
    ("Overflix", "https://www.overflix.tires/"),
    ("Vizer", "https://www.vizer.men"),
    ("PobreflixTV", "https://www.pobreflixtv.locker"),
    ("PixelFlix", "https://pixelflix.cc/"),
    ("TugaStreams", "https://tugastreams.st/"),
]

STREAMHUB_IPTV = [
    ("SportsOnline — programação (vc)", "https://sportsonline.vc/prog.txt"),
    ("SportsOnline — programação (pk)", "https://sportsonline.pk/prog.txt"),
    ("SportsOnline — programação (cx)", "https://sportsonline.cx/prog.txt"),
    ("IPTV Web", "https://iptv-web.app/#PH"),
    ("Worlds TV Mobile — desporto", "https://worldstvmobile.com/category/sports"),
    ("Rebel Pirate TV", "https://rebel-pirate-tv.vercel.app/"),
    ("NEO IPTV ⭐", "https://ptlegion.itch.io/neo-iptv"),
]

STREAMHUB_BTV = [
    ("BTV ao vivo", "https://sportzonline.click/channels/pt/btv.php"),
]


def _streamhub_sec(titulo: str, sites: list[tuple[str, str]]) -> list[str]:
    linhas = [f"", f"<b>{titulo}</b>"]
    for nome, url in sites:
        # Nome como hiperligação — o URL fica oculto (sem preview de link)
        linhas.append(f"▸ <a href=\"{url}\">{html.escape(nome)}</a>")
    return linhas


async def cmd_streamhub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lista de referência de sites de streaming (filmes, séries, IPTV)."""
    cat = " ".join(context.args).strip().lower() if context.args else ""
    texto = "🌐 <b>NEOBOT StreamHub</b> — Sites de Streaming Top List 2026\n"
    if cat in ("", "tudo", "all"):
        texto += "".join(
            _streamhub_sec("🌟 Hubs / Top list", STREAMHUB_HUBS)
            + _streamhub_sec("🎬 Filmes & Séries", STREAMHUB_FILMES)
            + _streamhub_sec("📡 IPTV — Canais & Desporto", STREAMHUB_IPTV)
            + _streamhub_sec("📺 BTV", STREAMHUB_BTV)
        )
    elif cat.startswith("filme") or cat.startswith("serie") or cat == "séries":
        texto += "".join(
            _streamhub_sec("🌟 Hubs / Top list", STREAMHUB_HUBS)
            + _streamhub_sec("🎬 Filmes & Séries", STREAMHUB_FILMES)
        )
    elif "iptv" in cat or "canal" in cat:
        texto += "".join(_streamhub_sec("📡 IPTV — Canais & Desporto", STREAMHUB_IPTV))
    elif "btv" in cat:
        texto += "".join(_streamhub_sec("📺 BTV", STREAMHUB_BTV))
    else:
        texto += (
            "\nCategoria não reconhecida. Usa:\n"
            "· <code>/streamhub</code> — lista completa\n"
            "· <code>/streamhub filmes</code> — sites de filmes e séries\n"
            "· <code>/streamhub iptv</code> — canais IPTV\n"
            "· <code>/streamhub btv</code> — canal BTV"
        )
    texto += "\n\n📡 Free Knowledge is the most Powerful Weapon 💻"
    await update.message.reply_text(texto, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# --- /phone ---

async def cmd_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    numero = " ".join(context.args).strip()
    if not numero:
        await update.message.reply_text("Como usar:\n`/phone +351912345678`", parse_mode="Markdown")
        return
    if phonenumbers is None:
        await update.message.reply_text("❌ O módulo `phonenumbers` não está instalado neste servidor.")
        return
    try:
        parsed = phonenumbers.parse(numero, None)
    except phonenumbers.NumberParseException as exc:
        await update.message.reply_text(f"❌ Número inválido: {exc}")
        return
    if not phonenumbers.is_valid_number(parsed):
        await update.message.reply_text("❌ Este número não parece ser válido.")
        return
    regiao = _pn_geocoder.description_for_number(parsed, "pt") or "?"
    operadora = _pn_carrier.name_for_number(parsed, "pt") or "?"
    fusos = ", ".join(_pn_timezone.time_zones_for_number(parsed)) or "?"
    tipo = phonenumbers.number_type(parsed)
    tipos = {
        phonenumbers.PhoneNumberType.MOBILE: "📱 Móvel",
        phonenumbers.PhoneNumberType.FIXED_LINE: "☎️ Fixo",
        phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "☎️/📱 Fixo ou móvel",
        phonenumbers.PhoneNumberType.VOIP: "💻 VoIP",
        phonenumbers.PhoneNumberType.TOLL_FREE: "🆓 Gratuito",
    }
    texto = (
        f"📞 *{phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)}*\n"
        f"🌍 País: +{parsed.country_code} ({regiao})\n"
        f"🏷️ Tipo: {tipos.get(tipo, 'Outro')}\n"
        f"📡 Operadora: {operadora}\n"
        f"🕓 Fuso: {fusos}"
    )
    try:
        await update.message.reply_text(texto, parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text(texto)


# --- Health server (obrigatório em hospedagem: HF Spaces/Koyeb fazem probe HTTP;
#     sem porta aberta o container é considerado morto) ---
_health_started = False


def _start_health_server() -> None:
    """HTTP mínimo na porta $PORT (8080 por defeito): / e /healthz → 200 OK."""
    global _health_started
    if _health_started:
        return
    _health_started = True
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("NEOBOT online 🤖".encode())

        def log_message(self, *args: object) -> None:  # silencia access log
            pass

    port = int(os.environ.get("PORT", "7860"))
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), _Health)
    except OSError as exc:
        logger.warning("Health server não arrancou na porta %s: %s", port, exc)
        return
    threading.Thread(target=srv.serve_forever, daemon=True, name="health-server").start()
    logger.info("Health server a escutar na porta %s", port)


async def _keepalive_loop(app: Application) -> None:
    """Mantém o serviço acordado: ping à URL pública (cloud) ou local.

    Na cloud (Render/Koyeb), instâncias grátis adormecem sem tráfego — o
    self-ping à URL pública conta como tráfego e mantém o bot 24/7.
    """
    public = _public_url().rstrip("/")
    port = os.environ.get("PORT", "7860")
    url = f"{public}/healthz" if public else f"http://127.0.0.1:{port}/healthz"
    await asyncio.sleep(60)
    while True:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url)
                logger.info("Keepalive self-ping: %s", r.status_code)
        except Exception as exc:  # não deve nunca derrubar o bot
            logger.warning("Keepalive falhou (inofensivo): %s", exc)
        await asyncio.sleep(300)


def _public_url() -> str:
    """URL pública do serviço na cloud, se existir (Render ou Koyeb)."""
    u = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KOYEB_PUBLIC_DOMAIN") or "").strip()
    if u and not u.startswith("http"):
        u = f"https://{u}"
    return u.rstrip("/")


async def _post_init(app: Application) -> None:
    """Lança o keepalive self-ping quando o bot fica operacional."""
    app.create_task(_keepalive_loop(app))


def main() -> None:
    _load_env()
    _acquire_instance_lock()
    token = os.environ.get("NEOBOT_TOKEN", "").strip()
    if not token:
        logger.error("NEOBOT_TOKEN não definido! Exporta a variável de ambiente ou cria um ficheiro .env")
        raise SystemExit(1)

    # read_timeout generoso: um "TimedOut" no polling não deve matar o processo
    app = (
        Application.builder()
        .token(token)
        .get_updates_read_timeout(60.0)
        .get_updates_write_timeout(60.0)
        .get_updates_connect_timeout(60.0)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ajuda", cmd_ajuda))
    app.add_handler(CommandHandler("help", cmd_ajuda))
    app.add_handler(CommandHandler("hora", cmd_hora))
    app.add_handler(CommandHandler("data", cmd_data))
    app.add_handler(CommandHandler("piada", cmd_piada))
    app.add_handler(CommandHandler("dado", cmd_dado))
    app.add_handler(CommandHandler("moeda", cmd_moeda))
    app.add_handler(CommandHandler("escolhe", cmd_escolhe))
    app.add_handler(CommandHandler("google", cmd_google))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("wiki", cmd_wiki))
    app.add_handler(CommandHandler("image", cmd_img))
    app.add_handler(CommandHandler("audio", cmd_audio))
    app.add_handler(CommandHandler("meteo", cmd_meteo))
    app.add_handler(CommandHandler("youtube", cmd_youtube))
    app.add_handler(CommandHandler("crypto", cmd_crypto))
    app.add_handler(CommandHandler("cinema", cmd_cinema))
    app.add_handler(CommandHandler("estreias", cmd_estreias))
    app.add_handler(CommandHandler("imdb", cmd_imdb))
    app.add_handler(CommandHandler("play", cmd_play))
    app.add_handler(CommandHandler("ipinfo", cmd_ipinfo))
    app.add_handler(CommandHandler("ipscan", cmd_ipscan))
    app.add_handler(CommandHandler("iplookup", cmd_iplookup))
    app.add_handler(CommandHandler("phone", cmd_phone))
    app.add_handler(CommandHandler("torrent", cmd_torrent))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler("mp3", cmd_mp3))
    app.add_handler(CommandHandler("radio", cmd_radio))
    app.add_handler(CommandHandler("music", cmd_music))
    app.add_handler(CommandHandler("video", cmd_video))
    app.add_handler(CommandHandler("streamhub", cmd_streamhub))

    # Responde quando alguém escreve 'neobot' numa mensagem de grupo (sem slash)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, neobot_mention))

    # Log handler errors without crashing the bot (network blips, API hiccups)
    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Handler error:", exc_info=context.error)

    app.add_error_handler(_on_error)

    logger.info("🤖 NEOBOT a arrancar...")
    public_url = _public_url()
    if public_url:
        # ☁️ Cloud: modo webhook — cada mensagem do Telegram é tráfego HTTP que
        # chega ao serviço (mantém instâncias grátis acordadas e é mais rápido).
        # O health_path embutido do PTB responde 200 em /healthz para os probes.
        port = int(os.environ.get("PORT", "7860"))
        logger.info("Modo webhook em %s (porta %s)", public_url, port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=token,
            webhook_url=f"{public_url}/{token}",
            health_path="healthz",
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        # 💻 Local: polling + servidor de saúde próprio na mesma porta
        _start_health_server()
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    # Supervisor: se o polling morrer por rede (TimedOut, ConnectionError...),
    # o bot reinicia sozinho em vez de ficar morto até alguém o arrancar de novo.
    import sys as _sys
    tentativa = 0
    while True:
        tentativa += 1
        try:
            if tentativa > 1:
                logger.warning("NEOBOT: tentativa de arranque nº %d", tentativa)
            main()
        except KeyboardInterrupt:
            logger.info("NEOBOT: parado pelo utilizador (Ctrl+C)")
            _sys.exit(0)
        except SystemExit:
            raise
        except Exception:
            logger.exception("NEOBOT: crash inesperado — a reiniciar em 15s")
            time.sleep(15)
