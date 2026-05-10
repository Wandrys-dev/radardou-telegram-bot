"""Bot Telegram para a API Radar DOU.

Recursos:
- /chave por usuario (cada um usa a propria API key)
- Busca por orgao, secao, tipo, data via texto livre ou /buscar
- Cards estilo site, paginacao com botao inline
- /menu com botoes de açao rapida
- Alertas proativos: a cada CHECK_ALERTS_INTERVAL_MIN o bot verifica os
  alertas configurados de cada usuario na conta dele e notifica novos hits
"""

import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

try:
    from openai import AsyncOpenAI
except Exception:  # openai opcional — bot continua funcionando sem IA
    AsyncOpenAI = None  # type: ignore

from radardou import RadarDOU
from storage import UserStorage

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = os.getenv("DATABASE_PATH", "bot_users.db")
CHECK_ALERTS_INTERVAL_MIN = int(os.getenv("CHECK_ALERTS_INTERVAL_MIN", "30"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not TOKEN:
    raise SystemExit(
        "TELEGRAM_BOT_TOKEN nao definido. Crie um .env a partir do .env.example."
    )

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("radardou-bot")

storage = UserStorage(DB_PATH)
API_KEY_RE = re.compile(r"^rdk_(prod|test)_[A-Za-z0-9]{32,}$")

PAGE_SIZE = 5
DEFAULT_LIMIT = 20

SEARCH_STATE: dict[int, dict] = {}  # paginacao por chat_id

# Cliente OpenAI (opcional — se nao tiver chave, comandos /ia e /conversar
# devolvem mensagem amigavel pedindo pra configurar)
ai_client = (
    AsyncOpenAI(api_key=OPENAI_API_KEY)
    if (AsyncOpenAI and OPENAI_API_KEY)
    else None
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_client(chat_id: int):
    api_key = storage.get_api_key(chat_id)
    if not api_key:
        return None
    return RadarDOU(api_key=api_key)


def parse_filters(text: str) -> dict:
    """Extrai filtros estilo 'orgao:Banco Central data:01/05/2026-08/05/2026'."""
    text = re.sub(r"^[\s•·•\-\*]+", "", text or "")
    text = re.sub(r"^/buscar\b\s*", "", text, flags=re.IGNORECASE).strip()

    filters_out = {}
    remaining = text

    patterns = {
        "orgao": r"orgao:\s*([^\s][^:]*?)(?=\s+\w+:|$)",
        "secao": r"secao:\s*(DO\d|Extra)",
        "tipo": r"tipo:\s*([^\s][^:]*?)(?=\s+\w+:|$)",
        "data": r"data:\s*(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})",
        "desde": r"desde:\s*(\d{2}/\d{2}/\d{4})",
    }

    m = re.search(patterns["data"], remaining, re.IGNORECASE)
    if m:
        filters_out["date_from"] = _br_to_iso(m.group(1))
        filters_out["date_to"] = _br_to_iso(m.group(2))
        remaining = remaining.replace(m.group(0), "").strip()

    m = re.search(patterns["desde"], remaining, re.IGNORECASE)
    if m:
        filters_out["date_from"] = _br_to_iso(m.group(1))
        remaining = remaining.replace(m.group(0), "").strip()

    for key in ["orgao", "secao", "tipo"]:
        m = re.search(patterns[key], remaining, re.IGNORECASE)
        if m:
            filters_out[key] = m.group(1).strip()
            remaining = remaining.replace(m.group(0), "").strip()

    query = remaining.strip()
    if query:
        filters_out["query"] = query

    return filters_out


def _br_to_iso(date_br: str) -> str:
    d, m, y = date_br.split("/")
    return f"{y}-{m}-{d}"


def _iso_to_br(date_iso: str) -> str:
    if not date_iso:
        return ""
    if "T" in date_iso:
        date_iso = date_iso.split("T", 1)[0]
    parts = date_iso.split("-")
    if len(parts) == 3:
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    return date_iso


def html_card(pub: dict) -> str:
    titulo = escape(pub.get("titulo") or "(sem titulo)")
    secao = escape(pub.get("secao_codigo") or "?")
    tipo = escape(pub.get("tipo_ato") or "")
    orgao_full = pub.get("orgao_hierarquia") or ""
    orgao_curto = orgao_full.split("/")[-1] if orgao_full else ""
    orgao_curto = escape(orgao_curto[:80])
    data_pub = _iso_to_br(pub.get("data_publicacao") or "")
    pagina = pub.get("numero_pagina") or ""
    link = pub.get("link_ato") or ""
    resumo = pub.get("texto_resumo") or ""
    if resumo:
        resumo = escape(resumo[:250] + ("..." if len(resumo) > 250 else ""))

    parts = [f"<b>{titulo}</b>"]
    badges = []
    if secao and secao != "?":
        badges.append(f"📋 {secao}")
    if tipo:
        badges.append(f"📄 {tipo}")
    if data_pub:
        badges.append(f"📅 {data_pub}")
    if pagina:
        badges.append(f"📑 pág. {pagina}")
    if badges:
        parts.append(" • ".join(badges))
    if orgao_curto:
        parts.append(f"🏛️ <i>{orgao_curto}</i>")
    if resumo:
        parts.append(resumo)
    if link:
        parts.append(f'🔗 <a href="{escape(link, quote=True)}">Ler ato completo</a>')
    return "\n".join(parts)


def search_summary(filters_used: dict, total: int, shown_count: int, page: int) -> str:
    lines = ["<b>🔍 Busca no DOU</b>"]
    if filters_used.get("query"):
        lines.append(f'• Termo: <i>{escape(filters_used["query"])}</i>')
    if filters_used.get("orgao"):
        lines.append(f'• Órgão: <i>{escape(filters_used["orgao"])}</i>')
    if filters_used.get("secao"):
        lines.append(f'• Seção: <i>{escape(filters_used["secao"])}</i>')
    if filters_used.get("tipo"):
        lines.append(f'• Tipo: <i>{escape(filters_used["tipo"])}</i>')
    if filters_used.get("date_from") or filters_used.get("date_to"):
        df = _iso_to_br(filters_used.get("date_from") or "")
        dt = _iso_to_br(filters_used.get("date_to") or "")
        if df and dt:
            lines.append(f"• Período: {df} até {dt}")
        elif df:
            lines.append(f"• Desde: {df}")
    lines.append("")
    lines.append(f"📊 <b>Total no banco:</b> {total} | <b>Mostrando:</b> {shown_count} (pág {page + 1})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    has_key = storage.get_api_key(chat_id) is not None
    name = update.effective_user.first_name or "voce"

    txt = (
        f"Olá, {name}! Eu sou o bot do <b>Radar DOU</b>.\n\n"
        "Eu busco publicações do Diário Oficial da União pra você, "
        "com filtros por órgão, data, seção e tipo. Notifico automaticamente "
        "quando novos atos batem com seus alertas.\n\n"
    )
    if has_key:
        txt += (
            "Você já tem uma chave cadastrada ✅\n\n"
            "<b>Como usar:</b>\n"
            "• Digite qualquer termo (ex: <i>concurso público</i>) → busco direto\n"
            "• Use filtros: <code>orgao:Banco Central data:01/05/2026-09/05/2026</code>\n"
            "• /menu — abre menu de ações rápidas\n"
            "• /ajuda — ver todos os comandos e exemplos"
        )
    else:
        txt += (
            "Pra começar, você precisa de uma chave de API.\n\n"
            "1️⃣ Crie em https://www.radar-dou.com/api-keys\n"
            "    👉 Marque <b>todos os scopes</b> pra todas as funcionalidades\n"
            "2️⃣ Cadastre aqui com:\n"
            "    <code>/chave rdk_prod_sua_chave</code>\n\n"
            "Plano trial gratuito de 5 dias disponível."
        )
    await update.message.reply_html(txt, disable_web_page_preview=True)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Hoje", callback_data="menu:hoje"),
            InlineKeyboardButton("🗓️ 7 dias", callback_data="menu:7d"),
            InlineKeyboardButton("📆 30 dias", callback_data="menu:30d"),
        ],
        [
            InlineKeyboardButton("🔔 Alertas", callback_data="menu:alertas"),
            InlineKeyboardButton("⭐ Favoritos", callback_data="menu:favoritos"),
        ],
        [
            InlineKeyboardButton("🔕 Notif: ?", callback_data="menu:toggle_notif"),
            InlineKeyboardButton("ℹ️ Ajuda", callback_data="menu:ajuda"),
        ],
    ])


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    notif_on = storage.get_notifications(chat_id)

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Hoje", callback_data="menu:hoje"),
            InlineKeyboardButton("🗓️ Últimos 7 dias", callback_data="menu:7d"),
        ],
        [
            InlineKeyboardButton("📆 Últimos 30 dias", callback_data="menu:30d"),
            InlineKeyboardButton("🎛️ Filtros avançados", callback_data="menu:filtros"),
        ],
        [
            InlineKeyboardButton("🤖 Falar com a IA", callback_data="menu:ia"),
        ],
        [
            InlineKeyboardButton("🔔 Meus alertas", callback_data="menu:alertas"),
            InlineKeyboardButton("⭐ Favoritos", callback_data="menu:favoritos"),
        ],
        [
            InlineKeyboardButton(
                ("🔕 Desligar notif" if notif_on else "🔔 Ligar notif"),
                callback_data="menu:toggle_notif",
            ),
            InlineKeyboardButton("ℹ️ Ajuda", callback_data="menu:ajuda"),
        ],
    ])

    txt = (
        "<b>Menu — Radar DOU</b>\n\n"
        f"🔔 Notificações automáticas: {'<b>LIGADAS</b>' if notif_on else 'desligadas'}\n"
        "Quando ligadas, você recebe alertas a cada "
        f"{CHECK_ALERTS_INTERVAL_MIN} min com novas publicações que batem "
        "com os alertas configurados na sua conta Radar DOU.\n\n"
        "Pra busca direta, é só digitar o termo no chat."
    )
    await update.message.reply_html(txt, reply_markup=kb)


