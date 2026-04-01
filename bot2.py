import asyncio
import hashlib
import hmac
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import pandas as pd
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

# =========================
# CONFIG
# =========================
BOT_TOKEN = "8714374090:AAGuK9h8anhvZ-xU1zvHPeOZDNExPgIdWW4"
API_KEY = "7H1ybsFext1ELWWa3dMdL52SCdsoPbMnN4Lngurs2oGYBr3wAj3YwT2LkIupiU06"
API_SECRET = "uBTcVPicuUlyezym37Xu0FY7B8DucMCGNSN7LdiJWPA1PRZuQ8cuVka5l6sGw6Hd"

BASE_URL = "https://demo-fapi.binance.com"
SYMBOL = "BTCUSDT"
INTERVAL = "1m"

LEVERAGE = 5
STOP_LOSS_PCT = 0.01       # 1%
TAKE_PROFIT_PCT = 0.02     # 2%
LOOP_SECONDS = 5

# Easier EMA for testing
EMA_RELAX = 0.98

# Levels format: buy_rsi, sell_rsi, margin_usdt
LEVELS: List[Dict[str, float]] = [
    {"buy": 45, "sell": 55, "size": 100},
    {"buy": 35, "sell": 60, "size": 50},
    {"buy": 25, "sell": 70, "size": 25},
]

AUTHORIZED_CHAT_ID: Optional[int] = None
AUTO_ON = False

# Local tracking
triggered_levels = set()
open_legs: List[Dict[str, Any]] = []
trade_history: List[Dict[str, Any]] = []
realized_pnl = 0.0
wins = 0
losses = 0

# Cached market data
last_price: Optional[float] = None
last_rsi: Optional[float] = None
last_ema: Optional[float] = None
last_msg_ts = 0.0


# =========================
# HTTP HELPERS
# =========================
def _sign(params: Dict[str, Any]) -> str:
    qs = urlencode(params, doseq=True)
    return hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()


def _headers() -> Dict[str, str]:
    return {"X-MBX-APIKEY": API_KEY}


def _round_qty(qty: float) -> float:
    return max(0.001, round(qty, 3))


def _request(method: str, path: str, params: Optional[Dict[str, Any]] = None, signed: bool = False):
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 10000
        params["signature"] = _sign(params)

    url = f"{BASE_URL}{path}"
    resp = requests.request(
        method,
        url,
        params=params,
        headers=_headers() if signed else None,
        timeout=15
    )
    resp.raise_for_status()
    return resp.json()


async def public_get(path: str, params: Optional[Dict[str, Any]] = None):
    return await asyncio.to_thread(_request, "GET", path, params, False)


async def signed_get(path: str, params: Optional[Dict[str, Any]] = None):
    return await asyncio.to_thread(_request, "GET", path, params, True)


async def signed_post(path: str, params: Optional[Dict[str, Any]] = None):
    return await asyncio.to_thread(_request, "POST", path, params, True)


# =========================
# TELEGRAM HELPERS
# =========================
def is_authorized(update: Update) -> bool:
    return AUTHORIZED_CHAT_ID is not None and update.effective_chat and update.effective_chat.id == AUTHORIZED_CHAT_ID


async def safe_send(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    global last_msg_ts
    if AUTHORIZED_CHAT_ID is None:
        return

    if time.time() - last_msg_ts < 0.8:
        await asyncio.sleep(0.8)

    for _ in range(3):
        try:
            await context.bot.send_message(chat_id=AUTHORIZED_CHAT_ID, text=text)
            last_msg_ts = time.time()
            return
        except Exception as e:
            print("Telegram send retry:", e)
            await asyncio.sleep(1)


# =========================
# BINANCE HELPERS
# =========================
async def set_leverage(symbol: str, leverage: int) -> None:
    await signed_post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})


async def get_wallet() -> Dict[str, float]:
    balances = await signed_get("/fapi/v2/balance")
    usdt_row = next((b for b in balances if b["asset"] == "USDT"), None)
    if not usdt_row:
        return {"balance": 0.0, "available": 0.0}
    return {
        "balance": float(usdt_row["balance"]),
        "available": float(usdt_row["availableBalance"]),
    }


async def get_position_amt(symbol: str) -> float:
    positions = await signed_get("/fapi/v3/positionRisk", {"symbol": symbol})
    if isinstance(positions, list) and positions:
        return float(positions[0]["positionAmt"])
    return 0.0


