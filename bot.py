import os
import ssl
import json
import uuid
import logging
import aiohttp
import certifi
from datetime import datetime
from dotenv import load_dotenv
from groq import Groq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TON_API_BASE = "https://toncenter.com/api/v2"
COINGECKO_API = "https://api.coingecko.com/api/v3"

# Supported coins: symbol -> CoinGecko ID
SUPPORTED_COINS = {
    "TON":   "the-open-network",
    "NOT":   "notcoin",
    "DOGS":  "dogs-2",
    "BTC":   "bitcoin",
    "ETH":   "ethereum",
    "SOL":   "solana",
    "BNB":   "binancecoin",
    "USDT":  "tether",
    "USDC":  "usd-coin",
    "DOGE":  "dogecoin",
    "ADA":   "cardano",
    "TRX":   "tron",
}

SYSTEM_PROMPT = """You are TON Copilot, an AI financial assistant on the TON blockchain inside Telegram.
You help users with prices, wallet balances, transaction history, portfolio tracking, DeFi yields, and price alerts.
You support TON, NOT, DOGS, BTC, ETH, SOL, BNB, USDT, USDC, DOGE, ADA, TRX and more.
Be friendly, concise, and explain crypto simply. Never ask for private keys or seed phrases.
Supported DEXes: STON.fi, DeDust.io, Megaton Finance. Wallets: Tonkeeper, TON Space. Explorer: tonscan.org"""

INTENT_PROMPT = """You are an intent classifier for a TON blockchain Telegram bot.
Analyze the user message and return ONLY a JSON object with this exact structure:
{
  "intent": "<intent_name>",
  "address": "<TON address if found, else null>",
  "amount": "<number if found, else null>",
  "token": "<token symbol if found e.g. BTC, ETH, TON, else null>"
}

Intent options:
- "get_price": user wants token price (e.g. price of TON, how much is BTC, ETH price)
- "get_balance": user wants TON wallet balance (e.g. check balance, how much TON in EQ...)
- "get_transactions": user wants transaction history (e.g. show transactions, recent txns)
- "convert": user wants to convert amount to USD (e.g. how much is 10 TON in dollars)
- "send_ton": user wants to send a specific TON amount to a specific address
- "send_guide": user asks how to send TON but no specific address or amount
- "swap_guide": user wants to swap tokens
- "portfolio": user wants to see their portfolio
- "yields": user wants DeFi yield info
- "chat": general question or conversation

For "send_ton", extract both "address" and "amount".
Return ONLY the JSON, no explanation, no markdown."""


# ── SSL helper ───────────────────────────────────────────────────────────────

def make_connector():
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_context)


# ── Blockchain helpers ───────────────────────────────────────────────────────

async def get_prices(coin_ids: list):
    """Fetch prices for a list of CoinGecko IDs"""
    try:
        connector = make_connector()
        headers = {"User-Agent": "TONCopilot/1.0"}
        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            params = {
                "ids": ",".join(coin_ids),
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_market_cap": "true"
            }
            async with session.get(f"{COINGECKO_API}/simple/price", params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return await resp.json()
    except Exception as e:
        logger.error(f"Error fetching prices: {e}")
        return None


async def get_ton_price():
    data = await get_prices(["the-open-network"])
    if data:
        ton = data.get("the-open-network", {})
        return {"price": ton.get("usd", 0), "change_24h": ton.get("usd_24h_change", 0),
                "market_cap": ton.get("usd_market_cap", 0)}
    return None


async def get_wallet_balance(address: str):
    try:
        connector = make_connector()
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(f"{TON_API_BASE}/getAddressBalance",
                                   params={"address": address},
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return {"balance": int(data["result"]) / 1_000_000_000, "address": address}
    except Exception as e:
        logger.error(f"Error fetching wallet balance: {e}")
    return None


async def get_transactions(address: str, limit: int = 5):
    try:
        connector = make_connector()
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(f"{TON_API_BASE}/getTransactions",
                                   params={"address": address, "limit": limit},
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return data["result"]
    except Exception as e:
        logger.error(f"Error fetching transactions: {e}")
    return None


def format_transactions(txns: list, address: str) -> str:
    if not txns:
        return "No transactions found for this wallet."
    lines = ["📋 *Recent Transactions*\n"]
    for i, tx in enumerate(txns[:5], 1):
        ts = tx.get("utime", 0)
        dt = datetime.utcfromtimestamp(ts).strftime("%b %d, %H:%M") if ts else "Unknown"
        in_msg = tx.get("in_msg") or {}
        out_msgs = tx.get("out_msgs") or []
        try:
            in_value = int(in_msg.get("value") or 0) / 1_000_000_000
        except (ValueError, TypeError):
            in_value = 0
        try:
            out_value = sum(int(m.get("value") or 0) for m in out_msgs) / 1_000_000_000
        except (ValueError, TypeError):
            out_value = 0
        in_src = in_msg.get("source") or ""
        is_incoming = bool(in_src) and in_value > 0
        if is_incoming:
            short_src = f"{in_src[:6]}...{in_src[-4:]}" if len(in_src) > 10 else in_src
            direction = f"📥 +{in_value:.4f} TON"
            counterpart = f"From: `{short_src}`"
        elif out_value > 0:
            dest = (out_msgs[0].get("destination") or "") if out_msgs else ""
            short_dest = f"{dest[:6]}...{dest[-4:]}" if len(dest) > 10 else dest
            direction = f"📤 -{out_value:.4f} TON"
            counterpart = f"To: `{short_dest}`"
        else:
            direction = "⚙️ Contract interaction"
            counterpart = ""
        tx_hash = (tx.get("transaction_id") or {}).get("hash") or ""
        line = f"*{i}.* {direction}\n   🕐 {dt} UTC\n"
        if counterpart:
            line += f"   {counterpart}\n"
        if tx_hash:
            line += f"   🔗 [View tx](https://tonscan.org/tx/{tx_hash})\n"
        lines.append(line)
    lines.append(f"[View all on Explorer](https://tonscan.org/address/{address}#transactions)")
    return "\n".join(lines)


# ── Price alert background job ───────────────────────────────────────────────

async def check_price_alerts(context):
    bot_data = context.application.bot_data
    alerts = bot_data.get("price_alerts", {})
    if not alerts:
        return

    # Collect all unique coin IDs needed
    coin_ids = list({a.get("cg_id", "the-open-network") for a in alerts.values()})
    prices_data = await get_prices(coin_ids)
    if not prices_data:
        return

    triggered = []
    for alert_id, alert in list(alerts.items()):
        target = alert["target"]
        direction = alert["direction"]
        chat_id = alert["chat_id"]
        symbol = alert.get("symbol", "TON")
        cg_id = alert.get("cg_id", "the-open-network")
        current_price = prices_data.get(cg_id, {}).get("usd", 0)
        if not current_price:
            continue
        if direction == "above" and current_price >= target:
            triggered.append((alert_id, chat_id, target, direction, current_price, symbol))
        elif direction == "below" and current_price <= target:
            triggered.append((alert_id, chat_id, target, direction, current_price, symbol))

    for alert_id, chat_id, target, direction, price, symbol in triggered:
        emoji = "📈" if direction == "above" else "📉"
        # Format based on price magnitude
        def fmt(p):
            if p >= 1000: return f"${p:,.2f}"
            elif p >= 1: return f"${p:.4f}"
            else: return f"${p:.6f}"
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{emoji} *Price Alert Triggered!*\n\n"
                    f"*{symbol}* has gone *{direction}* your target!\n\n"
                    f"🎯 Target: {fmt(target)}\n"
                    f"💹 Current: {fmt(price)}\n\n"
                    f"[View on CoinGecko](https://www.coingecko.com)"
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💹 Live Prices", callback_data="price"),
                    InlineKeyboardButton("🏠 Menu", callback_data="menu")
                ]])
            )
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")
        del alerts[alert_id]
    bot_data["price_alerts"] = alerts


