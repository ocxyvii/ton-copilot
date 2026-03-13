import os
import ssl
import json
import aiohttp
import certifi
from aiohttp import web
import aiohttp_cors
from dotenv import load_dotenv

load_dotenv()

COINGECKO_API = "https://api.coingecko.com/api/v3"

SUPPORTED_COINS = {
    "TON":  "the-open-network",
    "NOT":  "notcoin",
    "DOGS": "dogs-2",
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "SOL":  "solana",
    "BNB":  "binancecoin",
    "USDT": "tether",
    "USDC": "usd-coin",
    "DOGE": "dogecoin",
    "ADA":  "cardano",
    "TRX":  "tron",
}

def make_connector():
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_context)

# Cache prices for 60 seconds to avoid rate limits
_price_cache = {"data": {}, "ts": 0}

async def fetch_prices():
    import time
    now = time.time()
    if now - _price_cache["ts"] < 60 and _price_cache["data"]:
        return _price_cache["data"]
    try:
        ids = ",".join(SUPPORTED_COINS.values())
        connector = make_connector()
        headers = {"User-Agent": "TONCopilot/1.0"}
        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            params = {
                "ids": ids,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
            }
            async with session.get(f"{COINGECKO_API}/simple/price", params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    _price_cache["data"] = data
                    _price_cache["ts"] = now
                    return data
                else:
                    print(f"CoinGecko status: {resp.status}")
                    return _price_cache["data"]
    except Exception as e:
        print(f"Error fetching prices: {e}")
        return _price_cache["data"]

async def prices_handler(request):
    data = await fetch_prices()
    id_to_symbol = {v: k for k, v in SUPPORTED_COINS.items()}
    result = []
    order = ["TON", "NOT", "DOGS", "BTC", "ETH", "SOL", "BNB", "DOGE", "ADA", "TRX", "USDT", "USDC"]
    for symbol in order:
        cg_id = SUPPORTED_COINS.get(symbol)
        if not cg_id or cg_id not in data:
            continue
        coin = data[cg_id]
        price = coin.get("usd", 0)
        change = coin.get("usd_24h_change", 0)
        mcap = coin.get("usd_market_cap", 0)
        vol = coin.get("usd_24h_vol", 0)
        result.append({
            "symbol": symbol,
            "price": price,
            "change_24h": round(change, 2),
            "market_cap": mcap,
            "volume_24h": vol,
        })
    return web.json_response({"coins": result, "status": "ok"})

async def health_handler(request):
    return web.json_response({"status": "ok", "service": "TON Copilot API"})

async def chat_handler(request):
    try:
        from groq import Groq
        body = await request.json()
        messages = body.get("messages", [])
        if not messages:
            return web.json_response({"reply": "No message received."})

        # Fetch live prices to inject into system prompt
        price_context = ""
        try:
            prices = await fetch_prices()
            if prices:
                order = ["BTC", "ETH", "SOL", "TON", "NOT", "DOGS", "BNB", "DOGE", "ADA", "TRX"]
                lines = []
                for sym in order:
                    cg_id = SUPPORTED_COINS.get(sym)
                    if cg_id and cg_id in prices:
                        p = prices[cg_id].get("usd", 0)
                        c = prices[cg_id].get("usd_24h_change", 0)
                        lines.append(f"{sym}: ${p:,.4f} ({c:+.2f}% 24h)")
                price_context = "\n\nCURRENT LIVE PRICES (fetched right now):\n" + "\n".join(lines)
        except Exception as e:
            print(f"Price fetch for chat: {e}")

        groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        system_content = (
            "You are TON Copilot, an AI crypto assistant specializing in the TON blockchain. "
            "Help users with crypto prices, DeFi, wallets, staking, swapping, and blockchain questions. "
            "Be concise, friendly, and accurate. Never ask for private keys or seed phrases. "
            "When asked about prices, ALWAYS use the live prices provided below — never use outdated training data."
            + price_context
        )
        system = {"role": "system", "content": system_content}

        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[system] + messages[-10:],
            max_tokens=512,
            temperature=0.7,
        )
        reply = response.choices[0].message.content
        return web.json_response({"reply": reply})
    except Exception as e:
        print(f"Chat error: {e}")
        return web.json_response({"reply": "⚠️ AI is unavailable right now. Try again!"}, status=500)

async def index_handler(request):
    return web.FileResponse("./webapp/index.html")

def create_app():
    app = web.Application()

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods=["GET", "POST", "OPTIONS"]
        )
    })

    price_resource = cors.add(app.router.add_resource("/api/prices"))
    cors.add(price_resource.add_route("GET", prices_handler))

    health_resource = cors.add(app.router.add_resource("/health"))
    cors.add(health_resource.add_route("GET", health_handler))

    chat_resource = cors.add(app.router.add_resource("/api/chat"))
    cors.add(chat_resource.add_route("POST", chat_handler))

    # Serve index.html at root
    app.router.add_get("/", index_handler)

    # Serve other static files (manifest, icons etc)
    app.router.add_static("/", path="./webapp", name="static")

    return app

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    print(f"🌐 Starting TON Copilot API server on port {port}...")
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)