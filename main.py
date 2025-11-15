import os
import json
import asyncio
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, PreCheckoutQueryHandler, CallbackQueryHandler
from new_parser import parse_wb_product_api
import aiohttp
from telegram import LabeledPrice
from datetime import datetime, timedelta, timezone
import pytz
import calendar
import base64
import json as _json
import time
import uuid
import logging

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
WEB_APP_URL = "https://wb-seller.vercel.app/"
# WEB_APP_URL = "https://wb-miniapp-demo.loca.lt"
# BACKEND_URL = "http://localhost:8000"
BACKEND_URL = "https://api.hikinamuri.ru"
SUPPORT_USERNAME = "@ekzoskidki7"
# CHANNEL_ID = '@wbsellers_test'
CHANNEL_ID = '@ekzoskidki'
PENDING_MESSAGES = {}
SENT_INVOICES = {}   

# 🔐 Список Telegram ID администраторов
ADMIN_IDS = {933791537, 455197004, 810503099, 535437088}  # замени на свои tg_id

# Кэш для хранения результатов парсинга
parsing_cache = {}

# --- Конфиг для YooKassa (из env) ---
YOOKASSA_ACCOUNT = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET = os.getenv("YOOKASSA_SECRET_KEY")

# Порог возраста YK-платежа (в секундах), старше которого мы пытаемся отменить чтобы избежать duplicate.
YK_AGE_CANCEL_THRESHOLD = int(os.getenv("YK_AGE_CANCEL_THRESHOLD", "60"))  # дефолт 60s

# ---------- Вспомогательные функции для YooKassa ----------
async def fetch_yk_payment(payment_id: str) -> dict | None:
    """Получить информацию о платеже в YooKassa по id. Возвращает JSON или None при ошибке."""
    if not (YOOKASSA_ACCOUNT and YOOKASSA_SECRET and payment_id):
        return None
    try:
        auth = aiohttp.BasicAuth(YOOKASSA_ACCOUNT, YOOKASSA_SECRET)
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.yookassa.ru/v3/payments/{payment_id}", auth=auth, timeout=10.0) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    text = await resp.text()
                    print(f"⚠️ YooKassa fetch returned {resp.status}: {text}")
    except Exception as e:
        print(f"❌ Ошибка fetch_yk_payment: {e}")
    return None

async def cancel_yk_payment(payment_id: str) -> tuple[int, str]:
    """Попытаться отменить платеж в YooKassa. Возвращает (status_code, response_text)."""
    if not (YOOKASSA_ACCOUNT and YOOKASSA_SECRET and payment_id):
        return (0, "missing_credentials_or_id")
    try:
        auth = aiohttp.BasicAuth(YOOKASSA_ACCOUNT, YOOKASSA_SECRET)
        async with aiohttp.ClientSession() as session:
            async with session.post(f"https://api.yookassa.ru/v3/payments/{payment_id}/cancel", auth=auth, timeout=10.0) as resp:
                text = await resp.text()
                return (resp.status, text)
    except Exception as e:
        print(f"❌ Ошибка cancel_yk_payment: {e}")
        return (0, str(e))

# ---------- Конец вспомогательных функций ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tg_id = user.id

    registered = await is_user_registered(tg_id)

    if registered:
        # ✅ Уже зарегистрирован — показываем WebApp с tg_id в URL
        keyboard = [
            [
                KeyboardButton(
                    text="📱 Оформить заказ",
                    web_app=WebAppInfo(url=f"{WEB_APP_URL}?tg_id={tg_id}")
                )
            ],
            [KeyboardButton("🛠 Тех. поддержка")]
        ]
        greeting = (
            f"Привет, {user.first_name}! 👋\n\n"
            "Добро пожаловать обратно! Вы можете открыть приложение для оформления заказа 👇"
        )
    else:
        # ❌ Не зарегистрирован — только кнопка для контакта
        keyboard = [
            [KeyboardButton(text="📞 Поделиться контактом", request_contact=True)],
            [KeyboardButton("🛠 Тех. поддержка")]
        ]
        greeting = (
            f"Привет, {user.first_name}! 👋\n\n"
            "Я бот канала @ekzoskidki и помогу тебе разместить рекламу быстро и без лишних шагов.\n\n"
            "🔹 Поделитесь контактом, чтобы зарегистрироваться\n\n"
            "🔹 Отправьте ссылку на товар \n\n"
            "🔹 Выберите категорию (для дома, детям, одежда и т.д.) \n\n"
            "🔹 Укажите время публикации \n\n"
            "Сейчас реклама размещается только в @ekzoskidki, но скоро появятся и другие каналы. \n\n"
            "Нажми «Оформить заказ», чтобы начать 🚀."
        )

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(greeting, parse_mode="HTML", reply_markup=reply_markup)

