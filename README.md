# 🤖 NEOBOT

Assistente multiuso para Telegram, em Python (`python-telegram-bot`), a falar português de Portugal.
Pesquisa na web, responde por voz, gera imagens, música e vídeo, serve IPTV, descarrega media — e corre
24/7 de graça em GitHub Actions.

> 34 comandos + 4 atalhos, resposta a notas de voz e menção `neobot` em grupos.

## Comandos

### IA, voz e geração

| Comando | O que faz |
|---|---|
| `/start` | Boas-vindas e menu principal |
| `/ajuda` (`/help`) | Lista de comandos |
| `/piada` | Piada aleatória (filosófica, informática ou de hackers) |
| `/ask <pergunta>` | Pergunta à IA com pesquisa web — OpenCode Zen como motor, Groq em fallback. Também aceita responder a uma mensagem com `/ask` |
| `/audio <pergunta>` | Igual ao `/ask`, mas responde em voz pt-PT |
| `/voz <texto>` | Converte texto em voz de Portugal (`/voz boa noite`, `/voz raquel olá!`) |
| *(nota de voz)* | Envia um clip de voz sem comando: transcreve (Whisper) e responde em áudio |
| `/image <prompt>` | Gera imagem — HF `Z-Image-Turbo`, com fallback automático para Pollinations |
| `/music <tema>` | Música original: Lyria (Gemini) primeiro, ACE-Step (HF Space) como alternativa |
| `/video <prompt>` | Vídeo texto→vídeo (Wan2.2 via HF Inference Providers → CogVideoX-5B → LTX-Video destilado; Veo como alternativa). A responder a uma imagem, anima-a (Wan2.2 I2V Lightning) |
| `/opencode <tarefa>` (`/oc`) | Corre uma tarefa de código no OpenCode CLI local, com modelo free do Zen |
| `/opencode_iniciar` (`/oci`) | Abre uma sessão OpenCode contínua (uma por utilizador) |
| `/opencode_continuar <pedido>` (`/occ`) | Continua a sessão (`opencode run --continue`) |
| `/opencode_status` (`/ocs`) | Estado da sessão OpenCode |

### Informação e pesquisa

| Comando | O que faz |
|---|---|
| `/google <pesquisa>` | Pesquisa na internet com resumo por IA |
| `/news [tema]` | Notícias da atualidade |
| `/wiki <termo>` | Pesquisa na Wikipédia |
| `/meteo [cidade]` | Meteorologia (Open-Meteo) |
| `/youtube <pesquisa>` | Resultados do YouTube |
| `/crypto [moeda]` | Cotações de criptomoedas |
| `/cinema` | Filmes em cartaz |
| `/estreias` | Estreias da semana |
| `/imdb <título>` | Informação de um filme/série |
| `/play <url IMDb>` | Link de streaming a partir de um URL do IMDb |

### Rede e segurança

| Comando | O que faz |
|---|---|
| `/ipinfo <ip\|domínio>` | Geolocalização e dono de um IP ou domínio |
| `/ipscan <ip>` | Portas abertas de um IP |
| `/iplookup <ip>` | DNS inverso |
| `/phone <número>` | Operadora, tipo e região de um número (`/phone +351912345678`) |

### Media

| Comando | O que faz |
|---|---|
| `/download <url>` | Descarrega vídeo (YouTube, TikTok…) com o motor Seal: yt-dlp nightly + aria2c + ffmpeg + deno |
| `/mp3 <url>` | Extrai o áudio de um link |
| `/torrent <termo>` | Procura torrents em índices públicos |
| `/radio` | Rádios portuguesas + rádio parceira HellGate |

### IPTV e streaming

| Comando | O que faz |
|---|---|
| `/iptv [filtro]` | Estado da playlist e categorias; `/iptv validar <cat>` valida os streams de uma categoria |
| `/canal <nome>` | Procura canais por nome/categoria; `/canal 2` apanha o 2.º resultado anterior |
| `/webcams [categoria]` | Webcams ao vivo (`/webcams beach`) |
| `/streamhub [categoria]` | Lista de referência de sites de streaming (filmes, séries, IPTV) |
| `/kodi` | Kodi: download oficial, repositório NEO IPTV e player |
| `/capcut [pc\|web]` | Alternativas grátis ao CapCut, com downloads diretos |

### Em grupos

Escrever `neobot` numa mensagem de grupo (sem `/`) devolve uma saudação com o atalho para `/ajuda`.

## Como funciona

### Onde corre

Tudo vive no workflow [`.github/workflows/neobot.yml`](.github/workflows/neobot.yml), num repositório
público (minutos de Actions ilimitados). Cada execução faz checkout, instala Python 3.12, `ffmpeg`,
`aria2`, `deno`, as dependências de `requirements.txt` e o OpenCode CLI, e por fim lança
`python -X utf8 bot.py`.

O GitHub mata qualquer job às **6 horas**, por isso cada execução é um turno.

### Handover entre turnos

O próprio job pede a execução seguinte via `workflow_dispatch` **5h20 depois de arrancar** (usa
`GITHUB_TOKEN` com `actions: write` — `workflow_dispatch` é a exceção documentada em que o token
consegue criar runs). Com `concurrency: neobot` + `cancel-in-progress`, a execução nova mata a antiga,
garantindo **uma só instância** e nunca dois processos no mesmo token.

