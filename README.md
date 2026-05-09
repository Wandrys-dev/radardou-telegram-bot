# Bot Telegram — Radar DOU

[![CI](https://img.shields.io/badge/python-3.11+-blue)]() [![License](https://img.shields.io/badge/license-MIT-green)]()

Bot oficial para Telegram que consulta a [API do Radar DOU](https://www.radar-dou.com).

Cada usuário cadastra a própria chave de API (gerada em `/api-keys`); o uso é
contabilizado no plano dele, não no plano do operador do bot.

🤖 **Bot em produção**: [@radar_dou_oficial_bot](https://t.me/radar_dou_oficial_bot)

## Recursos

- 🔍 **Busca por texto livre ou comando** (`/buscar`) com filtros por órgão, seção, tipo e data
- 📅 **`/menu`** com botões para "Hoje", "7 dias", "30 dias", "Alertas", "Favoritos"
- 🔔 **Alertas proativos**: a cada 30 min o bot verifica seus alertas configurados em [radar-dou.com/alerts](https://www.radar-dou.com/alerts) e notifica novas publicações automaticamente
- ⏭️ **Paginação** com botão inline "Próximas 20"
- 🎨 **Cards estilo do site** (badges, órgão, resumo, link)
- 🛡️ **Auto-delete** da mensagem que continha a chave para não vazar no histórico

## Comandos

| Comando | Função |
|---|---|
| `/start` | Boas-vindas e status |
| `/menu` | Menu rápido com botões |
| `/chave <api_key>` | Cadastra ou atualiza a chave |
| `/revogar` | Remove a chave deste bot |
| `/hoje` | Publicações do DOU de hoje |
| `/buscar <termo>` | Busca por palavra-chave |
| `/alertas` | Lista alertas configurados |
| `/favoritos` | Lista publicações favoritadas |
| `/ajuda` | Mostra todos os comandos |

### Filtros (formato chave:valor)

```
orgao:Banco Central
secao:DO1                     # DO1, DO2, DO3 ou Extra
tipo:Portaria                 # Portaria, Edital, Despacho, etc.
data:01/05/2026-09/05/2026    # intervalo
desde:01/05/2026              # só data inicial
```

Sem `/buscar`, é só digitar o termo no chat — o bot aciona busca automaticamente.

### Exemplos

```
concurso público
orgao:Banco Central data:01/05/2026-09/05/2026
edital orgao:Tribunal de Contas secao:DO3
licitação tipo:Aviso de Licitação
```

## Setup local

### 1) Criar o bot no Telegram

1. Abra o Telegram, busque **@BotFather**
2. Mande `/newbot`
3. Escolha um **nome** (display name): `Radar DOU`
4. Escolha um **username** (precisa terminar em `bot`): `radardou_oficial_bot` (por exemplo)
5. Copie o **token** que aparece (formato: `12345:ABC-DEF...`)

### 2) Configurar e instalar

```bash
git clone https://github.com/Wandrys-dev/radardou-telegram-bot.git
cd radardou-telegram-bot

cp .env.example .env
# Edite .env e cole o TELEGRAM_BOT_TOKEN

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/Mac

pip install -r requirements.txt
```

### 3) Rodar

```bash
python bot.py
```

O bot fica em foreground. Para parar, `Ctrl+C`.

### 4) Personalização do BotFather (opcional, mas recomendado)

Mande pro @BotFather:

- `/setuserpic` — foto de perfil (use o logo do Radar DOU)
- `/setdescription` — texto longo (sobre o que o bot faz)
- `/setabouttext` — texto curto (aparece no perfil)
- `/setcommands` — lista de comandos pra autocompletar:

```
start - Boas-vindas e status
menu - Menu rapido com botoes
hoje - Publicacoes do DOU de hoje
buscar - Busca por palavra-chave
alertas - Lista seus alertas configurados
favoritos - Lista suas publicacoes favoritas
chave - Cadastra sua chave de API
revogar - Remove sua chave deste bot
ajuda - Mostra todos os comandos
```

## Deploy 24/7

### Opção A — Railway (mais fácil, recomendado)

1. Faça login em https://railway.app conectando sua conta GitHub
2. **New Project** → **Deploy from GitHub repo** → escolha `radardou-telegram-bot`
3. Em **Variables**, adicione:
   - `TELEGRAM_BOT_TOKEN` = (seu token do BotFather)
   - `CHECK_ALERTS_INTERVAL_MIN` = `30` (opcional, default já é 30)
4. **Deploy**. Railway detecta o `Dockerfile` e roda automaticamente.
5. Adicione um **Volume** em `/data` para persistir o SQLite com as chaves dos usuários.

Tier gratuito: ~500h/mês, suficiente pra bot pequeno.

### Opção B — Fly.io

Pré-requisito: instalar `flyctl`.

```bash
fly auth login
fly launch --copy-config --no-deploy   # usa o fly.toml do repo
fly volumes create bot_data --region gru --size 1
fly secrets set TELEGRAM_BOT_TOKEN=seu_token_aqui
fly deploy
```

Tier free: 3 VMs `shared-cpu-1x` 256MB. Bot leve cabe num.

### Opção C — VPS / Raspberry Pi (systemd)

```bash
# /etc/systemd/system/radardou-bot.service
[Unit]
Description=Radar DOU Telegram Bot
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/home/botuser/radardou-telegram-bot
ExecStart=/home/botuser/radardou-telegram-bot/.venv/bin/python bot.py
Restart=on-failure
EnvironmentFile=/home/botuser/radardou-telegram-bot/.env

[Install]
WantedBy=multi-user.target
```

Habilita com:
```bash
sudo systemctl enable --now radardou-bot
sudo systemctl status radardou-bot
journalctl -u radardou-bot -f   # logs ao vivo
```

## Variáveis de ambiente

| Variável | Default | Descrição |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (obrigatório) | Token do BotFather |
| `DATABASE_PATH` | `bot_users.db` | Caminho do SQLite |
| `CHECK_ALERTS_INTERVAL_MIN` | `30` | Intervalo (em min) entre checks proativos de alertas |
| `RADAR_API_BASE_URL` | (vazio) | Override da URL da API (default: `https://www.radar-dou.com/api/v1`) |

## Arquitetura

```
┌──────────────────┐         ┌─────────────────────┐
│ Usuário Telegram │ ◄────► │ Bot (long polling) │
└──────────────────┘         └──────────┬──────────┘
                                        │
                                        │ HTTP Bearer
                                        ▼
                             ┌──────────────────────┐
                             │ API Radar DOU v1     │
                             │ radar-dou.com/api/v1 │
                             └──────────────────────┘

      ┌──────────────────────┐
      │ JobQueue (a cada 30m)│  ── lista alertas do user via SDK ──►
      └──────────────────────┘  ── envia novos hits via Telegram ──►
```

- **Stack**: Python 3.11 · python-telegram-bot 21.6 · APScheduler · SQLite
- **SDK**: [`radardou`](https://github.com/Wandrys-dev/radardou-python) v1.0.2+

## Segurança

- **Chaves em texto puro** no SQLite (`bot_users.db`). Não comite. Em produção
  considere criptografar com `cryptography.Fernet` antes de salvar.
- O bot **deleta a mensagem original do `/chave`** depois de validar, para que a
  chave não fique no histórico do chat.
- Tokens do bot e API keys nunca são logados.

## Licença

MIT
