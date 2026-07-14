import asyncio
import json
import logging
import os
import traceback
from datetime import datetime, timezone

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

# ══════════════════════════════════════════════
# 🔐 ДАННЫЕ
# ══════════════════════════════════════════════
API_ID = 22376342
API_HASH = "f623dc4ae2b015463cfde7874ab0f270"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8997883874:AAG1H8eF-b2y47kXV82uxwtlzQ_zrSxgzE8")
SESSION_FILE = "user_session"
CHANNELS_FILE = "channels.json"

# ══════════════════════════════════════════════
# 📝 ЛОГИРОВАНИЕ
# ══════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════
# 📦 СОСТОЯНИЕ
# ══════════════════════════════════════════════
state = {
    "owner_chat_id": None,
    "client": None,
    "phone": None,
    "phone_code_hash": None,
    "monitoring": False,
    "monitor_start_time": None,
    "notified_keys": set(),
    "draft_text": "🥇 Первый!",
    "pending_action": None,
    "tracked_channels": [],
    "first_comment_sent": False,
}

bot_app = None


# ══════════════════════════════════════════════
# 🛠️ ИНСТРУМЕНТЫ
# ══════════════════════════════════════════════

def fmt_draft(text: str) -> str:
    now = datetime.now()
    return text.replace("{time}", now.strftime("%H:%M")).replace(
        "{date}", now.strftime("%d.%m.%Y")
    )


def load_channels():
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "r") as f:
            return json.load(f)
    return []


def save_channels(channels):
    with open(CHANNELS_FILE, "w") as f:
        json.dump(channels, f)


def main_menu_keyboard():
    rows = [
        [InlineKeyboardButton("🔐 Войти", callback_data="login"),
         InlineKeyboardButton("▶️ Запустить", callback_data="start_monitoring")],
        [InlineKeyboardButton("⏹ Остановить", callback_data="stop_monitoring"),
         InlineKeyboardButton("✏️ Текст", callback_data="set_draft")],
        [InlineKeyboardButton("📋 Каналы", callback_data="manage_channels"),
         InlineKeyboardButton("🚪 Выйти", callback_data="logout")],
    ]
    return InlineKeyboardMarkup(rows)


def authorized(update: Update) -> bool:
    if state["owner_chat_id"] is None:
        return True
    return update.effective_chat.id == state["owner_chat_id"]


def make_post_link(channel_id: int, message_id: int) -> str:
    chat_id_str = str(channel_id)
    if chat_id_str.startswith("-100"):
        chat_id_clean = chat_id_str[4:]
        return f"https://t.me/c/{chat_id_clean}/{message_id}"
    return f"https://t.me/c/{channel_id}/{message_id}"


# ══════════════════════════════════════════════
# 🔌 TELETHON КЛИЕНТ
# ══════════════════════════════════════════════

async def get_connected_client() -> TelegramClient:
    client = state["client"]

    if client is not None:
        try:
            if client.is_connected():
                return client
        except Exception:
            logger.warning("Клиент не отвечает, пробую переподключить...")

    if client is not None:
        try:
            await client.connect()
            if client.is_connected():
                logger.info("🔄 Клиент переподключен")
                return client
        except Exception as e:
            if "AuthRestartError" in str(e) or "restart" in str(e).lower():
                logger.warning("Сессия сломана, удаляю файл...")
                try:
                    await client.disconnect()
                except Exception:
                    pass
                if os.path.exists(f"{SESSION_FILE}.session"):
                    os.remove(f"{SESSION_FILE}.session")
                client = None
                state["client"] = None
            else:
                logger.error(f"Ошибка переподключения: {e}")

    logger.info("Создаю новый Telethon-клиент")
    new_client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await new_client.connect()
    state["client"] = new_client
    if state["monitoring"]:
        register_handler(new_client)
    return new_client


def register_handler(client: TelegramClient):
    @client.on(events.NewMessage)
    async def _handler(event):
        await handle_new_message(event)


# ══════════════════════════════════════════════
# 📡 ОБРАБОТКА НОВЫХ ПОСТОВ
# ══════════════════════════════════════════════

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

    logger.info(f"📩 Новый пост в канале: {title}")

    asyncio.create_task(_send_with_retry(chat.id, event.message.id, comment_text, title, key))


