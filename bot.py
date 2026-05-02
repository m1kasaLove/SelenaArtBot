import asyncio
import logging
import os
import aiohttp
import base64
from io import BytesIO
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import LabeledPrice, PreCheckoutQuery, SuccessfulPayment, BufferedInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import redis.asyncio as redis

# ===== КОНФИГУРАЦИЯ =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
POLZA_API_KEY = os.getenv("POLZA_API_KEY")
REDIS_URL = os.getenv("REDIS_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", 532229128))

BASE_URL = os.getenv("BASE_URL", "https://selenaartbot.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
redis_client = None

# ===== НАСТРОЙКИ =====
FREE_GENERATIONS_PER_DAY = 3
FREE_EDITS_PER_DAY = 3
PRICE_GENERATION = 10
PRICE_EDIT = 15
PREMIUM_PRICE = 50
PREMIUM_DAYS = 30

# ===== ФУНКЦИИ REDIS =====
async def get_premium(user_id: int) -> bool:
    try:
        status = await redis_client.get(f"selena:premium:{user_id}")
        return status == "1"
    except:
        return False

async def set_premium(user_id: int, days: int = PREMIUM_DAYS):
    await redis_client.setex(f"selena:premium:{user_id}", days * 86400, "1")

async def get_generations_today(user_id: int) -> int:
    day_key = int(datetime.now().timestamp() // 86400)
    key = f"selena:gen:{user_id}:{day_key}"
    val = await redis_client.get(key)
    return int(val) if val else 0

async def incr_generations_today(user_id: int) -> int:
    day_key = int(datetime.now().timestamp() // 86400)
    key = f"selena:gen:{user_id}:{day_key}"
    new = await redis_client.incr(key)
    await redis_client.expire(key, 86400)
    return new

async def get_edits_today(user_id: int) -> int:
    day_key = int(datetime.now().timestamp() // 86400)
    key = f"selena:edit:{user_id}:{day_key}"
    val = await redis_client.get(key)
    return int(val) if val else 0

async def incr_edits_today(user_id: int) -> int:
    day_key = int(datetime.now().timestamp() // 86400)
    key = f"selena:edit:{user_id}:{day_key}"
    new = await redis_client.incr(key)
    await redis_client.expire(key, 86400)
    return new

# ===== ГЕНЕРАЦИЯ ЧЕРЕЗ POLZA.AI (КАЧЕСТВЕННО) =====
async def generate_image(prompt: str) -> BytesIO | None:
    """Генерация через Polza.ai Qwen/Image-2"""
    
    headers = {
        "Authorization": f"Bearer {POLZA_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Улучшаем промпт для лучшего качества
    enhanced_prompt = f"High quality, detailed, beautiful: {prompt}"
    
    payload = {
        "model": "qwen/image-2",
        "input": {
            "prompt": enhanced_prompt,
            "aspect_ratio": "1:1",
            "output_format": "png",
            "guidance_scale": 7.5
        },
        "async": True
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            # Отправляем запрос
            async with session.post("https://api.polza.ai/v1/media", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Polza API error {resp.status}: {error_text}")
                    return None
                data = await resp.json()
                task_id = data.get("id")
                logger.info(f"Generation task_id: {task_id}")
            
            # Ждём результат (до 30 секунд)
            for attempt in range(30):
                await asyncio.sleep(2)
                async with session.get(f"https://api.polza.ai/v1/media/{task_id}", headers=headers) as status_resp:
                    if status_resp.status != 200:
                        continue
                    status_data = await status_resp.json()
                    status = status_data.get("status")
                    
                    if status == "completed":
                        images = status_data.get("output", {}).get("images", [])
                        if images:
                            image_url = images[0].get("url")
                            if image_url:
                                async with session.get(image_url) as img_resp:
                                    if img_resp.status == 200:
                                        img_data = await img_resp.read()
                                        logger.info(f"Image generated, size: {len(img_data)} bytes")
                                        return BytesIO(img_data)
                    elif status == "failed":
                        logger.error(f"Generation failed: {status_data}")
                        return None
                    else:
                        logger.info(f"Waiting... status: {status}")
            
            logger.error("Generation timeout")
            return None
            
        except Exception as e:
            logger.error(f"Polza generation error: {e}")
            return None

async def edit_image(image_bytes: BytesIO, prompt: str) -> BytesIO | None:
    """Редактирование через Polza.ai с референсом"""
    
    # Конвертируем фото в base64
    image_bytes.seek(0)
    image_base64 = base64.b64encode(image_bytes.read()).decode('utf-8')
    
    headers = {
        "Authorization": f"Bearer {POLZA_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "qwen/image-2",
        "input": {
            "prompt": prompt,
            "image": image_base64,
            "strength": 0.7,
            "aspect_ratio": "1:1",
            "output_format": "png"
        },
        "async": True
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://api.polza.ai/v1/media", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    logger.error(f"Polza edit API error: {resp.status}")
                    return None
                data = await resp.json()
                task_id = data.get("id")
            
            for attempt in range(30):
                await asyncio.sleep(2)
                async with session.get(f"https://api.polza.ai/v1/media/{task_id}", headers=headers) as status_resp:
                    status_data = await status_resp.json()
                    if status_data.get("status") == "completed":
                        images = status_data.get("output", {}).get("images", [])
                        if images:
                            image_url = images[0].get("url")
                            async with session.get(image_url) as img_resp:
                                img_data = await img_resp.read()
                                return BytesIO(img_data)
                    elif status_data.get("status") == "failed":
                        return None
            return None
        except Exception as e:
            logger.error(f"Polza edit error: {e}")
            return None

# ===== КОМАНДЫ БОТА =====
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    is_premium = await get_premium(user_id)
    
    await message.answer(
        "🎨 *SelenaArtBot* — твой AI-художник!\n\n"
        "Я использую нейросеть *Qwen/Image-2* для создания качественных изображений.\n\n"
        "✨ *Что умею:*\n"
        "• Генерировать картинки из текста\n"
        "• Редактировать твои фото\n\n"
        "📝 *Примеры:*\n"
        "• `кот в сапогах`\n"
        "• `закат на море`\n"
        "• `киберпанк город`\n\n"
        f"🎁 Бесплатно: {FREE_GENERATIONS_PER_DAY} генераций в день\n"
        f"⭐ Премиум: /buy — {PREMIUM_PRICE}⭐ на {PREMIUM_DAYS} дней\n\n"
        "/status — твоя статистика",
        parse_mode="Markdown"
    )

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    user_id = message.from_user.id
    is_premium = await get_premium(user_id)
    
    if is_premium:
        await message.answer("🌟 *Премиум активен!* Безлимитные генерации!", parse_mode="Markdown")
    else:
        today_gen = await get_generations_today(user_id)
        remaining = max(0, FREE_GENERATIONS_PER_DAY - today_gen)
        await message.answer(
            f"📊 *Твоя статистика*\n\n"
            f"🎨 Осталось генераций: {remaining} из {FREE_GENERATIONS_PER_DAY}\n\n"
            f"Купи премиум: /buy",
            parse_mode="Markdown"
        )

@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    prices = [LabeledPrice(label=f"Безлимит {PREMIUM_DAYS} дней", amount=PREMIUM_PRICE)]
    await message.answer_invoice(
        title="SelenaArtBot Premium",
        description=f"Безлимитные генерации на {PREMIUM_DAYS} дней!",
        payload="premium_30days",
        provider_token="",
        currency="XTR",
        prices=prices
    )

@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def payment_success(message: SuccessfulPayment):
    user_id = message.from_user.id
    await set_premium(user_id, days=30)
    await message.answer(
        "✅ *Премиум активирован!*\n\nТеперь безлимитные генерации! 🎨",
        parse_mode="Markdown"
    )

# ===== ГЕНЕРАЦИЯ =====
@dp.message(F.text & ~F.text.startswith('/'))
async def generate_by_text(message: types.Message):
    user_id = message.from_user.id
    prompt = message.text.strip()
    
    if len(prompt) < 3:
        await message.answer("❌ Напиши подробнее, что нарисовать")
        return
    
    is_premium = await get_premium(user_id)
    today_used = await get_generations_today(user_id)
    
    if not is_premium and today_used >= FREE_GENERATIONS_PER_DAY:
        prices = [LabeledPrice(label="Одна генерация", amount=PRICE_GENERATION)]
        await message.answer_invoice(
            title="Генерация",
            description=f"Запрос: {prompt[:80]}",
            payload=f"single_gen:{prompt}",
            provider_token="",
            currency="XTR",
            prices=prices
        )
        return
    
    if not is_premium:
        await incr_generations_today(user_id)
    
    status_msg = await message.answer(f"🎨 *Генерирую:* {prompt[:50]}...\n⏳ Обычно 5-15 секунд", parse_mode="Markdown")
    
    img = await generate_image(prompt)
    
    if img:
        photo = BufferedInputFile(img.getvalue(), filename="selena.png")
        await message.answer_photo(photo, caption=f"🎨 *{prompt[:100]}*\n✨ Qwen/Image-2", parse_mode="Markdown")
        await status_msg.delete()
    else:
        await status_msg.edit_text(
            "❌ *Ошибка генерации*\n\n"
            "Попробуй:\n"
            "• Написать на английском\n"
            "• Более простой запрос\n"
            "• Пример: `cat with boots`",
            parse_mode="Markdown"
        )

# ===== ЗАПУСК ВЕБ-СЕРВЕРА =====
async def root(request):
    return web.Response(text="SelenaArtBot is alive! 🎨")

async def on_startup(app):
    global redis_client
    redis_client = await redis.from_url(REDIS_URL, decode_responses=True)
    logger.info("✅ Redis connected")
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"✅ Webhook set: {WEBHOOK_URL}")

async def on_shutdown(app):
    if redis_client:
        await redis_client.close()
    await bot.session.close()

def create_app():
    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", root)
    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
