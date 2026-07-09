import os
import asyncio
import json
import re
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

# ---------- ЖЁСТКИЙ ХАРДКОД ----------
API_ID = 22376342
API_HASH = "f623dc4ae2b015463cfde7874ab0f270"
BOT_TOKEN = "8807622473:AAHXPohZMOBpJm-75SQ-TaQ_oVazVOJ4wyY"

# ---------- Константы ----------
SESSION_FILE = "user_session"

# ---------- Глобальное состояние ----------
state = {
    "client": None,
    "phone": None,
    "phone_code_hash": None,
    "pending_action": None,
    "target_chat": None,
    "target_message_id": None,
    "number_start": None,
    "number_end": None,
    "number_interval": None,
    "current_number": None,
    "is_running": False,
    "running_task": None,
    "processing_callback": False,
    "owner_chat_id": None,
}

bot_app = None
_handler_registered = False

# ---------- Вспомогательные функции ----------
def log(msg: str, emoji: str = "•"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"  [{ts}] {emoji} {msg}")

def parse_post_link(link: str):
    link = link.split('?')[0]
    match = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if match:
        chat_id = -1000000000000 - int(match.group(1))
        msg_id = int(match.group(2))
        return chat_id, msg_id
    match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if match:
        username = match.group(1)
        msg_id = int(match.group(2))
        return username, msg_id
    return None, None

# ---------- Клавиатура ----------
def main_menu_keyboard():
    rows = [
        [
            InlineKeyboardButton("🔐 Войти", callback_data="login", style="primary"),
            InlineKeyboardButton("🎯 Запустить", callback_data="start_numbers", style="success")
        ],
        [
            InlineKeyboardButton("⏹ Остановить", callback_data="stop_numbers", style="danger"),
            InlineKeyboardButton("📊 Статус", callback_data="status", style="primary")
        ],
        [
            InlineKeyboardButton("🚪 Выйти", callback_data="logout", style="danger")
        ],
    ]
    return InlineKeyboardMarkup(rows)

# ---------- ДЛЯ ВСЕХ ----------
def authorized(update: Update) -> bool:
    return True

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
        _handler_registered = True
        log("Клиент Telethon готов", "✅")
    return new_client

# ---------- Отправка чисел ----------
async def send_numbers_task(chat, message_id, start, end, interval, owner_chat_id=None):
    try:
        current = start
        while current <= end and state["is_running"]:
            if not state["is_running"]:
                break
            try:
                client = await get_connected_client()
                await client.send_message(chat, str(current), comment_to=message_id)
                log(f"Отправлено: {current}", "🔢")
                state["current_number"] = current
                if current % 10 == 0 or current == start or current == end:
                    if owner_chat_id and bot_app:
                        try:
                            percent = int((current - start) / (end - start) * 100)
                            await bot_app.bot.send_message(
                                owner_chat_id,
                                f"📊 Прогресс: {current}/{end} ({percent}%)"
                            )
                        except:
                            pass
                current += 1
                if current <= end and state["is_running"]:
                    await asyncio.sleep(interval)
            except FloodWaitError as e:
                log(f"FloodWait: {e.seconds} сек", "⏳")
                if owner_chat_id and bot_app:
                    try:
                        await bot_app.bot.send_message(
                            owner_chat_id,
                            f"⏳ Telegram просит подождать {e.seconds} сек"
                        )
                    except:
                        pass
                await asyncio.sleep(e.seconds)
                continue
            except Exception as e:
                log(f"Ошибка: {e}", "❌")
                if state["client"]:
                    try:
                        await state["client"].disconnect()
                    except:
                        pass
                    state["client"] = None
                await asyncio.sleep(5)
                continue
        if current > end:
            log(f"✅ Готово! {end - start + 1} чисел", "🎉")
            if owner_chat_id and bot_app:
                await bot_app.bot.send_message(
                    owner_chat_id,
                    f"✅ Готово! Отправлено {end - start + 1} чисел от {start} до {end}"
                )
        else:
            log("⏹ Остановлено", "⏹")
            if owner_chat_id and bot_app:
                await bot_app.bot.send_message(
                    owner_chat_id,
                    f"⏹ Остановлено на числе {current}"
                )
        state["is_running"] = False
        state["running_task"] = None
    except Exception as e:
        log(f"Критическая ошибка: {e}", "💀")
        state["is_running"] = False
        state["running_task"] = None
        if owner_chat_id and bot_app:
            await bot_app.bot.send_message(
                owner_chat_id,
                f"❌ Ошибка: {e}"
            )