async def _send_with_retry(channel_id, message_id, text, title, key):
    logger.info(f"Попытка отправки комментария в канал: {title}")
    success = await send_comment(channel_id, message_id, text)

    if success:
        logger.info(f"✅ Комментарий опубликован: {title} — {text}")
        print(f">>> {title}: {text}")

        # ── Уведомление владельцу ──
        if state["owner_chat_id"] and bot_app:
            try:
                link = make_post_link(channel_id, message_id)
                await bot_app.bot.send_message(
                    state["owner_chat_id"],
                    f"💬 Комментарий\n📢 {title}\n📝 {text}\n🔗 {link}"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление: {e}")

        # ── Первый комментарий за запуск ──
        if not state["first_comment_sent"]:
            state["first_comment_sent"] = True
            if state["owner_chat_id"] and bot_app:
                try:
                    link = make_post_link(channel_id, message_id)
                    now = datetime.now().strftime("%H:%M:%S")
                    await bot_app.bot.send_message(
                        state["owner_chat_id"],
                        f"🎉 Первый комментарий опубликован!\n\n"
                        f"📢 Канал: {title}\n"
                        f"📝 Пост: {link}\n"
                        f"💬 Комментарий:\n{text}\n"
                        f"🕒 Время: {now}"
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление о первом комментарии: {e}")
    else:
        logger.error(f"❌ Не удалось отправить комментарий в канал: {title}")
        state["notified_keys"].discard(key)


async def send_comment(channel_id: int, message_id: int, text: str) -> bool:
    for attempt in range(1, 4):
        try:
            client = await get_connected_client()
            entity = await client.get_entity(channel_id)

            try:
                await client.send_message(entity, text, comment_to=message_id)
                return True
            except Exception:
                pass

            try:
                await client.send_message(entity, text, reply_to=message_id)
                return True
            except Exception:
                pass

            try:
                await client.send_message(entity, text)
                return True
            except Exception:
                pass

        except FloodWaitError as fw:
            logger.warning(f"⏳ FloodWait: {fw.seconds}с (попытка {attempt})")
        except Exception as e:
            logger.error(f"Ошибка отправки (попытка {attempt}): {e}")
            state["client"] = None

    return False


# ══════════════════════════════════════════════
# 🤖 КОМАНДЫ БОТА
# ══════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if state["owner_chat_id"] is None:
        state["owner_chat_id"] = update.effective_chat.id
        logger.info(f"Владелец бота: {state['owner_chat_id']}")

    if not authorized(update):
        return

    logged = "✅ Да" if (state["client"] and await state["client"].is_user_authorized()) else "❌ Нет"
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


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not authorized(update):
        await query.answer("Не авторизован")
        return

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
        state["first_comment_sent"] = False
        register_handler(await get_connected_client())
        logger.info("▶️ Мониторинг запущен")
        await query.message.reply_text(f"▶️ ЗАПУЩЕНО!\n\n💬 {state['draft_text']}\n⚡ Мгновенно")

    elif data == "stop_monitoring":
        state["monitoring"] = False
        logger.info("⏹ Мониторинг остановлен")
        await query.message.reply_text("⏹ Остановлено!")

    elif data == "set_draft":
        state["pending_action"] = "await_draft"
        await query.message.reply_text("✏️ Отправь текст:\nМожно {time} и {date}")

    elif data == "manage_channels":
        keyboard = [
            [InlineKeyboardButton("➕ Добавить", callback_data="add_channel"),
             InlineKeyboardButton("❌ Удалить", callback_data="remove_channel")],
            [InlineKeyboardButton("🌍 Все каналы", callback_data="all_channels")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")],
        ]
        await query.message.reply_text("📋 Управление каналами", reply_markup=InlineKeyboardMarkup(keyboard))

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
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                pass
        if os.path.exists(f"{SESSION_FILE}.session"):
            os.remove(f"{SESSION_FILE}.session")
        state["client"] = None
        state["monitoring"] = False
        state["notified_keys"].clear()
        logger.info("🚪 Пользователь вышел из аккаунта")
        await query.message.reply_text("🚪 Вышел!")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    action = state["pending_action"]
    text = update.message.text.strip()

    if action == "await_phone":
        state["phone"] = text
        try:
            client = await get_connected_client()
            result = await client.send_code_request(text)
            state["phone_code_hash"] = result.phone_code_hash
            state["pending_action"] = "await_code"
            logger.info(f"Код отправлен на номер {text[:5]}...")
            await update.message.reply_text("📩 Код отправлен!\n\n⚠️ Через точки:\n1.8.3.8.3")
        except FloodWaitError as fw:
            logger.warning(f"FloodWait при отправке кода: {fw.seconds}с")
            await update.message.reply_text(f"⏳ Подожди {fw.seconds} секунд")
        except Exception as e:
            logger.error(f"Ошибка отправки кода: {traceback.format_exc()}")
            await update.message.reply_text(f"❌ Ошибка: {e}")
            state["pending_action"] = None

    elif action == "await_code":
        code = text.replace(" ", "").replace(".", "").replace("-", "")
        try:
            client = await get_connected_client()
            await client.sign_in(phone=state["phone"], code=code, phone_code_hash=state["phone_code_hash"])
            state["pending_action"] = None
            logger.info("Пользователь успешно авторизовался")
            await update.message.reply_text("✅ Успешный вход!", reply_markup=main_menu_keyboard())
        except SessionPasswordNeededError:
            state["pending_action"] = "await_password"
            await update.message.reply_text("🔒 Введи облачный пароль:")
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            logger.warning("Неверный или просроченный код")
            await update.message.reply_text("❌ Неверный или просроченный код")
        except Exception as e:
            logger.error(f"Ошибка входа: {traceback.format_exc()}")
            await update.message.reply_text(f"❌ Ошибка: {e}")

    elif action == "await_password":
        try:
            client = await get_connected_client()
            await client.sign_in(password=text)
            state["pending_action"] = None
            logger.info("Пользователь вошёл с 2FA")
            await update.message.reply_text("✅ Успешный вход!", reply_markup=main_menu_keyboard())
        except Exception as e:
            logger.error(f"Ошибка 2FA: {traceback.format_exc()}")
            await update.message.reply_text(f"❌ Ошибка: {e}")

    elif action == "await_draft":
        state["draft_text"] = text
        state["pending_action"] = None
        logger.info(f"Текст комментария изменён: {text}")
        await update.message.reply_text(f"✅ Новый текст:\n\n{text}")

    elif action == "await_channel_add":
        channel = text.replace("@", "").strip()
        if channel and channel not in state["tracked_channels"]:
            state["tracked_channels"].append(channel)
            save_channels(state["tracked_channels"])
            state["pending_action"] = None
            logger.info(f"Канал добавлен: @{channel}")
            await update.message.reply_text(f"✅ @{channel} добавлен!")

    elif action == "await_channel_remove":
        channel = text.replace("@", "").strip()
        if channel in state["tracked_channels"]:
            state["tracked_channels"].remove(channel)
            save_channels(state["tracked_channels"])
            state["pending_action"] = None
            logger.info(f"Канал удалён: @{channel}")
            await update.message.reply_text(f"✅ @{channel} удалён!")


# ══════════════════════════════════════════════
# 🔄 KEEP-ALIVE
# ══════════════════════════════════════════════

async def keep_alive():
    while True:
        await asyncio.sleep(60)
        if state["client"] and state["monitoring"]:
            try:
                client = await get_connected_client()
                await client.get_me()
            except Exception as e:
                logger.warning(f"Keep-alive ошибка: {e}")


# ══════════════════════════════════════════════
# 🚀 ЗАПУСК
# ══════════════════════════════════════════════

async def main():
    global bot_app

    state["tracked_channels"] = load_channels()

    logger.info("=" * 50)
    logger.info("БОТ-КОММЕНТАТОР ЗАПУСКАЕТСЯ")
    logger.info("=" * 50)

    app = Application.builder().token(BOT_TOKEN).build()
    bot_app = app

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logger.info("✅ Бот готов к работе!")

    asyncio.create_task(keep_alive())

    await asyncio.Event().wait()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен!")
    finally:
        loop.close()