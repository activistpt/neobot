# 🤖 NEOBOT

Assistente multiusos para Telegram — hospedado 24/7 gratuitamente no GitHub Actions.

## Comandos

`/ajuda` mostra a lista completa: /ask (IA Groq) · /google · /news · /wiki · /image · /audio ·
/meteo · /youtube · /crypto · /cinema · /estreias · /imdb · /play · /ipinfo · /ipscan ·
/iplookup · /phone · /torrent · /download · /mp3 · /radio · /piada · e mais.

## Como corre 24/7

- **GitHub Actions** mantém o bot online: o workflow `neobot.yml` corre o bot como job
  de longa duração (6h) e o cron renova a sessão a cada 5h25 — sem interrupções.
- Repositório público = minutos de Actions ilimitados no plano grátis.
- `concurrency` garante **1 única instância** (nunca dois bots no mesmo token).
- O token do bot e a chave da Groq vivem em **GitHub Secrets** (nunca no código).

## Local (opcional)

```bash
pip install -r requirements.txt
# .env com NEOBOT_TOKEN e GROQ_API_KEY
python bot.py
```