# ── Intent classifier ────────────────────────────────────────────────────────

async def classify_intent(message: str) -> dict:
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": INTENT_PROMPT},
                {"role": "user", "content": message}
            ],
            max_tokens=150,
            temperature=0.1,
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Intent classification error: {e}")
        return {"intent": "chat", "address": None, "amount": None, "token": None}


# ── Keyboard helpers ─────────────────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💹 Live Prices", callback_data="price"),
         InlineKeyboardButton("👛 My Wallet", callback_data="my_wallet")],
        [InlineKeyboardButton("📊 Portfolio", callback_data="portfolio"),
         InlineKeyboardButton("🏦 DeFi Yields", callback_data="yields")],
        [InlineKeyboardButton("🔔 Price Alerts", callback_data="set_alert"),
         InlineKeyboardButton("📋 Transactions", callback_data="transactions")],
        [InlineKeyboardButton("📤 Send TON", callback_data="send"),
         InlineKeyboardButton("🤖 Ask AI", callback_data="ask_ai")],
    ])

def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")]])


# ── Intent actions ───────────────────────────────────────────────────────────

async def action_get_price(update, context, token=None):
    """Show prices — optionally for a specific token"""
    # Build list of coins to fetch
    if token and token.upper() in SUPPORTED_COINS:
        coin_ids = [SUPPORTED_COINS[token.upper()]]
        specific = token.upper()
    else:
        # Default: show main TON ecosystem + major coins
        coin_ids = list(SUPPORTED_COINS.values())
        specific = None

    prices = await get_prices(coin_ids)
    if not prices:
        msg = "⚠️ Couldn't fetch prices right now. Try again!"
        target = update.message if update.message else update.callback_query.message
        await target.reply_text(msg)
        return

    # Build reverse map: coingecko_id -> symbol
    id_to_symbol = {v: k for k, v in SUPPORTED_COINS.items()}

    if specific:
        # Single coin view
        cg_id = SUPPORTED_COINS[specific]
        data = prices.get(cg_id, {})
        price = data.get("usd", 0)
        change = data.get("usd_24h_change", 0)
        mcap = data.get("usd_market_cap", 0)
        emoji = "📈" if change >= 0 else "📉"
        mcap_text = f"MCap: ${mcap/1_000_000_000:.2f}B\n" if mcap else ""
        text = (
            f"💹 *{specific} Price*\n\n"
            f"{emoji} *${price:.6f}*\n"
            f"24h: {change:+.2f}%\n"
            f"{mcap_text}\n"
            f"_Updated just now_ ⚡"
        )
    else:
        # Multi-coin view — TON ecosystem first, then majors
        lines = ["💹 *Live Crypto Prices*\n"]
        order = ["TON", "NOT", "DOGS", "BTC", "ETH", "SOL", "BNB", "DOGE", "ADA", "TRX", "USDT", "USDC"]
        for symbol in order:
            cg_id = SUPPORTED_COINS.get(symbol)
            if not cg_id:
                continue
            data = prices.get(cg_id, {})
            price = data.get("usd", 0)
            change = data.get("usd_24h_change", 0)
            if not price:
                continue
            emoji = "📈" if change >= 0 else "📉"
            # Format price sensibly
            if price >= 1000:
                price_str = f"${price:,.2f}"
            elif price >= 1:
                price_str = f"${price:.4f}"
            else:
                price_str = f"${price:.6f}"
            lines.append(f"{emoji} *{symbol}* {price_str} ({change:+.2f}%)")
        lines.append("\n_Updated just now_ ⚡")
        text = "\n".join(lines)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="price"),
         InlineKeyboardButton("📊 Portfolio", callback_data="portfolio")],
        [InlineKeyboardButton("🔔 Set Alert", callback_data="set_alert"),
         InlineKeyboardButton("🏠 Menu", callback_data="menu")]
    ])
    target = update.message if update.message else update.callback_query.message
    await target.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def action_get_balance(update, context, address):
    if not address or not ((address.startswith("EQ") or address.startswith("UQ")) and len(address) >= 40):
        saved = context.user_data.get("saved_wallet")
        if saved:
            address = saved
        else:
            target = update.message if update.message else update.callback_query.message
            await target.reply_text(
                "👛 I need a wallet address!\n\nTry: _\"check balance of EQD4FP...\"_\nOr save yours: `/savewallet <address>`",
                parse_mode="Markdown"
            )
            return
    target = update.message if update.message else update.callback_query.message
    await target.reply_text("🔍 Looking up wallet...")
    result = await get_wallet_balance(address)
    if not result:
        await target.reply_text(f"⚠️ Couldn't fetch balance. [Check on tonscan.org](https://tonscan.org/address/{address})", parse_mode="Markdown")
        return
    balance = result["balance"]
    ton_data = await get_ton_price()
    usd_value = balance * ton_data["price"] if ton_data else None
    short = f"{address[:6]}...{address[-4:]}"
    usd_text = f"≈ *${usd_value:,.2f} USD*" if usd_value else ""
    await target.reply_text(
        f"👛 *Wallet Balance*\n\n📍 `{short}`\n💎 *{balance:,.4f} TON*\n{usd_text}\n\n🔗 [Explorer](https://tonscan.org/address/{address})",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Transactions", callback_data="transactions"),
             InlineKeyboardButton("🔄 Refresh", callback_data="my_wallet")],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu")]
        ])
    )


