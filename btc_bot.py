import os, json, time, requests, numpy as np
from datetime import datetime
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

pk = os.getenv("POLY_PRIVATE_KEY")
if not pk:
    from dotenv import load_dotenv
    load_dotenv(os.path.expanduser("~/.env_polymarket"))
    pk = os.getenv("POLY_PRIVATE_KEY")

creds = ApiCreds(
    api_key=os.getenv("POLY_API_KEY"),
    api_secret=os.getenv("POLY_SECRET"),
    api_passphrase=os.getenv("POLY_PASSPHRASE")
)
client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, creds=creds, signature_type=1)

MAX_PER_TRADE = 10
MIN_EDGE = 10
MAX_DAILY_LOSS = 30
traded_today = set()
daily_pnl = 0

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def get_btc_price():
    r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", timeout=10)
    return float(r.json()["bitcoin"]["usd"])

def get_btc_history():
    r = requests.get("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=60", timeout=10)
    prices = r.json()["prices"]
    return [float(p[1]) for p in prices]

def calc_probability(current_price, target_price, days_left, history):
    if target_price <= current_price * 0.5 or target_price >= current_price * 5:
        return None
    returns = [np.log(history[i]/history[i-1]) for i in range(1, len(history))]
    daily_vol = np.std(returns)
    drift = np.mean(returns)
    hits = sum(
        1 for _ in range(2000)
        if np.prod([np.exp(drift + daily_vol * np.random.randn()) for _ in range(max(1, days_left))]) * current_price >= target_price
    )
    return hits / 2000 * 100

def get_btc_markets():
    r = requests.get("https://gamma-api.polymarket.com/markets?limit=100&active=true", timeout=10)
    result = []
    for m in r.json():
        q = m.get("question", "").lower()
        if ("bitcoin" not in q and "btc" not in q) or any(x in q for x in ["ethereum", "eth ", "sol", "mega", "market cap"]):
            continue
        try:
            prices = json.loads(m.get("outcomePrices", '["?","?"]'))
            result.append({"question": m["question"], "yes": float(prices[0])*100, "no": float(prices[1])*100, "id": m.get("id"), "tokens": m.get("clobTokenIds", []), "end": m.get("endDate", "")})
        except:
            pass
    return result

def extract_target(question):
    import re
    for val_str, suffix in re.findall(r'\$([0-9,]+)([kK]?)', question):
        val = float(val_str.replace(",", "")) * (1000 if suffix.lower() == 'k' else 1)
        if 50000 <= val <= 500000:
            return val
    return None

def extract_days(end_date):
    try:
        return max(1, (datetime.fromisoformat(end_date.replace("Z", "")) - datetime.now()).days)
    except:
        return 30

def execute_trade(token_id, amount, question, direction):
    global daily_pnl
    try:
        signed = client.create_market_order(MarketOrderArgs(token_id=token_id, amount=amount, side=BUY, order_type=OrderType.FOK))
        client.post_order(signed, OrderType.FOK)
        log(f"✅ {direction} ${amount} | {question[:50]}")
        daily_pnl -= amount
        return True
    except Exception as e:
        log(f"❌ {e}")
        return False

def scan():
    global daily_pnl
    if daily_pnl <= -MAX_DAILY_LOSS:
        log("⛔ עוצר להיום")
        return
    log("סריקה מתחילה...")
    btc_price = get_btc_price()
    history = get_btc_history()
    log(f"BTC: ${btc_price:,.0f}")
    markets = get_btc_markets()
    log(f"שווקי BTC: {len(markets)}")
    opps = []
    for m in markets:
        if m["id"] in traded_today:
            continue
        target = extract_target(m["question"])
        if not target:
            continue
        days = extract_days(m["end"])
        prob = calc_probability(btc_price, target, days, history)
        if prob is None:
            continue
        edge = prob - m["yes"]
        if abs(edge) >= MIN_EDGE:
            opps.append({**m, "prob": prob, "edge": edge, "days": days, "target": target})
    opps.sort(key=lambda x: abs(x["edge"]), reverse=True)
    if not opps:
        log("אין הזדמנויות")
        return
    log(f"🎯 {len(opps)} הזדמנויות:")
    for o in opps[:3]:
        if daily_pnl <= -MAX_DAILY_LOSS:
            break
        tokens = o.get("tokens", [])
        if not tokens:
            continue
        log(f"{o['question'][:60]} | יעד: ${o['target']:,.0f} | שוק: {o['yes']:.1f}% | מודל: {o['prob']:.1f}% | Edge: {o['edge']:.1f}%")
        direction = "YES" if o["edge"] > 0 else "NO"
        token_id = tokens[0] if direction == "YES" else (tokens[1] if len(tokens) > 1 else None)
        if not token_id:
            continue
        if execute_trade(token_id, MAX_PER_TRADE, o["question"], direction):
            traded_today.add(o["id"])
        time.sleep(2)

log("🤖 בוט BTC")
while True:
    try:
        scan()
    except Exception as e:
        log(f"שגיאה: {e}")
    log("ממתין שעה...")
    time.sleep(3600)
