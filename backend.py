from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
import httpx, uuid, hashlib, json
from yookassa import Configuration, Payment
from telegram import Bot
import os
import re
from database.db import get_session
from database.models import Product, User
from backend.new_parser import parse_wb_product_api

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = "@wbsellers_test"  # или твой канал
TELEGRAM_PROVIDER_TOKEN=os.getenv("TELEGRAM_PROVIDER_TOKEN")

bot = Bot(token=BOT_TOKEN)

app = FastAPI() 

scheduler = AsyncIOScheduler()
scheduler.start()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # можно указать ["http://localhost:5173"] если хочешь строго
    allow_credentials=True,
    allow_methods=["*"],  # разрешаем все методы (GET, POST, OPTIONS и т.д.)
    allow_headers=["*"],
)

def _sanitize_meta_field(value: any, max_len: int = 128) -> str:
    if value is None:
        return ""
    s = str(value)
    s = re.sub(r"[\r\n\t]+", " ", s).strip()
    if len(s) > max_len:
        return s[:max_len]
    return s

@app.post("/api/payments/create")
async def create_payment(request: Request):
    import uuid

    try:
        data = await request.json()
    except Exception:
        data = {}

    amount = float(data.get("amount", 100))
    meta = data.get("meta", {}) or {}

    order_id = str(uuid.uuid4())

    title = "Оплата размещения товара"
    description = f"Размещение товара: {meta.get('name', 'Товар')}"

    # Telegram требует сумму в КОПЕЙКАХ
    prices = [{"label": "Публикация", "amount": int(amount * 100)}]

    # Храним короткое metadata, чтобы потом обработать callback
    safe_meta = {
        "order_id": order_id,
        "user_id": _sanitize_meta_field(meta.get("user_id") or meta.get("tg_id") or "", 64),
        "url": _sanitize_meta_field(meta.get("url", ""), 200),
        "name": _sanitize_meta_field(meta.get("name", ""), 128),
        "description": _sanitize_meta_field(meta.get("description", ""), 200),
        "price": _sanitize_meta_field(meta.get("price", ""), 32),
        "scheduled_date": _sanitize_meta_field(meta.get("scheduled_date", ""), 64),
    }

    # Возвращаем всё, что нужно боту
    return {
        "success": True,
        "payload": f"order_{order_id}",
        "title": title,
        "description": description,
        "currency": "RUB",
        "prices": prices,
        "provider_token": os.getenv("TELEGRAM_PROVIDER_TOKEN"),
        "metadata": safe_meta,
    }

async def publish_product(product_id: int):
    async for session in get_session():
        result = await session.execute(select(Product).where(Product.id == product_id))
        product = result.scalar_one_or_none()
        if not product:
            print(f"❌ Товар с id={product_id} не найден")
            return

        caption = (
            f"🛍 {product.name}\n\n"
            f"{product.description or ''}\n\n"
            f"💰 Цена: {product.price} руб.\n"
            f"🔗 {product.url}"
        )

        try:
            if product.image_url:
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=product.image_url,
                    caption=caption[:1024],
                )
            else:
                await bot.send_message(chat_id=CHANNEL_ID, text=caption)

            product.status = "posted"
            await session.commit()

            print(f"✅ Товар опубликован: {product.name}")
        except Exception as e:
            print(f"❌ Ошибка публикации товара {product_id}: {e}")
        
@app.post("/api/products/parse")
async def parse_product(request: Request):
    """
    Парсит карточку товара по URL, но НЕ сохраняет её в базу.
    """
    data = await request.json()
    url = data.get("url")

    if not url:
        return {"success": False, "error": "Не передан url"}

    print(f"📩 Запрос на парсинг товара: {url}")

    # 🧩 Парсим карточку товара
    product_data = await parse_wb_product_api(url)
    if not product_data or not product_data.get("success"):
        print(f"⚠️ Не удалось распарсить товар: {url}")
        return {"success": False, "error": "Не удалось получить данные с Wildberries"}

    print(f"✅ Товар успешно распарсен: {product_data.get('name')}")
    return product_data

@app.post("/api/products/add")
async def add_product(request: Request):
    """
    Добавляет распарсенный товар в базу данных и планирует выкладку.
    """
    data = await request.json()
    tg_id = data.get("user_id")
    url = data.get("url")
    name = data.get("name")
    description = data.get("description")
    image_url = data.get("image_url")
    price = data.get("price")
    scheduled_date = data.get("scheduled_date")

    if not all([tg_id, url, name, scheduled_date]):
        return {"success": False, "error": "Отсутствуют обязательные поля"}

    async for session in get_session():
        # Проверяем пользователя
        result = await session.execute(select(User).where(User.tg_id == str(tg_id)))
        user = result.scalar_one_or_none()
        if not user:
            return {"success": False, "error": "Пользователь не найден"}

        # Проверяем и парсим дату
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_date)
        except ValueError:
            return {"success": False, "error": "Некорректный формат даты (ожидается ISO)"}

        # Создаём товар
        product = Product(
            user_id=str(tg_id),
            url=url,
            name=name,
            description=description,
            image_url=image_url,
            price=price,
            status="pending",  # ожидает выкладки
            scheduled_date=scheduled_dt,
        )

        session.add(product)
        await session.commit()
        await session.refresh(product)

        # Планируем публикацию
        scheduler.add_job(
            publish_product,
            trigger=DateTrigger(run_date=scheduled_dt),
            args=[product.id],  # передаем id, не объект!
            id=f"publish_{product.id}",
        )

        print(f"✅ Товар сохранён и запланирован на {scheduled_dt}: {product.name}")

        return {
            "success": True,
            "message": "Товар добавлен в очередь на выкладку",
            "product_id": product.id,
        }

