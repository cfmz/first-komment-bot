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
    AuthKeyError,
    UnauthorizedError,
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

# ---------- Чтение секретов из переменных окружения ----------
API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise ValueError("Не заданы обязательные переменные окружения: API_ID, API_HASH, BOT_TOKEN")

# ---------- Остальные константы ----------
SESSION_FILE = "user_session"
CHANNELS_FILE = "channels.json"

# ---------- Глобальное состояние ----------
state = {
    "client": None,
    "phone": None,
    "phone_code_hash": None,
    "monitoring": False,
    "monitor_start_time": None,
    "notified_keys": set(),
    "draft_text": "🥇 Первый!",
    "pending_action": None,
    "tracked_channels": [],
}

bot_app = None
_handler_registered = False

# ---------- Вспомогательные функции ----------
def log(msg: str, emoji: str = "•"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"  [{ts}] {emoji} {msg}")

def fmt_draft(text: str) -> str:
    now = datetime.now()
    return text.replace("{time}", now.strftime("%H:%M")).replace("{date}", now.strftime("%d.%m.%Y"))

def load_channels():
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "r") as f:
            return json.load(f)
    return []

def save_channels(channels):
    with open(CHANNELS_FILE, "w") as f:
        json.dump(channels, f)

# ---------- Клавиатуры с цветными кнопками ----------
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

# ---------- ВСЕ МОГУТ ПОЛЬЗОВАТЬСЯ ----------
def authorized(update: Update) -> bool:
    return True  # <--- ВСЕ РАЗРЕШЕНЫ

def make_post_link(channel_id: int, message_id: int) -> str:
    chat_id_str = str(channel_id)
    if chat_id_str.startswith("-100"):
        chat_id_clean = chat_id_str[4:]
        return f"https://t.me/c/{chat_id_clean}/{message_id}"
    return f"https://t.me/c/{channel_id}/{message_id}"

# ---------- Работа с клиентом ----------
async def get_connected_client() -> TelegramClient:
    global _handler_registered
    client = state["client"]

    if client is not None:
        try:
            if not client.is_connected():
                await client.connect()
            return client
        except Exception:
            try:
                await client.disconnect()
            except:
                pass
            if os.path.exists(f"{SESSION_FILE}.session"):
                os.remove(f"{SESSION_FILE}.session")
            client = None
            state["client"] = None

    new_client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await new_client.connect()
    state["client"] = new_client

    if not _handler_registered:
        @new_client.on(events.NewMessage)
        async def handler(event):
            await handle_new_message(event)
        _handler_registered = True
        log("Обработчик Telethon зарегистрирован", "✅")

    return new_client

# ---------- Обработчик новых сообщений ----------
async def handle_new_message(event):
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

    asyncio.create_task(_send_with_retry(chat, event.message.id, comment_text, title, key))

async def _send_with_retry(chat, message_id, text, title, key):
    success = await send_comment(chat, message_id, text)

    if success:
        log(f"{title}: {text}", "💬")
    else:
        state["notified_keys"].discard(key)

async def send_comment(chat, message_id: int, text: str) -> bool:
    for attempt in range(3):
        try:
            client = await get_connected_client()
            await client.send_message(chat, text, comment_to=message_id)
            return True
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
            continue
        except Exception as e:
            log(f"Ошибка (попытка {attempt+1}): {e}", "❌")
            if state["client"]:
                try:
                    await state["client"].disconnect()
                except:
                    pass
            state["client"] = None
    return False