# ---------- /start ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if state["owner_chat_id"] is None:
        state["owner_chat_id"] = update.effective_chat.id
        log(f"Первый пользователь: {state['owner_chat_id']}", "👤")

    client = state["client"]
    is_auth = False
    if client:
        try:
            is_auth = await client.is_user_authorized()
        except:
            is_auth = False

    status_text = "🟢 Работает" if state["is_running"] else "🔴 Остановлен"
    progress = ""
    if state["is_running"] and state["current_number"] is not None and state["number_end"]:
        progress = f"\n📊 Прогресс: {state['current_number']}/{state['number_end']}"

    await update.effective_chat.send_message(
        f"⚡ БОТ-КОММЕНТАТОР ЧИСЕЛ\n\n"
        f"🔐 {'✅ Вход выполнен' if is_auth else '❌ Не авторизован'}\n"
        f"📡 {status_text}{progress}\n"
        f"📌 {'👉 Сначала войди!' if not is_auth else 'Готов к работе!'}",
        reply_markup=main_menu_keyboard()
    )

# ---------- /cancel ----------
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if state["pending_action"]:
        state["pending_action"] = None
        await update.message.reply_text("❌ Отменено.")
    else:
        await update.message.reply_text("Нет активного действия.")

# ---------- ДИАГНОСТИКА: ответ на любое сообщение ----------
async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"✅ Бот работает! Ты написал: {update.message.text}")

# ---------- Обработчик callback ----------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_app
    
    if state["processing_callback"]:
        await update.callback_query.answer("⏳ Обрабатываю...")
        return
    
    query = update.callback_query
    await query.answer()
    data = query.data

    state["processing_callback"] = True

    try:
        if data == "login":
            client = state["client"]
            if client and await client.is_user_authorized():
                await query.message.reply_text("✅ Вы уже авторизованы!")
                state["processing_callback"] = False
                return
            state["pending_action"] = "await_phone"
            await query.message.reply_text("📱 Отправь номер:\n+79001234567")

        elif data == "start_numbers":
            client = state["client"]
            if client is None or not await client.is_user_authorized():
                await query.message.reply_text("❌ Сначала войди!")
                state["processing_callback"] = False
                return
            if state["is_running"]:
                await query.message.reply_text("⚠️ Процесс уже запущен! Останови.")
                state["processing_callback"] = False
                return
            state["pending_action"] = "await_post_link"
            await query.message.reply_text(
                "📎 Отправь ссылку на пост:\n\n"
                "Пример: https://t.me/c/1234567890/123\n"
                "Или: https://t.me/username/123"
            )

        elif data == "stop_numbers":
            if state["is_running"]:
                state["is_running"] = False
                await query.message.reply_text("⏹ Останавливаю...")
            else:
                await query.message.reply_text("ℹ️ Процесс не запущен")

        elif data == "status":
            if state["is_running"]:
                await query.message.reply_text(
                    f"📊 Идёт отправка\n"
                    f"📌 {state['current_number']}/{state['number_end']}\n"
                    f"⏱ Интервал: {state['number_interval']} сек"
                )
            else:
                await query.message.reply_text("ℹ️ Процесс остановлен")

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
            state["is_running"] = False
            state["running_task"] = None
            await query.message.reply_text("🚪 Вышел!")

    except Exception as e:
        log(f"Ошибка: {e}", "❌")
        await query.message.reply_text(f"❌ Ошибка: {e}")
    finally:
        state["processing_callback"] = False

