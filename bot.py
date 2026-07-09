import os
import asyncio
import json
from datetime import datetime, timezone
from aiohttp import web

from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- КОНФИГ ----------
BOT_TOKEN = "8956643411:AAHU2b5FmZ2In7Bvf7XJebWxrylx9NOVwp0"
API_ID = 22376342
API_HASH = "f623dc4ae2b015463cfde7874ab0f270"

CHANNELS_FILE = "channels.json"
PORT = int(os.getenv("PORT", 8080))

# ---------- СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЕЙ ----------
user_states = {}

def get_user_state(user_id):
    if user_id not in user_states:
        user_states[user_id] = {
            "client": None,
            "is_authorized": False,
            "phone": None,
            "phone_code_hash": None,
            "monitoring": False,
            "monitor_start_time": None,
            "notified_keys": set(),
            "draft_text": "🥇 Первый!",
            "pending_action": None,
            "tracked_channels": [],
            "handler_registered": False,
        }
    return user_states[user_id]

# ---------- БАЗА КАНАЛОВ ----------
def load_channels():
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_channels(data):
    with open(CHANNELS_FILE, "w") as f:
        json.dump(data, f)

# ---------- ВСПОМОГАТЕЛЬНЫЕ ----------
def log(msg: str, emoji: str = "•"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"  [{ts}] {emoji} {msg}")

def fmt_draft(text: str) -> str:
    now = datetime.now()
    return text.replace("{time}", now.strftime("%H:%M")).replace("{date}", now.strftime("%d.%m.%Y"))

def make_post_link(channel_id: int, message_id: int) -> str:
    chat_id_str = str(channel_id)
    if chat_id_str.startswith("-100"):
        chat_id_clean = chat_id_str[4:]
        return f"https://t.me/c/{chat_id_clean}/{message_id}"
    return f"https://t.me/c/{channel_id}/{message_id}"

# ---------- КЛАВИАТУРЫ ----------
def main_menu_keyboard():
    rows = [
        [
            InlineKeyboardButton("🔐 Войти", callback_data="login", style="primary"),
            InlineKeyboardButton("▶️ Запустить", callback_data="start_monitoring", style="success")
        ],
        [
            InlineKeyboardButton("⏹ Остановить", callback_data="stop_monitoring", style="danger"),
            InlineKeyboardButton("✏️ Текст", callback_data="set_draft", style="primary")
        ],
        [
            InlineKeyboardButton("📋 Каналы", callback_data="manage_channels", style="primary"),
            InlineKeyboardButton("🚪 Выйти", callback_data="logout", style="danger")
        ],
    ]
    return InlineKeyboardMarkup(rows)

def channels_menu_keyboard():
    rows = [
        [
            InlineKeyboardButton("➕ Добавить", callback_data="add_channel", style="success"),
            InlineKeyboardButton("❌ Удалить", callback_data="remove_channel", style="danger")
        ],
        [
            InlineKeyboardButton("🌍 Все каналы", callback_data="all_channels", style="primary"),
        ],
        [
            InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu", style="primary")
        ],
    ]
    return InlineKeyboardMarkup(rows)

# ---------- КЛИЕНТ С ПЕРЕСОЗДАНИЕМ ПРИ ОШИБКЕ ----------
async def get_client(user_id):
    state = get_user_state(user_id)
    client = state["client"]

    # Если клиент есть, пробуем использовать
    if client is not None:
        try:
            if not client.is_connected():
                await client.connect()
            if not state["is_authorized"]:
                state["is_authorized"] = await client.is_user_authorized()
            return client
        except Exception as e:
            log(f"Клиент {user_id} сломался: {e}", "❌")
            # Удаляем старый клиент
            try:
                await client.disconnect()
            except:
                pass
            client = None
            state["client"] = None
            state["is_authorized"] = False

    # Создаём новый
    session_file = f"user_session_{user_id}"
    new_client = TelegramClient(session_file, API_ID, API_HASH)
    await new_client.connect()
    state["client"] = new_client
    state["is_authorized"] = await new_client.is_user_authorized()

    if state["is_authorized"]:
        log(f"✅ {user_id} авторизован", "✅")
    else:
        log(f"⚠️ {user_id} не авторизован", "⚠️")

    # Регистрируем обработчик один раз
    if not state["handler_registered"]:
        @new_client.on(events.NewMessage)
        async def handler(event, uid=user_id):
            await handle_new_message(event, uid)
        state["handler_registered"] = True

    return new_client

