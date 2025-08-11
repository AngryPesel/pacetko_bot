import os
from flask import Flask, request, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from datetime import datetime, timezone, timedelta
import random

# === Configuration from environment ===
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
WEBHOOK_BASE_URL = os.getenv('WEBHOOK_BASE_URL')  # e.g. https://my-service.up.railway.app (no trailing slash)
DATABASE_URL = os.getenv('DATABASE_URL')  # Railway provided Postgres URL
PORT = int(os.getenv('PORT', '8080'))

if not TELEGRAM_TOKEN:
    raise RuntimeError('TELEGRAM_TOKEN is not set in environment variables')
if not WEBHOOK_BASE_URL:
    print('WARNING: WEBHOOK_BASE_URL not set. Bot will still run but webhook will not be set automatically.')

app = Flask(__name__)

# === DB helpers ===
def get_conn():
    # use sslmode=require for Railway-managed Postgres
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    sql_players = """
    CREATE TABLE IF NOT EXISTS players (
      chat_id BIGINT NOT NULL,
      user_id BIGINT NOT NULL,
      username TEXT,
      pet_name TEXT,
      weight INTEGER NOT NULL DEFAULT 10,
      last_feed TIMESTAMPTZ,
      last_zonewalk TIMESTAMPTZ,
      created_at TIMESTAMPTZ DEFAULT now(),
      PRIMARY KEY (chat_id, user_id)
    );
    """
    sql_inv = """
    CREATE TABLE IF NOT EXISTS inventory (
      id SERIAL PRIMARY KEY,
      chat_id BIGINT NOT NULL,
      user_id BIGINT NOT NULL,
      item TEXT NOT NULL,
      quantity INTEGER NOT NULL DEFAULT 0,
      UNIQUE (chat_id, user_id, item)
    );
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql_players)
    cur.execute(sql_inv)
    conn.commit()
    cur.close()
    conn.close()

# === Game data ===
ITEMS = {
    "baton": {"u_name": "Батон", "feed_delta": (-5,5), "uses_for": ["feed"]},
    "sausage": {"u_name": "Ковбаса", "feed_delta": (-9,9), "uses_for": ["feed"]},
    "can": {"u_name": 'Консерва "Сніданок Пацєти"', "feed_delta": (-15,15), "uses_for": ["feed"]},
    "vodka": {"u_name": 'Горілка "Пацятки"', "feed_delta": (-25,25), "uses_for": ["feed","zonewalk"]},
    "energy": {"u_name": 'Енергетик "Нон Хрюк"', "feed_delta": None, "uses_for": ["zonewalk"]},
}
ALIASES = {
    "батон":"baton","хліб":"baton","baton":"baton",
    "ковбаса":"sausage","sausage":"sausage",
    "консерва":"can","сніданок":"can","can":"can",
    "горілка":"vodka","пацятки":"vodka","vodka":"vodka",
    "енергетик":"energy","енергітик":"energy","energy":"energy"
}

LOOT_POOL = ["baton","sausage","can","vodka","energy"]
LOOT_WEIGHTS = [35,30,13,7,15]

# === Utility helpers ===
def now_utc():
    return datetime.now(timezone.utc)

def ensure_player(chat_id, user_id, username):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM players WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
    row = cur.fetchone()
    if not row:
        pet_name = f"Пацєтко_{user_id%1000}"
        cur.execute("INSERT INTO players (chat_id, user_id, username, pet_name, weight, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (chat_id, user_id, username or '', pet_name, 10, now_utc()))
        conn.commit()
        cur.execute("SELECT * FROM players WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
        row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def update_weight(chat_id, user_id, new_weight):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET weight=%s WHERE chat_id=%s AND user_id=%s", (new_weight, chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def get_last_feed(chat_id, user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT last_feed FROM players WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
    r = cur.fetchone()
    cur.close()
    conn.close()
    return r[0] if r else None

def set_last_feed(chat_id, user_id, ts=None):
    ts = ts or now_utc()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET last_feed=%s WHERE chat_id=%s AND user_id=%s", (ts, chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def get_last_zonewalk(chat_id, user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT last_zonewalk FROM players WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
    r = cur.fetchone()
    cur.close()
    conn.close()
    return r[0] if r else None

def set_last_zonewalk(chat_id, user_id, ts=None):
    ts = ts or now_utc()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET last_zonewalk=%s WHERE chat_id=%s AND user_id=%s", (ts, chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def get_inventory(chat_id, user_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT item, quantity FROM inventory WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {r['item']: r['quantity'] for r in rows}

def add_item(chat_id, user_id, item, qty=1):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT quantity FROM inventory WHERE chat_id=%s AND user_id=%s AND item=%s", (chat_id, user_id, item))
    r = cur.fetchone()
    if r:
        cur.execute("UPDATE inventory SET quantity=quantity+%s WHERE chat_id=%s AND user_id=%s AND item=%s", (qty, chat_id, user_id, item))
    else:
        cur.execute("INSERT INTO inventory (chat_id, user_id, item, quantity) VALUES (%s,%s,%s,%s)", (chat_id, user_id, item, qty))
    conn.commit()
    cur.close()
    conn.close()

def remove_item(chat_id, user_id, item, qty=1):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT quantity FROM inventory WHERE chat_id=%s AND user_id=%s AND item=%s", (chat_id, user_id, item))
    r = cur.fetchone()
    if not r or r[0] < qty:
        cur.close()
        conn.close()
        return False
    newq = r[0] - qty
    if newq > 0:
        cur.execute("UPDATE inventory SET quantity=%s WHERE chat_id=%s AND user_id=%s AND item=%s", (newq, chat_id, user_id, item))
    else:
        cur.execute("DELETE FROM inventory WHERE chat_id=%s AND user_id=%s AND item=%s", (chat_id, user_id, item))
    conn.commit()
    cur.close()
    conn.close()
    return True

def top_players(chat_id, limit=10):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT user_id, username, pet_name, weight FROM players WHERE chat_id=%s ORDER BY weight DESC LIMIT %s", (chat_id, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

# === Game mechanics ===
def bounded_weight(old, delta):
    new = old + delta
    return max(1, new)

def pick_item_count():
    r = random.random()
    if r < 0.50:
        return 0
    if r < 0.80:
        return 1
    if r < 0.95:
        return 2
    return 3

def pick_loot(n):
    return random.choices(LOOT_POOL, weights=LOOT_WEIGHTS, k=n)

def zonewalk_weight_delta():
    r = random.random()
    if r < 0.50:
        return 0
    elif r < 0.75:
        return -random.randint(1,5)
    else:
        return random.randint(1,5)

# === Telegram helpers ===
def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print('send_message error', e)

def set_webhook():
    if not WEBHOOK_BASE_URL:
        print('WEBHOOK_BASE_URL not set; skip setWebhook')
        return
    hook = f"{WEBHOOK_BASE_URL}/{TELEGRAM_TOKEN}"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    try:
        r = requests.post(url, data={'url': hook}, timeout=10)
        print('setWebhook result:', r.status_code, r.text)
    except Exception as e:
        print('setWebhook failed:', e)

# === Command handlers (simple parsing) ===
def handle_start(chat_id):
    txt = (

        "П.А.Ц.Є.Т.К.О. 2 — бот про вирощування пацєток.\n\n"

        "У кожного гравця є пацєтко: його можна кормити (/feed), чухати за вушком (/pet), "

        "ходити в ходки (/zonewalk). Є інвентар (/inventory), можна назвати пацєтко (/name), "

        "і подивитися топ по вазі (/top).\n\n"

        "Формат команд:\n"

        "/feed [предмет] - безкоштовна кормьожка раз на 24 години (UTC). Додатково можна вказати предмет з інвентарю для додаткової корміжки.\n"

        "/name Ім'я - дати ім'я пацєтці\n"

        "/top - топ 10 пацєток чату за вагою\n"

        "/pet - почухати за вушком\n"

        "/inventory - показати інвентарь\n"


    )
    send_message(chat_id, txt)

def handle_name(chat_id, user_id, username, args_text):
    newname = args_text.strip()[:64]
    if not newname:
        send_message(chat_id, "Вкажи ім'я: /name Ім'я")
        return
    ensure_player(chat_id, user_id, username)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET pet_name=%s WHERE chat_id=%s AND user_id=%s", (newname, chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()
    send_message(chat_id, f"Готово — твоє пацєтко тепер звати: {newname}")

def handle_top(chat_id):
    rows = top_players(chat_id, limit=10)
    if not rows:
        send_message(chat_id, "Ще немає пацєток у цьому чаті.")
        return
    lines = []
    for i, p in enumerate(rows, start=1):
        name = p.get('pet_name') or p.get('username') or str(p['user_id'])
        lines.append(f"{i}. {name} — {p['weight']} кг")
    send_message(chat_id, "Топ пацєток:\n" + "\n".join(lines))

def handle_pet(chat_id, user_id, username):
    row = ensure_player(chat_id, user_id, username)
    old = row['weight']
    if random.random() < 0.05:
        sign = random.choice([-1,1])
        delta = random.randint(1,3) * sign
        neww = bounded_weight(old, delta)
        update_weight(chat_id, user_id, neww)
        if delta > 0:
            send_message(chat_id, f"Почухав — пацєтко трохи під'їлося: +{delta} кг → {neww} кг")
        else:
            send_message(chat_id, f"Почухав — пацєтко трохи розтеклося: {delta} кг → {neww} кг")
    else:
        send_message(chat_id, "Почухав за вушком — пацєтко заспокоїлося, але нічого не змінилося.")

def handle_inventory(chat_id, user_id, username):
    ensure_player(chat_id, user_id, username)
    inv = get_inventory(chat_id, user_id)
    if not inv:
        send_message(chat_id, "Інвентар порожній.")
        return
    lines = []
    for k,q in inv.items():
        u = ITEMS.get(k, {}).get('u_name', k)
        lines.append(f"{u}: {q}")
    send_message(chat_id, "Інвентар:\n" + "\n".join(lines))

def handle_feed(chat_id, user_id, username, arg_item):
    row = ensure_player(chat_id, user_id, username)
    old = row['weight']
    last = row['last_feed']
    now = now_utc()
    free_allowed = False
    if last is None:
        free_allowed = True
    else:
        # last is timezone-aware from DB; compute diff
        diff = now - last
        if diff >= timedelta(hours=24):
            free_allowed = True
    messages = []
    if free_allowed:
        delta = random.randint(-40,40)
        neww = bounded_weight(old, delta)
        update_weight(chat_id, user_id, neww)
        set_last_feed(chat_id, user_id, now)
        messages.append(f"Безкоштовна кормьожка: {old} кг → {neww} кг (Δ {delta:+d})")
        old = neww
    inv = get_inventory(chat_id, user_id)
    avail_feed = {k:v for k,v in inv.items() if k in ITEMS and 'feed' in (ITEMS[k]['uses_for'] or [])}
    if avail_feed:
        lines = [f"{ITEMS[k]['u_name']}: {q}" for k,q in avail_feed.items()]
        messages.append("У тебе є предмети для додаткової корміжки: " + ", ".join(lines))
    # if user asked to use item
    if arg_item:
        key = ALIASES.get(arg_item.lower())
        if not key:
            messages.append("Невідомий предмет. Доступні: батон, ковбаса, консерва, горілка, енергетик.")
        else:
            if key not in ITEMS or 'feed' not in (ITEMS[key]['uses_for'] or []):
                messages.append(f"{ITEMS.get(key, {}).get('u_name', key)} не годиться для кормьожки.")
            else:
                ok = remove_item(chat_id, user_id, key, qty=1)
                if not ok:
                    messages.append(f"У тебе немає {ITEMS[key]['u_name']} в інвентарі.")
                else:
                    a,b = ITEMS[key]['feed_delta']
                    d = random.randint(a,b)
                    neww = bounded_weight(old, d)
                    update_weight(chat_id, user_id, neww)
                    messages.append(f"Використано {ITEMS[key]['u_name']}: {old} кг → {neww} кг (Δ {d:+d})")
                    old = neww
    send_message(chat_id, '\n'.join(messages) if messages else 'Нічого не сталося.')

def handle_zonewalk(chat_id, user_id, username, arg_item):
    row = ensure_player(chat_id, user_id, username)
    last = row['last_zonewalk']
    now = now_utc()
    free_allowed = False
    if last is None:
        free_allowed = True
    else:
        diff = now - last
        if diff >= timedelta(hours=24):
            free_allowed = True
    messages = []
    inv = get_inventory(chat_id, user_id)
    zone_items = {k:v for k,v in inv.items() if k in ITEMS and 'zonewalk' in (ITEMS[k]['uses_for'] or [])}
    if zone_items:
        messages.append("У тебе є предмети для додаткових ходок: " + ", ".join(f"{ITEMS[k]['u_name']}: {q}" for k,q in zone_items.items()))
    def do_one_walk():
        cnt = pick_item_count()
        loot = []
        if cnt>0:
            loot = pick_loot(cnt)
            for it in loot:
                add_item(chat_id, user_id, it, 1)
        delta = zonewalk_weight_delta()
        player = ensure_player(chat_id, user_id, username)
        oldw = player['weight']
        neww = bounded_weight(oldw, delta)
        update_weight(chat_id, user_id, neww)
        return cnt, loot, delta, oldw, neww
    if free_allowed:
        cnt, loot, delta, oldw, neww = do_one_walk()
        set_last_zonewalk(chat_id, user_id, now)
        s = f"Безкоштовна ходка: вага {oldw} → {neww} (Δ {delta:+d}). "
        if cnt==0:
            s += "Нічого не приніс."
        else:
            s += "Приніс: " + ", ".join(ITEMS[it]['u_name'] for it in loot)
        messages.append(s)
    if arg_item:
        key = ALIASES.get(arg_item.lower())
        if not key:
            messages.append("Невідомий предмет для використання в ходці.")
        else:
            if key not in ITEMS or 'zonewalk' not in (ITEMS[key]['uses_for'] or []):
                messages.append(f"{ITEMS.get(key, {}).get('u_name', key)} не дає можливості ходити в зону.")
            else:
                ok = remove_item(chat_id, user_id, key, qty=1)
                if not ok:
                    messages.append(f"У тебе немає {ITEMS[key]['u_name']} в інвентарі.")
                else:
                    cnt, loot, delta, oldw, neww = do_one_walk()
                    messages.append(f"Використано {ITEMS[key]['u_name']} для додаткової ходки: вага {oldw} → {neww} (Δ {delta:+d}).")
                    if cnt>0:
                        messages.append("Приніс: " + ", ".join(ITEMS[it]['u_name'] for it in loot))
    send_message(chat_id, '\n'.join(messages) if messages else 'Нічого не сталося.')

# === Webhook endpoint ===
@app.route(f"/{TELEGRAM_TOKEN}", methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    # process commands if message exists
    if not update:
        return jsonify({'ok': True})
    msg = update.get('message') or update.get('edited_message')
    if not msg:
        return jsonify({'ok': True})
    chat = msg.get('chat') or {}
    chat_id = chat.get('id')
    from_u = msg.get('from') or {}
    user_id = from_u.get('id')
    username = from_u.get('username')
    text = msg.get('text') or ''
    if not text.startswith('/'):
        # ignore non-command messages
        return jsonify({'ok': True})
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts)>1 else ''
    try:
        if cmd == '/start':
            handle_start(chat_id)
        elif cmd == '/name':
            handle_name(chat_id, user_id, username, arg)
        elif cmd == '/top':
            handle_top(chat_id)
        elif cmd == '/pet':
            handle_pet(chat_id, user_id, username)
        elif cmd == '/inventory':
            handle_inventory(chat_id, user_id, username)
        elif cmd == '/feed':
            handle_feed(chat_id, user_id, username, arg)
        elif cmd == '/zonewalk':
            handle_zonewalk(chat_id, user_id, username, arg)
        else:
            send_message(chat_id, 'Невідома команда.')
    except Exception as e:
        print('error handling command', e)
        send_message(chat_id, 'Сталася помилка при обробці команди.')
    return jsonify({'ok': True})

if __name__ == '__main__':
    # init DB and set webhook
    if DATABASE_URL:
        init_db()
    set_webhook()
    # Run Flask
    app.run(host='0.0.0.0', port=PORT)
