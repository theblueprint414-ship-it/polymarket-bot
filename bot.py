import os
import json
import time
import requests
import threading
import logging
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from dotenv import load_dotenv
from datetime import datetime

logging.basicConfig(
    filename="trades.log",
    level=logging.INFO,
    format="%(asctime)s %(message)s"
)

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
MIN_EDGE = 15
MAX_DAILY_LOSS = 30
KELLY_FRACTION = 0.25

traded_today = set()
daily_pnl = 0
trade_count = 0

def log(msg):
    print(msg)
    logging.info(msg)

def kelly_size(edge_pct, max_size):
    edge = edge_pct / 100
    kelly = edge * KELLY_FRACTION
    size = min(max_size, max(1, round(max_size * kelly, 2)))
    return size

def get_metaculus(question):
    try:
        words = question.split()[:4]
        query = "+".join(words)
        url = f"https://www.metaculus.com/api2/questions/?search={query}&limit=3&resolved=false"
        r = requests.get(url, timeout=5)
        results = r.json().get("results", [])
        best_match = None
        best_score = 0
        q_words = set(question.lower().split())
        for res in results:
            title = res.get("title", "").lower()
            t_words = set(title.split())
            score = len(q_words & t_words) / max(len(q_words), 1)
            if score > best_score:
                best_score = score
                best_match = res
        if best_match and best_score > 0.3:
            prob = best_match.get("community_prediction", {}).get("full", {}).get("q2")
            if prob:
                return float(prob) * 100, best_score
    except:
        pass
    return None, 0

def get_all_markets():
    all_markets = []
    offset = 0
    while True:
        url = f"https://gamma-api.polymarket.com/markets?limit=100&active=true&offset={offset}"
        r = requests.get(url, timeout=10)
        batch = r.json()
        if not batch:
            break
        all_markets.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(0.5)
    return all_markets

def execute_trade(token_id, amount, question, direction):
    global daily_pnl, trade_count
    try:
        order = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=BUY,
            order_type=OrderType.FOK
        )
        signed = client.create_market_order(order)
        resp = client.post_order(signed, OrderType.FOK)
        trade_count += 1
        msg = f"✅ עסקה #{trade_count} | {direction} ${amount} | {question[:50]}"
        log(msg)
        return True
    except Exception as e:
        log(f"❌ שגיאה: {e}")
        return False

def scan():
    global daily_pnl
    now = datetime.now().strftime("%H:%M:%S")
    log(f"\n[{now}] סריקה מתחילה...")

    if daily_pnl <= -MAX_DAILY_LOSS:
        log(f"⛔ הפסד יומי ${abs(daily_pnl)} — עוצר להיום")
        return

    markets = get_all_markets()
    log(f"סה\"כ שווקים: {len(markets)}")

    opportunities = []

    for m in markets:
        market_id = m.get("id")
        if market_id in traded_today:
            continue
        try:
            prices = json.loads(m.get("outcomePrices", '["?","?"]'))
            yes = float(prices[0]) * 100
            no = float(prices[1]) * 100
        except:
            continue

        question = m.get("question", "")
        meta_prob, match_score = get_metaculus(question)
        if meta_prob is None:
            continue

        edge = meta_prob - yes

        if abs(edge) >= MIN_EDGE:
            opportunities.append({
                "question": question,
                "yes": yes,
                "meta": meta_prob,
                "edge": edge,
                "match": match_score,
                "id": market_id,
                "tokens": m.get("clobTokenIds", [])
            })
        time.sleep(0.3)

    opportunities.sort(key=lambda x: abs(x["edge"]) * x["match"], reverse=True)

    if not opportunities:
        log("אין הזדמנויות כרגע")
        return

    log(f"\n🎯 {len(opportunities)} הזדמנויות:\n")

    for o in opportunities[:5]:
        if daily_pnl <= -MAX_DAILY_LOSS:
            break

        tokens = o.get("tokens", [])
        if not tokens:
            continue

        size = kelly_size(abs(o["edge"]), MAX_PER_TRADE)

        log(f"שוק: {o['question'][:60]}")
        log(f"Poly: {o['yes']:.1f}% | Meta: {o['meta']:.1f}% | Edge: {o['edge']:.1f}%")

        if o["edge"] > 0:
            token_id = tokens[0]
            direction = "YES"
        else:
            if len(tokens) < 2:
                continue
            token_id = tokens[1]
            direction = "NO"

        success = execute_trade(token_id, size, o["question"], direction)
        if success:
            traded_today.add(o["id"])
            daily_pnl -= size

        time.sleep(2)

def reset_daily():
    global traded_today, daily_pnl, trade_count
    traded_today = set()
    daily_pnl = 0
    trade_count = 0
    log("🔄 איפוס יומי")

def scheduler():
    import schedule
    schedule.every(1).hours.do(scan)
    schedule.every().day.at("00:00").do(reset_daily)
    while True:
        schedule.run_pending()
        time.sleep(30)

log("🤖 בוט מופעל")
log(f"מקסימום לעסקה: ${MAX_PER_TRADE} | edge מינימלי: {MIN_EDGE}% | עצירה יומית: ${MAX_DAILY_LOSS}")

scan()

t = threading.Thread(target=scheduler, daemon=True)
t.start()

while True:
    time.sleep(60)