async def cmd_chave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "Uso: /chave rdk_prod_sua_chave_aqui\n\n"
            "Crie a chave em https://www.radar-dou.com/api-keys"
        )
        return

    api_key = context.args[0].strip()
    if not API_KEY_RE.match(api_key):
        await update.message.reply_text(
            "Formato de chave inválido. Deve começar com rdk_prod_ ou rdk_test_."
        )
        return

    await update.message.reply_text("Validando chave...")
    try:
        client = RadarDOU(api_key=api_key)
        client.buscar(date_from=date.today().isoformat(), limit=1)
        client.close()
    except Exception as exc:
        log.warning("validacao falhou: %s", exc)
        await update.message.reply_text(
            f"Chave inválida ou plano expirado.\n\nDetalhe: {exc}"
        )
        return

    storage.set_api_key(chat_id, api_key)
    storage.set_notifications(chat_id, True)
    storage.set_last_alert_check(chat_id)  # reset baseline = agora

    try:
        await update.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "Chave cadastrada com sucesso ✅\n\n"
            "Apaguei sua mensagem com a chave por segurança.\n\n"
            "<b>Notificações automáticas ligadas</b> 🔔 — vou checar seus alertas "
            f"a cada {CHECK_ALERTS_INTERVAL_MIN} min.\n\n"
            "Experimente: /menu ou digite <code>orgao:Banco Central</code>"
        ),
        parse_mode="HTML",
    )


async def cmd_revogar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    storage.delete_user(chat_id)
    SEARCH_STATE.pop(chat_id, None)
    await update.message.reply_text(
        "Sua chave foi removida do bot. Pode cadastrar nova com /chave."
    )


async def cmd_hoje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    hoje_str = date.today().isoformat()
    await _do_search(update, context, chat_id, {"date_from": hoje_str}, page=0)


async def cmd_buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_html(
            "<b>Uso:</b> <code>/buscar &lt;termo&gt; [filtros]</code>\n\n"
            "<b>Exemplos (clique pra copiar):</b>\n"
            "<code>/buscar concurso público</code>\n"
            "<code>/buscar orgao:Banco Central</code>\n"
            "<code>/buscar licitação data:01/05/2026-09/05/2026</code>\n"
            "<code>/buscar edital secao:DO3 tipo:Edital</code>\n\n"
            "Dica: sem o <code>/buscar</code> também funciona — é só digitar o termo direto."
        )
        return
    text = " ".join(context.args)
    await _do_search_from_text(update, context, text)


async def on_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    await _do_search_from_text(update, context, text)


async def _do_search_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    filters_parsed = parse_filters(text)

    if not any(filters_parsed.get(k) for k in ("date_from", "date_to", "orgao", "secao", "tipo")):
        filters_parsed["date_from"] = (date.today() - timedelta(days=7)).isoformat()

    await _do_search(update, context, chat_id, filters_parsed, page=0)