# ---------- ОБРАБОТЧИК НОВЫХ ПОСТОВ ----------
async def handle_new_message(event, user_id):
    state = get_user_state(user_id)
    if not state["monitoring"]:
        return
    if not event.is_channel:
        return

    chat = await event.get_chat()
    if getattr(chat, "megagroup", False):
        return
    if not getattr(chat, "broadcast", False):
        return

    if state["tracked_channels"]:
        username = getattr(chat, "username", None)
        chat_id_str = str(chat.id)
        if username not in state["tracked_channels"] and chat_id_str not in state["tracked_channels"]:
            return

    msg_date = event.message.date
    if msg_date.tzinfo is None:
        msg_date = msg_date.replace(tzinfo=timezone.utc)
    if state["monitor_start_time"] and msg_date < state["monitor_start_time"]:
        return

    key = f"{chat.id}:{event.message.id}"
    if key in state["notified_keys"]:
        return
    state["notified_keys"].add(key)

    title = getattr(chat, "title", str(chat.id))
    comment_text = fmt_draft(state["draft_text"])

    log(f"📢 [{user_id}] {title} → {comment_text}", "📢")
    asyncio.create_task(_send_with_retry(user_id, chat, event.message.id, comment_text, title, key))

async def _send_with_retry(user_id, chat, message_id, text, title, key):
    success = await send_comment(user_id, chat, message_id, text)
    state = get_user_state(user_id)
    if success:
        log(f"✅ [{user_id}] {title}: {text}", "💬")
    else:
        state["notified_keys"].discard(key)

async def send_comment(user_id, chat, message_id: int, text: str) -> bool:
    state = get_user_state(user_id)
    for attempt in range(3):
        try:
            client = await get_client(user_id)
            if not state["is_authorized"]:
                return False
            await client.send_message(chat, text, comment_to=message_id)
            return True
        except FloodWaitError as e:
            log(f"⏳ FloodWait {e.seconds} сек для {user_id}", "⏳")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            log(f"❌ [{user_id}] Ошибка {attempt+1}: {e}", "❌")
            # Сбрасываем клиент, чтобы пересоздать
            if state["client"]:
                try:
                    await state["client"].disconnect()
                except:
                    pass
                state["client"] = None
                state["is_authorized"] = False
            await asyncio.sleep(2)
    return False

