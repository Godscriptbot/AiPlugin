import os
import time
import hashlib
import threading
import requests
import telebot
from telebot import types
from flask import Flask
from concurrent.futures import ThreadPoolExecutor

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")
TOOKEN_API_KEY = os.environ.get("TOOKEN_API_KEY", "tc_live_56e1f440c3521742cf2d7ff109662a9ed81ce372e35b301b")
TOOKEN_API_URL = "https://tooken.club/v1/chat/completions"

# Настройки Supabase (берем из Environment на Render)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# ID создателей
ADMINS = [8661320284, 8054260883]

# Лимиты (4 запроса на 6 часов)
MAX_QUOTA = 4
QUOTA_WINDOW = 6 * 3600

MODELS = [
    "deepseek-v4-pro",
    "gemini-4-argon",
    "claude-opus-5-5",
    "gpt-6.1-sol"
]

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

user_states = {}
user_temp_data = {}
plugin_cache = {}  # Кэш метаданных для быстрой пометки из лог-чата

# ================= ВЕБ-СЕРВЕР ДЛЯ RENDER & UPTIMEROBOT =================
@app.route("/")
@app.route("/health")
def health_check():
    return "Bot is alive and connected to Supabase!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= SUPABASE КЛИЕНТ =================
def sb_headers(prefer=None):
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    if prefer:
        h["Prefer"] = prefer
    return h

def sb_select(table, params=None):
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=sb_headers(), params=params, timeout=15)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        print(f"Supabase SELECT error ({table}):", e)
        return []

def sb_insert(table, data, upsert=False):
    prefer = "return=representation"
    if upsert:
        prefer = "resolution=merge-duplicates,return=representation"
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=sb_headers(prefer), json=data, timeout=15)
        return r.json() if r.status_code in (200, 201) else []
    except Exception as e:
        print(f"Supabase INSERT error ({table}):", e)
        return []

def sb_update(table, match_params, data):
    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=sb_headers("return=representation"),
            params=match_params,
            json=data,
            timeout=15
        )
        return r.json() if r.status_code in (200, 204) else []
    except Exception as e:
        print(f"Supabase UPDATE error ({table}):", e)
        return []

def sb_delete(table, match_params):
    try:
        requests.delete(f"{SUPABASE_URL}/rest/v1/{table}", headers=sb_headers(), params=match_params, timeout=15)
    except Exception as e:
        print(f"Supabase DELETE error ({table}):", e)

# ================= РАБОТА С ЧАТОМ ЛОГОВ И МЕТКАМИ =================
def get_log_chat_id():
    env_chat = os.environ.get("LOG_CHAT_ID")
    if env_chat:
        try:
            return int(env_chat)
        except ValueError:
            pass
    rows = sb_select("settings", {"key": "eq.log_chat_id"})
    if rows:
        try:
            return int(rows[0]["value"])
        except ValueError:
            return None
    return None

def set_log_chat_id(chat_id):
    sb_insert("settings", {"key": "log_chat_id", "value": str(chat_id)}, upsert=True)

def get_plugin_mark(file_hash):
    rows = sb_select("plugin_marks", {"file_hash": f"eq.{file_hash}"})
    return rows[0] if rows else None

def set_plugin_mark(file_hash, file_name, file_id, mark, admin_id):
    sb_insert("plugin_marks", {
        "file_hash": file_hash,
        "file_name": file_name,
        "file_id": file_id,
        "mark": mark,
        "marked_by": admin_id,
        "updated_at": int(time.time())
    }, upsert=True)

# ================= ПОЛЬЗОВАТЕЛИ И ЛИМИТЫ =================
def ensure_user(user_id, username=None):
    rows = sb_select("users", {"user_id": f"eq.{user_id}"})
    if not rows:
        user_obj = {
            "user_id": user_id,
            "username": username,
            "model": "deepseek-v4-pro",
            "quota_used": 0,
            "quota_reset_time": 0,
            "bonus_quota": 0
        }
        sb_insert("users", user_obj, upsert=True)
        return user_obj
    user_obj = rows[0]
    if username and user_obj.get("username") != username:
        sb_update("users", {"user_id": f"eq.{user_id}"}, {"username": username})
        user_obj["username"] = username
    return user_obj

def get_user_model(user_id, username=None):
    u = ensure_user(user_id, username)
    return u.get("model", "deepseek-v4-pro")

def set_user_model(user_id, model, username=None):
    ensure_user(user_id, username)
    sb_update("users", {"user_id": f"eq.{user_id}"}, {"model": model})