@app.post("/api/users/register")
async def register_user(request: Request):
    data = await request.json()
    tg_id = data.get("tg_id")
    name = data.get("name")
    phone = data.get("phone")

    if not tg_id or not phone:
        return {"success": False, "error": "Не переданы tg_id или телефон"}

    async for session in get_session():
        # Проверяем, существует ли уже пользователь
        result = await session.execute(select(User).where(User.tg_id == str(tg_id)))
        user = result.scalars().first()

        if not user:
            # Создаём нового
            user = User(tg_id=str(tg_id), name=name, phone=phone)
            session.add(user)
            await session.commit()
            await session.refresh(user)
            print(f"✅ Новый пользователь зарегистрирован: {user.name} ({user.phone})")
        else:
            print(f"ℹ️ Пользователь уже есть: {user.name} ({user.phone})")

        return {"success": True, "user_id": user.id}
    
    
@app.get("/api/users/{tg_id}")
async def check_user_exists(tg_id: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    user = result.scalar_one_or_none()
    return {"exists": user is not None}

@app.get("/api/products/{tg_id}")
async def get_user_products(tg_id: str, session: AsyncSession = Depends(get_session)):
    """Возвращает список товаров пользователя по его Telegram ID"""
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        return {"success": False, "error": "Пользователь не найден"}

    # ✅ теперь ищем по строковому user_id (tg_id)
    result = await session.execute(select(Product).where(Product.user_id == user.tg_id))
    products = result.scalars().all()

    return {
        "success": True,
        "tg_id": tg_id,
        "user_id": user.tg_id,  # тоже исправляем, чтобы всё было консистентно
        "products": [
            {
                "id": p.id,
                "name": p.name,
                "price": p.price,
                "url": p.url,
                "status": p.status.value if hasattr(p.status, "value") else p.status,
                "created_at": p.created_at,
                "scheduled_date": p.scheduled_date,
            }
            for p in products
        ],
    }

@app.post("/api/payments/callback")
async def yookassa_callback(request: Request):
    payload = await request.json()
    event = payload.get("event")
    obj = payload.get("object", {})  # здесь обычно payment

    print("💳 YooKassa callback:", event)

    # В разных версиях event'ы называются по-разному, проверим вариант окончания
    if event in ("payment.succeeded", "payment.waiting_for_capture", "payment.captured"):
        payment = obj.get("payment") or obj  # иногда объект вложен
        metadata = payment.get("metadata", {}) if isinstance(payment, dict) else {}

        # Берём поля из metadata (те, что мы положили в create_payment)
        user_id = metadata.get("user_id") or metadata.get("tg_id")
        url = metadata.get("url")
        name = metadata.get("name")
        short_desc = metadata.get("short_desc") or ""
        image_url = metadata.get("image_url") or ""
        price = metadata.get("price") or 0
        scheduled_date = metadata.get("scheduled_date")

        # Добавляем в БД (если есть все обязательные поля)
        if user_id and url and name and scheduled_date:
            try:
                res = await add_product_to_db(
                    user_id=user_id,
                    url=url,
                    name=name,
                    description=short_desc,
                    image_url=image_url,
                    price=float(price) if price else 0,
                    scheduled_date=scheduled_date,
                )
                if res.get("success"):
                    print("✅ Товар добавлен в БД после оплаты")
                else:
                    print("❌ Не получилось добавить товар после оплаты:", res)
            except Exception as e:
                print("❌ Ошибка при добавлении товара после оплаты:", e)
        else:
            print("⚠️ Недостаточно данных в metadata для добавления товара:", metadata)

    # Всегда возвращаем 200 OK
    return {"success": True}

async def add_product_to_db(
    user_id: str,
    url: str,
    name: str,
    description: str,
    image_url: str,
    price: float,
    scheduled_date: str,
):
    async for session in get_session():
        result = await session.execute(select(User).where(User.tg_id == str(user_id)))
        user = result.scalar_one_or_none()
        if not user:
            print(f"❌ Пользователь {user_id} не найден при добавлении товара в DB")
            return {"success": False, "error": "Пользователь не найден"}

        try:
            scheduled_dt = datetime.fromisoformat(scheduled_date)
        except Exception as e:
            print(f"❌ Некорректный формат даты: {scheduled_date} ({e})")
            return {"success": False, "error": "Некорректный формат даты"}

        product = Product(
            user_id=str(user.tg_id),
            url=url,
            name=name,
            description=description,
            image_url=image_url,
            price=price,
            status="pending",
            scheduled_date=scheduled_dt,
        )

        session.add(product)
        await session.commit()
        await session.refresh(product)

        # Планируем публикацию
        try:
            scheduler.add_job(
                publish_product,
                trigger=DateTrigger(run_date=scheduled_dt),
                args=[product.id],
                id=f"publish_{product.id}",
            )
        except Exception as e:
            print(f"⚠️ Не удалось добавить задачу в scheduler: {e}")

        print(f"✅ Товар '{product.name}' сохранён и запланирован на {scheduled_dt}")
        return {"success": True, "product_id": product.id}