async def action_get_transactions(update, context, address):
    if not address or not ((address.startswith("EQ") or address.startswith("UQ")) and len(address) >= 40):
        saved = context.user_data.get("saved_wallet")
        if saved:
            address = saved
        else:
            target = update.message if update.message else update.callback_query.message
            await target.reply_text(
                "📋 I need a wallet address!\n\nTry: _\"show transactions for EQD4FP...\"_\nOr save yours: `/savewallet <address>`",
                parse_mode="Markdown"
            )
            return
    target = update.message if update.message else update.callback_query.message
    await target.reply_text("🔍 Fetching transactions...")
    txns = await get_transactions(address)
    text = format_transactions(txns, address)
    await target.reply_text(text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="transactions"),
             InlineKeyboardButton("👛 Balance", callback_data="my_wallet")],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu")]
        ])
    )


async def action_convert(update, context, amount, token=None):
    symbol = (token or "TON").upper()
    cg_id = SUPPORTED_COINS.get(symbol, "the-open-network")
    prices = await get_prices([cg_id])
    if not prices:
        target = update.message if update.message else update.callback_query.message
        await target.reply_text("⚠️ Couldn't fetch price for conversion.")
        return
    current_price = prices.get(cg_id, {}).get("usd", 0)
    try:
        amt = float(amount) if amount else 1.0
    except (ValueError, TypeError):
        amt = 1.0
    usd = amt * current_price
    target = update.message if update.message else update.callback_query.message
    await target.reply_text(
        f"💱 *{symbol} Converter*\n\n"
        f"*{amt:.4f} {symbol}* = *${usd:,.2f} USD*\n\n"
        f"Rate: 1 {symbol} = ${current_price:.6f}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("💹 All Prices", callback_data="price"),
            InlineKeyboardButton("🏠 Menu", callback_data="menu")
        ]])
    )


async def action_send_ton(update, context, address, amount):
    if not address or not ((address.startswith("EQ") or address.startswith("UQ")) and len(address) >= 40):
        await update.message.reply_text(
            "📤 I need a destination address!\nTry: _\"send 5 TON to EQD4FP...\"_",
            parse_mode="Markdown"
        )
        return
    try:
        ton_amount = float(amount)
        if ton_amount <= 0:
            raise ValueError
    except (ValueError, TypeError):
        await update.message.reply_text("📤 I need a valid amount!\nTry: _\"send 5 TON to EQD4FP...\"_", parse_mode="Markdown")
        return
    ton_data = await get_ton_price()
    usd_value = ton_amount * ton_data["price"] if ton_data else None
    usd_text = f"\n   (~${usd_value:.2f} USD)" if usd_value else ""
    nanotons = int(ton_amount * 1_000_000_000)
    tonkeeper_link = f"https://app.tonkeeper.com/transfer/{address}?amount={nanotons}"
    short_addr = f"{address[:8]}...{address[-6:]}"
    await update.message.reply_text(
        f"📤 *Ready to Send TON*\n\n"
        f"💎 Amount: *{ton_amount:.4f} TON*{usd_text}\n"
        f"📍 To: `{short_addr}`\n"
        f"⛽ Fee: ~0.005 TON\n\n"
        f"Tap below to open in Tonkeeper — just confirm!\n\n"
        f"⚠️ _Always verify the address before confirming!_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Open in Tonkeeper", url=tonkeeper_link)],
            [InlineKeyboardButton("❌ Cancel", callback_data="menu")]
        ])
    )