async def _do_search(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     chat_id: int, filters_used: dict, page: int):
    client = get_client(chat_id)
    if not client:
        await update.effective_message.reply_text(
            "Cadastre sua chave primeiro: /chave rdk_prod_xxx"
        )
        return

    try:
        result = client.buscar(
            **{k: v for k, v in filters_used.items() if k in
               ("query", "orgao", "secao", "tipo", "date_from", "date_to")},
            page=page + 1,
            limit=DEFAULT_LIMIT,
        )
    except Exception as exc:
        msg = str(exc)
        log.exception("erro na busca")
        if "permiss" in msg.lower() or "scope" in msg.lower():
            await update.effective_message.reply_html(
                "Sua chave não tem permissão para essa busca.\n\n"
                "Crie nova chave em https://www.radar-dou.com/api-keys "
                "marcando <code>publications:read</code>."
            )
            return
        await update.effective_message.reply_text(f"Erro ao buscar: {exc}")
        return
    finally:
        client.close()

    total = result["pagination"]["total"]
    items = result["data"]

    if total == 0:
        await update.effective_message.reply_html(
            search_summary(filters_used, 0, 0, page) + "\n\n<i>Nenhum resultado.</i>"
        )
        return

    SEARCH_STATE[chat_id] = {
        "filters": filters_used,
        "page": page,
        "total": total,
        "shown": len(items),
    }

    header = search_summary(filters_used, total, len(items), page)
    await update.effective_message.reply_html(header, disable_web_page_preview=True)

    chunks = [items[i:i + PAGE_SIZE] for i in range(0, len(items), PAGE_SIZE)]
    for chunk in chunks:
        body = "\n\n────────\n\n".join(html_card(p) for p in chunk)
        await update.effective_message.reply_html(body, disable_web_page_preview=True)

    total_pages = -(-total // DEFAULT_LIMIT)
    if page + 1 < total_pages:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"⏭️ Próximas {DEFAULT_LIMIT} (pág {page + 2}/{total_pages})",
                callback_data=f"page:{page + 1}",
            )
        ]])
        await update.effective_message.reply_text(
            f"Página {page + 1} de {total_pages}.",
            reply_markup=kb,
        )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Roteia todos os clicks em botoes inline."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data or ""

    if data.startswith("page:"):
        state = SEARCH_STATE.get(chat_id)
        if not state:
            await query.message.reply_text("Sessão expirou. Faça uma nova busca.")
            return
        next_page = int(data.split(":", 1)[1])
        fake = Update(update.update_id, message=query.message)
        await _do_search(fake, context, chat_id, state["filters"], page=next_page)
        return

    if data == "menu:hoje":
        fake = Update(update.update_id, message=query.message)
        await _do_search(fake, context, chat_id, {"date_from": date.today().isoformat()}, page=0)
        return

    if data == "menu:7d":
        fake = Update(update.update_id, message=query.message)
        df = (date.today() - timedelta(days=7)).isoformat()
        await _do_search(fake, context, chat_id, {"date_from": df}, page=0)
        return

    if data == "menu:30d":
        fake = Update(update.update_id, message=query.message)
        df = (date.today() - timedelta(days=30)).isoformat()
        await _do_search(fake, context, chat_id, {"date_from": df}, page=0)
        return

    if data == "menu:alertas":
        fake = Update(update.update_id, message=query.message)
        await cmd_alertas(fake, context)
        return

    if data == "menu:favoritos":
        fake = Update(update.update_id, message=query.message)
        await cmd_favoritos(fake, context)
        return

    if data == "menu:toggle_notif":
        atual = storage.get_notifications(chat_id)
        novo = not atual
        storage.set_notifications(chat_id, novo)
        if novo:
            storage.set_last_alert_check(chat_id)
        await query.message.reply_html(
            f"Notificações automáticas: <b>{'LIGADAS' if novo else 'desligadas'}</b> "
            f"{'🔔' if novo else '🔕'}"
        )
        return

    if data == "menu:ajuda":
        fake = Update(update.update_id, message=query.message)
        await cmd_ajuda(fake, context)
        return

    if data == "menu:ia":
        await query.message.reply_html(
            "🤖 <b>Assistente Virtual com IA</b>\n\n"
            "Duas formas de usar:\n\n"
            "1️⃣ Pergunta única — manda /ia seguido da sua pergunta:\n"
            "<code>/ia me mostra publicacoes de hoje sobre concursos</code>\n\n"
            "2️⃣ Conversa contínua — manda /conversar e tudo que digitar vai pra IA "
            "(com memória do contexto). Sai com /sair quando quiser.\n\n"
            "A IA pode buscar publicações, criar alertas, listar favoritos e mais — "
            "usando a sua chave do Radar DOU."
        )
        return


async def cmd_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    client = get_client(chat_id)
    if not client:
        await update.effective_message.reply_text(
            "Cadastre sua chave primeiro: /chave rdk_prod_xxx"
        )
        return

    try:
        result = client.listar_alertas()
    except Exception as exc:
        msg = str(exc)
        if "permiss" in msg.lower() or "permission" in msg.lower() or "scope" in msg.lower():
            await update.effective_message.reply_html(
                "Sua chave de API <b>não tem permissão</b> para alertas.\n\n"
                "Pra liberar:\n"
                "1️⃣ Vá em https://www.radar-dou.com/api-keys\n"
                "2️⃣ Crie uma <b>nova chave</b> marcando <code>alerts:read</code> "
                "(ou clique em <i>Marcar todos</i>)\n"
                "3️⃣ Cadastre aqui com /chave &lt;nova_chave&gt;"
            )
            return
        log.exception("erro em /alertas")
        await update.effective_message.reply_text(f"Erro: {exc}")
        return
    finally:
        client.close()

    items = result.get("data", []) if isinstance(result, dict) else result
    if not items:
        await update.effective_message.reply_text(
            "Você não tem alertas configurados.\n\n"
            "Crie alertas em https://www.radar-dou.com/alertas"
        )
        return

    lines = ["<b>Seus alertas configurados:</b>\n"]
    for alert in items[:20]:
        nome = escape(alert.get("name", "(sem nome)"))
        freq = escape(alert.get("frequency", "?"))
        ativo = "✅" if alert.get("active", True) else "❌"
        lines.append(f"{ativo} <b>{nome}</b> — {freq}")
    notif_on = storage.get_notifications(chat_id)
    lines.append("")
    lines.append(
        f"🔔 Notificações automáticas: <b>{'ON' if notif_on else 'OFF'}</b> — /menu pra alternar"
    )
    await update.effective_message.reply_html("\n".join(lines))