async def market_buy(symbol: str, qty: float):
    return await signed_post("/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty,
    })


async def market_sell_reduce(symbol: str, qty: float):
    return await signed_post("/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true",
    })


async def fetch_price(symbol: str) -> float:
    data = await public_get("/fapi/v2/ticker/price", {"symbol": symbol})
    return float(data["price"])


async def fetch_klines(symbol: str, interval: str, limit: int = 100) -> pd.DataFrame:
    rows = await public_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "num_trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


# =========================
# STRATEGY HELPERS
# =========================
def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def current_used_margin() -> float:
    return sum(float(t["size"]) for t in open_legs)


async def maybe_open_leg(
    context: ContextTypes.DEFAULT_TYPE,
    level: Dict[str, float],
    price: float,
    prev_price: Optional[float],
    rsi: float,
    ema: float
):
    wallet = await get_wallet()
    available = wallet["available"]

    if level["buy"] in triggered_levels:
        return
    if prev_price is None:
        return
    if rsi >= level["buy"]:
        return
    if price <= prev_price:
        return
    if price <= ema * EMA_RELAX:
        return
    if available < level["size"]:
        return

    notional = level["size"] * LEVERAGE
    qty = _round_qty(notional / price)
    if qty <= 0:
        return

    await set_leverage(SYMBOL, LEVERAGE)
    order = await market_buy(SYMBOL, qty)

    open_legs.append({
        "entry": price,
        "qty": qty,
        "size": level["size"],
        "buy": level["buy"],
        "sell": level["sell"],
        "opened_at": time.time(),
        "order_id": order.get("orderId"),
    })
    triggered_levels.add(level["buy"])

    await safe_send(
        context,
        f"🟢 BUY ${level['size']} margin | {LEVERAGE}x | RSI<{level['buy']} @ {price:.2f} | qty {qty}"
    )


async def maybe_close_legs(context: ContextTypes.DEFAULT_TYPE, price: float, rsi: float):
    global realized_pnl, wins, losses

    remaining: List[Dict[str, Any]] = []

    for leg in open_legs:
        entry = float(leg["entry"])
        qty = float(leg["qty"])
        pnl = (price - entry) * qty

        reason = None
        if price <= entry * (1 - STOP_LOSS_PCT):
            reason = "SL"
        elif price >= entry * (1 + TAKE_PROFIT_PCT):
            reason = "TP"
        elif rsi > float(leg["sell"]):
            reason = f"RSI>{leg['sell']}"

        if reason is None:
            remaining.append(leg)
            continue

        try:
            await market_sell_reduce(SYMBOL, qty)
        except Exception as e:
            print("Reduce-only close failed:", e)
            remaining.append(leg)
            continue

        realized_pnl += pnl
        if pnl > 0:
            wins += 1
        else:
            losses += 1

        trade_history.append({
            "entry": entry,
            "exit": price,
            "profit": pnl,
            "qty": qty,
            "size": leg["size"],
            "buy": leg["buy"],
            "sell": leg["sell"],
            "reason": reason,
        })

        triggered_levels.discard(leg["buy"])
        await safe_send(
            context,
            f"🔴 EXIT {reason} | ${leg['size']} | qty {qty} | PnL {pnl:.2f}"
        )

    open_legs[:] = remaining


# =========================
# AUTO LOOP
# =========================
async def auto_signal(context: ContextTypes.DEFAULT_TYPE):
    global last_price, last_rsi, last_ema

    if not AUTO_ON or AUTHORIZED_CHAT_ID is None:
        return

    try:
        df = await fetch_klines(SYMBOL, INTERVAL, 100)
        df["rsi"] = calculate_rsi(df)
        df["ema"] = df["close"].ewm(span=50).mean()

        price = float(df["close"].iloc[-1])
        rsi = float(df["rsi"].iloc[-1])
        ema = float(df["ema"].iloc[-1])

        prev_price = last_price
        last_price = price
        last_rsi = rsi
        last_ema = ema

        print(f"RSI: {rsi}, Price: {price}, EMA: {ema}")

        for lvl in LEVELS:
            await maybe_open_leg(context, lvl, price, prev_price, rsi, ema)

        for lvl in LEVELS:
            if rsi > lvl["buy"] + 2:
                triggered_levels.discard(lvl["buy"])

        await maybe_close_legs(context, price, rsi)

    except Exception as e:
        print("auto_signal error:", e)