def generate_unique_payload(base_id):
    return f"{base_id}_{uuid.uuid4().hex[:8]}_{int(time.time())}"

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка shared контакта"""
    contact = update.message.contact
    user = update.effective_user

    print(f"📞 Получен контакт: {contact.phone_number} от пользователя {user.id}")

    # Отправляем данные на бэкенд для регистрации
    payload = {
        "tg_id": user.id,
        "name": user.first_name,
        "phone": contact.phone_number,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{BACKEND_URL}/api/users/register", json=payload) as resp:
                result = await resp.json()

        if result.get("success"):
            await update.message.reply_text(
                f"✅ Спасибо, {user.first_name}! Вы успешно зарегистрированы.\n\n"
                "Теперь можете открыть приложение 👇",
                reply_markup=await get_main_keyboard(user.id),
            )
        else:
            await update.message.reply_text(
                "❌ Ошибка при регистрации. Попробуйте позже."
            )
            print("Ошибка при регистрации:", result)

    except Exception as e:
        print(f"❌ Ошибка при обращении к бэкенду: {e}")
        await update.message.reply_text("⚠️ Не удалось сохранить контакт в БД.")

async def handle_product_parsing(update: Update, product_url: str):
    """Обработка парсинга товара через API Wildberries"""
    try:
        # Отправляем сообщение о начале парсинга
        parsing_msg = await update.message.reply_text("🔍 Парсим информацию о товаре через API...")
        
        # Используем API парсер
        product_data = await parse_wb_product_api(product_url)
        
        if product_data.get('success'):
            # Форматируем сообщение с реальными данными
            message = format_api_product_message(product_data)
            await parsing_msg.edit_text(message, parse_mode='HTML')
            
            # Сохраняем в кэш для использования в приложении
            cache_key = f"product_{update.effective_user.id}"
            parsing_cache[cache_key] = product_data
            
        else:
            await parsing_msg.edit_text(
                f"❌ Не удалось получить информацию о товаре\n\n"
                f"Ошибка: {product_data.get('error', 'Неизвестная ошибка')}\n"
                f"Проверьте ссылку и попробуйте снова."
            )
            
    except Exception as e:
        print(f"❌ Ошибка при парсинге: {e}")
        await update.message.reply_text(
            "❌ Произошла ошибка при получении информации о товаре"
        )

def format_api_product_message(product_data: dict) -> str:
    """Форматирование сообщения с реальными данными из API"""
    name = product_data.get('name', 'Неизвестно')
    price = product_data.get('price', 0)
    brand = product_data.get('brand', 'Неизвестно')
    rating = product_data.get('rating', 0)
    feedbacks = product_data.get('feedbacks', 0)
    supplier = product_data.get('supplier', 'Неизвестно')
    discount = product_data.get('discount', 0)
    basic_price = product_data.get('basic_price')
    
    message = (
        f"🛍️ <b>Информация о товаре</b>\n\n"
        f"<b>Название:</b> {name}\n"
        f"<b>Бренд:</b> {brand}\n"
        f"<b>Продавец:</b> {supplier}\n"
    )
    
    if discount > 0 and basic_price:
        message += f"<b>Цена:</b> <s>{basic_price} руб.</s> <b>{price} руб.</b> (-{discount}%)\n"
    else:
        message += f"<b>Цена:</b> {price} руб.\n"
    
    if rating > 0:
        message += f"<b>Рейтинг:</b> {rating} ⭐\n"
    
    if feedbacks > 0:
        message += f"<b>Отзывов:</b> {feedbacks}\n"
    
    description = product_data.get('description', '')
    if description and len(description) > 10:
        message += f"\n<b>Описание:</b>\n{description[:200]}..."
    
    # Добавляем характеристики
    characteristics = product_data.get('characteristics', {})
    if characteristics:
        message += f"\n\n<b>Характеристики:</b>"
        for key, value in list(characteristics.items())[:2]:
            message += f"\n• {key}: {value}"
    
    message += f"\n\n<b>Артикул:</b> {product_data.get('articul', 'N/A')}"
    
    return message


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        print("⚠️ Обновление без текстового сообщения — пропускаем")
        return

    text = update.message.text
    user_id = update.effective_user.id

    if text == "📱 Открыть приложение":
        print(f"🔗 Пользователь {user_id} пытается открыть Web App")

        # Проверяем регистрацию
        registered = await is_user_registered(user_id)
        if not registered:
            await update.message.reply_text(
                "⚠️ Сначала поделитесь контактом для регистрации!\n\n"
                "Нажмите кнопку <b>📞 Поделиться контактом</b> ниже 👇",
                parse_mode='HTML'
            )
            return  # ❌ Прерываем выполнение, не открываем WebApp

        # ✅ Пользователь зарегистрирован — разрешаем
        await update.message.reply_text(
            "✅ Отлично! Можете открыть приложение 👇",
            reply_markup=await get_main_keyboard(user_id)
        )

        return

    if text == "🛠 Тех. поддержка":
        await update.message.reply_text(
            f"📞 По всем вопросам обращайтесь: {SUPPORT_USERNAME} или на почту vitya.starikov.2001@mail.ru\n\n"
            "Мы поможем с:\n"
            "• Настройкой бота\n"
            "• Проблемами с выкладкой\n"
            "• Оплатой и возвратами\n"
            "• Техническими вопросами"
        )

    else:
        await update.message.reply_text(
            "Используйте кнопки для управления 👇",
            reply_markup = await get_main_keyboard(user_id)
        )

async def is_user_registered(tg_id: int) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BACKEND_URL}/api/users/{tg_id}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("exists", False)
    except Exception as e:
        print(f"⚠️ Ошибка проверки пользователя: {e}")
    return False

async def get_main_keyboard(user_id: int):
    web_app_button = KeyboardButton(
        text="📱 Открыть приложение",
        web_app=WebAppInfo(url=f"{WEB_APP_URL}?tg_id={user_id}")  # ✅ tg_id добавлен в URL
    )
    keyboard = [
        [web_app_button],
        [KeyboardButton("🛠 Тех. поддержка")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Функция для получения данных парсинга (для API)
def get_parsed_product(user_id: int) -> dict:
    """Получение результатов парсинга для пользователя"""
    return parsing_cache.get(f"product_{user_id}")


async def cancel_all_pending_invoices(context, chat_id):
    """Удаляет ВСЕ висящие инвойсы у пользователя"""
    to_remove = []

    for payload, info in list(SENT_INVOICES.items()):
        if info["chat_id"] == chat_id:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=info["message_id"]
                )
                print(f"🗑 Removed pending invoice msg={info['message_id']} payload={payload}")
                to_remove.append(payload)
            except Exception as e:
                print(f"⚠️ Could not remove invoice {payload}: {e}")

    # Чистим словарь
    for payload in to_remove:
        SENT_INVOICES.pop(payload, None)
  
async def maybe_cancel_yk_after_delay(payment_id: str, chat_id: int, delay_seconds: int = 10, reason_msg: str = None):
    await asyncio.sleep(delay_seconds)
    try:
        yk = await fetch_yk_payment(payment_id)
        if not yk:
            print(f"ℹ️ cannot fetch yk payment {payment_id} after delay")
            return
        status = yk.get("status")
        print(f"ℹ️ Post-delay YooKassa status for {payment_id}: {status}")
        if status in ("pending", "waiting_for_capture"):
            code, text = await cancel_yk_payment(payment_id)
            print(f"🗑 Auto-cancel attempt for {payment_id} -> {code} {text}")
            # уведомим пользователя (если надо)
            try:
                await context.bot.send_message(chat_id=chat_id, text=("⛔ <b>Оплата отменена</b>\nЕсли вы закрыли форму — попробуйте снова." if not reason_msg else reason_msg), parse_mode="HTML")
            except Exception as e:
                print("Ошибка отправки сообщения после автo-отмены:", e)
    except Exception as e:
        print("Ошибка maybe_cancel_yk_after_delay:", e)


async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка данных из Web App — с подробным логированием invoice"""
    if not update.message or not update.message.web_app_data:
        return

    try:
        data = json.loads(update.message.web_app_data.data)
        print("📦 WebApp data received:", data)

        # ==========================
        #  ОБРАБОТКА ОПЛАТЫ
        # ==========================
        if data.get("success") and "prices" in data:
            await cancel_all_pending_invoices(context, update.effective_chat.id)
            context.user_data["pending_orders"] = {}

            # --- базовый payload от фронта ---
            raw_key = data.get("payload") or "order"

            # --- создаём полностью уникальный payload ---
            payload = generate_unique_payload(raw_key)
            data["payload"] = payload
            
            print(f"🔐 Generated payload via function: {payload}")

            # =================================================
            #   УДАЛЯЕМ старый invoice, если он был ранее
            # =================================================
            old = PENDING_MESSAGES.get(raw_key)
            if old:
                try:
                    await context.bot.delete_message(
                        chat_id=old["chat_id"],
                        message_id=old["message_id"]
                    )
                    print(f"🗑 Deleted old invoice message {old['message_id']} for key {raw_key}")
                except Exception as e:
                    print(f"⚠️ Could not delete old invoice {old}: {e}")

                PENDING_MESSAGES.pop(raw_key, None)

            # ==========================
            #  Проверяем переданный yookassa_payment_id (если есть)
            #  и пытаемся отменить старый pending-платеж, чтобы избежать duplicate
            # ==========================
            incoming_yk = data.get("yookassa_payment_id")
            accepted_yk = None

            if incoming_yk:
                print("ℹ️ WebApp provided yookassa_payment_id:", incoming_yk)
                yk_info = await fetch_yk_payment(incoming_yk)
                if not yk_info:
                    print("⚠️ Не удалось получить данные по YooKassa платежу или креды отсутствуют — игнорируем incoming id")
                else:
                    yk_status = yk_info.get("status")
                    created_at = yk_info.get("created_at")
                    print(f"ℹ️ YooKassa status={yk_status}, created_at={created_at} for id={incoming_yk}")

                    # Попробуем вычислить возраст платежа (в сек)
                    age_seconds = None
                    if created_at:
                        try:
                            # fromisoformat может парсить +00:00, если есть Z — заменим
                            created_norm = created_at.replace("Z", "+00:00")
                            created_dt = datetime.fromisoformat(created_norm)
                            now_utc = datetime.now(timezone.utc)
                            # если created_dt не имеет tzinfo, считаем как UTC
                            if created_dt.tzinfo is None:
                                created_dt = created_dt.replace(tzinfo=timezone.utc)
                            age_seconds = (now_utc - created_dt).total_seconds()
                        except Exception as e:
                            print("⚠️ Не удалось распарсить created_at:", e)

                    # Логика: если статус pending/waiting_for_capture и возраст > threshold -> отменяем
                    if yk_status in ("pending", "waiting_for_capture"):
                        if age_seconds is None:
                            print("⚠️ Не удалось получить возраст платежа — по безопасности игнорируем incoming id")
                        else:
                            print(f"ℹ️ YooKassa payment age={age_seconds:.1f}s (threshold={YK_AGE_CANCEL_THRESHOLD}s)")
                            if age_seconds > YK_AGE_CANCEL_THRESHOLD:
                                code, text = await cancel_yk_payment(incoming_yk)
                                print(f"🗑 Cancel attempt for {incoming_yk} -> {code} {text}")
                                # не сохраняем incoming id (он отменён)
                            else:
                                # Если платёж совсем свежий (< threshold), чтобы избежать race — лучше не переиспользовать старый id,
                                # т.к. submit duplicate может появиться при повторном использовании. Решение: **не сохраняем** incoming id
                                print("⚠️ YooKassa payment is fresh but to avoid duplicates we will ignore incoming id and let Telegram create a new one.")
                    elif yk_status in ("succeeded", "succeeded_by_provider", "captured"):
                        # Теоретически можно принять, но чаще всего это не случится в момент создания invoice — логируем и принимаем
                        accepted_yk = incoming_yk
                        print("✅ YooKassa payment already succeeded — accepting incoming id.")
                    else:
                        print("⚠️ YooKassa payment in unexpected status -> ignoring:", yk_status)

            # ==========================
            #  ФОРМИРУЕМ ЧЕК ЮКАССЫ (local provider_data для Telegram)
            # ==========================
            prices = [LabeledPrice(**p) for p in data["prices"]]

            amount_cop = data["prices"][0]["amount"]
            amount_rub = amount_cop / 100

            base_desc = data.get("description", "")[:110]  # оставляем запас для хвоста
            unique_suffix = uuid.uuid4().hex[:6]          # уникальный короткий ID
            receipt_description = f"{base_desc} | {unique_suffix}"  # ← уникальна для каждого вызова

            provider_data = {
                "receipt": {
                    "items": [
                        {
                            "description": receipt_description,
                            "quantity": "1.00",
                            "amount": {
                                "value": f"{amount_rub:.2f}",
                                "currency": "RUB"
                            },
                            "vat_code": 1,
                            "payment_mode": "full_payment",
                            "payment_subject": "service",
                        }
                    ],
                    "tax_system_code": 1
                }
            }

            # ==========================
            #  СОХРАНЯЕМ МЕТАДАННЫЕ ПЛАТЕЖА (но НЕ вслепую incoming yk id)
            # ==========================
            pending_meta = data.get("metadata", {}) or {}
            if accepted_yk:
                pending_meta["yookassa_payment_id"] = accepted_yk
            else:
                # чтобы избежать дубликатов, явно не сохраняем incoming yk id
                if data.get("yookassa_payment_id"):
                    print("ℹ️ Ignoring incoming yookassa_payment_id to avoid duplicate submits.")

            # сохраняем meta по УНИКАЛЬНОМУ payload
            context.user_data.setdefault("pending_orders", {})[payload] = {
                **pending_meta,
                "raw_key": raw_key
            }

            # ==========================
            #  ОТПРАВЛЯЕМ INVOICE
            # ==========================
            sent = await update.message.reply_invoice(
                title=data["title"],
                description=data["description"],
                payload=payload,
                provider_token=os.getenv("TELEGRAM_PROVIDER_TOKEN") or "390540012:LIVE:82345",
                currency=data["currency"],
                prices=prices,
                start_parameter="publish",
                need_name=True,
                need_email=True,
                send_email_to_provider=True,
                provider_data=json.dumps(provider_data, ensure_ascii=False),
            )


            # ==========================
            #  РЕГИСТРАЦИЯ ОТПРАВЛЕННОГО ИНВОЙСА
            # ==========================
            info = {
                "chat_id": update.effective_chat.id,
                "message_id": sent.message_id,
                "ts": int(time.time()),
                "provider_data": provider_data,
                "raw_key": raw_key,
            }

            PENDING_MESSAGES[raw_key] = info
            SENT_INVOICES[payload] = info

            print(f"✅ Sent invoice. payload={payload} chat={info['chat_id']} msg={info['message_id']}")
            return

        # ==========================
        #  ОСТАЛЬНЫЕ ДЕЙСТВИЯ WEB-APP
        # ==========================
        action = data.get("action")

        if action == "create_order":
            await update.message.reply_text(f"✅ Заказ создан!\n🛍️ {data.get('product_name','N/A')}")

        elif action == "parse_product":
            product_url = data.get("product_url")
            if product_url:
                await handle_product_parsing(update, product_url)

        else:
            await update.message.reply_text("✅ Данные получены!")

    except Exception as e:
        print(f"❌ Error handling WebApp data: {e}")
        await update.message.reply_text("❌ Ошибка обработки данных от приложения")