async def action_chat(update, context, user_message):
    try:
        if "history" not in context.user_data:
            context.user_data["history"] = []
        context.user_data["history"].append({"role": "user", "content": user_message})
        history = context.user_data["history"][-10:]
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, *history],
            max_tokens=1024,
            temperature=0.7,
        )
        assistant_message = response.choices[0].message.content
        context.user_data["history"].append({"role": "assistant", "content": assistant_message})
        target = update.message if update.message else update.callback_query.message
        await target.reply_text(assistant_message,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Menu", callback_data="menu"),
                InlineKeyboardButton("💹 Prices", callback_data="price")
            ]])
        )
    except Exception as e:
        logger.error(f"Chat error: {e}")
        target = update.message if update.message else update.callback_query.message
        await target.reply_text("⚠️ Something went wrong. Please try again!")


# ── Portfolio helpers ────────────────────────────────────────────────────────

async def show_portfolio(update, context):
    portfolio = context.user_data.get("portfolio", {})
    target = update.message if update.message else update.callback_query.message

    if not portfolio:
        await target.reply_text(
            "📊 *Your Portfolio is Empty*\n\n"
            "Add holdings with:\n"
            "`/addholding TON 100 1.20` — 100 TON bought at $1.20\n"
            "`/addholding BTC 0.01 60000` — 0.01 BTC at $60,000\n"
            "`/addholding ETH 2 2500` — 2 ETH at $2,500\n"
            "`/addholding DOGS 1000000 0.0005` — 1M DOGS\n\n"
            f"Supported: {', '.join(SUPPORTED_COINS.keys())}",
            parse_mode="Markdown"
        )
        return

    coin_ids = list({SUPPORTED_COINS[s] for s in portfolio if s in SUPPORTED_COINS})
    prices = await get_prices(coin_ids)
    if not prices:
        await target.reply_text("⚠️ Couldn't fetch prices. Try again!")
        return

    id_to_price = {cg_id: data.get("usd", 0) for cg_id, data in prices.items()}

    lines = ["📊 *Your Portfolio*\n"]
    total_invested = 0.0
    total_current = 0.0

    for symbol, holding in portfolio.items():
        cg_id = SUPPORTED_COINS.get(symbol)
        if not cg_id:
            continue
        amount = holding["amount"]
        buy_price = holding["buy_price"]
        current_price = id_to_price.get(cg_id, 0)
        invested = amount * buy_price
        current_val = amount * current_price
        pnl = current_val - invested
        pnl_pct = (pnl / invested * 100) if invested > 0 else 0
        total_invested += invested
        total_current += current_val
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        sign = "+" if pnl >= 0 else ""
        # Format price based on magnitude
        def fmt(p):
            if p >= 1000: return f"${p:,.2f}"
            elif p >= 1: return f"${p:.4f}"
            else: return f"${p:.6f}"
        lines.append(
            f"*{symbol}* — {amount:,.4f} units\n"
            f"   Buy: {fmt(buy_price)} → Now: {fmt(current_price)}\n"
            f"   Value: *${current_val:,.2f}* {pnl_emoji} {sign}${pnl:,.2f} ({sign}{pnl_pct:.1f}%)\n"
        )

    total_pnl = total_current - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0
    t_emoji = "📈" if total_pnl >= 0 else "📉"
    t_sign = "+" if total_pnl >= 0 else ""

    lines.append(
        f"{'─'*28}\n"
        f"💼 Invested: *${total_invested:,.2f}*\n"
        f"💰 Value: *${total_current:,.2f}*\n"
        f"{t_emoji} Total PnL: *{t_sign}${total_pnl:,.2f} ({t_sign}{total_pnl_pct:.1f}%)*"
    )

    await target.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="portfolio"),
             InlineKeyboardButton("💹 Prices", callback_data="price")],
            [InlineKeyboardButton("➕ Add Holding", callback_data="add_holding"),
             InlineKeyboardButton("🗑️ Clear All", callback_data="clear_portfolio")],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu")]
        ])
    )