# =========================
# COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AUTHORIZED_CHAT_ID
    AUTHORIZED_CHAT_ID = update.effective_chat.id
    await update.message.reply_text("⚡ Demo futures bot ready.")

async def autoon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AUTO_ON
    if not is_authorized(update):
        return
    AUTO_ON = True
    await update.message.reply_text("✅ Auto ON")

async def autooff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AUTO_ON
    if not is_authorized(update):
        return
    AUTO_ON = False
    await update.message.reply_text("🛑 Auto OFF")

async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    try:
        w = await get_wallet()
        await update.message.reply_text(
            f"💰 Demo Wallet\n"
            f"Total: {w['balance']:.2f} USDT\n"
            f"Free: {w['available']:.2f} USDT\n"
            f"Used by open legs (local): {current_used_margin():.2f} USDT\n"
            f"Realized PnL (local): {realized_pnl:.2f} USDT\n"
            f"Wins: {wins} | Losses: {losses}"
        )
    except Exception as e:
        await update.message.reply_text(f"Wallet error: {e}")

async def trades_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not open_legs:
        await update.message.reply_text("No open trades")
        return

    price = last_price if last_price is not None else await fetch_price(SYMBOL)
    msg = f"📊 Trades (Price: {price:.2f})\n"
    total = 0.0

    for i, t in enumerate(open_legs, 1):
        pnl = (price - float(t['entry'])) * float(t['qty'])
        total += pnl
        msg += (
            f"\n#{i} | ${t['size']} margin | qty {t['qty']}"
            f"\nEntry: {float(t['entry']):.2f}"
            f"\nPnL: {pnl:.2f}"
            f"\nBUY<{t['buy']} → SELL>{t['sell']}\n"
        )

    msg += f"\n💰 Total Unrealized PnL: {total:.2f}"
    await update.message.reply_text(msg)

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not trade_history:
        await update.message.reply_text("No trades yet")
        return

    msg = "📜 Trade History\n"
    for t in trade_history[-10:]:
        msg += (
            f"\n{t['reason']} | ${t['size']} margin"
            f"\nEntry: {t['entry']:.2f} → Exit: {t['exit']:.2f}"
            f"\nPnL: {t['profit']:.2f}\n"
        )
    await update.message.reply_text(msg)

async def levels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    msg = f"📊 Levels\nEMA Relax: {EMA_RELAX}\n"
    for lvl in LEVELS:
        msg += f"\nBUY<{lvl['buy']} → SELL>{lvl['sell']} | ${lvl['size']} margin"
    await update.message.reply_text(msg)

async def setlevels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LEVELS
    if not is_authorized(update):
        return

    try:
        args = list(map(float, context.args))
        if len(args) % 3 != 0:
            raise ValueError("Need triples of buy sell size")

        new_levels = []
        for i in range(0, len(args), 3):
            new_levels.append({
                "buy": args[i],
                "sell": args[i + 1],
                "size": args[i + 2],
            })

        LEVELS = new_levels
        msg = "✅ Levels updated\n"
        for lvl in LEVELS:
            msg += f"\nBUY<{lvl['buy']} → SELL>{lvl['sell']} | ${lvl['size']} margin"
        await update.message.reply_text(msg)

    except Exception:
        await update.message.reply_text("Usage:\n/setlevels 45 55 100 35 60 50 25 70 25")


# =========================
# APP
# =========================
request = HTTPXRequest(
    connect_timeout=15,
    read_timeout=15,
    write_timeout=15,
    pool_timeout=15
)

app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("autoon", autoon))
app.add_handler(CommandHandler("autooff", autooff))
app.add_handler(CommandHandler("wallet", wallet))
app.add_handler(CommandHandler("trades", trades_cmd))
app.add_handler(CommandHandler("history", history))
app.add_handler(CommandHandler("levels", levels_cmd))
app.add_handler(CommandHandler("setlevels", setlevels))

app.job_queue.run_repeating(auto_signal, interval=LOOP_SECONDS, first=3)

if __name__ == "__main__":
    print("⚡ Binance demo futures leveraged bot running...")
    app.run_polling()
