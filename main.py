import os
from flask import Flask, request, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from datetime import datetime, timezone, timedelta
import random
import math

# === Configuration from environment ===
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
WEBHOOK_BASE_URL = os.getenv('WEBHOOK_BASE_URL')
DATABASE_URL = os.getenv('DATABASE_URL')
PORT = int(os.getenv('PORT', '8080'))

if not TELEGRAM_TOKEN:
    raise RuntimeError('TELEGRAM_TOKEN is not set in environment variables')
if not WEBHOOK_BASE_URL:
    print('WARNING: WEBHOOK_BASE_URL not set. Bot will still run but webhook will not be set automatically.')

app = Flask(__name__)
BOT_USERNAME = None

def get_bot_username():
    """Отримує username бота з Telegram API."""
    global BOT_USERNAME
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=10)
        data = r.json()
        if data.get("ok"):
            BOT_USERNAME = data["result"]["username"].lower()
            print("Bot username:", BOT_USERNAME)
        else:
            print("Failed to get bot username:", data)
    except Exception as e:
        print("Error getting bot username:", e)

# === DB helpers ===
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    sql_players_create = """
    CREATE TABLE IF NOT EXISTS players (
      chat_id BIGINT NOT NULL,
      user_id BIGINT NOT NULL,
      username TEXT,
      pet_name TEXT,
      weight INTEGER NOT NULL DEFAULT 10,
      last_feed_utc DATE,
      daily_feeds_count INTEGER NOT NULL DEFAULT 0,
      last_zonewalk_utc DATE,
      daily_zonewalks_count INTEGER NOT NULL DEFAULT 0,
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
    
    # === Migration logic ===
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='players' AND column_name IN ('last_feed', 'last_zonewalk')")
    old_columns = [row[0] for row in cur.fetchall()]
    
    if 'last_feed' in old_columns:
        print("Migrating 'last_feed' column...")
        cur.execute("ALTER TABLE players RENAME COLUMN last_feed TO last_feed_utc")
        cur.execute("ALTER TABLE players ALTER COLUMN last_feed_utc TYPE DATE USING last_feed_utc::date")
        
    if 'last_zonewalk' in old_columns:
        print("Migrating 'last_zonewalk' column...")
        cur.execute("ALTER TABLE players RENAME COLUMN last_zonewalk TO last_zonewalk_utc")
        cur.execute("ALTER TABLE players ALTER COLUMN last_zonewalk_utc TYPE DATE USING last_zonewalk_utc::date")
    
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='players' AND column_name='daily_zonewalks_count'")
    if not cur.fetchone():
        print("Adding 'daily_zonewalks_count' column...")
        cur.execute("ALTER TABLE players ADD COLUMN daily_zonewalks_count INTEGER NOT NULL DEFAULT 0")

    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='players' AND column_name='daily_feeds_count'")
    if not cur.fetchone():
        print("Adding 'daily_feeds_count' column...")
        cur.execute("ALTER TABLE players ADD COLUMN daily_feeds_count INTEGER NOT NULL DEFAULT 0")

    # Create tables if they don't exist
    cur.execute(sql_players_create)
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

def set_last_feed_date_and_count(chat_id, user_id, ts=None, count=0):
    ts = ts or now_utc().date()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET last_feed_utc=%s, daily_feeds_count=%s WHERE chat_id=%s AND user_id=%s", (ts, count, chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def increment_feed_count(chat_id, user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET daily_feeds_count = daily_feeds_count + 1 WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def set_last_zonewalk_date_and_count(chat_id, user_id, ts=None, count=0):
    ts = ts or now_utc().date()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET last_zonewalk_utc=%s, daily_zonewalks_count=%s WHERE chat_id=%s AND user_id=%s", (ts, count, chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def increment_zonewalk_count(chat_id, user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET daily_zonewalks_count = daily_zonewalks_count + 1 WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
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
DAILY_FEEDS_LIMIT = 1  # ЗМІНІТЬ ЦЕ ЗНАЧЕННЯ ДЛЯ КЕРУВАННЯ КІЛЬКІСТЮ БЕЗКОШТОВНИХ КОРМЬОЖОК НА ДОБУ
DAILY_ZONEWALKS_LIMIT = 2  # ЗМІНІТЬ ЦЕ ЗНАЧЕННЯ ДЛЯ КЕРУВАННЯ КІЛЬКІСТЮ БЕЗКОШТОВНИХ ХОДОК НА ДОБУ

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
        
# === Time formatting helper ===
def format_timedelta_to_next_day():
    """Formats time until the next UTC day as 'Xh Ym'."""
    now = now_utc()
    tomorrow_utc = (now + timedelta(days=1)).date()
    start_of_tomorrow = datetime.combine(tomorrow_utc, datetime.min.time(), tzinfo=timezone.utc)
    time_left = start_of_tomorrow - now
    
    hours, remainder = divmod(time_left.total_seconds(), 3600)
    minutes, _ = divmod(remainder, 60)
    
    parts = []
    if hours > 0:
        parts.append(f"{math.floor(hours)} год")
    if minutes > 0:
        parts.append(f"{math.floor(minutes)} хв")
    
    if not parts:
        return "менше хвилини"
    
    return " ".join(parts)


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
        "П.А.Ц.Є.Т.К.О. 2.\n\n"
        "ПАЦЄТКО СІ ВРОДИЛО - РАДІЄ ВСЕ СЕЛО НОВАЧКІВ!\n\n"
        "У кожного гравця є своє сталкер-пацєтко: його можна кормити (/feed), чухати за вушком (/pet), "
        "ходити в ходки в зону за хабаром(/zonewalk). Є інвентар, де буде лежати весь хабар вашого пацєти, (/inventory), також можна дати клікуху вашому пацєтку (/name), "
        "і подивитися топ по вазі і дізнатися хто найкраще сталкерське пацєтко (/top).\n\n"
        "Формат команд:\n"
        f"/feed [предмет] - безкоштовне харчування прямо від Бармена з Бару 100 Пятачків ({DAILY_FEEDS_LIMIT} разів на добу UTC). Додатково можна вказати предмет з інвентарю.\n"
        f"/zonewalk [предмет] - організувати ходку в небезпечну Зону ({DAILY_ZONEWALKS_LIMIT} разів на добу UTC). Додатково можна тяпнути енергетика або горілки, щоб мати можливість і сили сходити більше разів.\n"
        "/name Ім'я - дати ім'я пацєтці\n"
        "/top - топ-10 Сталкерів Пацєток чату за вагою\n"
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
    pet_name = row.get('pet_name', 'Пацєтко') # Отримуємо ім'я
    if random.random() < 0.20:
        sign = random.choice([-1,1])
        delta = random.randint(1,3) * sign
        neww = bounded_weight(old, delta)
        update_weight(chat_id, user_id, neww)
        if delta > 0:
            send_message(chat_id, f"Так файно вчухав пацю, що {pet_name} від радості засвоїв додатково {delta} кг сальця і тепер важить {neww} кг")
        else:
            send_message(chat_id, f"В цей раз паця сі невподобало чух і напряглося. Через стрес {pet_name} втратило  {delta} кг сальця і тепер важить {neww} кг")
    else:
        send_message(chat_id, f"{pet_name} лише задоволено рохнуло і, поправивши протигазик, чавкнуло. Десь збоку дзижчала муха")

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
    last_feed_date = row.get('last_feed_utc')
    feed_count = row.get('daily_feeds_count')
    current_utc_date = now_utc().date()
    pet_name = row.get('pet_name', 'Пацєтко') # Отримуємо ім'я
    messages = []
    
    if last_feed_date is None or last_feed_date < current_utc_date:
        feed_count = 0
        set_last_feed_date_and_count(chat_id, user_id, current_utc_date, count=0)
    
    FEED_PRIORITY = ['baton', 'sausage', 'can', 'vodka']
    free_feeds_left = DAILY_FEEDS_LIMIT - feed_count
    
    # === Обробка безкоштовної годівлі ===
    if free_feeds_left > 0:
        if not arg_item:
            delta = random.randint(-40, 40)
            neww = bounded_weight(old, delta)
            update_weight(chat_id, user_id, neww)
            increment_feed_count(chat_id, user_id)

            # === Змінено: Умовні повідомлення для дельти ===
            if delta > 0:
                msg = f"{pet_name} наминає з апетитом, аж за вухами лящить. Файні харчі старий сьогодні привіз.\nПаця набрало {delta:+d} кг сальця і тепер важить {neww} кг"
            elif delta < 0:
                msg = f"{pet_name} неохоче поїло, після чого ви чуєте жахливий буркіт живота. Цей старий пиздун в цей раз передав протухші продукти.\n{pet_name} сильно просралося, втративши {delta:+d} кг сальця і тепер важить {neww} кг"
            else:
                msg = f"{pet_name} з претензією дивиться на тебе. Схоже, в цей раз старий хрін передав бутлі з водою та мінімум харчів, від яких толку - трохи більше, ніж дірка від бублика.\nВага {pet_name} змінилась аж на ЦІЛИХ {delta:+d} кг сальця і важить {neww} кг."
            # ===============================================

            messages.append(f"Ви відкриваєте безкоштовну поставку харчів від Бармена:\n{msg}")
            old = neww
            free_feeds_left -= 1
            
    # === Обробка, якщо безкоштовних годівль не залишилось, але предмет не вказано ===
    elif not arg_item:
        inv = get_inventory(chat_id, user_id)
        item_to_use = None
        for item_key in FEED_PRIORITY:
            if inv.get(item_key, 0) > 0 and 'feed' in ITEMS[item_key]['uses_for']:
                item_to_use = item_key
                break
        
        if item_to_use:
            ok = remove_item(chat_id, user_id, item_to_use, qty=1)
            if ok:
                a, b = ITEMS[item_to_use]['feed_delta']
                d = random.randint(a, b)
                neww = bounded_weight(old, d)
                update_weight(chat_id, user_id, neww)
                messages.append(f"У {pet_name} бурчить в животі, тому ти використав {ITEMS[item_to_use]['u_name']} з інвентарю.\n Паця набрало {delta:+d} кг сальця і тепер важить {neww} кг")
                old = neww
            else:
                messages.append("Якась помилка. Предмет мав бути в інвентарі, але його не знайшли.")
        else:
            time_left = format_timedelta_to_next_day()
            messages.append(f"У тебе немає предметів для годівлі в інвентарі. Пацєтко залишилося голодним і з сумними очима лягло спати на пошарпаний диван в сховку.")

    # === Обробка годівлі з вказаним предметом ===
    if arg_item:
        key = ALIASES.get(arg_item.lower())
        if not key:
            messages.append("Невідомий предмет. Доступні: батон, ковбаса, консерва, горілка, енергетик.")
        else:
            if key not in ITEMS or 'feed' not in (ITEMS[key]['uses_for'] or []):
                messages.append(f"{ITEMS.get(key, {}).get('u_name', key)} не годиться для харчування паці.")
            else:
                ok = remove_item(chat_id, user_id, key, qty=1)
                if not ok:
                    messages.append(f"У тебе немає {ITEMS[key]['u_name']} в інвентарі.")
                else:
                    a, b = ITEMS[key]['feed_delta']
                    d = random.randint(a, b)
                    neww = bounded_weight(old, d)
                    update_weight(chat_id, user_id, neww)

                    # === Змінено: Умовні повідомлення для дельти ===
                    if d > 0:
                        msg = f"Дав схрумкати паці {ITEMS[key]['u_name']}, і маєш приріст сальця!"
                    elif d < 0:
                        msg = f"Пацєтко з'їло {ITEMS[key]['u_name']} і щось пішло не так. Паця просралося і вага зменшилася - мінус сальце."
                    else:
                        msg = f"Накормив пацєтко {ITEMS[key]['u_name']}, але вага не змінилась, сальця не додалося."
                    # ===============================================

                    messages.append(f"{msg}.\nПаця важило {old} кг, тепер {neww} кг (зміна сальця на {d:+d} кг)")
                    old = neww
    
    if free_feeds_left > 0 and not arg_item:
        time_left = format_timedelta_to_next_day()
        messages.append(f"\nУ тебе залишилось {free_feeds_left} безкоштовних харчів від Бармена до кінця доби. Наступні будуть доступні через {time_left}.")
    elif free_feeds_left <= 0 and not arg_item:
        time_left = format_timedelta_to_next_day()
        messages.append(f"\nНаступна безкоштовна поставка харчів від Бармена через {time_left}.")

    inv = get_inventory(chat_id, user_id)
    avail_feed = {k:v for k,v in inv.items() if k in ITEMS and 'feed' in (ITEMS[k]['uses_for'] or [])}
    if avail_feed:
        lines = [f"{ITEMS[k]['u_name']}: {q}" for k,q in avail_feed.items()]
        messages.append("\nУ тебе є предмети для додаткового харчування: " + ", ".join(lines))
    
    send_message(chat_id, '\n'.join(messages) if messages else 'Нічого не сталося.')

def handle_zonewalk(chat_id, user_id, username, arg_item):
    row = ensure_player(chat_id, user_id, username)
    last_zonewalk_date = row.get('last_zonewalk_utc')
    zonewalk_count = row.get('daily_zonewalks_count')
    pet_name = row.get('pet_name', 'Пацєтко') # Отримуємо ім'я
    current_utc_date = now_utc().date()
    messages = []
    
    if last_zonewalk_date is None or last_zonewalk_date < current_utc_date:
        zonewalk_count = 0
        set_last_zonewalk_date_and_count(chat_id, user_id, current_utc_date, count=0)
    
    ZONEWALK_PRIORITY = ['energy', 'vodka']
    
    def do_one_walk(player):
        cnt = pick_item_count()
        loot = []
        if cnt > 0:
            loot = pick_loot(cnt)
            for it in loot:
                add_item(chat_id, user_id, it, 1)
        delta = zonewalk_weight_delta()
        oldw = player['weight']
        neww = bounded_weight(oldw, delta)
        update_weight(chat_id, user_id, neww)
        
        s = f"\nВ процесі ходки {pet_name} набрав {delta:+d} кг сальця, і тепер важить {neww} кг."
        if cnt == 0:
            s += "\nЦей раз без хабаря."
        else:
            s += f"\nЄ хабар! {pet_name} приніс: " + ", ".join(ITEMS[it]['u_name'] for it in loot)
        return s
        
    free_walks_left = DAILY_ZONEWALKS_LIMIT - zonewalk_count
    
    if free_walks_left > 0:
        if not arg_item:
            increment_zonewalk_count(chat_id, user_id)
            player = ensure_player(chat_id, user_id, username)
            s = f"Паця напялює протигаз, вдягає рюкзак, вішає за плече автомат і тупцює в Зону." + do_one_walk(player)
            messages.append(s)
            free_walks_left -= 1
    
    elif not arg_item:
        inv = get_inventory(chat_id, user_id)
        item_to_use = None
        for item_key in ZONEWALK_PRIORITY:
            if inv.get(item_key, 0) > 0 and 'zonewalk' in ITEMS[item_key]['uses_for']:
                item_to_use = item_key
                break
        
        if item_to_use:
            ok = remove_item(chat_id, user_id, item_to_use, qty=1)
            if ok:
                player = ensure_player(chat_id, user_id, username)
                s = f"Пацєтко втомилося, тому ти використав {ITEMS[item_to_use]['u_name']} з інвентарю для додаткової ходки: " + do_one_walk(player)
                messages.append(s)
            else:
                messages.append("Якась помилка. Предмет мав бути в інвентарі, але його не знайшли.")
        else:
            time_left = format_timedelta_to_next_day()
            messages.append(f"Паця втомилося, а у тебе немає ні енергетика, ні горілки в інвентарі. Пацєтко нікуди не пішло і залишилось травити анекдоти біля ватри з іншими пацєтками.")
    
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
                    player = ensure_player(chat_id, user_id, username)
                    s = f"Використано {ITEMS[key]['u_name']} для додаткової ходки: " + do_one_walk(player)
                    messages.append(s)
    
    if free_walks_left > 0 and not arg_item:
        time_left = format_timedelta_to_next_day()
        messages.append(f"А ще паця заряджене на перемогу і має сил на {free_walks_left} ходок до кінця доби. ")
    elif free_walks_left <= 0 and not arg_item:
        time_left = format_timedelta_to_next_day()
        messages.append(f"\n Це були останні сили на сьогодні для походів в Зону у паці. Сили на наступні будуть черех {time_left}.")

    inv = get_inventory(chat_id, user_id)
    zone_items = {k:v for k,v in inv.items() if k in ITEMS and 'zonewalk' in (ITEMS[k]['uses_for'] or [])}
    if zone_items:
        lines = [f"{ITEMS[k]['u_name']}: {q}" for k,q in zone_items.items()]
        messages.append("У тебе є предмети для додаткових ходок: " + ", ".join(lines))
    
    send_message(chat_id, '\n'.join(messages) if messages else 'Нічого не сталося.')


# === Webhook endpoint ===
@app.route(f"/{TELEGRAM_TOKEN}", methods=['POST'])
def telegram_webhook():
    update = request.get_json()
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
        return jsonify({'ok': True})
    parts = text.split(maxsplit=1)
    cmd_full = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ''

    if '@' in cmd_full:
        cmd_name, cmd_user = cmd_full.split('@', 1)
        if BOT_USERNAME and cmd_user != BOT_USERNAME:
            return jsonify({'ok': True})
        cmd = cmd_name
    else:
        cmd = cmd_full

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
    get_bot_username()
    if DATABASE_URL:
        init_db()
    set_webhook()
    app.run(host='0.0.0.0', port=PORT)