# ── Command handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"👋 *Welcome to TON Copilot, {user.first_name}!*\n\n"
        "Your AI-powered crypto assistant on TON.\n\n"
        "💡 *Just type naturally:*\n"
        "• _\"Price of BTC\"_\n"
        "• _\"How much is 10 ETH in dollars?\"_\n"
        "• _\"Check balance of EQD4FP...\"_\n"
        "• _\"Send 5 TON to EQ...\"_\n\n"
        "Or tap a button below 👇",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏠 *Main Menu*", parse_mode="Markdown", reply_markup=main_menu_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins = ", ".join(SUPPORTED_COINS.keys())
    await update.message.reply_text(
        "🤖 *TON Copilot Commands*\n\n"
        "/price — Live prices for all coins\n"
        "/portfolio — Your portfolio & PnL\n"
        "/addholding — Add a coin to portfolio\n"
        "/removeholding — Remove a coin\n"
        "/balance `<address>` — TON wallet balance\n"
        "/history `<address>` — Transaction history\n"
        "/savewallet `<address>` — Save your wallet\n"
        "/mywallet — Check saved wallet\n"
        "/mytxns — Your transactions\n"
        "/setalert — Set a price alert\n"
        "/myalerts — View active alerts\n"
        "/cancelalerts — Cancel all alerts\n"
        "/yields — Best DeFi yields on TON\n"
        "/send — How to send TON\n"
        "/swap — Best DEXes on TON\n\n"
        f"📊 *Supported coins:*\n{coins}\n\n"
        "💡 Or just chat naturally — no commands needed!",
        parse_mode="Markdown",
        reply_markup=back_keyboard()
    )

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await action_get_price(update, context)

async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await show_portfolio(update, context)

async def addholding_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    coins_list = ", ".join(SUPPORTED_COINS.keys())
    if not args or len(args) < 3:
        await update.message.reply_text(
            "➕ *Add a Holding*\n\n"
            "Usage: `/addholding <TOKEN> <AMOUNT> <BUY_PRICE>`\n\n"
            "Examples:\n"
            "`/addholding TON 100 1.20`\n"
            "`/addholding BTC 0.01 60000`\n"
            "`/addholding ETH 2 2500`\n"
            "`/addholding DOGS 1000000 0.0005`\n"
            "`/addholding SOL 10 150`\n\n"
            f"Supported: {coins_list}",
            parse_mode="Markdown"
        )
        return
    symbol = args[0].upper()
    if symbol not in SUPPORTED_COINS:
        await update.message.reply_text(
            f"⚠️ *{symbol}* not supported yet.\n\nSupported: {coins_list}",
            parse_mode="Markdown"
        )
        return
    try:
        amount = float(args[1])
        buy_price = float(args[2])
        if amount <= 0 or buy_price <= 0:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text("⚠️ Invalid amount or price.\nExample: `/addholding BTC 0.01 60000`", parse_mode="Markdown")
        return

    if "portfolio" not in context.user_data:
        context.user_data["portfolio"] = {}
    context.user_data["portfolio"][symbol] = {"amount": amount, "buy_price": buy_price, "symbol": symbol}

    invested = amount * buy_price
    # Quick PnL preview
    cg_id = SUPPORTED_COINS[symbol]
    prices = await get_prices([cg_id])
    current_price = prices.get(cg_id, {}).get("usd", 0) if prices else 0
    current_val = amount * current_price
    pnl = current_val - invested
    pnl_pct = (pnl / invested * 100) if invested > 0 else 0
    sign = "+" if pnl >= 0 else ""
    pnl_emoji = "📈" if pnl >= 0 else "📉"

    await update.message.reply_text(
        f"✅ *{symbol} Added to Portfolio*\n\n"
        f"Amount: {amount:,.6f} {symbol}\n"
        f"Buy price: ${buy_price:,.6f}\n"
        f"Invested: ${invested:,.2f}\n\n"
        f"Current: ${current_price:,.6f}\n"
        f"Value now: ${current_val:,.2f}\n"
        f"{pnl_emoji} PnL: *{sign}${pnl:,.2f} ({sign}{pnl_pct:.1f}%)*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 View Portfolio", callback_data="portfolio")],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu")]
        ])
    )

async def removeholding_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        portfolio = context.user_data.get("portfolio", {})
        if not portfolio:
            await update.message.reply_text("📊 Your portfolio is empty.")
            return
        coins = ", ".join(portfolio.keys())
        await update.message.reply_text(
            f"🗑️ Usage: `/removeholding <TOKEN>`\n\nYour holdings: {coins}",
            parse_mode="Markdown"
        )
        return
    symbol = args[0].upper()
    portfolio = context.user_data.get("portfolio", {})
    if symbol not in portfolio:
        await update.message.reply_text(f"⚠️ *{symbol}* not in your portfolio.", parse_mode="Markdown")
        return
    del context.user_data["portfolio"][symbol]
    await update.message.reply_text(
        f"✅ *{symbol}* removed from portfolio.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📊 View Portfolio", callback_data="portfolio")]])
    )

async def savewallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("💾 Usage: `/savewallet <your_wallet_address>`", parse_mode="Markdown")
        return
    address = args[0].strip()
    if not (address.startswith("EQ") or address.startswith("UQ")) or len(address) < 40:
        await update.message.reply_text("⚠️ Invalid TON address. Must start with `EQ` or `UQ`.", parse_mode="Markdown")
        return
    context.user_data["saved_wallet"] = address
    short = f"{address[:6]}...{address[-4:]}"
    await update.message.reply_text(
        f"✅ *Wallet saved!*\n\n📍 `{short}`\n\nNow just say _\"check my balance\"_ or _\"show my transactions\"_!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("👛 Check Balance", callback_data="my_wallet"),
            InlineKeyboardButton("📋 Transactions", callback_data="transactions")
        ]])
    )

async def mywallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = context.user_data.get("saved_wallet")
    if not address:
        await update.message.reply_text("👛 No wallet saved. Use `/savewallet <address>`", parse_mode="Markdown")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await action_get_balance(update, context, address)

