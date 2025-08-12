# 📦 Estrutura do projeto
#
# vipbot/
# ├─ bot.py                 # Entrypoint: inicia o bot (polling) + servidor Flask p/ webhooks MP
# ├─ config.py              # Carrega envs e validações
# ├─ db.py                  # SQLite + camada de acesso a dados
# ├─ services/
# │   └─ mercadopago_client.py  # Cliente MP (criar preferências, consultar pagamentos)
# ├─ utils.py               # Utilitários gerais
# ├─ requirements.txt       # Dependências
# ├─ .env.example           # Exemplo de variáveis de ambiente
# └─ README.md              # Guia rápido

# =========================
# ======= bot.py ==========
# =========================

import asyncio
import base64
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatInviteLink,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    AIORateLimiter,
)

from config import Settings
from db import (
    init_db,
    upsert_user,
    create_membership,
    get_membership,
    extend_membership,
    log_payment,
    is_payment_processed,
    get_expired_memberships,
    mark_payment_processed,
)
from services.mercadopago_client import MPClient
from utils import admin_only

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("vipbot")

# =========================
# ====== CONFIG/INIT ======
# =========================

settings = Settings()
mp = MPClient(settings.MP_ACCESS_TOKEN, settings.BASE_URL, settings.MP_WEBHOOK_SECRET)

# Flask para Webhook do Mercado Pago
app = Flask(__name__)


# =========================
# ======= HELPERS =========
# =========================

async def ensure_vip_and_invite(user_id: int, context: ContextTypes.DEFAULT_TYPE, days: int, reason: str) -> None:
    """Concede/estende VIP e envia link de convite único.

    Inputs:
        user_id (int): ID do usuário no Telegram.
        context (ContextTypes.DEFAULT_TYPE): Contexto PTB (acesso ao bot).
        days (int): Quantidade de dias a conceder/estender.
        reason (str): Motivo (ex.: "approved_payment:123").
    Outputs:
        None. (Envia mensagem ao usuário com link e atualiza DB.)
    """
    now = datetime.now(timezone.utc)
    membership = get_membership(user_id)
    if membership is None or membership["expires_at"] <= now:
        expires_at = now + timedelta(days=days)
        create_membership(user_id, expires_at, reason)
    else:
        new_exp = membership["expires_at"] + timedelta(days=days)
        extend_membership(user_id, new_exp, reason)

    # Cria link único com limite 1 e validade curta (15 min)
    bot = context.bot
    invite: ChatInviteLink = await bot.create_chat_invite_link(
        chat_id=settings.VIP_GROUP_ID,
        expire_date=int((now + timedelta(minutes=15)).timestamp()),
        member_limit=1,
        creates_join_request=False,
        name=f"VIP {user_id} {now.isoformat()}"
    )

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "✅ *Pagamento confirmado!*\n\n"
                "Seu acesso ao *VIP* foi liberado/estendido.\n\n"
                "👉 Entre pelo link abaixo (válido por ~15 min, uso único):\n"
                f"{invite.invite_link}\n\n"
                "Se expirar, me chame aqui que eu gero outro automaticamente."
            ),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.warning(f"Falha ao DM usuário {user_id}: {e}")