O `cron: "25 */6 * * *"` é apenas rede de segurança: o agendador do GitHub atrasa execuções agendadas
em horas (já se observaram intervalos reais de 7h a 14h entre arranques), pelo que não serve como
mecanismo principal. Com o handover, o intervalo morto é só o arranque da nova execução (~2–3 min).

### Ligação ao Telegram

- **Polling** (predefinido): `run_polling`, com timeouts de leitura/escrita/conexão a 60 s para que um
  `TimedOut` da rede não derrube o processo.
- **Webhook**: se `RENDER_EXTERNAL_URL` ou `KOYEB_PUBLIC_DOMAIN` estiverem definidos (Render/Koyeb),
  arranca em modo webhook na porta `PORT` (7860), com `url_path` igual ao token e health check em `/healthz`.
- Em polling é ainda levantado um pequeno servidor de saúde na mesma porta.

### Robustez

- `.neobot.lock` (criado com `O_EXCL`) impede duas instâncias com o mesmo token — evita o `409 Conflict`
  do Telegram.
- Supervisor no `if __name__ == "__main__"`: qualquer exceção inesperada é registada e o bot reinicia
  passados 15 s, em vez de ficar morto.
- `add_error_handler` registra erros de handler sem matar o processo.
- Keepalive: self-ping a `/healthz` a cada 5 minutos.

### Estado

Sessões OpenCode e contadores de rate-limit vivem **em memória**: perdem-se em cada handover. Não há
base de dados nem ficheiros persistentes.

## Configuração

### Variáveis de ambiente

| Variável | Obrigatória | Para que serve |
|---|---|---|
| `NEOBOT_TOKEN` | **sim** | Token do bot, obtido no [@BotFather](https://t.me/BotFather) |
| `GROQ_API_KEY` | recomendada | `/ask`, `/news`, `/google`; tradução de prompts do `/video` |
| `HF_TOKEN` | recomendada | `/image`, `/music`, `/video`, transcrição Whisper (Inference Providers + quota ZeroGPU) |
| `NEOBOT_HF_TOKENS` | opcional | Tokens HF extra, para rodar a quota quando a principal esgota |
| `GEMINI_API_KEY` | opcional | Lyria (`/music`) e Veo (`/video`) |
| `OPENROUTER_API_KEY` | opcional | Passada pelo workflow para o `/opencode` |
| `PORT` | opcional | Porta do servidor de saúde/webhook (predefinição `7860`) |
| `RENDER_EXTERNAL_URL` / `KOYEB_PUBLIC_DOMAIN` | opcional | Ativam o modo webhook com essa URL pública |
| `GROQ_MODEL` | opcional | Modelo Groq (predefinição `openai/gpt-oss-120b`) |
| `ASK_RATE_LIMIT` / `ASK_RATE_WINDOW` | opcional | Limite do `/ask` por janela em segundos (predefinição 5 / 60) |
| `WHISPER_MODEL` | opcional | Modelo de transcrição (predefinição `whisper-large-v3-turbo`) |
| `GROQ_SYSTEM_PROMPT`, `GOOGLE_SYSTEM_PROMPT`, `NEWS_SYSTEM_PROMPT` | opcional | Override dos prompts de sistema |

Os segredos do bot vivem em **Settings → Secrets and variables → Actions**: `NEOBOT_TOKEN`,
`GROQ_API_KEY`, `HF_TOKEN`, `NEOBOT_HF_TOKENS`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`.
O handover usa `github.token` e as permissões declaradas no workflow — não precisa de segredo extra.

### Correr localmente (Windows)

```bash
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# .env na raiz do projeto, com NEOBOT_TOKEN=... (e as chaves opcionais acima)
py bot.py
```

Sem `RENDER_EXTERNAL_URL`/`KOYEB_PUBLIC_DOMAIN` arranca em polling. Para parar: `Ctrl+C`
(o lock é libertado no `atexit`).

## Estrutura

| Ficheiro | Papel |
|---|---|
| `bot.py` | Todo o bot: handlers, motores de IA, voz, media e IPTV (~4,6 mil linhas) |
| `iptv.py` | Módulo IPTV — Rebel Pirate TV, SportOnline (25 canais) e TV Garden (por país) |
| `rebel.m3u` | Playlist Rebel gerada (canais `.m3u8` + páginas `#WEB`) |
| `webcams.json` | Catálogo de webcams por categoria |
| `gerar_rebel_m3u.py` | Regenera `rebel.m3u` |
| `gerar_webcams.py` | Regenera `webcams.json` |
| `.github/workflows/neobot.yml` | Hospedagem 24/7 e handover |

Workflows de diagnóstico, todos por `workflow_dispatch` manual: `mp3-test`, `mp3-full-test`,
`music-test`, `video-test`, `seal-test`, `cpu-studio-test`.

## Limites conhecidos

- **Não é um SLA 24/7**: depende do GitHub Actions, com turnos de 6h e ~2–3 min de paragem no handover.
  Se a cadeia se partir (por exemplo, falha a instalar dependências), o bot volta no cron seguinte.
- Quotas gratuitas: créditos HF, ZeroGPU e Gemini podem esgotar — os comandos de geração caem para o
  motor seguinte ou respondem com aviso.
- `/ask` tem rate-limit por utilizador.
- Sem estado persistente entre turnos.
- Comandos que dependem de sites de terceiros (cinema, torrents, IPTV, downloads) podem falhar quando a
  fonte muda ou bloqueia os IPs da cloud.