async def mytxns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = context.user_data.get("saved_wallet")
    if not address:
        await update.message.reply_text("👛 No wallet saved. Use `/savewallet <address>`", parse_mode="Markdown")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await action_get_transactions(update, context, address)

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("📋 Usage: `/history <wallet_address>`", parse_mode="Markdown")
        return
    address = args[0].strip()
    if not (address.startswith("EQ") or address.startswith("UQ")) or len(address) < 40:
        await update.message.reply_text("⚠️ Invalid TON address.", parse_mode="Markdown")
        return
    await update.message.reply_text("🔍 Fetching transactions...")
    await action_get_transactions(update, context, address)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    args = context.args
    if not args:
        await update.message.reply_text("👛 Usage: `/balance <wallet_address>`", parse_mode="Markdown")
        return
    await action_get_balance(update, context, args[0].strip())

async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📤 *How to Send TON*\n\n"
        "*Tonkeeper:* Open → Send → Address → Amount → Confirm\n"
        "*TON Space:* Open @wallet → Send → Follow steps\n\n"
        "⚠️ Always double-check the address!\n"
        "💡 Fee: ~0.005 TON · Speed: ~5 seconds\n\n"
        "Or just type: _\"send 5 TON to EQ...\"_ and I'll set it up!",
        parse_mode="Markdown", reply_markup=back_keyboard()
    )

async def swap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔄 *Swap Tokens on TON*\n\n"
        "1️⃣ [STON.fi](https://ston.fi) — Most popular\n"
        "2️⃣ [DeDust.io](https://dedust.io) — Great liquidity\n"
        "3️⃣ [Megaton Finance](https://megaton.fi) — Yield farming\n\n"
        "Connect Tonkeeper → Select tokens → Confirm swap!",
        parse_mode="Markdown", reply_markup=back_keyboard()
    )

async def yields_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    ton_data = await get_ton_price()
    ton_price = ton_data["price"] if ton_data else 0
    yields_data = [
        {"name": "TON Whales Staking", "type": "Liquid Staking", "apy": "~4.5%", "risk": "Low", "url": "https://tonwhales.com/staking", "min": "1 TON"},
        {"name": "Bemo Finance", "type": "Liquid Staking", "apy": "~4.3%", "risk": "Low", "url": "https://bemo.finance", "min": "1 TON"},
        {"name": "STON.fi TON/USDT", "type": "LP Farming", "apy": "~8-15%", "risk": "Medium", "url": "https://ston.fi/pools", "min": "Any"},
        {"name": "DeDust TON/NOT", "type": "LP Farming", "apy": "~12-20%", "risk": "Medium-High", "url": "https://dedust.io/pools", "min": "Any"},
        {"name": "Evaa Protocol", "type": "Lending", "apy": "~3-6%", "risk": "Low-Medium", "url": "https://evaa.finance", "min": "1 TON"},
    ]
    risk_emoji = {"Low": "🟢", "Low-Medium": "🟡", "Medium": "🟡", "Medium-High": "🟠"}
    lines = ["🏦 *Best DeFi Yields on TON*\n", f"_TON price: ${ton_price:.4f}_\n"]
    for y in yields_data:
        r = risk_emoji.get(y["risk"], "⚪")
        lines.append(f"*{y['name']}*\n   💰 APY: *{y['apy']}* | {y['type']}\n   {r} Risk: {y['risk']} | Min: {y['min']}\n   🔗 [{y['url'].replace('https://','')}]({y['url']})\n")
    lines.append("⚠️ _APYs are estimates. Always DYOR!_")
    target = update.message if update.message else update.callback_query.message
    await target.reply_text("\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="yields"),
             InlineKeyboardButton("💹 Prices", callback_data="price")],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu")]
        ])
    )