async def cmd_favoritos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    client = get_client(chat_id)
    if not client:
        await update.effective_message.reply_text(
            "Cadastre sua chave primeiro: /chave rdk_prod_xxx"
        )
        return

    try:
        result = client.listar_favoritos()
    except Exception as exc:
        msg = str(exc)
        if "permiss" in msg.lower() or "permission" in msg.lower() or "scope" in msg.lower():
            await update.effective_message.reply_html(
                "Sua chave de API <b>não tem permissão</b> para favoritos.\n\n"
                "Crie nova chave em https://www.radar-dou.com/api-keys marcando "
                "<code>favorites:read</code>."
            )
            return
        log.exception("erro em /favoritos")
        await update.effective_message.reply_text(f"Erro: {exc}")
        return
    finally:
        client.close()

    items = result.get("data", []) if isinstance(result, dict) else result
    if not items:
        await update.effective_message.reply_text(
            "Você ainda não tem publicações favoritas.\n\n"
            "Salve favoritos em https://www.radar-dou.com"
        )
        return

    pubs = [it.get("publication") or it for it in items[:10]]
    chunks = [pubs[i:i + PAGE_SIZE] for i in range(0, len(pubs), PAGE_SIZE)]
    await update.effective_message.reply_html(f"<b>Seus {len(pubs)} favoritos mais recentes:</b>")
    for chunk in chunks:
        body = "\n\n────────\n\n".join(html_card(p) for p in chunk)
        await update.effective_message.reply_html(body, disable_web_page_preview=True)


