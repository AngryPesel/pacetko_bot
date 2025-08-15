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

# === NEW FEATURE: Смерть і вербування (New parameters) ===
STARTING_WEIGHT = 10
DAILY_RECRUITS_LIMIT = 1
MAX_RECRUITED_PETS = 3
# ==========================================================

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
      last_wheel_utc DATE,
      daily_wheel_count INTEGER NOT NULL DEFAULT 0,
      last_pet_utc TIMESTAMPTZ,
      last_message_id BIGINT,
      cleanup_enabled BOOLEAN NOT NULL DEFAULT TRUE,
      created_at TIMESTAMPTZ DEFAULT now(),
      recruited_pets_count INTEGER NOT NULL DEFAULT 0,
      last_recruitment_utc DATE,
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

    # === NEW FEATURE: Колесо Фортуни (DB Migration) ===
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='players' AND column_name='last_wheel_utc'")
    if not cur.fetchone():
        print("Adding 'last_wheel_utc' and 'daily_wheel_count' columns...")
        cur.execute("ALTER TABLE players ADD COLUMN last_wheel_utc DATE")
        cur.execute("ALTER TABLE players ADD COLUMN daily_wheel_count INTEGER NOT NULL DEFAULT 0")
    # =================================================

    # === NEW FEATURE: Pet Cooldown (DB Migration) ===
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='players' AND column_name='last_pet_utc'")
    if not cur.fetchone():
        print("Adding 'last_pet_utc' column...")
        cur.execute("ALTER TABLE players ADD COLUMN last_pet_utc TIMESTAMPTZ")
    # ===============================================
    
    # === NEW FEATURE: Message cleanup (DB Migration) ===
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='players' AND column_name='last_message_id'")
    if not cur.fetchone():
        print("Adding 'last_message_id' column...")
        cur.execute("ALTER TABLE players ADD COLUMN last_message_id BIGINT")
    # =================================================
    
    # === NEW FEATURE: Cleanup toggle (DB Migration) ===
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='players' AND column_name='cleanup_enabled'")
    if not cur.fetchone():
        print("Adding 'cleanup_enabled' column...")
        cur.execute("ALTER TABLE players ADD COLUMN cleanup_enabled BOOLEAN NOT NULL DEFAULT TRUE")
    # =================================================

    # === NEW FEATURE: Смерть і вербування (DB Migration) ===
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='players' AND column_name='recruited_pets_count'")
    if not cur.fetchone():
        print("Adding 'recruited_pets_count' and 'last_recruitment_utc' columns...")
        cur.execute("ALTER TABLE players ADD COLUMN recruited_pets_count INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE players ADD COLUMN last_recruitment_utc DATE")
    # =======================================================
    
    # === NEW FEATURE: Fight cooldown (DB Migration) ===
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='players' AND column_name='last_fight_utc'")
    if not cur.fetchone():
        print("Adding 'last_fight_utc' column...")
        cur.execute("ALTER TABLE players ADD COLUMN last_fight_utc TIMESTAMPTZ")
    # ==================================================
    
    # --- Фрагмент у init_db() --- 
    cur.execute("""
      SELECT column_name
      FROM information_schema.columns
      WHERE table_name='players' AND column_name='born_utc'
    """)
    if not cur.fetchone():
      print("Adding 'born_utc' column...")
      cur.execute("ALTER TABLE players ADD COLUMN born_utc TIMESTAMPTZ")
      cur.execute("UPDATE players SET born_utc = NOW()")

    # Create tables if they don't exist
    cur.execute(sql_players_create)
    cur.execute(sql_inv)
    
    conn.commit()
    cur.close()
    conn.close()

# === Game data ===
ITEMS = {
    "baton": {"u_name": "Батон", "feed_delta": (-2, 5), "uses_for": ["feed"]},
    "sausage": {"u_name": "Ковбаса", "feed_delta": (-4, 9), "uses_for": ["feed"]},
    "can": {"u_name": 'Консерва "Сніданок Пацєти"', "feed_delta": (-7, 15), "uses_for": ["feed"]},
    "vodka": {"u_name": 'Горілка "Пацятки"', "feed_delta": (-12, 25), "uses_for": ["feed", "zonewalk"]},
    "energy": {"u_name": 'Енергетик "Нон Хрюк"', "feed_delta": None, "uses_for": ["zonewalk"]},
    "low_saloid": {"u_name": "Малий шприц з салоїдами", "feed_delta": (5, 5), "uses_for": ["feed"]},
    "mid_saloid": {"u_name": "Шприц з салоїдами", "feed_delta": (10, 10), "uses_for": ["feed"]},
    "big_saloid": {"u_name": "Великий шприц з салоїдами", "feed_delta": (15, 15), "uses_for": ["feed"]},
    "strange_saloid": {"u_name": "Дивний шприц з салоїдами", "feed_delta": (-50, 50), "uses_for": ["feed"]},
}

ALIASES = {
    "батон": "baton", "хліб": "baton", "baton": "baton",
    "ковбаса": "sausage", "sausage": "sausage",
    "консерва": "can", "сніданок": "can", "can": "can",
    "горілка": "vodka", "пацятки": "vodka", "vodka": "vodka",
    "енергетик": "energy", "енергітик": "energy", "energy": "energy",
    "малий_салоїд": "low_saloid", "малий_шприц": "low_saloid", "low_saloid": "low_saloid",
    "салоїд": "mid_saloid", "шприц": "mid_saloid", "mid_saloid": "mid_saloid",
    "великий_салоїд": "big_saloid", "великий_шприц": "big_saloid", "big_saloid": "big_saloid",
    "дивний_салоїд": "strange_saloid", "дивний_шприц": "strange_saloid", "strange_saloid": "strange_saloid",
}