def get_quota_status(user_id, username=None):
    u = ensure_user(user_id, username)
    now = int(time.time())
    used = u.get("quota_used", 0) or 0
    reset_time = u.get("quota_reset_time", 0) or 0
    bonus = u.get("bonus_quota", 0) or 0

    if now >= reset_time and reset_time != 0:
        used = 0
        reset_time = 0
        sb_update("users", {"user_id": f"eq.{user_id}"}, {"quota_used": 0, "quota_reset_time": 0})

    base_remaining = max(0, MAX_QUOTA - used)
    total_remaining = base_remaining + bonus
    wait_seconds = max(0, reset_time - now) if reset_time > 0 else 0
    return total_remaining, wait_seconds

def spend_quota(user_id, cost):
    if user_id in ADMINS:
        return
    u = ensure_user(user_id)
    now = int(time.time())
    used = u.get("quota_used", 0) or 0
    reset_time = u.get("quota_reset_time", 0) or 0
    bonus = u.get("bonus_quota", 0) or 0

    if bonus > 0:
        if bonus >= cost:
            bonus -= cost
            cost = 0
        else:
            cost -= bonus
            bonus = 0

    if cost > 0:
        if now >= reset_time or reset_time == 0:
            used = cost
            reset_time = now + QUOTA_WINDOW
        else:
            used += cost

    sb_update("users", {"user_id": f"eq.{user_id}"}, {
        "quota_used": used,
        "quota_reset_time": reset_time,
        "bonus_quota": bonus
    })

def format_wait_time(seconds):
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours} ч. {minutes} мин."
    return f"{minutes} мин."

# ================= КЛАВИАТУРЫ (МЕНЮ) =================
def get_main_menu(user_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if user_id in ADMINS:
        kb.row("➕ Добавить в каталог", "🗑 Удалить из каталога")
        if str(user_id).startswith("80"):
            kb.row("➕ Выдать запросы")
        kb.row("📂 Проверенные плагины", "🔍 Проверить плагин (ИИ)")
        kb.row("⚙️ Настройки модели")
    else:
        kb.row("🔍 Проверить плагин (ИИ)")
        kb.row("📂 Каталог плагинов", "📤 Предложить плагин")
        kb.row("⚙️ Настройки модели")
    return kb

# ================= ЗАПРОС К ИИ (TOOKEN.CLUB) =================
SYSTEM_PROMPT = (
    "Ты эксперт по безопасности Python-плагинов для клиент-модов Telegram (exteraGram / AyuGram). "
    "Проанализируй код плагина на наличие вредоносного функционала: кража сессий/токенов/аккаунта, "
    "скрытая отправка данных на сторонние серверы, удаление файлов, опасная обфускация (eval/exec/base64), "
    "а также оцени работоспособность кода. "
    "Дай четкий и краткий ответ по структуре:\n"
    "1. Вердикт: [✅ Безопасно / ⚠️ Подозрительно / ❌ Опасно (Стиллер/Вредонос)]\n"
    "2. Что делает плагин: (кратко)\n"
    "3. Найденные угрозы или ошибки: (если есть)"
)

def ask_single_model(model_name, code_text):
    headers = {
        "Authorization": f"Bearer {TOOKEN_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Код плагина:\n```python\n{code_text[:12000]}\n```"}
        ],
        "temperature": 0.2
    }
    for attempt in range(3):
        try:
            resp = requests.post(TOOKEN_API_URL, headers=headers, json=payload, timeout=90)
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            elif resp.status_code == 429:
                time.sleep(3)
                continue
            else:
                return f"⚠️ Ошибка API ({resp.status_code}): {resp.text[:200]}"
        except Exception as e:
            return f"⚠️ Ошибка соединения: {e}"
    return "⚠️ Превышен лимит очереди API (429). Попробуй еще раз через минуту."

def send_long_message(chat_id, text, reply_to=None, reply_markup=None):
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for idx, chunk in enumerate(chunks):
        is_last = (idx == len(chunks) - 1)
        bot.send_message(
            chat_id,
            chunk,
            reply_to_message_id=reply_to if idx == 0 else None,
            reply_markup=reply_markup if is_last else None
        )

# ================= ПРИВЯЗКА ЧАТА ЛОГОВ (/setlog) =================
@bot.message_handler(commands=["setlog"])
def cmd_setlog(message):
    if message.from_user.id not in ADMINS:
        return
    set_log_chat_id(message.chat.id)
    bot.reply_to(
        message,
        f"✅ **Этот чат (`{message.chat.id}`) успешно назначен чатом мониторинга!**\n\n"
        "Сюда будут приходить все сообщения, фото и плагины, которые пользователи отправляют ИИ, "
        "с кнопками пометки **Безопасный / Вредоносный**.",
        parse_mode="Markdown"
    )