async def setalert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "🔔 *Set a Price Alert*\n\n"
            "`/setalert BTC 72000` — when BTC hits $72,000\n"
            "`/setalert ETH above 3500` — when ETH goes above $3,500\n"
            "`/setalert TON below 1.20` — when TON drops below $1.20\n"
            "`/setalert TON 2.00` — when TON hits $2\n\n"
            f"Supported: {', '.join(SUPPORTED_COINS.keys())}",
            parse_mode="Markdown"
        )
        return

    try:
        # Parse: /setalert <COIN> <PRICE>
        # or:    /setalert <COIN> above/below <PRICE>
        # or:    /setalert <PRICE>  (defaults to TON)
        if len(args) == 1:
            # /setalert 2.00  → TON
            symbol = "TON"
            target_price = float(args[0])
            direction = None  # auto-detect below
        elif len(args) == 2:
            first = args[0].upper()
            if first in SUPPORTED_COINS:
                # /setalert BTC 72000
                symbol = first
                target_price = float(args[1])
                direction = None
            else:
                # /setalert above 2.00  → TON
                symbol = "TON"
                direction = args[0].lower()
                if direction not in ("above", "below"):
                    raise ValueError
                target_price = float(args[1])
        elif len(args) == 3:
            # /setalert BTC above 72000
            symbol = args[0].upper()
            direction = args[1].lower()
            if direction not in ("above", "below"):
                raise ValueError
            target_price = float(args[2])
        else:
            raise ValueError

        if symbol not in SUPPORTED_COINS:
            await update.message.reply_text(
                f"⚠️ *{symbol}* not supported.\n\nSupported: {', '.join(SUPPORTED_COINS.keys())}",
                parse_mode="Markdown"
            )
            return

        # Auto-detect direction if not specified — fetch price once below
        pass

    except (ValueError, IndexError):
        await update.message.reply_text(
            "⚠️ Invalid format.\n\nExamples:\n"
            "`/setalert BTC 72000`\n"
            "`/setalert ETH above 3500`\n"
            "`/setalert TON 2.00`",
            parse_mode="Markdown"
        )
        return

    # Fetch current price (always, for both display and direction detection)
    cg_id = SUPPORTED_COINS[symbol]
    prices = await get_prices([cg_id])
    current_price = prices.get(cg_id, {}).get("usd", 0) if prices else 0

    if direction is None:
        direction = "above" if target_price > current_price else "below"

    alert_id = str(uuid.uuid4())[:8]
    bot_data = context.application.bot_data
    if "price_alerts" not in bot_data:
        bot_data["price_alerts"] = {}
    bot_data["price_alerts"][alert_id] = {
        "chat_id": update.effective_chat.id,
        "target": target_price,
        "direction": direction,
        "symbol": symbol,
        "cg_id": cg_id,
        "alert_id": alert_id
    }

    emoji = "📈" if direction == "above" else "📉"
    # Format price sensibly
    if current_price >= 1000:
        current_str = f"${current_price:,.2f}"
        target_str = f"${target_price:,.2f}"
    elif current_price >= 1:
        current_str = f"${current_price:.4f}"
        target_str = f"${target_price:.4f}"
    else:
        current_str = f"${current_price:.6f}"
        target_str = f"${target_price:.6f}"

    await update.message.reply_text(
        f"✅ *Alert Set!*\n\n"
        f"{emoji} Notify when *{symbol}* goes *{direction}* {target_str}\n"
        f"💹 Current {symbol} price: {current_str}\n\n"
        f"Checking every 60 seconds 👀",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 My Alerts", callback_data="my_alerts"),
            InlineKeyboardButton("🏠 Menu", callback_data="menu")
        ]])
    )

async def myalerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.application.bot_data
    alerts = bot_data.get("price_alerts", {})
    chat_id = update.effective_chat.id
    user_alerts = {k: v for k, v in alerts.items() if v["chat_id"] == chat_id}
    if not user_alerts:
        await update.message.reply_text("🔔 No active alerts.\n\nSet one with:\n`/setalert BTC 72000`\n`/setalert TON 2.00`\n`/setalert ETH above 3500`", parse_mode="Markdown")
        return
    lines = ["🔔 *Your Active Price Alerts*\n"]
    for alert_id, alert in user_alerts.items():
        emoji = "📈" if alert["direction"] == "above" else "📉"
        symbol = alert.get("symbol", "TON")
        target = alert["target"]
        direction = alert["direction"]
        if target >= 1000:
            target_str = f"${target:,.2f}"
        elif target >= 1:
            target_str = f"${target:.4f}"
        else:
            target_str = f"${target:.6f}"
        lines.append(f"{emoji} *{symbol}* {direction} {target_str} (ID: `{alert_id}`)")
    lines.append("\nUse /cancelalerts to remove all")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel All", callback_data="cancel_alerts"),
            InlineKeyboardButton("🏠 Menu", callback_data="menu")
        ]])
    )

async def cancelalerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.application.bot_data
    alerts = bot_data.get("price_alerts", {})
    chat_id = update.effective_chat.id
    before = len(alerts)
    bot_data["price_alerts"] = {k: v for k, v in alerts.items() if v["chat_id"] != chat_id}
    removed = before - len(bot_data["price_alerts"])
    await update.message.reply_text(f"✅ Removed {removed} alert(s).\n\nSet new ones with `/setalert <price>`", parse_mode="Markdown")


# ── Smart message handler ────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    logger.info(f"Message from {update.effective_user.first_name}: {user_message}")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    intent_data = await classify_intent(user_message)
    intent = intent_data.get("intent", "chat")
    address = intent_data.get("address")
    amount = intent_data.get("amount")
    token = intent_data.get("token")

    logger.info(f"Intent: {intent} | Token: {token} | Address: {address} | Amount: {amount}")

    if intent == "get_price":
        await action_get_price(update, context, token)
    elif intent == "get_balance":
        await action_get_balance(update, context, address)
    elif intent == "get_transactions":
        await action_get_transactions(update, context, address)
    elif intent == "convert":
        await action_convert(update, context, amount, token)
    elif intent == "send_ton":
        await action_send_ton(update, context, address, amount)
    elif intent == "portfolio":
        await show_portfolio(update, context)
    elif intent == "yields":
        await yields_command(update, context)
    else:
        await action_chat(update, context, user_message)


