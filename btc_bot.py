import os
import json
import time
import requests
import numpy as np
from datetime import datetime, timedelta
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.env_polymarket"))
pk = os.getenv("POLY_PRIVATE_KEY")
creds = ApiCreds(
    api_key=os.getenv("POLY_API_KEY"),
    api_secret=os.getenv("POLY_SECRET"),
    api_passphrase=os.getenv("POLY_PASSPHRASE")
)
client = ClobClient(
    "https://clob.polymarket.com",
    key=pk, chain_id=137,
    creds=creds, signature_type=1
)

MAX_PER_TRADE = 10
MIN_EDGE = 10
MAX_DAILY_LOSS = 30
traded_today = set()
daily_pnl = 0

def log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}")

def get_btc_price():
    r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
    return float(r.json()["price"])

def get_btc_history():
    url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=30"
    r = requests.get(url, timeout=5)
    closes = [float(k[4]) for k in r.json()]
    return closes

def calc_probability(current_price, target_price, days_left, history):
    returns = [np.log(history[i]/history[i-1]) for i in range(1, len(history))]
    daily_vol = np.std(returns)
    drift = np.mean(returns)
    simulations = 1000
    hits = 0
    for _ in range(simulations):
        price = current_price
        for _ in range(days_left):
            price *= np.exp(drift + daily_vol * np.random.randn())
        if price >= target_price:
            hits += 1
    return hits / simulations * 100

def get_btc_markets():
    url = "https://gamma-api.polymarket.com/markets?limit=100&active=true"
    r = requests.get(url, timeout=10)
    markets = r.json()
    btc_markets = []
    keywords = ["bitcoin", "btc", "$"]
    for m in markets:
        q = m.get("question", "").lower()
        if any(w in q for w in keywords):
            try:
                prices = json.loads(m.get("outcomePrices", '["?","?"]'))
                yes = float(prices[0]) * 100
                no = float(prices[1]) * 100
                btc_markets.append({
                    "question": m["question"],
                    "yes": yes,
                    "no": no,
                    "id": m.get("id"),
                    "tokens": m.get("clobTokenIds", []),
                    "end": m.get("endDate", "")
                })
            except:
                pass
    return btc_markets

def extract_target_price(question):
    import re
    matches = re.findall(r'\$([0-9,]+)k?', question.replace(",", ""))
    if matches:
        val = float(matches[0].replace(",", ""))
        if val < 1000:
            val *= 1000
        return val
    return None

def extract_days_left(end_date):
    try:
        end = datetime.fromisoformat(end_date.replace("Z", ""))
        delta = end - datetime.utcnow()
        return max(1, delta.days)
    except:
        return 30

def execute_trade(token_id, amount, question, direction):
    global daily_pnl
    try:
        order = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=BUY,
            order_type=OrderType.FOK
        )
        signed = client.create_market_order(order)
        resp = client.post_order(signed, OrderType.FOK)
        log(f"✅ עסקה | {direction} ${amount} | {question[:50]}")
        daily_pnl -= amount
        return True
    except Exception as e:
        log(f"❌ שגיאה: {e}")
        return False

def scan():
    global daily_pnl

    if daily_pnl <= -MAX_DAILY_LOSS:
        log(f"⛔ הפסד יומי ${abs(daily_pnl)} — עוצר להיום")
        return

    log("סריקה מתחילה...")

    btc_price = get_btc_price()
    history = get_btc_history()
    log(f"מחיר BTC: ${btc_price:,.0f}")

    markets = get_btc_markets()
    log(f"שווקי BTC שנמצאו: {len(markets)}")

    opportunities = []

    for m in markets:
        if m["id"] in traded_today:
            continue

        target = extract_target_price(m["question"])
        if not target:
            continue

        days = extract_days_left(m["end"])
        our_prob = calc_probability(btc_price, target, days, history)
        market_prob = m["yes"]
        edge = our_prob - market_prob

        if abs(edge) >= MIN_EDGE:
            opportunities.append({
                **m,
                "our_prob": our_prob,
                "edge": edge,
                "days": days,
                "target": target
            })

    opportunities.sort(key=lambda x: abs(x["edge"]), reverse=True)

    if not opportunities:
        log("אין הזדמנויות כרגע")
        return

    log(f"\n🎯 {len(opportunities)} הזדמנויות:\n")

    for o in opportunities[:3]:
        if daily_pnl <= -MAX_DAILY_LOSS:
            break

        tokens = o.get("tokens", [])
        if not tokens:
            continue

        log(f"שוק: {o['question'][:60]}")
        log(f"יעד: ${o['target']:,.0f} | ימים: {o['days']} | שוק: {o['yes']:.1f}% | מודל: {o['our_prob']:.1f}% | Edge: {o['edge']:.1f}%")

        if o["edge"] > 0:
            token_id = tokens[0]
            direction = "YES"
        else:
            if len(tokens) < 2:
                continue
            token_id = tokens[1]
            direction = "NO"

        execute_trade(token_id, MAX_PER_TRADE, o["question"], direction)
        traded_today.add(o["id"])
        time.sleep(2)

log("🤖 בוט BTC מופעל")
log(f"הגדרות: מקסימום ${MAX_PER_TRADE} לעסקה | edge מינימלי {MIN_EDGE}%")

while True:
    try:
        scan()
    except Exception as e:
        log(f"שגיאה כללית: {e}")
    log("ממתין שעה לסריקה הבאה...")
    time.sleep(3600)
