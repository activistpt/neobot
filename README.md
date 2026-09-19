# 🤖 NEOBOT

Assistente multiusos para Telegram.

## Comandos

| Comando | Descrição |
|---------|-----------|
| `/start` | Mensagem de boas-vindas + menu |
| `/ajuda` | Mostra a lista de comandos |
| `/hora` | Que horas são |
| `/data` | Que dia é hoje |
| `/piada` | Uma piada aleatória |
| `/dado` | Lança um dado (1-6) |
| `/moeda` | Cara ou coroa |
| `/escolhe` | Escolhe por ti — ex: `/escolhe pizza sushi burger` |
| `/ask` | Pergunta algo à IA — ex: `/ask quem escreveu Dom Casmurro?` ou responde a uma msg com `/ask` |
| `/iptv` | IPTV mundial por página web: Rebel (756) + SportOnline (25) + TV Garden (20 países) |
| `/canal` | Procura canais e devolve streams ou páginas web — ex: `/canal sport tv`; `/canal 2` apanha o 2.º resultado |

## Setup

```bash
# 1. Criar o bot no Telegram
#    Fala com @BotFather -> /newbot -> nome: NEOBOT
#    Copia o token que ele te dá

# 2. Criar ambiente virtual e instalar dependências
py -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

# 3. Configurar o token e a chave Groq
copy .env.example .env
#    Edita o .env: cola o teu token do BotFather e a tua GROQ_API_KEY
#    (obtém a chave grátis em https://console.groq.com/keys)

# 4. Arrancar
py bot.py
```

O bot fica a correr no terminal. Para parar, `Ctrl+C`.