async def cmd_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "<b>Bot do Radar DOU — Comandos</b>\n\n"
        "<b>Cadastro:</b>\n"
        "/start — boas-vindas e status\n"
        "/chave &lt;api_key&gt; — cadastra/atualiza sua chave\n"
        "/revogar — remove sua chave deste bot\n\n"
        "<b>Consulta:</b>\n"
        "/menu — abre menu rápido com botões\n"
        "/filtros — busca guiada por etapas (período + termo + seção)\n"
        "/hoje — publicações de hoje\n"
        "/buscar &lt;termo&gt; [filtros] — busca via texto\n"
        "/alertas — lista seus alertas configurados\n"
        "/favoritos — lista suas publicações favoritas\n\n"
        "<b>Assistente Virtual com IA:</b>\n"
        "/ia &lt;pergunta&gt; — pergunta única à IA (responde direto)\n"
        "/conversar — modo conversação contínua (com memória)\n"
        "/sair — sai do modo conversação\n\n"
        "<b>Filtros (formato chave:valor):</b>\n"
        "<code>orgao:Banco Central</code> — match parcial no nome do órgão\n"
        "<code>secao:DO1</code> — DO1, DO2, DO3 ou Extra\n"
        "<code>tipo:Portaria</code> — Portaria, Edital, Despacho etc.\n"
        "<code>data:01/05/2026-09/05/2026</code> — intervalo\n"
        "<code>desde:01/05/2026</code> — só data inicial\n\n"
        "<b>Notificações automáticas:</b>\n"
        f"A cada {CHECK_ALERTS_INTERVAL_MIN} min eu checo seus alertas configurados em radar-dou.com/alertas e notifico aqui se houver novas publicações.\n"
        "Liga/desliga em /menu.\n\n"
        "<b>Exemplos:</b>\n"
        "<code>concurso público</code>\n"
        "<code>orgao:Banco Central data:01/05/2026-09/05/2026</code>\n"
        "<code>edital orgao:Tribunal de Contas secao:DO3</code>\n\n"
        "Site: https://www.radar-dou.com"
    )
    await update.effective_message.reply_html(txt, disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# Assistente Virtual com IA (/ia e /conversar)
# ---------------------------------------------------------------------------

AI_SYSTEM_PROMPT = """Voce eh o Assistente Virtual do Radar DOU, um sistema de monitoramento do Diario Oficial da Uniao do Brasil.

Suas capacidades:
- Buscar publicacoes do DOU por palavra-chave, data, secao, tipo de ato e orgao
- Detalhar uma publicacao especifica (texto completo)
- Listar e criar alertas que rodam automaticamente
- Listar favoritos do usuario

REGRAS GERAIS:
1. NUNCA invente dados — sempre use as ferramentas pra buscar informacoes reais.
2. Respostas em PT-BR, curtas e diretas. No maximo 4-5 linhas no texto principal.
3. Use formatacao Markdown leve: *negrito*, _italico_, `codigo` (sem HTML).
4. Se a pergunta for vaga, peca pra esclarecer ANTES de buscar.
5. Se o usuario pedir algo fora do escopo do DOU, redirecione gentilmente.

INTERPRETACAO DE EXPRESSOES DE TEMPO (convencao brasileira: semana comeca no DOMINGO):
- "hoje" → date_from = date_to = HOJE
- "ontem" → date_from = date_to = HOJE - 1 dia
- "esta semana" / "nesta semana" → date_from = ULTIMO DOMINGO (inclusive); date_to = HOJE
  IMPORTANTE: se HOJE for domingo, date_from = HOJE (a semana esta comecando)
- "semana passada" → date_from = domingo de 7 dias atras; date_to = sabado anterior a HOJE
- "ultimos 7 dias" → date_from = HOJE - 7; date_to = HOJE
- "este mes" → date_from = dia 1 do mes atual; date_to = HOJE
- "ultimos 30 dias" → date_from = HOJE - 30
- "no dia DD/MM/AAAA" → date_from = date_to = essa data
- "entre X e Y" → date_from = X, date_to = Y
- Se o usuario NAO especificou periodo, use date_from = HOJE - 7 dias.

ESTRATEGIA DE BUSCA (CRITICO):
- Para CATEGORIAS especiais, use o param 'categoria' (NAO use query):
  - "concurso publico" / "concursos" / "editais de concurso" → categoria="concursos"
  - "licitacoes" / "pregoes" → categoria="licitacoes"
  - "leis" / "decretos" / "medidas provisorias" → categoria="legislacao"
  Esses filtros casam com tipo_ato no banco e dao resultados PRECISOS, igual a aba
  /concursos do site (so traz Editais de Concurso, nao Portarias que mencionam concurso).

- Para BUSCA TEXTUAL livre, use 'query' (procura em titulo + corpo):
  - Use termos CURTOS — o DOU nao tem stemming, "concursos" != "concurso".
  - "portarias do MEC" → orgao="Ministerio da Educacao", tipo="Portaria" (sem query)

- Use 'orgao' pra match parcial no nome do orgao (ex: "Banco Central").
- Se a primeira busca trouxer 0 resultados, AMPLIE: aumente periodo OU troque
  categoria por query (ou vice-versa), OU use sinonimos.

FORMATACAO DA RESPOSTA AO USUARIO (CRITICO):
- NUNCA mostre IDs numericos das publicacoes no texto da resposta. Eles sao usados internamente.
- NUNCA liste publicacoes em texto longo no chat — o bot ja vai renderizar cards bonitos
  automaticamente depois da sua resposta.
- A sua resposta deve ser apenas um RESUMO ANALITICO em 2-3 linhas:
  "Encontrei N publicacoes sobre X. A maioria eh do orgao Y, dos dias Z."
- Termine sempre com uma SUGESTAO de proxima acao: "Quer criar um alerta?",
  "Quer que eu detalhe alguma especifica?", "Quer filtrar por DO3?"

CONTEXTO ATUAL:
- Data de hoje: {today}
- O usuario esta no Telegram com tela pequena. Seja conciso ao maximo.
"""

AI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "buscar_publicacoes",
            "description": (
                "Busca publicacoes no Diario Oficial da Uniao. Pelo menos um filtro eh "
                "obrigatorio entre query, date_from, date_to, secao, tipo, orgao ou categoria. "
                "Para 'concursos publicos', 'licitacoes' ou 'leis/decretos', PREFIRA usar "
                "o parametro 'categoria' em vez de 'query' — eh mais preciso pq filtra "
                "tipo_ato no banco (mesma logica das paginas /concursos, /licitacoes, /legislacao "
                "do site)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Palavra-chave em titulo ou corpo"},
                    "date_from": {"type": "string", "description": "Data inicial YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "Data final YYYY-MM-DD"},
                    "secao": {"type": "string", "enum": ["DO1", "DO2", "DO3", "Extra"]},
                    "tipo": {"type": "string", "description": "Tipo do ato (ex: Portaria, Edital, Despacho, Decreto)"},
                    "orgao": {"type": "string", "description": "Match parcial no nome do orgao (ex: 'Banco Central')"},
                    "categoria": {
                        "type": "string",
                        "enum": ["concursos", "licitacoes", "legislacao"],
                        "description": (
                            "Categoria especializada — filtra por tipo_ato. "
                            "Use 'concursos' pra editais de concurso publico, "
                            "'licitacoes' pra pregoes/licitacoes, "
                            "'legislacao' pra leis/decretos/MPs."
                        ),
                    },
                    "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obter_publicacao_completa",
            "description": "Retorna o texto completo de uma publicacao especifica pelo ID numerico.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "ID numerico da publicacao"}
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_alertas_usuario",
            "description": "Lista os alertas configurados pelo usuario.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "criar_alerta",
            "description": (
                "Cria um novo alerta automatico. O alerta vai rodar na frequencia escolhida e "
                "notificar o usuario quando aparecerem publicacoes que batem com os criterios."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nome curto e descritivo"},
                    "query": {"type": "string", "description": "Palavra-chave do alerta"},
                    "secao": {"type": "string", "enum": ["DO1", "DO2", "DO3", "Extra"]},
                    "tipo": {"type": "string", "description": "Tipo de ato"},
                    "frequency": {
                        "type": "string",
                        "enum": ["realtime", "hourly", "daily", "weekly"],
                        "default": "daily",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_favoritos_usuario",
            "description": "Lista as publicacoes favoritadas pelo usuario.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


async def _exec_tool(
    name: str,
    args: dict,
    radar: RadarDOU,
    pubs_to_render: list,
) -> str:
    """Executa uma ferramenta solicitada pela IA. Retorna string textual com o resultado.

    pubs_to_render: lista mutavel onde acumulamos publicacoes que o bot vai
    renderizar como cards bonitos depois da resposta da IA.
    """
    try:
        if name == "buscar_publicacoes":
            limit = min(int(args.pop("limit", 5)), 10)
            kwargs = {k: v for k, v in args.items() if v}
            if not kwargs:
                return "Erro: pelo menos um filtro eh obrigatorio (query, date_from, etc)."
            kwargs["limit"] = limit
            res = radar.buscar(**kwargs)
            total = res["pagination"]["total"]
            items = res["data"]
            if not items:
                return (
                    f"Nenhuma publicacao encontrada com {kwargs}. "
                    "Sugestao: amplie a query (use termos mais curtos) ou aumente o periodo."
                )

            # Acumula pra renderizacao posterior
            pubs_to_render.extend(items)

            # Resumo agregado pra IA processar (sem IDs ou detalhes que vao nos cards)
            datas = sorted({(p.get("data_publicacao") or "")[:10] for p in items if p.get("data_publicacao")})
            secoes = sorted({p.get("secao_codigo") or "?" for p in items})
            tipos = sorted({p.get("tipo_ato") or "?" for p in items})
            orgaos_set = {((p.get("orgao_hierarquia") or "").split("/")[-1] or "?") for p in items}
            orgaos_top = sorted(orgaos_set)[:5]

            lines = [
                f"BUSCA OK. Total no banco: {total}. Cards mostrados: {len(items)}.",
                f"Datas: {', '.join(datas) or '-'}",
                f"Secoes: {', '.join(secoes)}",
                f"Tipos: {', '.join(tipos)}",
                f"Orgaos (amostra): {', '.join(orgaos_top)}",
                "",
                "OBS: o bot ja vai mostrar os cards detalhados ao usuario. Voce so precisa "
                "fazer um RESUMO curto (2-3 linhas) e oferecer uma proxima acao.",
            ]
            return "\n".join(lines)

        if name == "obter_publicacao_completa":
            pub = radar.obter_publicacao(args["id"])
            texto = (pub.get("texto_puro") or "")[:2500]
            return (
                f"Titulo: {pub.get('titulo')}\n"
                f"Data: {(pub.get('data_publicacao') or '')[:10]}\n"
                f"Secao: {pub.get('secao_codigo')}\n"
                f"Tipo: {pub.get('tipo_ato')}\n"
                f"Orgao: {pub.get('orgao_hierarquia')}\n"
                f"Pagina: {pub.get('numero_pagina')}\n"
                f"Link: {pub.get('link_ato')}\n\n"
                f"Texto:\n{texto}"
            )

        if name == "listar_alertas_usuario":
            res = radar.listar_alertas()
            items = res.get("data") if isinstance(res, dict) else res
            if not items:
                return "Usuario nao tem alertas configurados."
            lines = [f"{len(items)} alertas:"]
            for a in items[:20]:
                ativo = "ON" if a.get("active", True) else "OFF"
                lines.append(f"- [{ativo}] {a.get('name','?')} ({a.get('frequency','?')})")
            return "\n".join(lines)

        if name == "criar_alerta":
            crit = {k: args[k] for k in ("query", "secao", "tipo") if args.get(k)}
            if not crit:
                return "Erro: criterios vazios. Defina ao menos query/secao/tipo."
            radar.criar_alerta(
                name=args["name"],
                search_criteria=crit,
                frequency=args.get("frequency", "daily"),
            )
            return f"Alerta '{args['name']}' criado com sucesso. Frequencia: {args.get('frequency','daily')}."

        if name == "listar_favoritos_usuario":
            res = radar.listar_favoritos()
            items = res.get("data") if isinstance(res, dict) else res
            if not items:
                return "Sem favoritos salvos."
            return f"{len(items)} favoritos. (Lista detalhada disponivel via /favoritos no bot.)"

        return f"Ferramenta desconhecida: {name}"

    except Exception as exc:
        log.exception("erro em tool %s", name)
        return f"Erro ao executar {name}: {exc}"


async def ask_ai(
    chat_id: int,
    user_message: str,
    history: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Manda pergunta pra OpenAI com tools disponiveis.

    Retorna (texto_resposta, lista_de_publicacoes_pra_renderizar).
    O bot vai enviar o texto + cards bonitos das publicacoes em sequencia.
    """
    if not ai_client:
        return (
            "🤖 IA nao configurada.\n\n"
            "O operador do bot precisa adicionar OPENAI_API_KEY como variavel de "
            "ambiente. Por enquanto, use os comandos manuais: /menu, /filtros, /buscar.",
            [],
        )

    radar = get_client(chat_id)
    if not radar:
        return ("Cadastre sua chave do Radar DOU primeiro: /chave rdk_prod_xxx", [])

    today_str = date.today().strftime("%Y-%m-%d (%A)")
    system = AI_SYSTEM_PROMPT.format(today=today_str)

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    if history:
        messages.extend(history[-12:])
    messages.append({"role": "user", "content": user_message})

    pubs_to_render: list[dict] = []

    try:
        for _ in range(5):
            resp = await ai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                tools=AI_TOOLS,
                tool_choice="auto",
                temperature=0.3,
                max_tokens=600,
            )
            choice = resp.choices[0].message
            tool_calls = choice.tool_calls or []

            messages.append(
                {
                    "role": "assistant",
                    "content": choice.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ]
                    if tool_calls
                    else None,
                }
            )

            if not tool_calls:
                return (choice.content or "(sem resposta da IA)", pubs_to_render)

            for tc in tool_calls:
                args_json = tc.function.arguments or "{}"
                try:
                    args = json.loads(args_json)
                except json.JSONDecodeError:
                    args = {}
                result = await _exec_tool(tc.function.name, args, radar, pubs_to_render)
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result}
                )

        return (
            "Nao consegui chegar a uma resposta apos 5 iteracoes. Tente reformular.",
            pubs_to_render,
        )
    except Exception as exc:
        log.exception("erro chamando OpenAI")
        return (f"Erro na IA: {exc}", pubs_to_render)
    finally:
        try:
            radar.close()
        except Exception:
            pass


async def _send_ai_response(update: Update, response_text: str, pubs: list[dict]):
    """Envia o texto da IA e depois os cards das publicacoes (se houver)."""
    if response_text:
        try:
            await update.effective_message.reply_text(response_text, parse_mode="Markdown")
        except Exception:
            await update.effective_message.reply_text(response_text)

    # Renderiza cards bonitos das publicacoes que a IA buscou
    for pub in pubs[:10]:
        try:
            await update.effective_message.reply_html(
                html_card(pub),
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log.warning("erro ao renderizar card da publicacao: %s", exc)


async def cmd_ia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_html(
            "<b>Assistente Virtual</b>\n\n"
            "Uso: <code>/ia &lt;pergunta&gt;</code>\n\n"
            "<b>Exemplos:</b>\n"
            "• <code>/ia me mostra publicacoes de hoje sobre concursos</code>\n"
            "• <code>/ia crie um alerta pra licitacoes de TI no DO3</code>\n"
            "• <code>/ia o que tem do Banco Central na ultima semana?</code>\n"
            "• <code>/ia quais alertas eu tenho?</code>\n\n"
            "Pra conversa continua, use /conversar."
        )
        return

    pergunta = " ".join(context.args)
    chat_id = update.effective_chat.id
    await update.message.chat.send_action(ChatAction.TYPING)
    response_text, pubs = await ask_ai(chat_id, pergunta)
    await _send_ai_response(update, response_text, pubs)


# Conversacao continua via ConversationHandler
CONV_AI = 100


async def cmd_conversar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not get_client(chat_id):
        await update.effective_message.reply_text(
            "Cadastre sua chave primeiro: /chave rdk_prod_xxx"
        )
        return ConversationHandler.END
    if not ai_client:
        await update.effective_message.reply_text(
            "🤖 IA nao configurada (OPENAI_API_KEY ausente). "
            "Use /menu, /filtros ou /buscar enquanto isso."
        )
        return ConversationHandler.END

    context.user_data["ai_history"] = []
    await update.effective_message.reply_html(
        "💬 <b>Modo conversação ativado</b>\n\n"
        "Tudo que voce digitar agora vai pro Assistente Virtual com memória de contexto.\n\n"
        "Mande <b>/sair</b> pra voltar ao bot normal."
    )
    return CONV_AI


async def conv_on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    if not text:
        return CONV_AI

    history = context.user_data.get("ai_history", [])
    await update.message.chat.send_action(ChatAction.TYPING)
    response_text, pubs = await ask_ai(chat_id, text, history)

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": response_text})
    context.user_data["ai_history"] = history[-24:]

    await _send_ai_response(update, response_text, pubs)
    return CONV_AI


async def conv_sair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("ai_history", None)
    await update.effective_message.reply_text(
        "Saiu do modo conversação. Bot voltou ao normal. /ajuda pra ver comandos."
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Wizard de filtros (/filtros)
# ---------------------------------------------------------------------------

WIZ_PERIODO, WIZ_PERIODO_CUSTOM, WIZ_TERMO, WIZ_SECAO = range(4)

DATE_RANGE_RE = re.compile(
    r"^\s*(\d{2})/(\d{2})/(\d{4})\s*-\s*(\d{2})/(\d{2})/(\d{4})\s*$"
)


def _wiz_kb_periodo() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Hoje", callback_data="wiz:periodo:hoje"),
            InlineKeyboardButton("🗓 Últimos 7 dias", callback_data="wiz:periodo:7d"),
        ],
        [
            InlineKeyboardButton("📆 Últimos 30 dias", callback_data="wiz:periodo:30d"),
            InlineKeyboardButton("✏️ Personalizado", callback_data="wiz:periodo:custom"),
        ],
        [InlineKeyboardButton("❌ Cancelar", callback_data="wiz:cancel")],
    ])


def _wiz_kb_termo() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ Pular (sem termo)", callback_data="wiz:termo:skip")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="wiz:cancel")],
    ])


def _wiz_kb_secao() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("DO1", callback_data="wiz:secao:DO1"),
            InlineKeyboardButton("DO2", callback_data="wiz:secao:DO2"),
            InlineKeyboardButton("DO3", callback_data="wiz:secao:DO3"),
        ],
        [
            InlineKeyboardButton("Edição Extra", callback_data="wiz:secao:Extra"),
            InlineKeyboardButton("✅ Todas", callback_data="wiz:secao:all"),
        ],
        [InlineKeyboardButton("❌ Cancelar", callback_data="wiz:cancel")],
    ])


async def cmd_filtros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point do wizard guiado. Pergunta período primeiro."""
    chat_id = update.effective_chat.id
    if not get_client(chat_id):
        await update.effective_message.reply_text(
            "Cadastre sua chave primeiro: /chave rdk_prod_xxx"
        )
        return ConversationHandler.END

    context.user_data["wiz_filtros"] = {}

    await update.effective_message.reply_html(
        "🔍 <b>Busca avançada — passo 1/3</b>\n\n"
        "Escolha o <b>período</b> que deseja consultar:",
        reply_markup=_wiz_kb_periodo(),
    )
    return WIZ_PERIODO


async def wiz_on_periodo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if len(parts) < 3:
        return WIZ_PERIODO
    choice = parts[2]
    today = date.today()
    f = context.user_data.setdefault("wiz_filtros", {})

    if choice == "hoje":
        f["date_from"] = today.isoformat()
        f["date_to"] = today.isoformat()
    elif choice == "7d":
        f["date_from"] = (today - timedelta(days=7)).isoformat()
    elif choice == "30d":
        f["date_from"] = (today - timedelta(days=30)).isoformat()
    elif choice == "custom":
        await query.edit_message_text(
            "📅 <b>Período personalizado</b>\n\n"
            "Digite no formato <code>DD/MM/AAAA-DD/MM/AAAA</code>\n\n"
            "Exemplos:\n"
            "<code>01/05/2026-09/05/2026</code>\n"
            "<code>15/04/2026-10/05/2026</code>",
            parse_mode="HTML",
        )
        return WIZ_PERIODO_CUSTOM

    return await _wiz_ask_termo(query.message, context, edit=True)


async def wiz_on_periodo_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = DATE_RANGE_RE.match(text)
    if not m:
        await update.message.reply_html(
            "Formato inválido. Use <code>DD/MM/AAAA-DD/MM/AAAA</code>\n"
            "Exemplo: <code>01/05/2026-09/05/2026</code>\n\n"
            "Tente de novo ou /cancelar:"
        )
        return WIZ_PERIODO_CUSTOM

    df = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    dt = f"{m.group(6)}-{m.group(5)}-{m.group(4)}"
    f = context.user_data.setdefault("wiz_filtros", {})
    f["date_from"] = df
    f["date_to"] = dt

    return await _wiz_ask_termo(update.message, context, edit=False)


async def _wiz_ask_termo(message, context: ContextTypes.DEFAULT_TYPE, edit: bool):
    f = context.user_data.get("wiz_filtros", {})
    df_br = _iso_to_br(f.get("date_from", ""))
    dt_br = _iso_to_br(f.get("date_to", ""))
    if df_br and dt_br:
        per = f"{df_br} até {dt_br}"
    elif df_br:
        per = f"desde {df_br}"
    else:
        per = "—"

    txt = (
        "🔍 <b>Busca avançada — passo 2/3</b>\n\n"
        f"✅ Período: <i>{per}</i>\n\n"
        "Digite agora um <b>termo</b> ou <b>órgão</b>:\n\n"
        "Exemplos:\n"
        "• <code>concurso público</code>\n"
        "• <code>orgao:Banco Central</code>\n"
        "• <code>tipo:Portaria orgao:Tribunal de Contas</code>\n\n"
        "Ou clique em <i>Pular</i> pra buscar sem filtro de texto."
    )
    if edit:
        await message.edit_text(txt, parse_mode="HTML", reply_markup=_wiz_kb_termo())
    else:
        await message.reply_html(txt, reply_markup=_wiz_kb_termo())
    return WIZ_TERMO


async def wiz_on_termo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    parsed = parse_filters(text)
    f = context.user_data.setdefault("wiz_filtros", {})
    # Não sobrescreve date_from/date_to já configurados
    for k, v in parsed.items():
        if k in ("date_from", "date_to") and k in f:
            continue
        f[k] = v

    return await _wiz_ask_secao(update.message, context, edit=False)


async def wiz_on_termo_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await _wiz_ask_secao(query.message, context, edit=True)


async def _wiz_ask_secao(message, context: ContextTypes.DEFAULT_TYPE, edit: bool):
    f = context.user_data.get("wiz_filtros", {})
    resumo = []
    if f.get("query"):
        resumo.append(f"termo: <i>{escape(f['query'])}</i>")
    if f.get("orgao"):
        resumo.append(f"órgão: <i>{escape(f['orgao'])}</i>")
    if f.get("tipo"):
        resumo.append(f"tipo: <i>{escape(f['tipo'])}</i>")

    txt = (
        "🔍 <b>Busca avançada — passo 3/3</b>\n\n"
        + (("Filtros: " + " · ".join(resumo) + "\n\n") if resumo else "")
        + "Escolha a <b>seção</b> do DOU:"
    )

    if edit:
        await message.edit_text(txt, parse_mode="HTML", reply_markup=_wiz_kb_secao())
    else:
        await message.reply_html(txt, reply_markup=_wiz_kb_secao())
    return WIZ_SECAO


async def wiz_on_secao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if len(parts) < 3:
        return WIZ_SECAO
    choice = parts[2]
    f = context.user_data.setdefault("wiz_filtros", {})
    if choice != "all":
        f["secao"] = choice

    # Mostra resumo final + executa busca
    chat_id = query.message.chat.id
    await query.edit_message_text("🔍 Buscando…", parse_mode="HTML")

    # Reaproveita _do_search com filtros coletados
    fake = Update(update.update_id, message=query.message)
    await _do_search(fake, context, chat_id, f, page=0)

    context.user_data.pop("wiz_filtros", None)
    return ConversationHandler.END


async def wiz_cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Busca cancelada.")
    context.user_data.pop("wiz_filtros", None)
    return ConversationHandler.END


async def wiz_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("❌ Busca cancelada.")
    context.user_data.pop("wiz_filtros", None)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Erro handler
# ---------------------------------------------------------------------------


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Exception while handling update", exc_info=context.error)


# ---------------------------------------------------------------------------
# Job proativo de alertas
# ---------------------------------------------------------------------------


async def check_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    """Roda a cada CHECK_ALERTS_INTERVAL_MIN. Para cada usuario com notif ON,
    busca os alertas configurados e checa se ha novas publicacoes desde o
    ultimo check. Notifica via Telegram."""
    log.info("[alerts-job] iniciando ciclo")

    users = storage.list_users_for_alerts()
    log.info("[alerts-job] %d usuarios para checar", len(users))

    for chat_id, api_key, last_check_iso in users:
        try:
            await _check_alerts_for_user(context, chat_id, api_key, last_check_iso)
        except Exception as exc:
            log.exception("[alerts-job] erro com chat_id=%s: %s", chat_id, exc)

    log.info("[alerts-job] ciclo concluido")


async def _check_alerts_for_user(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    api_key: str,
    last_check_iso: str | None,
):
    client = RadarDOU(api_key=api_key)
    try:
        try:
            res = client.listar_alertas()
        except Exception as exc:
            msg = str(exc).lower()
            if "permiss" in msg or "scope" in msg:
                log.info("[alerts-job] chat_id=%s sem permissao de alertas", chat_id)
                return
            raise

        alerts = res.get("data", []) if isinstance(res, dict) else res
        active = [a for a in (alerts or []) if a.get("active", True)]
        if not active:
            return

        # Janela: de last_check_iso (ou ultimas 2h se nunca checou) ate agora
        if last_check_iso:
            try:
                df = datetime.fromisoformat(last_check_iso.replace("Z", "+00:00")).date().isoformat()
            except Exception:
                df = date.today().isoformat()
        else:
            df = date.today().isoformat()

        for alert in active:
            crit = alert.get("searchCriteria") or alert.get("search_criteria") or {}
            kwargs = {"date_from": df, "limit": 5}
            if crit.get("query"):
                kwargs["query"] = crit["query"]
            if crit.get("secao"):
                kwargs["secao"] = crit["secao"]
            if crit.get("tipo"):
                kwargs["tipo"] = crit["tipo"]
            if crit.get("orgao"):
                kwargs["orgao"] = crit["orgao"]

            # Sem nenhum criterio = nao busca (evita scan)
            if not any(k in kwargs for k in ("query", "secao", "tipo", "orgao")):
                continue

            try:
                hits = client.buscar(**kwargs)
            except Exception as exc:
                log.warning("[alerts-job] alerta '%s' falhou: %s", alert.get("name"), exc)
                continue

            items = hits.get("data") or []
            if not items:
                continue

            nome = escape(alert.get("name") or "(alerta)")
            header = f"🔔 <b>Alerta: {nome}</b>\n{len(items)} nova(s) publicação(ões):"
            await context.bot.send_message(
                chat_id=chat_id,
                text=header,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            for pub in items[:5]:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=html_card(pub),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception as exc:
                    log.warning("[alerts-job] envio falhou: %s", exc)

        storage.set_last_alert_check(chat_id)
    finally:
        try:
            client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("chave", cmd_chave))
    app.add_handler(CommandHandler("revogar", cmd_revogar))
    app.add_handler(CommandHandler("hoje", cmd_hoje))
    app.add_handler(CommandHandler("buscar", cmd_buscar))
    app.add_handler(CommandHandler("alertas", cmd_alertas))
    app.add_handler(CommandHandler("favoritos", cmd_favoritos))
    app.add_handler(CommandHandler("ajuda", cmd_ajuda))
    app.add_handler(CommandHandler("help", cmd_ajuda))
    app.add_handler(CommandHandler("ia", cmd_ia))

    # Modo conversação contínua com a IA
    conversar_conv = ConversationHandler(
        entry_points=[CommandHandler("conversar", cmd_conversar)],
        states={
            CONV_AI: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_on_message)],
        },
        fallbacks=[
            CommandHandler("sair", conv_sair),
            CommandHandler("cancelar", conv_sair),
        ],
        per_chat=True,
        per_user=True,
    )
    app.add_handler(conversar_conv)

    # Wizard /filtros (ConversationHandler) — registrado ANTES dos handlers
    # genéricos pra que ele consuma o input em estados ativos
    filtros_conv = ConversationHandler(
        entry_points=[
            CommandHandler("filtros", cmd_filtros),
            CommandHandler("buscar_avancada", cmd_filtros),
            CallbackQueryHandler(cmd_filtros, pattern=r"^menu:filtros$"),
        ],
        states={
            WIZ_PERIODO: [
                CallbackQueryHandler(wiz_on_periodo, pattern=r"^wiz:periodo:"),
            ],
            WIZ_PERIODO_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_on_periodo_custom),
            ],
            WIZ_TERMO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_on_termo_text),
                CallbackQueryHandler(wiz_on_termo_skip, pattern=r"^wiz:termo:skip$"),
            ],
            WIZ_SECAO: [
                CallbackQueryHandler(wiz_on_secao, pattern=r"^wiz:secao:"),
            ],
        },
        fallbacks=[
            CommandHandler("cancelar", wiz_cancel_command),
            CallbackQueryHandler(wiz_cancel_button, pattern=r"^wiz:cancel$"),
        ],
        per_chat=True,
        per_user=True,
    )
    app.add_handler(filtros_conv)

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_plain_text))

    app.add_error_handler(on_error)

    # Job proativo de alertas
    interval_seconds = CHECK_ALERTS_INTERVAL_MIN * 60
    app.job_queue.run_repeating(
        check_alerts_job,
        interval=interval_seconds,
        first=60,  # primeiro check 60s apos start (warmup)
        name="check_alerts",
    )

    log.info(
        "Bot iniciando. Usuarios cadastrados: %d. Job de alertas a cada %d min.",
        storage.count_users(),
        CHECK_ALERTS_INTERVAL_MIN,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