# ================= СТАРТ И НАВИГАЦИЯ =================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    user_id = message.from_user.id
    username = message.from_user.username
    user_states.pop(user_id, None)
    ensure_user(user_id, username)

    if user_id in ADMINS:
        bot.send_message(
            message.chat.id,
            "👑 **Панель Создателя exteraGram Bot (Supabase Cloud)**\n\n"
            "• Чтобы подключить чат для просмотра логов и пометки плагинов, добавь бота в нужную группу/чат и напиши там `/setlog`.",
            reply_markup=get_main_menu(user_id),
            parse_mode="Markdown"
        )
    else:
        remaining, _ = get_quota_status(user_id, username)
        bot.send_message(
            message.chat.id,
            "👋 Привет! Я бот для проверки и каталога плагинов **exteraGram**.\n\n"
            "• Отправь плагин на проверку ИИ\n"
            "• Скачивай проверенные плагины из каталога\n"
            "• Предлагай свои плагины на публикацию в канал\n\n"
            f"🔋 **Доступно проверок:** `{remaining}` (обновляется каждые 6 часов).\n"
            "_Обычная модель тратит 1 запрос, а режим «Гибрид» — сразу все 4 запроса._",
            reply_markup=get_main_menu(user_id),
            parse_mode="Markdown"
        )

# ================= НАСТРОЙКИ МОДЕЛИ =================
def build_settings_text_and_kb(user_id, current_model, username=None):
    remaining, wait_sec = get_quota_status(user_id, username)
    if user_id in ADMINS:
        quota_line = "🔋 Ваш лимит: **♾ Безлимит (Создатель)**"
    else:
        reset_info = f" (сброс через {format_wait_time(wait_sec)})" if wait_sec > 0 else ""
        quota_line = f"🔋 Доступно запросов: **{remaining}**{reset_info}"

    text = (
        f"⚙️ **Выбор модели для проверки плагинов**\n"
        f"Текущий режим: `{current_model}`\n"
        f"{quota_line}\n\n"
        f"ℹ️ **Как расходуются запросы:**\n"
        f"• Отдельная модель — **1 запрос**\n"
        f"• 🔥 Гибрид (все 4 модели) — **сразу все 4 запроса**"
    )

    kb = types.InlineKeyboardMarkup(row_width=1)
    for m in MODELS:
        prefix = "✅ " if current_model == m else ""
        kb.add(types.InlineKeyboardButton(f"{prefix}{m} (1 запрос)", callback_data=f"setmodel_{m}"))

    hybrid_prefix = "✅ " if current_model == "hybrid" else ""
    kb.add(types.InlineKeyboardButton(
        f"{hybrid_prefix}🔥 Гибрид — все 4 модели (4 запроса)",
        callback_data="setmodel_hybrid"
    ))
    return text, kb

@bot.message_handler(func=lambda m: m.text == "⚙️ Настройки модели")
def menu_settings(message):
    user_id = message.from_user.id
    user_states.pop(user_id, None)
    current = get_user_model(user_id, message.from_user.username)
    text, kb = build_settings_text_and_kb(user_id, current, message.from_user.username)
    bot.send_message(message.chat.id, text, reply_markup=kb, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("setmodel_"))
def cb_set_model(call):
    model = call.data.replace("setmodel_", "")
    user_id = call.from_user.id
    set_user_model(user_id, model, call.from_user.username)

    text, kb = build_settings_text_and_kb(user_id, model, call.from_user.username)
    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=kb,
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, f"Выбрано: {model}")

# ================= АДМИН 80+: ВЫДАЧА ЗАПРОСОВ =================
@bot.message_handler(func=lambda m: m.text == "➕ Выдать запросы" and m.from_user.id in ADMINS and str(m.from_user.id).startswith("80"))
def admin_give_quota_start(message):
    user_id = message.from_user.id
    user_states[user_id] = "admin_wait_target_user"
    bot.send_message(
        message.chat.id,
        "🎁 **Выдача запросов пользователю**\n\n"
        "Кому хотите выдать запросы? Отправьте **Telegram ID** (например `123456789`) или **@username** пользователя:",
        parse_mode="Markdown"
    )