# Loot pool for /zonewalk command. The weights should sum up to 100.
LOOT_POOL = ["baton", "sausage", "can", "vodka", "energy", "low_saloid", "mid_saloid", "big_saloid", "strange_saloid"]
LOOT_WEIGHTS = [20, 15, 15, 5, 10, 15, 10, 7, 3]

# === NEW FEATURE: Колесо Фортуни (Rewards) ===
# Rewards for the /wheel command. The weights should sum up to 100.
WHEEL_REWARDS = {
    "nothing": {"u_name": "Дуля з маком і консервна банка від Сидора", "quantity": 0, "weight": 30},
    "baton": {"u_name": "Батон", "quantity": 1, "weight": 15},
    "sausage": {"u_name": "Ковбаса", "quantity": 1, "weight": 10},
    "can": {"u_name": 'Консерва "Сніданок Пацєти"', "quantity": 1, "weight": 10},
    "vodka": {"u_name": 'Горілка "Пацятки"', "quantity": 1, "weight": 5},
    "energy": {"u_name": 'Енергетик "Нон Хрюк"', "quantity": 1, "weight": 10},
    "low_saloid": {"u_name": "Малий шприц з салоїдами", "quantity": 1, "weight": 10},
    "mid_saloid": {"u_name": "Шприц з салоїдами", "quantity": 1, "weight": 5},
    "big_saloid": {"u_name": "Великий шприц з салоїдами", "quantity": 1, "weight": 3},
    "strange_saloid": {"u_name": "Дивний шприц з салоїдами", "quantity": 1, "weight": 2},
}
# ===============================================

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
        # --- Функція створення нового пацєтка ---
        cur.execute("""
            INSERT INTO players (chat_id, user_id, username, pet_name, weight, created_at, born_utc)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (chat_id, user_id, username or '', pet_name, STARTING_WEIGHT, now_utc(), now_utc()))
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

# === NEW FEATURE: Колесо Фортуни (DB Helpers) ===
def set_last_wheel_date_and_count(chat_id, user_id, ts=None, count=0):
    ts = ts or now_utc().date()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET last_wheel_utc=%s, daily_wheel_count=%s WHERE chat_id=%s AND user_id=%s", (ts, count, chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def increment_wheel_count(chat_id, user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET daily_wheel_count = daily_wheel_count + 1 WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()
# =================================================

# === NEW FEATURE: Pet Cooldown (DB Helper) ===
def update_last_pet_time(chat_id, user_id, ts=None):
    ts = ts or now_utc()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET last_pet_utc=%s WHERE chat_id=%s AND user_id=%s", (ts, chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()
# ===============================================

# === NEW FEATURE: Message cleanup (DB Helper) ===
def update_last_message_id(chat_id, user_id, message_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET last_message_id=%s WHERE chat_id=%s AND user_id=%s", (message_id, chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def get_chat_cleanup_status(chat_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT cleanup_enabled FROM players WHERE chat_id=%s LIMIT 1", (chat_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row['cleanup_enabled'] if row else True

def set_chat_cleanup_status(chat_id, status):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET cleanup_enabled=%s WHERE chat_id=%s", (status, chat_id))
    conn.commit()
    cur.close()
    conn.close()
# ===============================================

# === NEW FEATURE: Смерть і вербування (DB helpers) ===
def update_recruits_count(chat_id, user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT recruited_pets_count, last_recruitment_utc FROM players WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return

    recruits, last_date = row
    current_date = now_utc().date()

    if last_date is None or last_date < current_date:
        new_recruits = min(recruits + DAILY_RECRUITS_LIMIT, MAX_RECRUITED_PETS)
        cur.execute("UPDATE players SET recruited_pets_count=%s, last_recruitment_utc=%s WHERE chat_id=%s AND user_id=%s",
                    (new_recruits, current_date, chat_id, user_id))
        conn.commit()
        
    cur.close()
    conn.close()

def get_player_data(chat_id, user_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM players WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def kill_pet(chat_id, user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET weight=%s, pet_name=%s, last_feed_utc=NULL, daily_feeds_count=0, last_zonewalk_utc=NULL, daily_zonewalks_count=0, last_wheel_utc=NULL, daily_wheel_count=0, last_pet_utc=NULL WHERE chat_id=%s AND user_id=%s",
                (0, None, chat_id, user_id))
    cur.execute("DELETE FROM inventory WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def spawn_pet(chat_id, user_id, username):
    conn = get_conn()
    cur = conn.cursor()
    pet_name = f"Пацєтко_{user_id%1000}"
    cur.execute("UPDATE players SET weight=%s, pet_name=%s, recruited_pets_count=recruited_pets_count-1, last_feed_utc=NULL, daily_feeds_count=0, last_zonewalk_utc=NULL, daily_zonewalks_count=0, last_wheel_utc=NULL, daily_wheel_count=0, last_pet_utc=NULL WHERE chat_id=%s AND user_id=%s",
                (STARTING_WEIGHT, pet_name, chat_id, user_id))
    # --- Відродження після смерті ---
    cur.execute(
        "UPDATE players SET born_utc = %s WHERE chat_id=%s AND user_id=%s",
        (now_utc(), chat_id, user_id)
    )
    conn.commit()
    cur.close()
    conn.close()
# =======================================================

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
    cur.execute("SELECT user_id, username, pet_name, weight, born_utc FROM players WHERE chat_id=%s ORDER BY weight DESC LIMIT %s", (chat_id, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

# === Game mechanics ===
DAILY_FEEDS_LIMIT = 1
DAILY_ZONEWALKS_LIMIT = 2
DAILY_WHEEL_LIMIT = 3
PET_COOLDOWN_HOURS = 2
FIGHT_COOLDOWN_HOURS = 2
# =========================================================

# === NEW FEATURE: Смерть і вербування (Updated bounded_weight) ===
def bounded_weight(old, delta):
    new = old + delta
    return new
# ====================================================================

def pick_item_count():
    r = random.random()
    if r < 0.30:
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

# === NEW FEATURE: Колесо Фортуни (Main Logic) ===
def spin_wheel():
    items = list(WHEEL_REWARDS.keys())
    weights = [WHEEL_REWARDS[item]['weight'] for item in items]
    reward = random.choices(items, weights=weights, k=1)[0]
    return reward
# ===============================================
        
# === Time formatting helper ===
def format_timedelta(td):
    hours, remainder = divmod(td.total_seconds(), 3600)
    minutes, _ = divmod(remainder, 60)
    
    parts = []
    if hours > 0:
        parts.append(f"{math.floor(hours)} год")
    if minutes > 0:
        parts.append(f"{math.floor(minutes)} хв")
    
    if not parts:
        return "менше хвилини"
    
    return " ".join(parts)

def format_timedelta_to_next_day():
    """Formats time until the next UTC day as 'Xh Ym'."""
    now = now_utc()
    tomorrow_utc = (now + timedelta(days=1)).date()
    start_of_tomorrow = datetime.combine(tomorrow_utc, datetime.min.time(), tzinfo=timezone.utc)
    time_left = start_of_tomorrow - now
    
    return format_timedelta(time_left)

# --- Нові хелпери ---
def update_last_fight_time(chat_id, user_id, ts=None):
    ts = ts or now_utc()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET last_fight_utc=%s WHERE chat_id=%s AND user_id=%s", (ts, chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def get_alive_opponents(chat_id, exclude_user_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT user_id, pet_name, weight FROM players
        WHERE chat_id=%s AND user_id != %s AND weight > 0
        ORDER BY weight DESC
    """, (chat_id, exclude_user_id))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