# ---------- КОМАНДЫ БОТА ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    state = get_user_state(user_id)
    all_channels = load_channels()
    state["tracked_channels"] = all_channels.get(str(user_id), [])

    await get_client(user_id)

    logged = "✅ Да" if state["is_authorized"] else "❌ Нет"
    status = "🟢 Работает" if state["monitoring"] else "🔴 Остановлен"
    channels_info = f"{len(state['tracked_channels'])} шт" if state["tracked_channels"] else "Все"

    await update.effective_chat.send_message(
        f"⚡ БОТ-КОММЕНТАТОР\n\n"
        f"🔐 {logged}\n"
        f"💬 {state['draft_text']}\n"
        f"📋 Каналы: {channels_info}\n"
        f"📡 {status}",
        reply_markup=main_menu_keyboard()
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    state = get_user_state(user_id)
    if state["pending_action"]:
        state["pending_action"] = None
        await update.message.reply_text("❌ Отменено.")
    else:
        await update.message.reply_text("Нет активного действия.")

# ---------- ОБРАБОТЧИК КНОПОК ----------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    state = get_user_state(user_id)
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "login":
        await get_client(user_id)
        if state["is_authorized"]:
            await query.message.reply_text("✅ Уже авторизован!")
            return
        state["phone"] = None
        state["phone_code_hash"] = None
        state["pending_action"] = "await_phone"
        await query.message.reply_text("📱 Отправь номер:\n+79001234567")

    elif data == "start_monitoring":
        if not state["is_authorized"]:
            await query.message.reply_text("❌ Сначала войди!")
            return
        state["monitoring"] = True
        state["monitor_start_time"] = datetime.now(timezone.utc)
        state["notified_keys"].clear()
        await query.message.reply_text(f"▶️ ЗАПУЩЕНО!\n\n💬 {state['draft_text']}")

    elif data == "stop_monitoring":
        state["monitoring"] = False
        await query.message.reply_text("⏹ Остановлено!")

    elif data == "set_draft":
        state["pending_action"] = "await_draft"
        await query.message.reply_text("✏️ Отправь текст:\nМожно {time} и {date}")

    elif data == "manage_channels":
        await query.message.reply_text("📋 Управление каналами", reply_markup=channels_menu_keyboard())

    elif data == "add_channel":
        state["pending_action"] = "await_channel_add"
        await query.message.reply_text("➕ Отправь @username канала:")

    elif data == "remove_channel":
        state["pending_action"] = "await_channel_remove"
        await query.message.reply_text("❌ Отправь @username для удаления:")

    elif data == "all_channels":
        state["tracked_channels"] = []
        all_channels = load_channels()
        all_channels[str(user_id)] = []
        save_channels(all_channels)
        await query.message.reply_text("🌍 Все каналы!")

    elif data == "back_to_menu":
        await cmd_start(update, context)

    elif data == "logout":
        if state["client"]:
            try:
                await state["client"].disconnect()
            except:
                pass
        state["client"] = None
        state["is_authorized"] = False
        state["monitoring"] = False
        state["notified_keys"].clear()
        state["handler_registered"] = False
        await query.message.reply_text("🚪 Вышел")

# ---------- ОБРАБОТЧИК ТЕКСТА ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    state = get_user_state(user_id)
    action = state["pending_action"]
    text = update.message.text.strip()

    if text.lower() == "/cancel":
        if action:
            state["pending_action"] = None
            await update.message.reply_text("❌ Отменено.")
        else:
            await update.message.reply_text("Нет активного действия.")
        return

    if action == "await_phone":
        state["phone"] = text
        state["phone_code_hash"] = None
        state["pending_action"] = None  # временно сбрасываем
        try:
            client = await get_client(user_id)
            result = await client.send_code_request(text)
            state["phone_code_hash"] = result.phone_code_hash
            state["pending_action"] = "await_code"
            await update.message.reply_text("📩 Код отправлен!\nВведи код через точки: 1.8.3.8.3")
        except FloodWaitError as fw:
            await update.message.reply_text(
                f"⏳ Telegram просит подождать {fw.seconds} секунд.\n"
                f"Попробуй позже или используй другой номер."
            )
            state["pending_action"] = "await_phone"
            state["phone"] = None
            state["phone_code_hash"] = None
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            state["pending_action"] = None
        return

    if action == "await_code":
        code = text.replace(" ", "").replace(".", "").replace("-", "")
        try:
            client = await get_client(user_id)
            await client.sign_in(phone=state["phone"], code=code, phone_code_hash=state["phone_code_hash"])
            state["is_authorized"] = True
            state["pending_action"] = None
            await update.message.reply_text("✅ Успешный вход!", reply_markup=main_menu_keyboard())
        except SessionPasswordNeededError:
            state["pending_action"] = "await_password"
            await update.message.reply_text("🔒 Введи облачный пароль:")
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            await update.message.reply_text("❌ Неверный или просроченный код")
            state["pending_action"] = None
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            state["pending_action"] = None
        return

    if action == "await_password":
        try:
            client = await get_client(user_id)
            await client.sign_in(password=text)
            state["is_authorized"] = True
            state["pending_action"] = None
            await update.message.reply_text("✅ Успешный вход!", reply_markup=main_menu_keyboard())
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            state["pending_action"] = None
        return

    if action == "await_draft":
        state["draft_text"] = text
        state["pending_action"] = None
        await update.message.reply_text(f"✅ Новый текст:\n\n{text}")
        return

    if action == "await_channel_add":
        channel = text.replace("@", "").strip()
        if channel and channel not in state["tracked_channels"]:
            state["tracked_channels"].append(channel)
            all_channels = load_channels()
            all_channels[str(user_id)] = state["tracked_channels"]
            save_channels(all_channels)
            await update.message.reply_text(f"✅ @{channel} добавлен!")
        else:
            await update.message.reply_text("⚠️ Канал уже в списке или пустой!")
        state["pending_action"] = None
        return

    if action == "await_channel_remove":
        channel = text.replace("@", "").strip()
        if channel in state["tracked_channels"]:
            state["tracked_channels"].remove(channel)
            all_channels = load_channels()
            all_channels[str(user_id)] = state["tracked_channels"]
            save_channels(all_channels)
            await update.message.reply_text(f"✅ @{channel} удалён!")
        else:
            await update.message.reply_text("⚠️ Канал не найден в списке!")
        state["pending_action"] = None
        return

# ---------- ВЕБ-СЕРВЕР ДЛЯ HEALTH CHECK ----------
async def health(request):
    return web.Response(text="OK")

async def run_web():
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    log(f"🌐 Веб-сервер на порту {PORT}", "🌐")
    await asyncio.Event().wait()

# ---------- KEEP-ALIVE ----------
async def keep_alive():
    while True:
        await asyncio.sleep(60)
        for uid, st in user_states.items():
            if st["client"] and st["client"].is_connected():
                try:
                    await st["client"].get_me()
                except:
                    pass

# ---------- ЗАПУСК ----------
async def main():
    print("\n  ⚡ БОТ-КОММЕНТАТОР\n")
    # Запускаем веб-сервер в фоне
    asyncio.create_task(run_web())

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    log("✅ Бот готов!", "✅")
    asyncio.create_task(keep_alive())
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  👋 Бот остановлен")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        # Перезапуск при падении
        os.execv(sys.executable, ['python'] + sys.argv)