# ---------- Команды бота (для ВСЕХ) ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = state["client"]
    is_auth = False
    if client:
        try:
            is_auth = await client.is_user_authorized()
        except:
            is_auth = False

    logged = "✅ Да" if is_auth else "❌ Нет"
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
    if state["pending_action"]:
        state["pending_action"] = None
        await update.message.reply_text("❌ Отменено.")
    else:
        await update.message.reply_text("Нет активного действия.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "login":
        state["pending_action"] = "await_phone"
        await query.message.reply_text("📱 Отправь номер:\n+79001234567")

    elif data == "start_monitoring":
        client = state["client"]
        if client is None or not await client.is_user_authorized():
            await query.message.reply_text("❌ Сначала войди!")
            return
        state["monitoring"] = True
        state["monitor_start_time"] = datetime.now(timezone.utc)
        state["notified_keys"].clear()
        await query.message.reply_text(
            f"▶️ ЗАПУЩЕНО!\n\n"
            f"💬 {state['draft_text']}\n"
            f"⚡ Мгновенно"
        )

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
        save_channels([])
        await query.message.reply_text("🌍 Все каналы!")

    elif data == "back_to_menu":
        await cmd_start(update, context)

    elif data == "logout":
        client = state["client"]
        if client:
            try:
                await client.log_out()
            except:
                pass
            try:
                await client.disconnect()
            except:
                pass
        if os.path.exists(f"{SESSION_FILE}.session"):
            os.remove(f"{SESSION_FILE}.session")
        state["client"] = None
        state["monitoring"] = False
        state["notified_keys"].clear()
        global _handler_registered
        _handler_registered = False
        await query.message.reply_text("🚪 Вышел!")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        try:
            client = await get_connected_client()
            result = await client.send_code_request(text)
            state["phone_code_hash"] = result.phone_code_hash
            state["pending_action"] = "await_code"
            await update.message.reply_text("📩 Код отправлен!\n\n⚠️ Введи код через точки:\n1.8.3.8.3")
        except FloodWaitError as fw:
            await update.message.reply_text(f"⏳ Подожди {fw.seconds} секунд")
            state["pending_action"] = None
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            state["pending_action"] = None

    elif action == "await_code":
        code = text.replace(" ", "").replace(".", "").replace("-", "")
        try:
            client = await get_connected_client()
            await client.sign_in(phone=state["phone"], code=code, phone_code_hash=state["phone_code_hash"])
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

    elif action == "await_password":
        try:
            client = await get_connected_client()
            await client.sign_in(password=text)
            state["pending_action"] = None
            await update.message.reply_text("✅ Успешный вход!", reply_markup=main_menu_keyboard())
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            state["pending_action"] = None

    elif action == "await_draft":
        state["draft_text"] = text
        state["pending_action"] = None
        await update.message.reply_text(f"✅ Новый текст:\n\n{text}")

    elif action == "await_channel_add":
        channel = text.replace("@", "").strip()
        if channel and channel not in state["tracked_channels"]:
            state["tracked_channels"].append(channel)
            save_channels(state["tracked_channels"])
            await update.message.reply_text(f"✅ @{channel} добавлен!")
        else:
            await update.message.reply_text("⚠️ Канал уже в списке или пустой!")
        state["pending_action"] = None

    elif action == "await_channel_remove":
        channel = text.replace("@", "").strip()
        if channel in state["tracked_channels"]:
            state["tracked_channels"].remove(channel)
            save_channels(state["tracked_channels"])
            await update.message.reply_text(f"✅ @{channel} удалён!")
        else:
            await update.message.reply_text("⚠️ Канал не найден в списке!")
        state["pending_action"] = None

# ---------- Keep-alive ----------
async def keep_alive():
    while True:
        await asyncio.sleep(60)
        if state["client"]:
            try:
                client = state["client"]
                if not client.is_connected():
                    await client.connect()
                await client.get_me()
            except Exception:
                if state["client"]:
                    try:
                        await state["client"].disconnect()
                    except:
                        pass
                    state["client"] = None

# ---------- Веб-сервер для Render ----------
async def health_check(request):
    return web.Response(text="OK")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
    await site.start()
    log(f"Веб-сервер запущен на порту {os.getenv('PORT', 8080)}", "🌐")
    await asyncio.Event().wait()

# ---------- Запуск ----------
async def main():
    global bot_app
    state["tracked_channels"] = load_channels()
    print("\n  ⚡ БОТ-КОММЕНТАТОР (ДЛЯ ВСЕХ)\n")
    
    asyncio.create_task(run_web_server())
    
    app = Application.builder().token(BOT_TOKEN).build()
    bot_app = app

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    log("Бот готов для ВСЕХ!", "✅")
    asyncio.create_task(keep_alive())
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\n  👋 Бот остановлен!\n")
    finally:
        loop.close()