async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    pending_orders = context.user_data.setdefault("pending_orders", {})
    pending_meta = pending_orders.get(payload, {}) or {}

    yk_id = pending_meta.get("yookassa_payment_id")

    if not yk_id:
        print("⚠️ yookassa_payment_id не найден в context.user_data, пробуем provider_payment_charge_id как fallback")
        yk_id = payment.provider_payment_charge_id

    # Получаем ключи
    yookassa_account = os.getenv("YOOKASSA_SHOP_ID")
    yookassa_secret = os.getenv("YOOKASSA_SECRET_KEY")

    message = update.message or \
        (update.callback_query.message if update.callback_query else None)
    if not message:
        print("⚠️ successful_payment пришёл, но message нет!")
        return

    payment = message.successful_payment
    print("🎉 PAYMENT DATA:", payment.to_dict())
        
    # Если есть yk_id и креды — делаем запрос в YooKassa, чтобы получить официальные metadata
    remote_meta = {}
    if yk_id and yookassa_account and yookassa_secret:
        try:
            auth = aiohttp.BasicAuth(yookassa_account, yookassa_secret)
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.yookassa.ru/v3/payments/{yk_id}", auth=auth) as resp:
                    if resp.status == 200:
                        payment_data = await resp.json()
                        print(f"📦 Ответ YooKassa: {json.dumps(payment_data, ensure_ascii=False, indent=2)}")
                        remote_meta = payment_data.get("metadata", {}) or {}
                    else:
                        text = await resp.text()
                        print(f"⚠️ YooKassa returned {resp.status}: {text}")
        except Exception as e:
            print(f"❌ Ошибка при запросе к YooKassa: {e}")

    # Если remote_meta пустой — используем pending_meta, иначе используем remote_meta (точнее)
    meta = remote_meta or pending_meta or {}

    # Гарантируем наличие category
    category = meta.get("category") or "Не указана"
    meta["category"] = category

    # Валидация обязательных полей перед отправкой на backend
    user_id = meta.get("user_id")
    url = meta.get("url")
    name = meta.get("name")
    scheduled_date = meta.get("scheduled_date")

    if not (user_id and url and name and scheduled_date):
        await update.message.reply_text("⚠️ Не удалось получить все данные заказа из платежа. Обратитесь в поддержку.")
        print("❌ Недостаточно данных для добавления товара:", meta)
        return

    # Отправляем на backend /api/products/add
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{BACKEND_URL}/api/products/add",
                json={
                    "user_id": user_id,
                    "url": url,
                    "name": name,
                    "description": meta.get("description") or "",
                    "image_url": meta.get("image_url") or None,
                    "price": float(meta.get("price") or 0),
                    "scheduled_date": scheduled_date,
                    "category": category,
                },
            ) as resp:
                result = await resp.json()
                print(f"📦 Ответ от /api/products/add: {result}")

        if result.get("success"):
            await update.message.reply_text("✅ Оплата подтверждена! Товар добавлен в очередь на выкладку.")
            if payload in pending_orders:
                del pending_orders[payload]
        else:
            await update.message.reply_text(f"⚠️ Оплата прошла, но не удалось добавить товар: {result.get('error')}")
    except Exception as e:
        print(f"❌ Ошибка при добавлении товара после оплаты: {e}")
        await update.message.reply_text("❌ Ошибка при добавлении товара в базу.")

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    query = update.pre_checkout_query

    yk_id = query.provider_payment_charge_id  # <-- ЭТО id юкассы, нужный нам
    payload = query.invoice_payload
    chat_id = query.from_user.id

    print("💳 pre_checkout:", yk_id, payload)

    # запускаем авто-отмену через 8 секунд, если платёж зависнет
    if yk_id:
        asyncio.create_task(
            maybe_cancel_yk_after_delay(
                payment_id=yk_id,
                chat_id=chat_id,
                delay_seconds=8,
                reason_msg="⛔️ Оплата не была подтверждена. Попробуйте ещё раз."
            )
        )

    await query.answer(ok=True)


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    try:
        invoice_payload = query.invoice_payload
        print(f"➡️ PreCheckout received. invoice_payload={invoice_payload} from user={query.from_user.id}")

        # логируем соответствие сохранённых инвойсов
        sent = SENT_INVOICES.get(invoice_payload)
        if sent:
            print(f"🔎 Matched sent invoice: {sent}")
            # можно дополнительно проверить возраст инвойса
            age = int(time.time()) - sent["ts"]
            if age > 60 * 15:  # 15 минут
                print("⚠️ Invoice older than 15min, rejecting precheckout to force new flow.")
                await query.answer(ok=False, error_message="Срок формы оплаты истёк — откройте форму снова.")
                return

            # всё ок — подтверждаем
            await query.answer(ok=True)
            print(f"✅ PreCheckout confirmed: {invoice_payload}")
        else:
            # Нет соответствия — логируем ВАЖНО и НЕ подтверждаем, чтобы не создавать неотслеживаемые оплаты
            print(f"❌ PreCheckout payload NOT FOUND in SENT_INVOICES! payload={invoice_payload}")
            # Включаем подробное состояние pending keys
            print("CURRENT PENDING_KEYS:", list(PENDING_MESSAGES.keys()))
            print("CURRENT SENT_PAYLOADS:", list(SENT_INVOICES.keys())[:50])
            # можно временно ответить false, чтобы клиент увидел ошибку и не продолжал
            await query.answer(ok=False, error_message="Не найдено соответствие инвойсу. Откройте оплату снова.")
            return

    except Exception as e:
        print(f"❌ Ошибка precheckout: {e}")
        try:
            await query.answer(ok=False, error_message="Ошибка при подготовке оплаты. Попробуйте снова.")
        except Exception:
            pass

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню статистики (выбор месяца или день)"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет доступа.")
        return

    now = datetime.now()
    year = now.year
    months = [
        ("Январь", 1), ("Февраль", 2), ("Март", 3), ("Апрель", 4),
        ("Май", 5), ("Июнь", 6), ("Июль", 7), ("Август", 8),
        ("Сентябрь", 9), ("Октябрь", 10), ("Ноябрь", 11), ("Декабрь", 12)
    ]

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"month:{year}:{m}")]
        for name, m in months
    ]
    # добавим кнопку за сегодня
    keyboard.insert(0, [InlineKeyboardButton("📅 Сегодня", callback_data="stats_today")])

    await update.message.reply_text(
        "📊 Выберите период для просмотра статистики:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def stats_months_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возвращает список месяцев"""
    query = update.callback_query
    await query.answer()

    now = datetime.now()
    year = now.year
    months = [
        ("Январь", 1), ("Февраль", 2), ("Март", 3), ("Апрель", 4),
        ("Май", 5), ("Июнь", 6), ("Июль", 7), ("Август", 8),
        ("Сентябрь", 9), ("Октябрь", 10), ("Ноябрь", 11), ("Декабрь", 12)
    ]

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"month:{year}:{m}")]
        for name, m in months
    ]
    keyboard.insert(0, [InlineKeyboardButton("📅 Сегодня", callback_data="stats_today")])

    await query.edit_message_text(
        "📊 Выберите месяц:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
async def month_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, year_s, month_s = query.data.split(":")
        year, month = int(year_s), int(month_s)
    except Exception:
        await query.edit_message_text("⚠️ Некорректные данные от кнопки.")
        return

    # Получаем статистику за месяц
    async with aiohttp.ClientSession() as session:
        url = f"{BACKEND_URL}/api/admin/stats?type=month&year={year}&month={month}"
        async with session.get(url) as resp:
            if resp.status != 200:
                await query.edit_message_text("⚠️ Ошибка при запросе статистики.")
                return
            data = await resp.json()

    if not data.get("success") or "stats" not in data:
        await query.edit_message_text("⚠️ Ошибка ответа от сервера.")
        return

    stats = data["stats"]

    # Формируем текст
    month_name = datetime(year, month, 1).strftime("%B %Y")
    msg_lines = [
        f"📊 <b>Статистика за {month_name}</b>\n",
        f"✅ Выложено: {stats['posted_count']} постов × 300₽ = {stats['posted_amount']}₽",
        f"⌛ Ожидает выкладки: {stats['pending_count']} постов × 300₽ = {stats['pending_amount']}₽",
        "",
        "Выберите неделю:"
    ]

    # Недели
    days_in_month = calendar.monthrange(year, month)[1]
    keyboard = []
    day = 1
    week_index = 0
    while day <= days_in_month:
        week_index += 1
        week_start = datetime(year, month, day)
        week_end = datetime(year, month, min(day + 6, days_in_month))
        label = f"Неделя {week_index} ({week_start:%d.%m}–{week_end:%d.%m})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"week:{year}:{month}:{week_index}")])
        day += 7

    # Кнопка назад
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="stats_months")])

    await query.edit_message_text(
        "\n".join(msg_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def week_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, year_s, month_s, week_s = query.data.split(":")
        year, month, week = int(year_s), int(month_s), int(week_s)
    except Exception:
        await query.edit_message_text("⚠️ Ошибка данных недели.")
        return

    # Считаем диапазон недели
    days_in_month = calendar.monthrange(year, month)[1]
    start_day = 1 + (week - 1) * 7
    end_day = min(start_day + 6, days_in_month)

    async with aiohttp.ClientSession() as session:
        url = f"{BACKEND_URL}/api/admin/stats?type=week&year={year}&month={month}&week={week}"
        async with session.get(url) as resp:
            if resp.status != 200:
                await query.edit_message_text("⚠️ Ошибка при запросе статистики.")
                return
            data = await resp.json()

    if not data.get("success") or "stats" not in data:
        await query.edit_message_text("⚠️ Ошибка данных с сервера.")
        return

    stats = data["stats"]

    msg = (
        f"📅 <b>Неделя {week}</b> ({start_day:02}.{month:02}.{year} — {end_day:02}.{month:02}.{year})\n\n"
        f"✅ Выложено: {stats['posted_count']} × 300₽ = {stats['posted_amount']}₽\n"
        f"⌛ Ожидает: {stats['pending_count']} × 300₽ = {stats['pending_amount']}₽"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад к месяцу", callback_data=f"month:{year}:{month}")]
    ])

    await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb)