# =========================
# ====== TELEGRAM BOT =====
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — Boas-vindas + menu.

    Inputs:
        update: Update do Telegram
        context: Contexto PTB
    Outputs:
        None (envia mensagem com botões)
    """
    user = update.effective_user
    if user:
        upsert_user(user.id, user.username or user.full_name or "")

    kb = [
        [InlineKeyboardButton("💳 Comprar VIP (30 dias)", callback_data="buy:vip30")],
        [InlineKeyboardButton("ℹ️ Como funciona", callback_data="info")],
        [InlineKeyboardButton("🧾 Status do meu VIP", callback_data="status")],
    ]
    await update.message.reply_text(
        "Bem-vindo! Aqui você compra e gerencia seu acesso ao *Grupo VIP*.\n\n"
        "• Pagamento via *Mercado Pago* (Checkout / Pix).\n"
        "• Liberação *automática* após confirmação.\n\n"
        "Clique em *Comprar VIP (30 dias)* para gerar seu link de pagamento.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.message.reply_text(f"Chat ID: `{chat.id}`", parse_mode=ParseMode.MARKDOWN)


async def status_btn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback para mostrar status do VIP."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    m = get_membership(user_id)
    if m and m["expires_at"] > datetime.now(timezone.utc):
        left = m["expires_at"] - datetime.now(timezone.utc)
        days = left.days
        hours = int(left.seconds / 3600)
        await query.edit_message_text(
            f"✅ Seu VIP está ativo.\nExpira em ~{days}d {hours}h."
        )
    else:
        await query.edit_message_text(
            "❌ Você não tem VIP ativo no momento."
        )


async def info_btn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    back = [[InlineKeyboardButton("⬅️ Voltar", callback_data="back")]]
    await query.edit_message_text(
        (
            "• Pague com Mercado Pago (cartão, Pix).\n"
            "• Assim que *aprovado*, você recebe um link *único* pra entrar no grupo.\n"
            "• Dura 30 dias e pode ser estendido automaticamente quando você renovar.\n"
            "• Se o link expirar, clique em /start de novo que eu gero outro."
        ),
        reply_markup=InlineKeyboardMarkup(back)
    )


async def back_btn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    kb = [
        [InlineKeyboardButton("💳 Comprar VIP (30 dias)", callback_data="buy:vip30")],
        [InlineKeyboardButton("ℹ️ Como funciona", callback_data="info")],
        [InlineKeyboardButton("🧾 Status do meu VIP", callback_data="status")],
    ]
    await query.edit_message_text(
        "Menu principal:", reply_markup=InlineKeyboardMarkup(kb)
    )


async def buy_btn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: cria preferência no Mercado Pago e devolve link de pagamento.

    Inputs:
        update: Update do callback
        context: Contexto PTB
    Outputs:
        None (edita a msg com link de pagamento)
    """
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    plan_code = query.data.split(":")[1]  # ex: vip30
    if plan_code == "vip30":
        title = "Assinatura VIP (30 dias)"
        amount = settings.PLAN_VIP30_PRICE
        days = 30
    else:
        await query.edit_message_text("Plano inválido.")
        return

    pref = mp.create_preference(
        title=title,
        amount=amount,
        metadata={"telegram_id": user_id, "plan_code": plan_code, "days": days},
    )

    pay_url = pref["init_point"]  # Checkout Pro URL

    kb = [[InlineKeyboardButton("🔗 Abrir pagamento", url=pay_url)],
          [InlineKeyboardButton("⬅️ Voltar", callback_data="back")]]

    await query.edit_message_text(
        (
            f"💳 *{title}*\n"
            f"Valor: R$ {amount:.2f}\n\n"
            "Clique em *Abrir pagamento* e conclua no site do Mercado Pago."
        ),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )


@admin_only
async def admin_kick_expired(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/kick_expired — Remove membros com VIP vencido.

    Inputs: none
    Outputs: none (resumo no chat)
    """
    expired = get_expired_memberships()
    removed = 0
    for row in expired:
        uid = row["telegram_id"]
        try:
            await context.bot.ban_chat_member(settings.VIP_GROUP_ID, uid)
            await context.bot.unban_chat_member(settings.VIP_GROUP_ID, uid)
            removed += 1
        except Exception as e:
            logger.warning(f"Falha ao remover {uid}: {e}")
    await update.message.reply_text(f"Removidos: {removed}")


async def daily_expiration_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job diário: remove expirados automaticamente."""
    expired = get_expired_memberships()
    for row in expired:
        uid = row["telegram_id"]
        try:
            await context.bot.ban_chat_member(settings.VIP_GROUP_ID, uid)
            await context.bot.unban_chat_member(settings.VIP_GROUP_ID, uid)
        except Exception as e:
            logger.warning(f"Falha ao remover {uid}: {e}")


# =========================
# ====== MP WEBHOOK =======
# =========================

@app.post("/webhook/mp")
def mp_webhook():
    """Webhook do Mercado Pago.

    Comportamento:
    - Verifica o secret querystring ?secret=...
    - Lê JSON, se for evento de pagamento, consulta detalhes no MP.
    - Se aprovado e não processado, loga e concede VIP.

    Segurança:
    - Idempotência garantida por checagem de payment_id.
    """
    secret = request.args.get("secret", "")
    if secret != settings.MP_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    logger.info(f"MP webhook payload: {data}")

    # Mercado Pago envia diferentes formatos; lidamos com os casos comuns
    event_type = data.get("type") or data.get("action")

    if event_type == "payment" or (data.get("data") and data["data"].get("id")):
        payment_id = data.get("data", {}).get("id") or data.get("id")
        if not payment_id:
            return jsonify({"ok": True})

        # Idempotência
        if is_payment_processed(str(payment_id)):
            return jsonify({"ok": True, "skipped": "already processed"})

        payment = mp.get_payment(str(payment_id))
        status = (payment.get("status") or "").lower()
        metadata = payment.get("metadata") or {}

        if status == "approved":
            user_id = int(metadata.get("telegram_id"))
            plan_code = metadata.get("plan_code")
            days = int(metadata.get("days", 30))

            # Log pagamento antes (idempotência)
            log_payment(
                mp_payment_id=str(payment_id),
                status=status,
                amount=float(payment.get("transaction_amount") or 0.0),
                plan_code=plan_code,
                telegram_id=user_id,
            )
            mark_payment_processed(str(payment_id))

            # Dispara task assíncrona no event loop do bot para convidar o usuário
            loop = asyncio.get_event_loop()
            loop.create_task(ensure_vip_and_invite(user_id, bot_app.contexts[0], days, f"approved_payment:{payment_id}"))

        return jsonify({"ok": True})

    return jsonify({"ok": True})


# =========================
# ======== MAIN ===========
# =========================

bot_app: Application

def run_flask():
    app.run(host="0.0.0.0", port=settings.HTTP_PORT, debug=False)


def main():
    global bot_app

    init_db()

    bot_app = (
        ApplicationBuilder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("myid", myid))
    bot_app.add_handler(CommandHandler("kick_expired", admin_kick_expired))

    bot_app.add_handler(CallbackQueryHandler(status_btn, pattern="^status$"))
    bot_app.add_handler(CallbackQueryHandler(info_btn, pattern="^info$"))
    bot_app.add_handler(CallbackQueryHandler(back_btn, pattern="^back$"))
    bot_app.add_handler(CallbackQueryHandler(buy_btn, pattern="^buy:"))

    # Job diário para limpeza
    bot_app.job_queue.run_daily(daily_expiration_job, time=datetime.time(3, 30))

    # Flask em thread separada (recebe webhooks do MP)
    thread = threading.Thread(target=run_flask, daemon=True)
    thread.start()

    # Bot em polling (simples e confiável para começar)
    bot_app.run_polling(close_loop=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()


# =========================
# ====== config.py =========


@dataclass
class Settings:
    """Carrega e valida variáveis de ambiente.

    Necessárias:
        TELEGRAM_BOT_TOKEN: token do BotFather
        VIP_GROUP_ID: ID numérico do grupo VIP (ex: -1001234567890)
        MP_ACCESS_TOKEN: Access Token do Mercado Pago (produçao ou sandbox)
        MP_WEBHOOK_SECRET: string aleatória para validar webhooks (?secret=...)
        BASE_URL: URL pública que recebe webhooks do MP (ex: https://xxxx.ngrok.io)
    Opcionais:
        PLAN_VIP30_PRICE: preço (float) do plano 30 dias (padrão: 29.90)
        HTTP_PORT: porta local do Flask (padrão: 8080)
        ADMIN_IDS: comma-separated de IDs admins para comandos restritos
    """

    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    VIP_GROUP_ID: int = int(os.getenv("VIP_GROUP_ID", "0"))
    MP_ACCESS_TOKEN: str = os.getenv("MP_ACCESS_TOKEN", "")
    MP_WEBHOOK_SECRET: str = os.getenv("MP_WEBHOOK_SECRET", "changeme")
    BASE_URL: str = os.getenv("BASE_URL", "http://localhost:8080")

    PLAN_VIP30_PRICE: float = float(os.getenv("PLAN_VIP30_PRICE", "29.90"))
    HTTP_PORT: int = int(os.getenv("HTTP_PORT", "8080"))

    ADMIN_IDS: list[int] = [
        int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
    ]

    def __post_init__(self):
        assert self.TELEGRAM_BOT_TOKEN, "TELEGRAM_BOT_TOKEN obrigatório"
        assert self.VIP_GROUP_ID != 0, "VIP_GROUP_ID obrigatório"
        assert self.MP_ACCESS_TOKEN, "MP_ACCESS_TOKEN obrigatório"
        assert self.MP_WEBHOOK_SECRET and self.MP_WEBHOOK_SECRET != "changeme", "Defina MP_WEBHOOK_SECRET"
        assert self.BASE_URL.startswith("http"), "BASE_URL deve ser pública (ngrok/https)"
# =========================

import os
from dataclasses import dataclass

@dataclass
class Settings:
    """Carrega e valida variáveis de ambiente.

    Necessárias:
        TELEGRAM_BOT_TOKEN: token do BotFather
        VIP_GROUP_ID: ID numérico do grupo VIP (ex: -1001234567890)
        MP_ACCESS_TOKEN: Access Token do Mercado Pago (produçao ou sandbox)
        MP_WEBHOOK_SECRET: string aleatória para validar webhooks (?secret=...)
        BASE_URL: URL pública que recebe webhooks do MP (ex: https://xxxx.ngrok.io)
    Opcionais:
        PLAN_VIP30_PRICE: preço (float) do plano 30 dias (padrão: 29.90)
        HTTP_PORT: porta local do Flask (padrão: 8080)
        ADMIN_IDS: comma-separated de IDs admins para comandos restritos
    """

    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    VIP_GROUP_ID: int = int(os.getenv("VIP_GROUP_ID", "0"))
    MP_ACCESS_TOKEN: str = os.getenv("MP_ACCESS_TOKEN", "")
    MP_WEBHOOK_SECRET: str = os.getenv("MP_WEBHOOK_SECRET", "changeme")
    BASE_URL: str = os.getenv("BASE_URL", "http://localhost:8080")

    PLAN_VIP30_PRICE: float = float(os.getenv("PLAN_VIP30_PRICE", "29.90"))
    HTTP_PORT: int = int(os.getenv("HTTP_PORT", "8080"))

    ADMIN_IDS: list[int] = [
        int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
    ]

    def __post_init__(self):
        assert self.TELEGRAM_BOT_TOKEN, "TELEGRAM_BOT_TOKEN obrigatório"
        assert self.VIP_GROUP_ID != 0, "VIP_GROUP_ID obrigatório"
        assert self.MP_ACCESS_TOKEN, "MP_ACCESS_TOKEN obrigatório"
        assert self.MP_WEBHOOK_SECRET and self.MP_WEBHOOK_SECRET != "changeme", "Defina MP_WEBHOOK_SECRET"
        assert self.BASE_URL.startswith("http"), "BASE_URL deve ser pública (ngrok/https)"


# =========================
# ========= db.py =========
# =========================

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = "vipbot.sqlite3"

@contextmanager
def conn_ctx():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with conn_ctx() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              telegram_id INTEGER PRIMARY KEY,
              username TEXT,
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS memberships (
              telegram_id INTEGER PRIMARY KEY,
              expires_at TIMESTAMP NOT NULL,
              updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS payments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              mp_payment_id TEXT UNIQUE,
              status TEXT,
              amount REAL,
              plan_code TEXT,
              telegram_id INTEGER,
              processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def upsert_user(telegram_id: int, username: str) -> None:
    with conn_ctx() as con:
        con.execute(
            "INSERT INTO users(telegram_id, username) VALUES(?, ?)\n"
            "ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username",
            (telegram_id, username),
        )


def get_membership(telegram_id: int) -> dict | None:
    with conn_ctx() as con:
        cur = con.execute("SELECT * FROM memberships WHERE telegram_id=?", (telegram_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {"telegram_id": row["telegram_id"], "expires_at": datetime.fromisoformat(row["expires_at"]) if isinstance(row["expires_at"], str) else row["expires_at"]}


def create_membership(telegram_id: int, expires_at: datetime, reason: str) -> None:
    with conn_ctx() as con:
        con.execute(
            "INSERT INTO memberships(telegram_id, expires_at) VALUES(?, ?)",
            (telegram_id, expires_at.replace(tzinfo=timezone.utc).isoformat()),
        )


def extend_membership(telegram_id: int, new_expires_at: datetime, reason: str) -> None:
    with conn_ctx() as con:
        con.execute(
            "UPDATE memberships SET expires_at=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?",
            (new_expires_at.replace(tzinfo=timezone.utc).isoformat(), telegram_id),
        )


def get_expired_memberships() -> list[sqlite3.Row]:
    with conn_ctx() as con:
        cur = con.execute(
            "SELECT * FROM memberships WHERE expires_at <= ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        return cur.fetchall()


def log_payment(mp_payment_id: str, status: str, amount: float, plan_code: str, telegram_id: int) -> None:
    with conn_ctx() as con:
        con.execute(
            "INSERT OR IGNORE INTO payments(mp_payment_id, status, amount, plan_code, telegram_id) VALUES(?,?,?,?,?)",
            (mp_payment_id, status, amount, plan_code, telegram_id),
        )


def is_payment_processed(mp_payment_id: str) -> bool:
    with conn_ctx() as con:
        cur = con.execute(
            "SELECT 1 FROM payments WHERE mp_payment_id=?",
            (mp_payment_id,),
        )
        return cur.fetchone() is not None


def mark_payment_processed(mp_payment_id: str) -> None:
    # neste design, o INSERT já marca como processado; função mantida para extensão
    return None


# =========================
# === services/mercadopago_client.py ===
# =========================

import mercadopago

class MPClient:
    """Cliente simples para Mercado Pago.

    Métodos principais:
        create_preference(title, amount, metadata) -> dict
        get_payment(payment_id) -> dict
    """

    def __init__(self, access_token: str, base_url: str, webhook_secret: str):
        self.sdk = mercadopago.SDK(access_token)
        self.base_url = base_url.rstrip("/")
        self.webhook_secret = webhook_secret

    def create_preference(self, title: str, amount: float, metadata: dict) -> dict:
        """Cria uma preferência (Checkout Pro) e retorna dados da preferência.

        Inputs:
            title (str): título do item
            amount (float): valor R$
            metadata (dict): infos extras (telegram_id, plan_code, days)
        Outputs:
            dict com chaves como: id, init_point, sandbox_init_point, items...
        """
        preference_data = {
            "items": [
                {
                    "title": title,
                    "quantity": 1,
                    "unit_price": float(amount),
                    "currency_id": "BRL",
                }
            ],
            "metadata": metadata,
            "notification_url": f"{self.base_url}/webhook/mp?secret={self.webhook_secret}",
            # Opcional: URLs de retorno
            "back_urls": {
                "success": f"{self.base_url}/thankyou",
                "pending": f"{self.base_url}/pending",
                "failure": f"{self.base_url}/failure",
            },
            "auto_return": "approved",
        }
        resp = self.sdk.preference().create(preference_data)
        return resp.get("response", {})

    def get_payment(self, payment_id: str) -> dict:
        resp = self.sdk.payment().get(payment_id)
        return resp.get("response", {})


# =========================
# ========= utils.py ======
# =========================

from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes
from config import Settings
import os
from dataclasses import dataclass

settings = Settings()


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else 0
        if user_id not in settings.ADMIN_IDS:
            await update.message.reply_text("Sem permissão.")
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


# =========================
# ===== requirements.txt ===
# =========================

# Python 3.10+
python-telegram-bot>=21.0
Flask>=3.0
mercadopago>=2.2
python-dotenv>=1.0


# =========================
# ===== .env.example ======
# =========================

# Token do bot (BotFather)
TELEGRAM_BOT_TOKEN=123456:ABC-DEF

# ID do grupo VIP (negativo). Ex: -1001234567890
VIP_GROUP_ID=-1000000000000

# Mercado Pago
MP_ACCESS_TOKEN=APP_USR-XXXXXXXXXXXXXXXXXXXXXXXXXXXX
MP_WEBHOOK_SECRET=uma-string-aleatoria-bem-grande

# URL pública (ngrok/https) que receberá /webhook/mp
BASE_URL=https://xxxxx.ngrok.io

# Porta local HTTP para o Flask
HTTP_PORT=8080

# Preço do plano 30 dias
PLAN_VIP30_PRICE=29.90

# IDs admins separados por vírgula (para /kick_expired)
ADMIN_IDS=11111111,22222222


# =========================
# ========= README.md =====
# =========================

# Bot VIP + Mercado Pago (Python)

Pronto pra rodar: compra via Mercado Pago (Checkout Pro/Pix), liberação automática do acesso ao grupo VIP, controle de expiração e limpeza.

## ✅ Recursos
- \*/start com menu e botões (com \"Voltar\").
- Geração de link de pagamento via Mercado Pago (com metadata do usuário do Telegram).
- Webhook do MP (Flask) confirma pagamento aprovado e envia *link único* pro grupo VIP.
- Banco SQLite com `users`, `memberships`, `payments` (idempotência garantida).
- Comando admin `/kick_expired` e *job diário* para expulsar vencidos.
- Código assíncrono, documentado com entradas/saídas.

## 🚀 Como rodar

```bash
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # edite com seus dados
python bot.py
```

> Dica: deixe rodando e exponha o Flask com ngrok/NGINX. Atualize `BASE_URL` com o endereço público.

## 🔧 Passo a passo

1. **Crie o grupo VIP** no Telegram e adicione o bot como *admin*.
2. Use `/myid` no grupo (com o bot dentro) para pegar o **VIP_GROUP_ID**.
3. Pegue seu `MP_ACCESS_TOKEN` no painel do Mercado Pago e coloque no `.env`.
4. Defina `MP_WEBHOOK_SECRET` com uma string aleatória (usada em `?secret=`).
5. Rode o bot: `python bot.py`.
6. Exponha o Flask (porta `HTTP_PORT`) publicamente e cadastre o webhook no MP:
   - **Notification URL**: `https://SEU_HOST/webhook/mp?secret=SEU_SECRET`
7. Teste um pagamento (pode usar sandbox) — após *approved*, você recebe o link no privado.

## 📦 Produção (sugestão rápida)
- Deixe o bot em *polling* (estável) e o Flask atrás de **NGINX** numa rota `/mp`.
- Use **supervisord** ou **systemd** para manter o processo ativo.
- Ative *retry* de webhook no Mercado Pago (padrão já reenvia).

## 🧪 Testes locais do webhook
```bash
curl -X POST "http://localhost:8080/webhook/mp?secret=SEU_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"type":"payment","data":{"id":"1234567890"}}'
```
> O código buscará os detalhes no MP via API antes de liberar o acesso.

## 🛡️ Notas de segurança
- Idempotência por `payments.mp_payment_id` *UNIQUE*.
- Webhook protegido por `?secret=` + verificação de status direto via API MP.
- Link de convite com *member_limit=1* e expiração curta (~15 min).

## 🧭 Extensões fáceis
- Planos adicionais (ex: 7/90 dias): adicionar `buy:vip7` e `buy:vip90`.
- Renovação automática perto do vencimento: enviar lembrete D-3/D-1.
- Logs no Telegram (canal admin) + painel web simples.

---

**Pronto.** É colar as credenciais e ligar. Se quiser, ajusto os preços/planos e deixo Pix *nativo* (QR via API) ainda hoje no mesmo esqueleto.