# --- Хелпер --- 
def get_days_alive(born_utc):
    if not born_utc:
        return 0
    return (now_utc().date() - born_utc.date()).days
# =========================================================

# === Telegram helpers ===
def is_admin(chat_id, user_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChatMember"
    payload = {"chat_id": chat_id, "user_id": user_id}
    try:
        r = requests.post(url, json=payload, timeout=5)
        data = r.json()
        if data.get("ok"):
            status = data["result"]["status"]
            return status in ["creator", "administrator"]
    except Exception as e:
        print("is_admin error:", e)
    return False

def delete_message(chat_id, message_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage"
    payload = {"chat_id": chat_id, "message_id": message_id}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print('delete_message error', e)

def send_message(chat_id, user_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    
    # === NEW FEATURE: Message cleanup ===
    if chat_id < 0 and get_chat_cleanup_status(chat_id): # Only for group chats with cleanup enabled
        player = get_player_data(chat_id, user_id)
        if player:
            last_message_id = player.get('last_message_id')
            if last_message_id:
                delete_message(chat_id, last_message_id)
    # ====================================
    
    try:
        r = requests.post(url, json=payload, timeout=10)
        data = r.json()
        if data.get('ok'):
            message_id = data['result']['message_id']
            update_last_message_id(chat_id, user_id, message_id)
        return r
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
def handle_start(chat_id, user_id):
    txt = (
        "П.А.Ц.Є.Т.К.О. 2.\n\n"
        "ПАЦЄТКО СІ ВРОДИЛО - РАДІЄ ВСЕ СЕЛО НОВАЧКІВ!\n\n"
        "У кожного гравця є своє сталкер-пацєтко: його можна кормити (/feed), чухати за вушком (/pet), "
        "ходити в ходки в зону за хабаром(/zonewalk). Є інвентар, де буде лежати весь хабар вашого пацєти, (/inventory), також можна дати клікуху вашому пацєтку (/name), "
        "і подивитися топ по вазі і дізнатися хто найкраще сталкерське пацєтко (/top).\n\n"
        "Формат команд:\n"
        f"/feed [предмет] - безкоштовне харчування прямо від Бармена з Бару 100 Пятачків ({DAILY_FEEDS_LIMIT} разів на добу UTC). Додатково можна вказати предмет з інвентарю.\n"
        f"/zonewalk [предмет] - організувати ходку в небезпечну Зону ({DAILY_ZONEWALKS_LIMIT} разів на добу UTC). Додатково можна тяпнути енергетика або горілки, щоб мати можливість і сили сходити більше разів.\n"
        f"/wheel - крутнути умовне Колесо Фортуни, щоб виграти хабар ({DAILY_WHEEL_LIMIT} раз на добу UTC).\n"
        f"/pet - почухати пацю за вушком (кожні {PET_COOLDOWN_HOURS} год).\n"
        "/name Ім'я - дати ім'я пацєтці\n"
        "/top - топ-10 Сталкерів Пацєток чату за вагою\n"
        "/inventory - показати інвентарь\n"
        "/recruit - завербувати нове пацєтко, якщо старе померло.\n"
        "/check_recruits - перевірити кількість пацєток, доступних для вербування.\n"
        f"/fight - викликати пацєтко на бій (кожні {FIGHT_COOLDOWN_HOURS} год).\n"
        "\nАдмін-команди:\n"
        "/toggle_cleanup - вмикає/вимикає автоочищення повідомлень бота."
        "/clear_chat - видаляє останні повідомлення бота від кожного гравця."
    )
    send_message(chat_id, user_id, txt)

# === NEW FEATURE: Смерть і вербування (Death handler check) ===
def pet_is_dead_check(chat_id, user_id, pet_name, command_name):
    player = get_player_data(chat_id, user_id)
    if player and player['weight'] <= 0:
        recruits = player['recruited_pets_count']
        if recruits > 0:
            send_message(chat_id, user_id, f"На жаль, ваше пацєтко померло. Щоб продовжити грати, завербуйте нове за допомогою команди /recruit.\nУ вас є {recruits} пацєток для вербування.")
        else:
            time_left = format_timedelta_to_next_day()
            send_message(chat_id, user_id, f"На жаль, ваше пацєтко померло і у вас немає доступних пацєток для вербування. Нові пацєтки будуть доступні через {time_left}. Чекайте...")
        return True
    return False
# ==============================================================

def handle_name(chat_id, user_id, username, args_text):
    player = ensure_player(chat_id, user_id, username)
    update_recruits_count(chat_id, user_id)
    if pet_is_dead_check(chat_id, user_id, player.get('pet_name'), 'name'):
        return
        
    newname = args_text.strip()[:64]
    if not newname:
        send_message(chat_id, user_id, "Вкажи ім'я: /name Ім'я")
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET pet_name=%s WHERE chat_id=%s AND user_id=%s", (newname, chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()
    send_message(chat_id, user_id, f"Готово — твоє пацєтко тепер звати: {newname}")

def handle_top(chat_id, user_id):
    ensure_player(chat_id, user_id, None)
    update_recruits_count(chat_id, user_id)
    rows = top_players(chat_id, limit=10)
    if not rows:
        send_message(chat_id, user_id, "Ще немає пацєток у цьому чаті.")
        return
    lines = []
    # --- Топ пацєток --- 
    top_pets = rows
    top_lines = []
    for rank, row in enumerate(top_pets, start=1):
        if row['weight'] <= 0:
            continue
        days_alive = get_days_alive(row['born_utc'])
        name = row.get('pet_name') or row.get('username') or str(row['user_id'])
        line = f"{rank}. {name}  |  {row['weight']} кг  |  в Зоні {days_alive} дн."
        top_lines.append(line)
    
    send_message(chat_id, user_id, "Топ пацєток:\n" + "\n".join(top_lines))

def handle_pet(chat_id, user_id, username):
    player = ensure_player(chat_id, user_id, username)
    update_recruits_count(chat_id, user_id)
    if pet_is_dead_check(chat_id, user_id, player.get('pet_name'), 'pet'):
        return

    old = player['weight']
    pet_name = player.get('pet_name', 'Пацєтко')
    last_pet_time = player.get('last_pet_utc')
    current_time = now_utc()
    
    if last_pet_time:
        time_since_last_pet = current_time - last_pet_time
        cooldown = timedelta(hours=PET_COOLDOWN_HOURS)
        if time_since_last_pet < cooldown:
            time_left = cooldown - time_since_last_pet
            time_left_str = format_timedelta(time_left)
            send_message(chat_id, user_id, f"*звук цвіркунів* {pet_name} ніяк не реагує на чух. \nРаптом {pet_name} ліниво дістає годинник і дає тобі зрозуміти, що воно хоче наступний чух через {time_left_str}.")
            return

    update_last_pet_time(chat_id, user_id, current_time)
    
    if random.random() < 0.30:
        sign = random.choice([-1,1])
        delta = random.randint(1,3) * sign
        neww = bounded_weight(old, delta)
        update_weight(chat_id, user_id, neww)
        if neww <= 0:
            kill_pet(chat_id, user_id)
            send_message(chat_id, user_id, f"На жаль, {pet_name} так сильно налякалося, що отримало інфаркт і померло. Ви чухали його занадто сильно. Фініта ля комеді.")
            return

        if delta > 0:
            send_message(chat_id, user_id, f"Так файно вчухав пацю, що {pet_name} від радості засвоїв додатково {delta} кг сальця і тепер важить {neww} кг")
        else:
            send_message(chat_id, user_id, f"В цей раз паця сі невподобало чух і напряглося. Через стрес {pet_name} втратило {abs(delta)} кг сальця і тепер важить {neww} кг")
    else:
        send_message(chat_id, user_id, f"{pet_name} лише задоволено рохнуло і, поправивши протигазик, чавкнуло. Десь збоку дзижчала муха")


def handle_inventory(chat_id, user_id, username):
    player = ensure_player(chat_id, user_id, username)
    update_recruits_count(chat_id, user_id)
    if pet_is_dead_check(chat_id, user_id, player.get('pet_name'), 'inventory'):
        return

    inv = get_inventory(chat_id, user_id)
    if not inv:
        send_message(chat_id, user_id, "Інвентар порожній.")
        return
    lines = []
    for k,q in inv.items():
        u = ITEMS.get(k, {}).get('u_name', k)
        lines.append(f"* {u}: {q}")
    send_message(chat_id, user_id, "Інвентар:\n" + "\n".join(lines))

def handle_feed(chat_id, user_id, username, arg_item):
    player = ensure_player(chat_id, user_id, username)
    update_recruits_count(chat_id, user_id)
    if pet_is_dead_check(chat_id, user_id, player.get('pet_name'), 'feed'):
        return

    old = player['weight']
    last_feed_date = player.get('last_feed_utc')
    feed_count = player.get('daily_feeds_count')
    current_utc_date = now_utc().date()
    pet_name = player.get('pet_name', 'Пацєтко')
    messages = []
    
    if last_feed_date is None or last_feed_date < current_utc_date:
        feed_count = 0
        set_last_feed_date_and_count(chat_id, user_id, current_utc_date, count=0)
    
    FEED_PRIORITY = ['baton', 'sausage', 'can', 'vodka']
    free_feeds_left = DAILY_FEEDS_LIMIT - feed_count
    
    # === Обробка безкоштовної годівлі ===
    if free_feeds_left > 0 and not arg_item:
        r = random.random()
        if r < 0.35:
            # 35% шанс втрати ваги (від -40 до -1)
            delta = random.randint(1, 20)
        elif r < 0.50:
            # 15% шанс, що вага не зміниться (з 40% по 45%)
            delta = random.randint(21, 30)
        elif r < 0.55:
            # 5% шанс, що вага не зміниться (з 40% по 45%)
            delta = random.randint(31, 40)
        elif r < 0.60:
            # 5% шанс, що вага не зміниться (з 40% по 45%)
            delta = 0
        elif r < 0.85:
            # 25% шанс, що вага не зміниться (з 40% по 45%)
            delta = random.randint(-20, -1)
        elif r < 0.95:
            # 10% шанс, що вага не зміниться (з 40% по 45%)
            delta = random.randint(-30, -21)
        else:
            # 5% шанс набрати вагу (від 1 до 40)
            delta = random.randint(-40, -31)
        
        neww = bounded_weight(old, delta)
        update_weight(chat_id, user_id, neww)
        increment_feed_count(chat_id, user_id)
        if neww <= 0:
            kill_pet(chat_id, user_id)
            messages.append(f"Ви відкриваєте безкоштовну поставку харчів від Бармена: {pet_name} хряцає їжу, після чого так сильно просирається, що вмирає від срачки. Інколи зустріч з продуктами Бармена гірше, ніж зустріч з салососом.")
            send_message(chat_id, user_id, '\n'.join(messages))
            return

        if delta > 0:
            msg = f"{pet_name} наминає з апетитом, аж за вухами лящить. Файні харчі старий сьогодні привіз.\nПаця набрало {delta:+d} кг сальця і тепер важить {neww} кг"
        elif delta < 0:
            msg = f"{pet_name} неохоче поїло, після чого ви чуєте жахливий буркіт живота. Цей старий пиздун в цей раз передав протухші продукти.\n{pet_name} сильно просралося, втративши {abs(delta)} кг сальця і тепер важить {neww} кг"
        else:
            msg = f"{pet_name} з претензією дивиться на тебе. Схоже, в цей раз старий хрін передав бутлі з водою та мінімум харчів, від яких толку - трохи більше, ніж дірка від бублика.\nВага {pet_name} змінилась аж на ЦІЛИХ {delta:+d} кг сальця і важить {neww} кг."

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
                if random.random() < 0.40:
                    d = random.randint(a, 0)
                else:
                    d = random.randint(0, b)
                    
                neww = bounded_weight(old, d)
                update_weight(chat_id, user_id, neww)
                if neww <= 0:
                    kill_pet(chat_id, user_id)
                    messages.append(f"У {pet_name} бурчить в животі, і ти вирішив скористатися {ITEMS[item_to_use]['u_name']}. Але {ITEMS[item_to_use]['u_name']} виявилось отруєним, після чого пацєтко дає рідким і помирає від отруєння.")
                    send_message(chat_id, user_id, '\n'.join(messages))
                    return

                messages.append(f"У {pet_name} бурчить в животі, тому ти використав {ITEMS[item_to_use]['u_name']} з інвентарю. Паця набрало {d:+d} кг сальця і тепер важить {neww} кг")
                old = neww
            else:
                messages.append("Якась помилка. Предмет мав бути в інвентарі, але його не знайшли.")
        else:
            time_left = format_timedelta_to_next_day()
            messages.append(f"У тебе немає предметів для годівлі в інвентарі. {pet_name} залишилося голодним і з сумними очима лягло спати на пошарпаний диван в сховку.")
    
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
                    if neww <= 0:
                        kill_pet(chat_id, user_id)
                        messages.append(f"Пацєтко з'їло {ITEMS[key]['u_name']}, але {ITEMS[key]['u_name']} було отруєним і пацєтко смертельно просралося. Фініта ля комеді.")
                        send_message(chat_id, user_id, '\n'.join(messages))
                        return

                    if d > 0:
                        msg = f"Дав схрумкати {pet_name} {ITEMS[key]['u_name']}, і маєш приріст сальця!"
                    elif d < 0:
                        msg = f"{pet_name} з'їло {ITEMS[key]['u_name']} і щось пішло не так. {pet_name} просралося і вага зменшилася - мінус сальце."
                    else:
                        msg = f"Накормив пацєтко {ITEMS[key]['u_name']}, але вага не змінилась, сальця не додалося."

                    messages.append(f"{msg}. {pet_name} важило {old} кг, тепер {neww} кг (зміна сальця на {d:+d} кг)")
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
    
    send_message(chat_id, user_id, '\n'.join(messages) if messages else 'Нічого не сталося.')

def handle_zonewalk(chat_id, user_id, username, arg_item):
    player = ensure_player(chat_id, user_id, username)
    update_recruits_count(chat_id, user_id)

    if pet_is_dead_check(chat_id, user_id, player.get('pet_name'), 'zonewalk'):
        return

    last_zonewalk_date = player.get('last_zonewalk_utc')
    zonewalk_count = player.get('daily_zonewalks_count')
    pet_name = player.get('pet_name', 'Пацєтко')
    current_utc_date = now_utc().date()
    messages = []

    if last_zonewalk_date is None or last_zonewalk_date < current_utc_date:
        zonewalk_count = 0
        set_last_zonewalk_date_and_count(chat_id, user_id, current_utc_date, count=0)

    ZONEWALK_PRIORITY = ['energy', 'vodka']

    def do_one_walk(player_data):
        death_messages = [
            f"Під час ходки, {pet_name} загризли собаки.",
            f"{pet_name} вирішив дослідити закинуте село, і жахливий салосіся висмоктав все сальце у {pet_name}.",
            f"{pet_name} вирішив повеселитися і заліз в Карусель.",
            f"{pet_name} був поранений і просив допомоги, але інше пацєтко йомо лише сказало 'До зустрічі!'.",
            f"{pet_name} потрапив під Викид і розплавилося на шкварочки.",
            f"{pet_name} поліз з цікавості куди не треба і потрапив під вплив іншого Моноліту."
        ]

        # === Моментальна смерть (5% шанс) ===
        if random.random() < 0.05:
            kill_pet(chat_id, player_data['user_id'])
            death_title = "☠️Ще одне пацєтко поглинула Зона...☠️"
            death_text = random.choice(death_messages)
            return "Смерть", f"\n{death_title}\n{death_text}"
        # =====================================

        cnt = pick_item_count()
        loot = []
        if cnt > 0:
            loot = pick_loot(cnt)
            for it in loot:
                add_item(chat_id, user_id, it, 1)
        delta = zonewalk_weight_delta()
        oldw = player_data['weight']
        neww = bounded_weight(oldw, delta)
        update_weight(chat_id, user_id, neww)

        if neww <= 0:
            kill_pet(chat_id, user_id)
            return "Смерть", f"Під час ходки, {pet_name} наступив на аномалію, і помер. Смерть в зоні – звичне діло. Царство йому небесне."

        s = f"\nВ процесі ходки {pet_name} набрав {delta:+d} кг сальця, і тепер важить {neww} кг."
        if cnt == 0:
            s += "\nЦей раз без хабаря."
        else:
            s += f"\nЄ хабар! {pet_name} приніс: " + ", ".join(f"{ITEMS[it]['u_name']}" for it in loot)
        return "Продовження", s

    free_walks_left = DAILY_ZONEWALKS_LIMIT - zonewalk_count

    if free_walks_left > 0:
        if not arg_item:
            increment_zonewalk_count(chat_id, user_id)
            player_data = get_player_data(chat_id, user_id)
            status, s = do_one_walk(player_data)
            messages.append(f"Паця напялює протигаз, вдягає рюкзак, вішає за плече автомат і тупцює в Зону." + s)
            if status == "Смерть":
                send_message(chat_id, user_id, '\n'.join(messages))
                return
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
                player_data = get_player_data(chat_id, user_id)
                status, s = do_one_walk(player_data)
                messages.append(f"Пацєтко втомилося, тому ти використав {ITEMS[item_to_use]['u_name']} з інвентарю для додаткової ходки: " + s)
                if status == "Смерть":
                    send_message(chat_id, user_id, '\n'.join(messages))
                    return
            else:
                messages.append("Якась помилка. Предмет мав бути в інвентарі, але його не знайшли.")
        else:
            time_left = format_timedelta_to_next_day()
            messages.append(f"Паця втомилося, а у тебе немає ні енергетика, ні горілки в інвентарі. \n{pet_name} нікуди не пішло і залишилось травити анекдоти біля ватри з іншими пацєтками.")

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
                    player_data = get_player_data(chat_id, user_id)
                    status, s = do_one_walk(player_data)
                    messages.append(f"Використано {ITEMS[key]['u_name']} для додаткової ходки: " + s)
                    if status == "Смерть":
                        send_message(chat_id, user_id, '\n'.join(messages))
                        return

    if free_walks_left > 0 and not arg_item:
        time_left = format_timedelta_to_next_day()
        messages.append(f"\nА ще паця заряджене на перемогу і має сил на {free_walks_left} ходок до кінця доби.")
    elif free_walks_left <= 0 and not arg_item:
        time_left = format_timedelta_to_next_day()
        messages.append(f"\nЦе були останні сили на сьогодні для походів в Зону у паці. Сили на наступні будуть через {time_left}.")

    inv = get_inventory(chat_id, user_id)
    zone_items = {k: v for k, v in inv.items() if k in ITEMS and 'zonewalk' in (ITEMS[k]['uses_for'] or [])}
    if zone_items:
        lines = [f"{ITEMS[k]['u_name']}: {q}" for k, q in zone_items.items()]
        messages.append("У тебе є предмети для додаткових ходок: " + ", ".join(lines))

    send_message(chat_id, user_id, '\n'.join(messages) if messages else 'Нічого не сталося.')

# === NEW FEATURE: Колесо Фортуни (Command Handler) ===
def handle_wheel(chat_id, user_id, username):
    player = ensure_player(chat_id, user_id, username)
    update_recruits_count(chat_id, user_id)
    if pet_is_dead_check(chat_id, user_id, player.get('pet_name'), 'wheel'):
        return

    last_wheel_date = player.get('last_wheel_utc')
    wheel_count = player.get('daily_wheel_count')
    current_utc_date = now_utc().date()
    pet_name = player.get('pet_name', 'Пацєтко')
    
    if last_wheel_date is None or last_wheel_date < current_utc_date:
        wheel_count = 0
        set_last_wheel_date_and_count(chat_id, user_id, current_utc_date, count=0)
    
    pet_name = player.get('pet_name', 'Пацєтко')

    spins_left = DAILY_WHEEL_LIMIT - wheel_count

    if spins_left <= 0:
        time_left = format_timedelta_to_next_day()
        send_message(chat_id, user_id, f"Нажаль, на сьогодні для {pet_name} казино Золотий Хряцик закрите. Охоронці офають з позором {pet_name} і виганяють його з казіка. Наступний деп буде доступний через {time_left}.")
        return
        
    reward = spin_wheel()
    reward_info = WHEEL_REWARDS[reward]
    reward_name = reward_info['u_name']
    reward_qty = reward_info['quantity']
    new_spins_left = DAILY_WHEEL_LIMIT - (wheel_count + 1)
    
    if reward != "nothing":
        add_item(chat_id, user_id, reward, reward_qty)
        send_message(chat_id, user_id, f"Казіч крутиться, Сидор мутиться... і ви виграли: {reward_name} ({reward_qty} шт)! 🎉\n\nУ {pet_name} залишилося {new_spins_left} депів на сьогодні.")
    else:
        send_message(chat_id, user_id, f"Казіч крутиться, Сидор мутиться... і ви виграли: {reward_name}. \nНа жаль, фортуна сьогодні не на вашому боці. 😬\n\nУ {pet_name} залишилося {new_spins_left} депів на сьогодні.")
    
    increment_wheel_count(chat_id, user_id)
# =======================================================

# === NEW FEATURE: Смерть і вербування (New command handler) ===
def handle_recruit(chat_id, user_id, username):
    player = ensure_player(chat_id, user_id, username)
    update_recruits_count(chat_id, user_id)
    
    if player['weight'] > 0:
        send_message(chat_id, user_id, f"Ваше пацєтко ще живе! Ви не можете завербувати нове, доки старе не помре.")
        return

    recruits = get_player_data(chat_id, user_id)['recruited_pets_count']
    if recruits <= 0:
        time_left = format_timedelta_to_next_day()
        send_message(chat_id, user_id, f"На жаль, на ваш Моноліт наразі не молиться жодне паця. Нові послідовники будуть доступні через {time_left}.")
        return

    spawn_pet(chat_id, user_id, username)
    player = get_player_data(chat_id, user_id)
    new_recruits_count = player['recruited_pets_count']
    send_message(chat_id, user_id, f"Пацєтко сі вродило!\n\nВи активуєте ваш Моноліт, призиваючи і вербучи пацєтко. \nЙого вага {STARTING_WEIGHT} кг, а звуть {player['pet_name']}. \nУ вас залишилось {new_recruits_count} вірних пацєток для вербування.")

def handle_check_recruits(chat_id, user_id, username):
    player = ensure_player(chat_id, user_id, username)
    update_recruits_count(chat_id, user_id)
    
    recruits = get_player_data(chat_id, user_id)['recruited_pets_count']
    time_left = format_timedelta_to_next_day()

    if recruits > 0:
        send_message(chat_id, user_id, f"На ваш Моноліт зараз моляться {recruits} пацєток. Завербувати їх можна командою /recruit, якщо ваше поточне пацєтко помре.")
    else:
        send_message(chat_id, user_id, f"Наразі у вас немає доступних пацєток для вербування. Нові будуть доступні через {time_left}.")
# =============================================================

# --- Логіка бою ---
def process_fight(chat_id, attacker_id, defender_id):
    attacker = get_player_data(chat_id, attacker_id)
    defender = get_player_data(chat_id, defender_id)

    if not attacker or attacker['weight'] <= 0:
        send_message(chat_id, attacker_id, "Твоє пацєтко мертве і не може битися.")
        return
    if not defender or defender['weight'] <= 0:
        send_message(chat_id, attacker_id, "Обране пацєтко вже мертве.")
        return

    # Випадковий вибір переможця та переможеного
    fighters = [attacker, defender]
    winner_data = random.choice(fighters)
    loser_data = next(f for f in fighters if f['user_id'] != winner_data['user_id'])

    # Випадкові зміни ваги
    winner_delta = random.randint(1, 5)
    loser_delta = random.randint(-5, -1)

    winner_new_weight = bounded_weight(winner_data['weight'], winner_delta)
    loser_new_weight = bounded_weight(loser_data['weight'], loser_delta)

    update_weight(chat_id, winner_data['user_id'], winner_new_weight)
    update_weight(chat_id, loser_data['user_id'], loser_new_weight)

    fight_story = [
        f"Пацєтко {attacker['pet_name']} ({attacker['weight']} кг) підкотило до {defender['pet_name']} ({defender['weight']} кг).",
        "Пацєтки схрестили п’ятачки, і почалось... лупцювання, наче за останній батон у барі Сидора!"
    ]

    fight_story.append(f"💥 Пацєтко {winner_data['pet_name']} добряче відгатило {loser_data['pet_name']}! 💥 ")
    fight_story.append(f" По результатам потужне {winner_data['pet_name']} набрало {winner_delta} кг сальця і тепер важить {winner_new_weight} кг. \nВіддухопелене і відгачене {loser_data['pet_name']} втратило {abs(loser_delta)} кг сальця і тепер важить {loser_new_weight} кг.")

    if loser_new_weight <= 0:
        kill_pet(chat_id, loser_data['user_id'])
        loot = get_inventory(chat_id, loser_data['user_id'])
        if loot:
            for item, qty in loot.items():
                add_item(chat_id, winner_data['user_id'], item, qty)
        fight_story.append(f"💀 {loser_data['pet_name']} загинув у бою! Переможець хрюкаючи витрушує лут з туші і лутає хабар.")
    else:
        fight_story.append("Пацєтки розійшлися на перекур, пообіцявши продовжити якось іншим разом.")

    update_last_fight_time(chat_id, attacker_id)
    send_message(chat_id, attacker_id, "\n".join(fight_story))


# --- Команда /fight ---
def handle_fight(chat_id, user_id, username):
    player = ensure_player(chat_id, user_id, username)
    update_recruits_count(chat_id, user_id)
    pet_name = player.get('pet_name', 'Пацєтко')

    if pet_is_dead_check(chat_id, user_id, player.get('pet_name'), 'fight'):
        return

    last_fight_time = player.get('last_fight_utc')
    if last_fight_time:
        elapsed = now_utc() - last_fight_time
        cooldown = timedelta(hours=FIGHT_COOLDOWN_HOURS)
        if elapsed < cooldown:
            time_left = format_timedelta(cooldown - elapsed)
            send_message(chat_id, user_id, f"{pet_name} ще облизує подряпини після попередньої бійки і тягне чарку. \n{pet_name} відчуває що буде готовий знову гатитися через {time_left}.")
            return

    opponents = get_alive_opponents(chat_id, user_id)
    if not opponents:
        send_message(chat_id, user_id, "У цьому чаті немає живих пацєток для битви.")
        return

    buttons = []
    for opp in opponents:
        label = f"{opp['pet_name']} ({opp['weight']} кг)"
        buttons.append([{"text": label, "callback_data": f"fight:{user_id}:{opp['user_id']}"}])

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "Вибери, з ким твоя паця піде лупцюватися:",
        "reply_markup": {"inline_keyboard": buttons}
    }
    requests.post(url, json=payload)
# ========================================================


# === NEW FEATURE: Admin commands ===
def handle_toggle_cleanup(chat_id, user_id):
    if chat_id > 0:
        send_message(chat_id, user_id, "Ця команда працює лише в групових чатах.")
        return
    if not is_admin(chat_id, user_id):
        send_message(chat_id, user_id, "Лише адміністратори можуть використовувати цю команду.")
        return
    
    status = not get_chat_cleanup_status(chat_id)
    set_chat_cleanup_status(chat_id, status)
    
    if status:
        send_message(chat_id, user_id, "Автоматичне очищення повідомлень увімкнено.")
    else:
        send_message(chat_id, user_id, "Автоматичне очищення повідомлень вимкнено.")
    
def handle_clear_chat(chat_id, user_id):
    if chat_id > 0:
        send_message(chat_id, user_id, "Ця команда працює лише в групових чатах.")
        return
    if not is_admin(chat_id, user_id):
        send_message(chat_id, user_id, "Лише адміністратори можуть використовувати цю команду.")
        return
    
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT user_id, last_message_id FROM players WHERE chat_id=%s AND last_message_id IS NOT NULL", (chat_id,))
    players_to_clear = cur.fetchall()
    cur.close()
    conn.close()

    if not players_to_clear:
        send_message(chat_id, user_id, "Немає повідомлень бота для видалення.")
        return

    for player in players_to_clear:
        delete_message(chat_id, player['last_message_id'])
        update_last_message_id(chat_id, player['user_id'], None)

    send_message(chat_id, user_id, f"Видалено {len(players_to_clear)} останніх повідомлень бота.")
# ===============================================

# === Webhook endpoint ===
@app.route(f"/{TELEGRAM_TOKEN}", methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    if not update:
        return jsonify({'ok': True})
        
    # --- Обробка callback ---
    callback = update.get('callback_query')
    if callback:
        data = callback.get('data')
        chat_id = callback['message']['chat']['id']
        message_id = callback['message']['message_id']
        from_user = callback['from']
        user_id = from_user['id']

        if data.startswith("fight:"):
            _, attacker_id, defender_id = data.split(":")
            attacker_id = int(attacker_id)
            defender_id = int(defender_id)
            if user_id != attacker_id:
                return jsonify({'ok': True})
            process_fight(chat_id, attacker_id, defender_id)
            delete_message(chat_id, message_id)
        return jsonify({'ok': True})
    # ========================================================
    
    msg = update.get('message') or update.get('edited_message')
    if not msg:
        return jsonify({'ok': True})
    chat = msg.get('chat') or {}
    chat_id = chat.get('id')
    from_u = msg.get('from') or {}
    user_id = from_u.get('id')
    username = from_u.get('username')
    text = msg.get('text') or ''
    message_id = msg.get('message_id')
    
    is_command = text.startswith('/')
    if is_command and chat_id < 0: # Delete user's command message in group chats
        try:
            delete_message(chat_id, message_id)
        except Exception as e:
            print(f"Failed to delete user's command message: {e}")
            
    if not is_command:
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
            handle_start(chat_id, user_id)
        elif cmd == '/name':
            handle_name(chat_id, user_id, username, arg)
        elif cmd == '/top':
            handle_top(chat_id, user_id)
        elif cmd == '/pet':
            handle_pet(chat_id, user_id, username)
        elif cmd == '/inventory':
            handle_inventory(chat_id, user_id, username)
        elif cmd == '/feed':
            handle_feed(chat_id, user_id, username, arg)
        elif cmd == '/zonewalk':
            handle_zonewalk(chat_id, user_id, username, arg)
        elif cmd == '/wheel':
            handle_wheel(chat_id, user_id, username)
        elif cmd == '/toggle_cleanup':
            handle_toggle_cleanup(chat_id, user_id)
        elif cmd == '/clear_chat':
            handle_clear_chat(chat_id, user_id)
        # === NEW FEATURE: Смерть і вербування (New command) ===
        elif cmd == '/recruit':
            handle_recruit(chat_id, user_id, username)
        elif cmd == '/check_recruits':
            handle_check_recruits(chat_id, user_id, username)
        # =======================================================
        # --- Реєстрація команди ---
        elif cmd == '/fight':
            handle_fight(chat_id, user_id, username)
        # =======================================================
        else:
            send_message(chat_id, user_id, 'Невідома команда.')
    except Exception as e:
        print('error handling command', e)
        send_message(chat_id, user_id, 'Сталася помилка при обробці команди.')
    return jsonify({'ok': True})

if __name__ == '__main__':
    get_bot_username()
    if DATABASE_URL:
        init_db()
    set_webhook()
    app.run(host='0.0.0.0', port=PORT)