# --- Сегодня ---
async def stats_today_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика за сегодняшний день"""
    query = update.callback_query
    await query.answer()

    async with aiohttp.ClientSession() as session:
        url = f"{BACKEND_URL}/api/admin/stats?type=day"
        async with session.get(url) as resp:
            if resp.status != 200:
                await query.edit_message_text("⚠️ Ошибка при запросе статистики.")
                return
            data = await resp.json()

    if not data.get("success") or "stats" not in data:
        await query.edit_message_text("⚠️ Ошибка ответа от сервера.")
        return

    stats = data["stats"]
    msg = (
        "📊 <b>Статистика за сегодня</b>\n\n"
        f"✅ Выложено: {stats['posted_count']} × 300₽ = {stats['posted_amount']}₽\n"
        f"⌛ Ожидает выкладки: {stats['pending_count']} × 300₽ = {stats['pending_amount']}₽"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад", callback_data="stats_months")]
    ])

    await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb)


async def debug_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat = await context.bot.get_chat(CHANNEL_ID)

        admins = await context.bot.get_chat_administrators(CHANNEL_ID)
        admin_usernames = [a.user.username for a in admins]

        can_post = False
        for a in admins:
            if a.user.id == context.bot.id:
                can_post = a.can_post_messages if hasattr(a, "can_post_messages") else True

        msg = [
            "🔍 <b>Проверка канала</b>",
            f"📢 Канал: {chat.title}",
            f"🆔 ID: <code>{chat.id}</code>",
            "",
            "👮 <b>Администраторы:</b>",
            "\n".join(f"• @{u}" for u in admin_usernames),
            "",
            f"🤖 Бот: @{context.bot.username}",
            f"🟢 Является админом: {'<b>ДА</b>' if context.bot.username in admin_usernames else '<b>НЕТ</b>'}",
            f"✍️ Может отправлять сообщения: {'<b>ДА</b>' if can_post else '<b>НЕТ</b>'}",
        ]

        await update.message.reply_text("\n".join(msg), parse_mode="HTML")

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")
        

async def remove_webhook_before_start(application):
    await application.bot.delete_webhook(drop_pending_updates=True)

if __name__ == "__main__":
    print("🚀 Запускаю бота для Wildberries...")
    print(f"🔑 Токен: {BOT_TOKEN[:10]}...")
    print(f"🌐 Web App URL: {WEB_APP_URL}")
    print(f"📞 Поддержка: {SUPPORT_USERNAME}")
    
    try:
        app = Application.builder().token(BOT_TOKEN).post_init(remove_webhook_before_start).build()
        
        # Обработчики
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
        app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment))
        app.add_handler(CommandHandler("stats", admin_stats))
        app.add_handler(CommandHandler("stats", admin_stats))
        app.add_handler(CommandHandler("debug_channel", debug_channel))
        app.add_handler(CallbackQueryHandler(stats_months_callback, pattern="^stats_months$"))
        app.add_handler(CallbackQueryHandler(stats_today_callback, pattern="^stats_today$"))
        app.add_handler(CallbackQueryHandler(month_callback, pattern=r"^month:\d{4}:\d{1,2}$"))
        app.add_handler(CallbackQueryHandler(week_callback, pattern=r"^week:\d{4}:\d{1,2}:\d+$"))
        app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
        
        print("✅ Бот запущен!")
        logging.basicConfig(level=logging.DEBUG)
        app.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=0.3)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