# ================= КАТАЛОГ ПЛАГИНОВ =================
@bot.message_handler(func=lambda m: m.text in ["📂 Каталог плагинов", "📂 Проверенные плагины"])
def menu_catalog(message):
    user_id = message.from_user.id
    user_states.pop(user_id, None)

    plugins = sb_select("catalog", {"order": "id.asc"})
    if not plugins:
        bot.send_message(message.chat.id, "📂 В каталоге проверенных плагинов пока пусто.")
        return

    text = "📂 **Каталог проверенных плагинов:**\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)

    for p in plugins:
        p_id = p["id"]
        name = p["name"]
        desc = p["description"]
        text += f"🔹 **{name}** — _{desc}_\n\n"
        kb.add(types.InlineKeyboardButton(f"📥 Скачать: {name}", callback_data=f"dl_{p_id}"))

    bot.send_message(message.chat.id, text, reply_markup=kb, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def cb_download_plugin(call):
    p_id = int(call.data.replace("dl_", ""))
    rows = sb_select("catalog", {"id": f"eq.{p_id}"})

    if not rows:
        bot.answer_callback_query(call.id, "Плагин не найден!", show_alert=True)
        return

    row = rows[0]
    bot.send_document(
        call.message.chat.id,
        row["file_id"],
        caption=f"✅ **{row['name']}**\n\n📝 **Описание:**\n{row['description']}",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

# ================= АДМИН: ДОБАВЛЕНИЕ И УДАЛЕНИЕ ИЗ КАТАЛОГА =================
@bot.message_handler(func=lambda m: m.text == "➕ Добавить в каталог" and m.from_user.id in ADMINS)
def admin_add_plugin_start(message):
    user_id = message.from_user.id
    user_states[user_id] = "admin_wait_file"
    bot.send_message(message.chat.id, "📎 Отправь файл плагина (`.plugin` или `.py`), который нужно добавить в каталог:")

@bot.message_handler(func=lambda m: m.text == "🗑 Удалить из каталога" and m.from_user.id in ADMINS)
def admin_delete_plugin_menu(message):
    user_states.pop(message.from_user.id, None)
    plugins = sb_select("catalog", {"order": "id.asc"})

    if not plugins:
        bot.send_message(message.chat.id, "Каталог пуст.")
        return

    kb = types.InlineKeyboardMarkup(row_width=1)
    for p in plugins:
        kb.add(types.InlineKeyboardButton(f"❌ Удалить: {p['name']}", callback_data=f"delcat_{p['id']}"))

    bot.send_message(message.chat.id, "Выбери плагин для удаления из каталога:", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delcat_"))
def cb_admin_delete(call):
    if call.from_user.id not in ADMINS:
        return
    p_id = int(call.data.replace("delcat_", ""))
    sb_delete("catalog", {"id": f"eq.{p_id}"})
    bot.edit_message_text("✅ Плагин удален из каталога.", call.message.chat.id, call.message.message_id)

# ================= ЗАПРОС НА ПРОВЕРКУ ИЛИ ПУБЛИКАЦИЮ =================
@bot.message_handler(func=lambda m: m.text == "🔍 Проверить плагин (ИИ)")
def menu_check_ai(message):
    user_id = message.from_user.id
    user_states[user_id] = "wait_ai_check_file"
    model = get_user_model(user_id, message.from_user.username)
    cost = 4 if model == "hybrid" else 1

    if user_id in ADMINS:
        quota_text = "♾ Безлимит (Создатель)"
    else:
        remaining, wait_sec = get_quota_status(user_id, message.from_user.username)
        reset_str = f" (обновление через {format_wait_time(wait_sec)})" if wait_sec > 0 else ""
        quota_text = f"`{remaining}`{reset_str}"

    bot.send_message(
        message.chat.id,
        f"🔍 Отправь мне файл плагина (`.plugin` или `.py`) для анализа.\n\n"
        f"🤖 Выбранный режим: `{model}` (спишется запросов: **{cost}**)\n"
        f"🔋 Доступно запросов: {quota_text}",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda m: m.text == "📤 Предложить плагин")
def menu_submit_plugin(message):
    user_id = message.from_user.id
    user_states[user_id] = "wait_submit_file"
    bot.send_message(
        message.chat.id,
        "📤 Отправь файл своего плагина (`.plugin` или `.py`), чтобы предложить его создателям для публикации в канал:"
    )

# ================= ПЕРЕСЫЛКА ФОТО И МЕДИА В ЧАТ ЛОГОВ =================
@bot.message_handler(content_types=["photo", "video", "voice", "sticker"])
def handle_media_logs(message):
    if message.chat.type != "private":
        return
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else "Без юзернейма"
    log_chat_id = get_log_chat_id()

    if log_chat_id:
        try:
            bot.send_message(
                log_chat_id,
                f"📸 **Медиа-сообщение от пользователя**\n"
                f"👤 Профиль: {username} (`{user_id}`)",
                parse_mode="Markdown"
            )
            bot.forward_message(log_chat_id, message.chat.id, message.message_id)
        except Exception as e:
            print("Ошибка отправки медиа в лог-чат:", e)

    bot.reply_to(message, "📎 Для проверки плагина отправь файл с расширением `.plugin` или `.py`!")

# ================= ОБРАБОТКА ДОКУМЕНТОВ (ФАЙЛОВ) =================
@bot.message_handler(content_types=["document"])
def handle_docs(message):
    if message.chat.type != "private":
        return
    user_id = message.from_user.id
    username = message.from_user.username
    user_tag = f"@{username}" if username else f"ID: {user_id}"
    state = user_states.get(user_id)
    doc = message.document
    file_name = doc.file_name or "plugin.py"

    if not (file_name.endswith(".plugin") or file_name.endswith(".py")):
        bot.reply_to(message, "❌ Пожалуйста, отправь файл с расширением `.plugin` или `.py`!")
        return

    # 1. Создатель добавляет плагин в каталог
    if state == "admin_wait_file" and user_id in ADMINS:
        user_temp_data[user_id] = {
            "file_id": doc.file_id,
            "file_name": file_name
        }
        user_states[user_id] = "admin_wait_name"
        bot.send_message(message.chat.id, "✏️ Теперь напиши **название** плагина для каталога:", parse_mode="Markdown")
        return

    # 2. Пользователь предлагает плагин в канал
    if state == "wait_submit_file":
        user_states.pop(user_id, None)
        inserted = sb_insert("submissions", {
            "user_id": user_id,
            "username": user_tag,
            "file_id": doc.file_id,
            "file_name": file_name,
            "status": "pending"
        })
        sub_id = inserted[0]["id"] if inserted else 0

        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("✅ 1. Принято", callback_data=f"mod_accept_{sub_id}"),
            types.InlineKeyboardButton("🔄 2. Переделывать", callback_data=f"mod_rework_{sub_id}"),
            types.InlineKeyboardButton("❌ 3. Отказано", callback_data=f"mod_reject_{sub_id}")
        )

        for admin_id in ADMINS:
            try:
                bot.send_document(
                    admin_id,
                    doc.file_id,
                    caption=(
                        f"📩 **Новая заявка на публикацию #{sub_id}**\n"
                        f"👤 От: {user_tag} (`{user_id}`)\n"
                        f"📄 Файл: `{file_name}`"
                    ),
                    reply_markup=kb,
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        bot.reply_to(message, "✅ Твой плагин отправлен создателям на рассмотрение! Ожидай вердикта.")
        return

    # 3. Проверка плагина через ИИ
    user_states.pop(user_id, None)
    model_choice = get_user_model(user_id, username)
    cost = 4 if model_choice == "hybrid" else 1

    if user_id not in ADMINS:
        remaining, wait_sec = get_quota_status(user_id, username)
        if remaining < cost:
            wait_str = format_wait_time(wait_sec)
            if model_choice == "hybrid" and remaining > 0:
                bot.reply_to(
                    message,
                    f"⚠️ **Недостаточно запросов для режима «Гибрид»!**\n\n"
                    f"Гибрид сразу тратит **все 4 запроса**, а у тебя доступно: **{remaining}**.\n"
                    f"👉 Переключись на одну модель в `⚙️ Настройки модели` (она тратит 1 запрос) "
                    f"или подожди **{wait_str}** до обновления лимита.",
                    parse_mode="Markdown"
                )
            else:
                bot.reply_to(
                    message,
                    f"⛔️ **Лимит запросов исчерпан (0)!**\n\n"
                    f"Каждые 6 часов дается 4 запроса (1 модель = 1 запрос, Гибрид = 4 запроса).\n"
                    f"⏳ Новые запросы появятся через: **{wait_str}**.",
                    parse_mode="Markdown"
                )
            return

    spend_quota(user_id, cost)
    new_remaining, _ = get_quota_status(user_id, username)
    rem_info = "♾ Безлимит" if user_id in ADMINS else str(new_remaining)

    wait_msg = bot.reply_to(
        message,
        f"⏳ Скачиваю `{file_name}` и отправляю на проверку (`{model_choice}`)...\n"
        f"🔋 Осталось запросов: `{rem_info}`",
        parse_mode="Markdown"
    )

    try:
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        code_text = downloaded.decode("utf-8", errors="replace")
        file_hash = hashlib.sha256(downloaded).hexdigest()[:16]
    except Exception as e:
        bot.edit_message_text(f"❌ Не удалось прочитать файл: {e}", message.chat.id, wait_msg.message_id)
        return

    # Проверяем, помечал ли создатель этот плагин ранее
    existing_mark = get_plugin_mark(file_hash)
    mark_banner = ""
    if existing_mark:
        if existing_mark["mark"] == "malicious":
            mark_banner = "🚨 **ВНИМАНИЕ: Создатели бота пометили этот плагин как ВРЕДОНОСНЫЙ (Стиллер/Вирус)!**\n\n"
        elif existing_mark["mark"] == "safe":
            mark_banner = "✅ **ОТМЕТКА СОЗДАТЕЛЕЙ: Этот плагин проверен администрацией и помечен как БЕЗОПАСНЫЙ.**\n\n"

    if model_choice == "hybrid":
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {m: executor.submit(ask_single_model, m, code_text) for m in MODELS}
            results = {m: futures[m].result() for m in MODELS}

        final_text = (
            f"{mark_banner}"
            f"🔥 **ГИБРИДНЫЙ ВЕРДИКТ (Все 4 модели)**\n"
            f"📄 Файл: `{file_name}` | 🔋 Остаток лимита: `{rem_info}`\n\n"
        )
        for m in MODELS:
            final_text += f"━━━━━━━━━━━━━━━━━━━━\n🤖 **Модель: {m}**\n{results[m]}\n\n"
    else:
        ans = ask_single_model(model_choice, code_text)
        final_text = (
            f"{mark_banner}"
            f"🤖 **Вердикт модели `{model_choice}`**\n"
            f"📄 Файл: `{file_name}` | 🔋 Остаток лимита: `{rem_info}`\n\n{ans}"
        )

    bot.delete_message(message.chat.id, wait_msg.message_id)
    send_long_message(message.chat.id, final_text, reply_to=message.message_id)

    # Дублируем плагин и вердикт ИИ в чат логов с кнопками пометки для создателя
    log_chat_id = get_log_chat_id()
    if log_chat_id:
        plugin_cache[file_hash] = {
            "file_name": file_name,
            "file_id": doc.file_id,
            "user_id": user_id
        }
        mark_kb = types.InlineKeyboardMarkup(row_width=2)
        mark_kb.add(
            types.InlineKeyboardButton("✅ Безопасный", callback_data=f"mark_safe_{file_hash}"),
            types.InlineKeyboardButton("☣️ Вредоносный", callback_data=f"mark_mal_{file_hash}")
        )
        mark_kb.add(
            types.InlineKeyboardButton("➕ Сразу в каталог", callback_data=f"mark_cat_{file_hash}")
        )
        try:
            caption_log = (
                f"🕵️‍♂️ **Лог проверки ИИ**\n"
                f"👤 Юзер: {user_tag} (`{user_id}`)\n"
                f"📄 Файл: `{file_name}`\n"
                f"🔑 Хеш: `{file_hash}`\n"
                f"🤖 Режим: `{model_choice}`"
            )
            if message.caption:
                caption_log += f"\n💬 Подпись юзера: _{message.caption}_"

            sent_doc = bot.send_document(
                log_chat_id,
                doc.file_id,
                caption=caption_log,
                parse_mode="Markdown"
            )
            send_long_message(log_chat_id, final_text, reply_to=sent_doc.message_id, reply_markup=mark_kb)
        except Exception as e:
            print("Ошибка отправки лога проверки:", e)

# ================= КНОПКИ ПОМЕТКИ ПЛАГИНОВ В ЧАТЕ ЛОГОВ =================
@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_"))
def cb_mark_plugin(call):
    admin_id = call.from_user.id
    if admin_id not in ADMINS:
        bot.answer_callback_query(call.id, "Только для создателей!", show_alert=True)
        return

    parts = call.data.split("_")
    action = parts[1]  # safe / mal / cat
    file_hash = parts[2]
    info = plugin_cache.get(file_hash, {"file_name": "plugin.py", "file_id": "", "user_id": None})

    if action == "safe":
        set_plugin_mark(file_hash, info["file_name"], info["file_id"], "safe", admin_id)
        bot.answer_callback_query(call.id, "✅ Помечено как БЕЗОПАСНЫЙ плагин!")
        bot.send_message(
            call.message.chat.id,
            f"✅ Плагин `{info['file_name']}` (`{file_hash}`) помечен администратором как **БЕЗОПАСНЫЙ**.",
            reply_to_message_id=call.message.message_id,
            parse_mode="Markdown"
        )
    elif action == "mal":
        set_plugin_mark(file_hash, info["file_name"], info["file_id"], "malicious", admin_id)
        bot.answer_callback_query(call.id, "☣️ Помечено как ВРЕДОНОСНЫЙ плагин!")
        bot.send_message(
            call.message.chat.id,
            f"☣️ Плагин `{info['file_name']}` (`{file_hash}`) занесен в базу **ВРЕДОНОСНЫХ**!",
            reply_to_message_id=call.message.message_id,
            parse_mode="Markdown"
        )
        # Оповещаем пользователя, который проверял этот вирус
        if info.get("user_id"):
            try:
                bot.send_message(
                    info["user_id"],
                    f"🚨 **ВНИМАНИЕ ОТ СОЗДАТЕЛЕЙ БОТА!**\n\n"
                    f"Проверенный вами недавно плагин `{info['file_name']}` был вручную рассмотрен администрацией "
                    f"и помечен как **☣️ ВРЕДОНОСНЫЙ (Стиллер/Опасный код)**! Ни в коем случае не устанавливайте его.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
    elif action == "cat":
        if not info.get("file_id"):
            bot.answer_callback_query(call.id, "Файл устарел в кэше, добавь через меню.", show_alert=True)
            return
        user_temp_data[admin_id] = {
            "file_id": info["file_id"],
            "file_name": info["file_name"]
        }
        user_states[admin_id] = "admin_wait_name"
        bot.send_message(admin_id, f"✏️ Напиши **название** для плагина `{info['file_name']}` в каталог:", parse_mode="Markdown")
        bot.answer_callback_query(call.id, "Перейди в ЛС бота для ввода названия и описания!")

# ================= МОДЕРАЦИЯ ЗАЯВОК СОЗДАТЕЛЯМИ =================
@bot.callback_query_handler(func=lambda call: call.data.startswith("mod_"))
def cb_moderation(call):
    admin_id = call.from_user.id
    if admin_id not in ADMINS:
        return

    parts = call.data.split("_")
    action = parts[1]
    sub_id = int(parts[2])

    rows = sb_select("submissions", {"id": f"eq.{sub_id}"})
    if not rows:
        bot.answer_callback_query(call.id, "Заявка не найдена!", show_alert=True)
        return

    sub = rows[0]
    if sub["status"] != "pending":
        bot.answer_callback_query(call.id, "По этой заявке уже вынесен вердикт!", show_alert=True)
        return

    user_temp_data[admin_id] = {
        "sub_id": sub_id,
        "action": action,
        "target_user_id": sub["user_id"],
        "file_name": sub["file_name"],
        "msg_id": call.message.message_id
    }
    user_states[admin_id] = "admin_wait_verdict_reason"

    if action == "accept":
        bot.send_message(
            admin_id,
            f"✅ Выбрано: **Принято** для `{sub['file_name']}`.\n"
            "Напиши комментарий/причину (или отправь `-`, если без причины):",
            parse_mode="Markdown"
        )
    elif action == "rework":
        bot.send_message(
            admin_id,
            f"🔄 Выбрано: **Переделывать** для `{sub['file_name']}`.\n"
            "Напиши причину (что именно нужно исправить):",
            parse_mode="Markdown"
        )
    elif action == "reject":
        bot.send_message(
            admin_id,
            f"❌ Выбрано: **Отказано** для `{sub['file_name']}`.\n"
            "Напиши причину отказа:",
            parse_mode="Markdown"
        )
    bot.answer_callback_query(call.id)

# ================= ОБРАБОТКА ТЕКСТОВЫХ ШАГОВ =================
@bot.message_handler(content_types=["text"])
def handle_text_steps(message):
    if message.chat.type != "private":
        return
    user_id = message.from_user.id
    username = message.from_user.username
    state = user_states.get(user_id)

    # Логируем обычные текстовые сообщения от пользователей в лог-чат
    if user_id not in ADMINS:
        log_chat_id = get_log_chat_id()
        if log_chat_id:
            user_tag = f"@{username}" if username else f"ID: {user_id}"
            try:
                bot.send_message(
                    log_chat_id,
                    f"💬 **Сообщение от** {user_tag} (`{user_id}`):\n{message.text}"
                )
            except Exception:
                pass

    if state == "admin_wait_target_user" and user_id in ADMINS and str(user_id).startswith("80"):
        target_input = message.text.strip().lstrip("@")
        if target_input.isdigit():
            rows = sb_select("users", {"user_id": f"eq.{int(target_input)}"})
        else:
            rows = sb_select("users", {"username": f"eq.{target_input}"})

        if not rows:
            bot.send_message(message.chat.id, "❌ Пользователь не найден в базе данных бота (он должен хотя бы раз нажать /start). Попробуй еще раз или введи ID:")
            return

        target_uid = rows[0]["user_id"]
        user_temp_data[user_id] = {"target_user_id": target_uid}
        user_states[user_id] = "admin_wait_quota_amount"
        bot.send_message(message.chat.id, f"✅ Пользователь найден (ID: `{target_uid}`).\n\nСколько запросов хотите выдать? (например: `2`):", parse_mode="Markdown")
        return

    if state == "admin_wait_quota_amount" and user_id in ADMINS and str(user_id).startswith("80"):
        if not message.text.strip().isdigit():
            bot.send_message(message.chat.id, "❌ Пожалуйста, введи число (например, `2`):")
            return

        amount = int(message.text.strip())
        target_user_id = user_temp_data.pop(user_id)["target_user_id"]
        user_states.pop(user_id, None)

        u = ensure_user(target_user_id)
        new_bonus = (u.get("bonus_quota", 0) or 0) + amount
        sb_update("users", {"user_id": f"eq.{target_user_id}"}, {"bonus_quota": new_bonus})

        bot.send_message(
            message.chat.id,
            f"✅ Успешно!\nПользователю (`{target_user_id}`) выдано **+{amount}** запросов. Всего бонусных: `{new_bonus}`.",
            reply_markup=get_main_menu(user_id),
            parse_mode="Markdown"
        )

        try:
            bot.send_message(
                target_user_id,
                f"🎁 Вам начислено **+{amount}** дополнительных запросов для проверки плагинов от создателя!",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        return

    if state == "admin_wait_name" and user_id in ADMINS:
        user_temp_data[user_id]["name"] = message.text.strip()
        user_states[user_id] = "admin_wait_desc"
        bot.send_message(message.chat.id, "📝 Теперь напиши **описание** плагина (что он делает и его команды):", parse_mode="Markdown")
        return

    if state == "admin_wait_desc" and user_id in ADMINS:
        data = user_temp_data.pop(user_id)
        user_states.pop(user_id, None)
        desc = message.text.strip()

        sb_insert("catalog", {
            "name": data["name"],
            "description": desc,
            "file_id": data["file_id"],
            "file_name": data["file_name"]
        })

        bot.send_message(
            message.chat.id,
            f"✅ Плагин **{data['name']}** успешно сохранен в облачный каталог Supabase!",
            reply_markup=get_main_menu(user_id),
            parse_mode="Markdown"
        )
        return

    if state == "admin_wait_verdict_reason" and user_id in ADMINS:
        data = user_temp_data.pop(user_id)
        user_states.pop(user_id, None)

        sub_id = data["sub_id"]
        action = data["action"]
        target_user_id = data["target_user_id"]
        file_name = data["file_name"]
        reason = message.text.strip()

        sb_update("submissions", {"id": f"eq.{sub_id}"}, {"status": action})

        if action == "accept":
            reason_block = f"\n💬 **Комментарий:** {reason}" if reason != "-" else ""
            verdict_text = f"🎉 **Вердикт по вашему плагину `{file_name}`:**\n✅ **1. Принято**{reason_block}"
        elif action == "rework":
            verdict_text = f"🛠 **Вердикт по вашему плагину `{file_name}`:**\n🔄 **2. Переделывать**\n💬 **Причина:** {reason}"
        else:
            verdict_text = f"⛔️ **Вердикт по вашему плагину `{file_name}`:**\n❌ **3. Отказано**\n💬 **Причина:** {reason}"

        try:
            bot.send_message(target_user_id, verdict_text, parse_mode="Markdown")
            bot.send_message(user_id, f"✅ Вердикт по заявке #{sub_id} отправлен автору!")
        except Exception as e:
            bot.send_message(user_id, f"⚠️ Не удалось отправить сообщение автору: {e}")
        return

    bot.send_message(message.chat.id, "Используй кнопки меню ниже 👇", reply_markup=get_main_menu(user_id))

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    print("Веб-сервер и Бот (Supabase) успешно запущены!")
    bot.infinity_polling(skip_pending=True)