# ---------- Обработчик текста ----------
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
        client = state["client"]
        if client and await client.is_user_authorized():
            await update.message.reply_text("✅ Вы уже авторизованы!")
            state["pending_action"] = None
            return
        state["phone"] = text
        try:
            client = await get_connected_client()
            result = await client.send_code_request(text)
            state["phone_code_hash"] = result.phone_code_hash
            state["pending_action"] = "await_code"
            await update.message.reply_text("📩 Код отправлен!\nВведи код (например: 1.2.3.4.5)")
        except FloodWaitError as fw:
            await update.message.reply_text(f"⏳ Подожди {fw.seconds} сек")
            state["pending_action"] = None
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            state["pending_action"] = None
        return

    if action == "await_code":
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
            await update.message.reply_text("❌ Неверный код")
            state["pending_action"] = None
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            state["pending_action"] = None
        return

    if action == "await_password":
        try:
            client = await get_connected_client()
            await client.sign_in(password=text)
            state["pending_action"] = None
            await update.message.reply_text("✅ Успешный вход!", reply_markup=main_menu_keyboard())
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            state["pending_action"] = None
        return

    if action == "await_post_link":
        chat_identifier, msg_id = parse_post_link(text)
        if not chat_identifier or not msg_id:
            await update.message.reply_text("❌ Неверная ссылка!\nПример: https://t.me/c/1234567890/123")
            return
        state["target_message_id"] = msg_id
        state["target_chat_identifier"] = chat_identifier
        state["pending_action"] = "await_start_number"
        await update.message.reply_text("🔢 Отправь число ОТ (например: 1)")
        return

    if action == "await_start_number":
        try:
            start = int(text)
            state["number_start"] = start
            state["pending_action"] = "await_end_number"
            await update.message.reply_text(f"✅ От {start}\n\n🔢 Отправь число ДО")
        except ValueError:
            await update.message.reply_text("❌ Отправь целое число!")
        return

    if action == "await_end_number":
        try:
            end = int(text)
            if end < state["number_start"]:
                await update.message.reply_text("❌ ДО должно быть больше ОТ!")
                return
            state["number_end"] = end
            state["pending_action"] = "await_interval"
            await update.message.reply_text(
                f"✅ От {state['number_start']} до {end}\n\n"
                "⏱ Отправь интервал в секундах (например: 5)"
            )
        except ValueError:
            await update.message.reply_text("❌ Отправь целое число!")
        return

    if action == "await_interval":
        try:
            interval = float(text)
            if interval < 0.5:
                await update.message.reply_text("❌ Минимум 0.5 сек")
                return
            state["number_interval"] = interval
            state["pending_action"] = None
            
            client = await get_connected_client()
            try:
                if isinstance(state["target_chat_identifier"], str):
                    chat = await client.get_entity(state["target_chat_identifier"])
                else:
                    chat = await client.get_entity(state["target_chat_identifier"])
            except Exception as e:
                await update.message.reply_text(f"❌ Не могу найти канал: {e}")
                return
            
            state["target_chat"] = chat
            state["is_running"] = True
            state["current_number"] = state["number_start"] - 1
            
            task = asyncio.create_task(send_numbers_task(
                chat,
                state["target_message_id"],
                state["number_start"],
                state["number_end"],
                state["number_interval"],
                update.effective_chat.id
            ))
            state["running_task"] = task
            
            await update.message.reply_text(
                f"🚀 ЗАПУЩЕНО!\n\n"
                f"📌 Пост: {state['target_chat_identifier']}/{state['target_message_id']}\n"
                f"🔢 {state['number_start']} → {state['number_end']}\n"
                f"⏱ {state['number_interval']} сек\n\n"
                f"Для остановки нажми ⏹ Остановить"
            )
            
        except ValueError:
            await update.message.reply_text("❌ Отправь число!")
        return

# ---------- Веб-сервер ----------
async def health_check(request):
    return web.Response(text="OK")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
    await site.start()
    log(f"Веб-сервер на порту {os.getenv('PORT', 8080)}", "🌐")
    await asyncio.Event().wait()

# ---------- Запуск ----------
async def main():
    global bot_app
    print("\n  🎲 БОТ-КОММЕНТАТОР ЧИСЕЛ (общий для всех)\n")
    
    asyncio.create_task(run_web_server())
    
    app = Application.builder().token(BOT_TOKEN).build()
    bot_app = app

    # ДИАГНОСТИКА ПЕРВЫМ — ответит на любое сообщение
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    log("Бот готов для всех пользователей!", "✅")
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