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

# === NEW FEATURE: Двобої (New parameters) ===
FIGHT_COOLDOWN_HOURS = 2
# =================================================

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
      last_fight_utc TIMESTAMPTZ,
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
    
    # === NEW FEATURE: Двобої (DB Migration) ===
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='players' AND column_name='last_fight_utc'")
    if not cur.fetchone():
        print("Adding 'last_fight_utc' column...")
        cur.execute("ALTER TABLE players ADD COLUMN last_fight_utc TIMESTAMPTZ")
    # =======================================================

    # Create tables if they don't exist
    cur.execute(sql_players_create)
    cur.execute(sql_inv)
    
    conn.commit()
    cur.close()
    conn.close()

# === Game data ===
ITEMS = {
    "baton": {"u_name": "Батон", "feed_delta": (-5,5), "uses_for": ["feed", "use_on_pet"]},
    "sausage": {"u_name": "Ковбаса", "feed_delta": (-9,9), "uses_for": ["feed", "use_on_pet"]},
    "can": {"u_name": 'Консерва "Сніданок Пацєти"', "feed_delta": (-15,15), "uses_for": ["feed", "use_on_pet"]},
    "vodka": {"u_name": 'Горілка "Пацятки"', "feed_delta": (-25,25), "uses_for": ["feed","zonewalk", "use_on_pet"]},
    "energy": {"u_name": 'Енергетик "Нон Хрюк"', "feed_delta": None, "uses_for": ["zonewalk", "use_on_pet"]},
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

# === NEW FEATURE: Колесо Фортуни (Rewards) ===
WHEEL_REWARDS = {
    "nothing": {"u_name": "Дуля з маком і консервна банка від Сидора", "quantity": 0, "weight": 40},
    "baton": {"u_name": "Батон", "quantity": 1, "weight": 20},
    "sausage": {"u_name": "Ковбаса", "quantity": 1, "weight": 20},
    "can": {"u_name": 'Консерва "Сніданок Пацєти"', "quantity": 1, "weight": 10},
    "vodka": {"u_name": 'Горілка "Пацятки"', "quantity": 1, "weight": 10},
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
        cur.execute("INSERT INTO players (chat_id, user_id, username, pet_name, weight, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (chat_id, user_id, username or '', pet_name, STARTING_WEIGHT, now_utc()))
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

def get_player_by_username(chat_id, username):
    if not username:
        return None
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM players WHERE chat_id=%s AND LOWER(username)=LOWER(%s)", (chat_id, username.lstrip('@')))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def get_player_by_pet_name(chat_id, pet_name):
    if not pet_name:
        return None
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM players WHERE chat_id=%s AND LOWER(pet_name)=LOWER(%s)", (chat_id, pet_name))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def kill_pet(chat_id, user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET weight=%s, pet_name=%s, last_feed_utc=NULL, daily_feeds_count=0, last_zonewalk_utc=NULL, daily_zonewalks_count=0, last_wheel_utc=NULL, daily_wheel_count=0, last_pet_utc=NULL, last_fight_utc=NULL WHERE chat_id=%s AND user_id=%s",
                (0, None, chat_id, user_id))
    cur.execute("DELETE FROM inventory WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def spawn_pet(chat_id, user_id, username):
    conn = get_conn()
    cur = conn.cursor()
    pet_name = f"Пацєтко_{user_id%1000}"
    cur.execute("UPDATE players SET weight=%s, pet_name=%s, recruited_pets_count=recruited_pets_count-1, last_feed_utc=NULL, daily_feeds_count=0, last_zonewalk_utc=NULL, daily_zonewalks_count=0, last_wheel_utc=NULL, daily_wheel_count=0, last_pet_utc=NULL, last_fight_utc=NULL WHERE chat_id=%s AND user_id=%s",
                (STARTING_WEIGHT, pet_name, chat_id, user_id))
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

def transfer_all_items(chat_id, from_user_id, to_user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT item, quantity FROM inventory WHERE chat_id=%s AND user_id=%s", (chat_id, from_user_id))
    items_to_transfer = cur.fetchall()
    for item, quantity in items_to_transfer:
        add_item(chat_id, to_user_id, item, quantity)
        remove_item(chat_id, from_user_id, item, quantity)
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
DAILY_FEEDS_LIMIT = 1
DAILY_ZONEWALKS_LIMIT = 2
DAILY_WHEEL_LIMIT = 3
PET_COOLDOWN_HOURS = 2

# === NEW FEATURE: Смерть і вербування (Updated bounded_weight) ===
def bounded_weight(old, delta):
    new = old + delta
    return new
# ====================================================================

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
        f"/fight @username - влаштувати кулачний двобій з пацєтком іншого гравця (раз на {FIGHT_COOLDOWN_HOURS} год).\n"
        f"/use <item> @username - використати свій предмет на пацєтці іншого гравця.\n"
        "/name Ім'я - дати ім'я пацєтці\n"
        "/top - топ-10 Сталкерів Пацєток чату за вагою\n"
        "/inventory - показати інвентарь\n"
        "/recruit - завербувати нове пацєтко, якщо старе померло.\n"
        "/check_recruits - перевірити кількість пацєток, доступних для вербування.\n"
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
    for i, p in enumerate(rows, start=1):
        if p['weight'] <= 0:
            continue
        name = p.get('pet_name') or p.get('username') or str(p['user_id'])
        lines.append(f"{i}. {name} — {p['weight']} кг")
    send_message(chat_id, user_id, "Топ пацєток:\n" + "\n".join(lines))

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
            send_message(chat_id, user_id, f"На жаль, {pet_name} так сильно налякалося, що отримало інфаркт і померло. Ви чухали його занадто сильно. Крапка. Кінець. Екран згас.")
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
        if r < 0.40:
            # 40% шанс втрати ваги (від -40 до -1)
            delta = random.randint(-40, -1)
        elif r < 0.45:
            # 5% шанс, що вага не зміниться (з 40% по 45%)
            delta = 0
        else:
            # 55% шанс набрати вагу (від 1 до 40)
            delta = random.randint(1, 40)
        
        neww = bounded_weight(old, delta)
        update_weight(chat_id, user_id, neww)
        increment_feed_count(chat_id, user_id)
        if neww <= 0:
            kill_pet(chat_id, user_id)
            messages.append(f"Ви відкриваєте безкоштовну поставку харчів від Бармена. Пацєтко дивиться на це, кашляє, і помирає від отруєння. Кінець. Амінь. Інші пацєтки ходять з цибулею і хлібом, бо старий хрін щось там намутив в продуктах.")
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
                    messages.append(f"У {pet_name} бурчить в животі, і ти вирішив скористатися {ITEMS[item_to_use]['u_name']}. Але замість їжі ти дістав протухлий іспорчений товар, після чого пацєтко помирає від отруєння. Кінець. Амінь.")
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
                        messages.append(f"Пацєтко з'їло {ITEMS[key]['u_name']}, але це виявився небезпечний продукт, і пацєтко померло від отруєння. Кінець. Амінь.")
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
            return "Смерть", f"Під час ходки, {pet_name} наступив на аномалію, і помер. Смерть в зоні – звичне діло. Царство йому небесне. Кінець. Амінь."

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
        messages.append(f"\nА ще паця заряджене на перемогу і має сил на {free_walks_left} ходок до кінця доби. ")
    elif free_walks_left <= 0 and not arg_item:
        time_left = format_timedelta_to_next_day()
        messages.append(f"\nЦе були останні сили на сьогодні для походів в Зону у паці. Сили на наступні будуть через {time_left}.")

    inv = get_inventory(chat_id, user_id)
    zone_items = {k:v for k,v in inv.items() if k in ITEMS and 'zonewalk' in (ITEMS[k]['uses_for'] or [])}
    if zone_items:
        lines = [f"{ITEMS[k]['u_name']}: {q}" for k,q in zone_items.items()]
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
    player = get_player_data(chat_id, user_id)
    if player and player['weight'] > 0:
        send_message(chat_id, user_id, "Ваше пацєтко ще живе. Не треба вербувати нове.")
        return
    
    if player and player['recruited_pets_count'] > 0:
        spawn_pet(chat_id, user_id, username)
        pet_name = get_player_data(chat_id, user_id).get('pet_name')
        send_message(chat_id, user_id, f"Ти що, думав, що на тебе немає заміни? Звісно є! Нове пацєтко на ім'я {pet_name} готове до пригод!")
    else:
        time_left = format_timedelta_to_next_day()
        send_message(chat_id, user_id, f"У вас немає доступних пацєток для вербування. Нові пацєтки будуть доступні через {time_left}. Чекайте...")

def handle_check_recruits(chat_id, user_id, username):
    player = ensure_player(chat_id, user_id, username)
    update_recruits_count(chat_id, user_id)
    recruits = player['recruited_pets_count']
    if recruits > 0:
        send_message(chat_id, user_id, f"У вас є {recruits} пацєток для вербування.")
    else:
        time_left = format_timedelta_to_next_day()
        send_message(chat_id, user_id, f"У вас немає доступних пацєток для вербування. Нові пацєтки будуть доступні через {time_left}.")
# =======================================================

# === NEW FEATURE: Двобої та використання предметів на інших гравцях ===
def handle_fight(chat_id, user_id, username, target_username):
    player1 = ensure_player(chat_id, user_id, username)
    if pet_is_dead_check(chat_id, user_id, player1.get('pet_name'), 'fight'):
        return

    player2 = get_player_by_username(chat_id, target_username)
    if not player2:
        send_message(chat_id, user_id, f"Пацєтко з кличкою '{target_username}' не знайдений.")
        return
    
    if player1['user_id'] == player2['user_id']:
        send_message(chat_id, user_id, "Твоє паця не Тайлер Дерден і не може битися саме з собою.")
        return

    if pet_is_dead_check(chat_id, player2['user_id'], player2.get('pet_name'), 'fight'):
        send_message(chat_id, user_id, f"На жаль, пацєтко гравця {player2.get('username', 'невідомий')} померло і не може брати участь в двобої.")
        return

    last_fight_time1 = player1.get('last_fight_utc')
    last_fight_time2 = player2.get('last_fight_utc')
    current_time = now_utc()
    cooldown = timedelta(hours=FIGHT_COOLDOWN_HOURS)

    if last_fight_time1 and (current_time - last_fight_time1) < cooldown:
        time_left = cooldown - (current_time - last_fight_time1)
        time_left_str = format_timedelta(time_left)
        send_message(chat_id, user_id, f"{player1['pet_name']} втомилося, вже билося і наразі відпчиває. На відновлення йому треба ще {time_left_str}.")
        return

    if last_fight_time2 and (current_time - last_fight_time2) < cooldown:
        time_left = cooldown - (current_time - last_fight_time2)
        time_left_str = format_timedelta(time_left)
        send_message(chat_id, user_id, f"Пацєтко гравця {player2.get('username', 'невідомий')} нещодавно билося. Воно відпочине ще {time_left_str}.")
        return
        
    update_last_pet_time(chat_id, player1['user_id'], current_time)
    update_last_pet_time(chat_id, player2['user_id'], current_time)

    update_recruits_count(chat_id, player1['user_id'])
    update_recruits_count(chat_id, player2['user_id'])

    pet1_name = player1.get('pet_name', 'Пацєтко_1')
    pet2_name = player2.get('pet_name', 'Пацєтко_2')
    weight1 = player1['weight']
    weight2 = player2['weight']

    messages = [f"Пацєтко {pet1_name} (вага {weight1} кг) викликає на махач на копитцях пацєтко {pet2_name} (вага {weight2} кг)!"]

    # Розрахунок переможця на основі ваги
    winner_weight, loser_weight = (weight1, weight2) if weight1 > weight2 else (weight2, weight1)
    
    # Більша вага дає перевагу, але не 100% перемогу
    win_chance_winner = winner_weight / (winner_weight + loser_weight)
    
    if random.random() < win_chance_winner:
        winner_id = player1['user_id'] if weight1 > weight2 else player2['user_id']
        loser_id = player2['user_id'] if weight1 > weight2 else player1['user_id']
        winner_name = pet1_name if weight1 > weight2 else pet2_name
        loser_name = pet2_name if weight1 > weight2 else pet1_name
    else:
        winner_id = player2['user_id'] if weight1 > weight2 else player1['user_id']
        loser_id = player1['user_id'] if weight1 > weight2 else player2['user_id']
        winner_name = pet2_name if weight1 > weight2 else pet1_name
        loser_name = pet1_name if weight1 > weight2 else pet2_name
        
    delta_winner = random.randint(1, 3)
    delta_loser = random.randint(1, 3)
    
    new_weight_winner = bounded_weight(get_player_data(chat_id, winner_id)['weight'], delta_winner)
    new_weight_loser = bounded_weight(get_player_data(chat_id, loser_id)['weight'], -delta_loser)

    update_weight(chat_id, winner_id, new_weight_winner)
    update_weight(chat_id, loser_id, new_weight_loser)
    
    messages.append(f"Переможець: {winner_name}! Він набрав {delta_winner} кг і тепер важить {new_weight_winner} кг.")
    messages.append(f"Переможений: {loser_name}. Він втратив {delta_loser} кг і тепер важить {new_weight_loser} кг.")
    
    # Логіка лутінгу
    if new_weight_loser <= 0:
        messages.append(f"Жах, {loser_name} отримав смертельні поранення і помер! {winner_name} забирає весь хабар з його інвентарю.")
        transfer_all_items(chat_id, loser_id, winner_id)
        kill_pet(chat_id, loser_id)
        
    send_message(chat_id, user_id, "\n".join(messages))

def handle_use(chat_id, user_id, username, args_text):
    player1 = ensure_player(chat_id, user_id, username)
    if pet_is_dead_check(chat_id, user_id, player1.get('pet_name'), 'use'):
        return

    parts = args_text.split()
    if len(parts) < 2:
        send_message(chat_id, user_id, "Вкажи предмет і ім'я гравця: /use <предмет> @username")
        return
        
    item_alias = parts[0].lower()
    target_username = parts[1]
    
    item_key = ALIASES.get(item_alias)
    if not item_key or 'use_on_pet' not in (ITEMS.get(item_key, {}).get('uses_for') or []):
        send_message(chat_id, user_id, "Цей предмет не можна використовувати на іншому пацєтку.")
        return

    if not remove_item(chat_id, user_id, item_key):
        send_message(chat_id, user_id, f"У тебе немає {ITEMS[item_key]['u_name']} в інвентарі.")
        return

    player2 = get_player_by_username(chat_id, target_username)
    if not player2:
        send_message(chat_id, user_id, f"Гравець з нікнеймом '{target_username}' не знайдений.")
        add_item(chat_id, user_id, item_key, 1) # Повертаємо предмет
        return

    if player1['user_id'] == player2['user_id']:
        send_message(chat_id, user_id, "Ти не можеш використати предмет на своєму пацєтку таким чином.")
        add_item(chat_id, user_id, item_key, 1) # Повертаємо предмет
        return
        
    if pet_is_dead_check(chat_id, player2['user_id'], player2.get('pet_name'), 'use'):
        send_message(chat_id, user_id, f"На жаль, пацєтко гравця {player2.get('username', 'невідомий')} померло і не може прийняти предмет.")
        add_item(chat_id, user_id, item_key, 1)
        return

    pet1_name = player1.get('pet_name', 'Пацєтко_1')
    pet2_name = player2.get('pet_name', 'Пацєтко_2')
    
    messages = []
    
    if item_key in ['baton', 'sausage', 'can', 'vodka']:
        a, b = ITEMS[item_key]['feed_delta']
        d = random.randint(a, b)
        new_weight = bounded_weight(player2['weight'], d)
        update_weight(chat_id, player2['user_id'], new_weight)

        if new_weight <= 0:
            kill_pet(chat_id, player2['user_id'])
            messages.append(f"Гравець {username} використав {ITEMS[item_key]['u_name']} на пацєтку {pet2_name}. На жаль, це виявився небезпечний продукт, і пацєтко померло від отруєння. Кінець. Амінь.")
        else:
            messages.append(f"Гравець {username} кинув {ITEMS[item_key]['u_name']} пацєтку {pet2_name}. Воно набрало {d:+d} кг і тепер важить {new_weight} кг.")

    elif item_key == 'energy':
        messages.append(f"Гравець {username} хлюпнув {ITEMS[item_key]['u_name']} на пацєтко {pet2_name}. Від дикого реву і трясіння воно здивовано кліпнуло очима і втекло в кущі. Жодних наслідків.")
        
    send_message(chat_id, user_id, "\n".join(messages))
    
def handle_clear_chat(chat_id, user_id):
    if not is_admin(chat_id, user_id):
        send_message(chat_id, user_id, "Тільки адміністратори можуть використовувати цю команду.")
        return

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT user_id, last_message_id FROM players WHERE chat_id=%s AND last_message_id IS NOT NULL", (chat_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        send_message(chat_id, user_id, "Немає повідомлень бота для видалення.")
        return

    for row in rows:
        delete_message(chat_id, row['last_message_id'])
        update_last_message_id(chat_id, row['user_id'], None)
    
    send_message(chat_id, user_id, "Повідомлення бота було очищено.")

def handle_toggle_cleanup(chat_id, user_id):
    if not is_admin(chat_id, user_id):
        send_message(chat_id, user_id, "Тільки адміністратори можуть використовувати цю команду.")
        return

    current_status = get_chat_cleanup_status(chat_id)
    new_status = not current_status
    set_chat_cleanup_status(chat_id, new_status)

    if new_status:
        send_message(chat_id, user_id, "Автоматичне очищення повідомлень бота тепер увімкнено.")
    else:
        send_message(chat_id, user_id, "Автоматичне очищення повідомлень бота тепер вимкнено.")

# === Main router ===
def handle_update(data):
    try:
        if 'message' not in data:
            return
        
        message = data['message']
        text = message.get('text', '')
        chat_id = message['chat']['id']
        from_user = message.get('from', {})
        user_id = from_user.get('id')
        username = from_user.get('username')
        
        if user_id is None:
            return # Skip messages without a user_id
            
        if BOT_USERNAME and text.lower().startswith(f'/{BOT_USERNAME}'):
            text = text[len(f'/{BOT_USERNAME}'):]

        parts = text.split()
        command = parts[0].lower() if parts else ''
        args = parts[1:]
        args_text = ' '.join(args)

        if command == '/start':
            handle_start(chat_id, user_id)
        elif command == '/name':
            handle_name(chat_id, user_id, username, args_text)
        elif command == '/top':
            handle_top(chat_id, user_id)
        elif command == '/pet':
            handle_pet(chat_id, user_id, username)
        elif command == '/inventory':
            handle_inventory(chat_id, user_id, username)
        elif command == '/feed':
            handle_feed(chat_id, user_id, username, args_text)
        elif command == '/zonewalk':
            handle_zonewalk(chat_id, user_id, username, args_text)
        elif command == '/wheel':
            handle_wheel(chat_id, user_id, username)
        elif command == '/recruit':
            handle_recruit(chat_id, user_id, username)
        elif command == '/check_recruits':
            handle_check_recruits(chat_id, user_id, username)
        elif command == '/fight':
            handle_fight(chat_id, user_id, username, args_text)
        elif command == '/use':
            handle_use(chat_id, user_id, username, args_text)
        elif command == '/clear_chat':
            handle_clear_chat(chat_id, user_id)
        elif command == '/toggle_cleanup':
            handle_toggle_cleanup(chat_id, user_id)
            
    except Exception as e:
        print("Error handling update:", e)


@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
def webhook():
    if request.method == 'POST':
        update = request.json
        handle_update(update)
        return jsonify({"status": "ok"})
    return "ok"

@app.route('/')
def index():
    return "Пацєтко 2.0 Bot is running."

if __name__ == '__main__':
    get_bot_username()
    init_db()
    set_webhook()
    app.run(host='0.0.0.0', port=PORT)