# ── Callback query handler ───────────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu":
        await query.message.reply_text("🏠 *Main Menu*", parse_mode="Markdown", reply_markup=main_menu_keyboard())
    elif data == "price":
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action="typing")
        await action_get_price(update, context)
    elif data == "my_wallet":
        address = context.user_data.get("saved_wallet")
        if not address:
            await query.message.reply_text("👛 No wallet saved.\n\nUse `/savewallet <address>`", parse_mode="Markdown", reply_markup=back_keyboard())
        else:
            await context.bot.send_chat_action(chat_id=query.message.chat_id, action="typing")
            await action_get_balance(update, context, address)
    elif data == "transactions":
        address = context.user_data.get("saved_wallet")
        if not address:
            await query.message.reply_text("👛 No wallet saved.\n\nUse `/savewallet <address>`", parse_mode="Markdown", reply_markup=back_keyboard())
        else:
            await context.bot.send_chat_action(chat_id=query.message.chat_id, action="typing")
            await action_get_transactions(update, context, address)
    elif data == "portfolio":
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action="typing")
        await show_portfolio(update, context)
    elif data == "add_holding":
        coins = ", ".join(SUPPORTED_COINS.keys())
        await query.message.reply_text(
            f"➕ *Add a Holding*\n\n"
            f"Usage: `/addholding <TOKEN> <AMOUNT> <BUY_PRICE>`\n\n"
            f"Examples:\n"
            f"`/addholding BTC 0.01 60000`\n"
            f"`/addholding ETH 2 2500`\n"
            f"`/addholding TON 100 1.20`\n"
            f"`/addholding DOGS 1000000 0.0005`\n\n"
            f"Supported: {coins}",
            parse_mode="Markdown", reply_markup=back_keyboard()
        )
    elif data == "clear_portfolio":
        context.user_data["portfolio"] = {}
        await query.message.reply_text("🗑️ Portfolio cleared!", reply_markup=back_keyboard())
    elif data == "yields":
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action="typing")
        await yields_command(update, context)
    elif data == "set_alert":
        await query.message.reply_text(
            "🔔 *Set a Price Alert*\n\n"
            "`/setalert 2.00` — when TON hits $2\n"
            "`/setalert above 2.50` — above $2.50\n"
            "`/setalert below 1.20` — below $1.20\n\n"
            "/myalerts — view active alerts\n"
            "/cancelalerts — cancel all",
            parse_mode="Markdown", reply_markup=back_keyboard()
        )
    elif data == "my_alerts":
        bot_data = context.application.bot_data
        alerts = bot_data.get("price_alerts", {})
        chat_id = query.message.chat_id
        user_alerts = {k: v for k, v in alerts.items() if v["chat_id"] == chat_id}
        if not user_alerts:
            await query.message.reply_text("🔔 No active alerts.\n\nSet one with:\n`/setalert BTC 72000`\n`/setalert TON 2.00`", parse_mode="Markdown", reply_markup=back_keyboard())
        else:
            lines = ["🔔 *Your Active Alerts*\n"]
            for _, alert in user_alerts.items():
                emoji = "📈" if alert["direction"] == "above" else "📉"
                symbol = alert.get("symbol", "TON")
                target = alert["target"]
                target_str = f"${target:,.2f}" if target >= 1000 else f"${target:.4f}" if target >= 1 else f"${target:.6f}"
                lines.append(f"{emoji} *{symbol}* {alert['direction']} {target_str}")
            await query.message.reply_text("\n".join(lines), parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel All", callback_data="cancel_alerts"),
                    InlineKeyboardButton("🏠 Menu", callback_data="menu")
                ]])
            )
    elif data == "cancel_alerts":
        bot_data = context.application.bot_data
        alerts = bot_data.get("price_alerts", {})
        chat_id = query.message.chat_id
        before = len(alerts)
        bot_data["price_alerts"] = {k: v for k, v in alerts.items() if v["chat_id"] != chat_id}
        removed = before - len(bot_data["price_alerts"])
        await query.message.reply_text(f"✅ Removed {removed} alert(s).", reply_markup=back_keyboard())
    elif data == "swap":
        await query.message.reply_text(
            "🔄 [STON.fi](https://ston.fi) · [DeDust.io](https://dedust.io) · [Megaton](https://megaton.fi)\n\nConnect Tonkeeper → Select tokens → Swap!",
            parse_mode="Markdown", reply_markup=back_keyboard()
        )
    elif data == "send":
        await query.message.reply_text(
            "📤 *Send TON*\n\nFastest: Open @wallet in Telegram\nOr type: _\"send 5 TON to EQ...\"_\n\n⚠️ Always double-check the address!",
            parse_mode="Markdown", reply_markup=back_keyboard()
        )
    elif data == "ask_ai":
        await query.message.reply_text(
            "🤖 *Just type anything!*\n\n"
            "• _\"Price of BTC\"_\n"
            "• _\"How much is 0.5 ETH?\"_\n"
            "• _\"What is DeFi?\"_\n"
            "• _\"Best TON wallets?\"_",
            parse_mode="Markdown", reply_markup=back_keyboard()
        )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("🚀 Starting TON Copilot Bot (Full Feature — Multi-Coin Portfolio)...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CommandHandler("portfolio", portfolio_command))
    app.add_handler(CommandHandler("addholding", addholding_command))
    app.add_handler(CommandHandler("removeholding", removeholding_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("savewallet", savewallet_command))
    app.add_handler(CommandHandler("mywallet", mywallet_command))
    app.add_handler(CommandHandler("mytxns", mytxns_command))
    app.add_handler(CommandHandler("send", send_command))
    app.add_handler(CommandHandler("swap", swap_command))
    app.add_handler(CommandHandler("yields", yields_command))
    app.add_handler(CommandHandler("setalert", setalert_command))
    app.add_handler(CommandHandler("myalerts", myalerts_command))
    app.add_handler(CommandHandler("cancelalerts", cancelalerts_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_repeating(check_price_alerts, interval=60, first=10)

    print("✅ Bot is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()