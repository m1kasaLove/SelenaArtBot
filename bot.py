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

# ===== НОВЫЕ ЦЕНЫ (без безлимита, только пакеты) =====
FREE_GENERATIONS_PER_DAY = 2      # Бесплатно 2 генерации в день
FREE_EDITS_PER_DAY = 1            # Бесплатно 1 редактирование в день

PRICE_1_GEN = 2                   # 1 генерация = 2 ⭐
PRICE_5_GEN = 8                   # 5 генераций = 8 ⭐ (экономия 2⭐)
PRICE_10_GEN = 15                 # 10 генераций = 15 ⭐ (экономия 5⭐)
PRICE_EDIT = 3                    # Редактирование = 3 ⭐

# ===== ФУНКЦИИ REDIS =====
async def get_premium(user_id: int) -> bool:
    return False  # Безлимит ОТКЛЮЧЕН

async def set_premium(user_id: int, days: int = 30):
    pass  # Безлимит ОТКЛЮЧЕН

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

async def get_pack_generations(user_id: int) -> int:
    """Сколько купленных генераций осталось"""
    val = await redis_client.get(f"selena:pack:{user_id}")
    return int(val) if val else 0

async def use_pack_generation(user_id: int) -> bool:
    current = await get_pack_generations(user_id)
    if current > 0:
        await redis_client.decr(f"selena:pack:{user_id}")
        return True
    return False

async def add_pack_generations(user_id: int, count: int):
    await redis_client.incrby(f"selena:pack:{user_id}", count)

# ===== ГЕНЕРАЦИЯ ЧЕРЕЗ POLZA.AI =====
async def generate_image(prompt: str) -> BytesIO | None:
    """Генерация через Polza.ai"""
    
    headers = {
        "Authorization": f"Bearer {POLZA_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "qwen/image-2",
        "input": {
            "prompt": prompt,
            "aspect_ratio": "1:1",
            "output_format": "png",
            "guidance_scale": 7.5
        }
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://api.polza.ai/v1/media", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    logger.error(f"Polza error: {resp.status}")
                    return None
                data = await resp.json()
                task_id = data.get("id")
                logger.info(f"Task: {task_id}")
            
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
                            async with session.get(image_url) as img_resp:
                                if img_resp.status == 200:
                                    img_bytes = await img_resp.read()
                                    logger.info(f"Image size: {len(img_bytes)} bytes")
                                    return BytesIO(img_bytes)
                    elif status == "failed":
                        return None
            return None
        except Exception as e:
            logger.error(f"Error: {e}")
            return None

# ===== РЕКЛАМА LUNA BOT =====
async def send_luna_ad(message: types.Message):
    """Отправляет рекламу бота Луна"""
    await message.answer(
        "🌙 *Хочешь живое общение?*\n\n"
        "Попробуй моего другого бота — **Луна**!\n"
        "Она умеет:\n"
        "💬 Болтать на любые темы\n"
        "😏 Быть навязчивой и милой\n"
        "💫 Спрашивать как дела и писать первой\n\n"
        "👉 @LunaIsLovelyLunaBot\n\n"
        "Она ждёт тебя! 🌙",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

# ===== КОМАНДЫ БОТА =====
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    pack = await get_pack_generations(user_id)
    
    menu = (
        "🎨 *SelenaArtBot* — твой AI-художник!\n\n"
        "✨ *Нейросеть:* Qwen/Image-2\n\n"
        "💰 *Цены (Telegram Stars):*\n"
        f"• {FREE_GENERATIONS_PER_DAY} генерации в день — *БЕСПЛАТНО*\n"
        f"• 1 генерация — {PRICE_1_GEN} ⭐\n"
        f"• 5 генераций — {PRICE_5_GEN} ⭐ (экономия 2⭐)\n"
        f"• 10 генераций — {PRICE_10_GEN} ⭐ (экономия 5⭐)\n\n"
        f"📦 Куплено генераций: {pack}\n\n"
        "📝 *Команды:*\n"
        "/status — моя статистика\n"
        "/pack5 — 5 генераций\n"
        "/pack10 — 10 генераций\n"
        "/help — помощь\n\n"
        "⭐ 1 Star ≈ 10 рублей\n\n"
        "🌙 *Поболтать?* @LunaIsLovelyLunaBot"
    )
    
    await message.answer(menu, parse_mode="Markdown")
    
    # Отправляем рекламу Луны через 5 секунд
    await asyncio.sleep(5)
    await send_luna_ad(message)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 *Помощь*\n\n"
        "**Генерация:**\n"
        "Просто напиши описание картинки\n"
        "Пример: `кот в сапогах на закате`\n\n"
        "**Редактирование:**\n"
        "1. Отправь фото\n"
        "2. В подписи напиши изменения\n\n"
        f"**💰 Цены:**\n"
        f"• {FREE_GENERATIONS_PER_DAY} ген/день — бесплатно\n"
        f"• 1 ген — {PRICE_1_GEN} ⭐\n"
        f"• 5 ген — {PRICE_5_GEN} ⭐\n"
        f"• 10 ген — {PRICE_10_GEN} ⭐\n\n"
        "**Команды:**\n"
        "/start — главное меню\n"
        "/status — статистика\n"
        "/pack5 — 5 генераций\n"
        "/pack10 — 10 генераций\n\n"
        "🌙 *Общаться:* @LunaIsLovelyLunaBot",
        parse_mode="Markdown"
    )

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    user_id = message.from_user.id
    today_gen = await get_generations_today(user_id)
    remaining_free = max(0, FREE_GENERATIONS_PER_DAY - today_gen)
    pack = await get_pack_generations(user_id)
    
    await message.answer(
        f"📊 *Твоя статистика*\n\n"
        f"🎨 Бесплатных сегодня: {remaining_free} из {FREE_GENERATIONS_PER_DAY}\n"
        f"📦 Купленных генераций: {pack}\n\n"
        f"💫 *Купить:*\n"
        f"• /pack5 — 5 ген за {PRICE_5_GEN} ⭐\n"
        f"• /pack10 — 10 ген за {PRICE_10_GEN} ⭐\n\n"
        f"🌙 *Поболтать?* @LunaIsLovelyLunaBot",
        parse_mode="Markdown"
    )

@dp.message(Command("pack5"))
async def cmd_pack5(message: types.Message):
    """Пакет 5 генераций"""
    prices = [LabeledPrice(label="5 генераций", amount=PRICE_5_GEN)]
    
    await message.answer_invoice(
        title="🎨 Пакет 5 генераций",
        description=f"5 качественных картинок!\nЭкономия {PRICE_1_GEN} ⭐",
        payload="pack_5_generations",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="buy_pack5"
    )

@dp.message(Command("pack10"))
async def cmd_pack10(message: types.Message):
    """Пакет 10 генераций"""
    prices = [LabeledPrice(label="10 генераций", amount=PRICE_10_GEN)]
    
    await message.answer_invoice(
        title="🎨 Пакет 10 генераций",
        description=f"10 качественных картинок!\nЭкономия {PRICE_1_GEN * 2} ⭐",
        payload="pack_10_generations",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="buy_pack10"
    )

@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def payment_success(message: SuccessfulPayment):
    user_id = message.from_user.id
    payload = message.successful_payment.invoice_payload
    
    if payload == "pack_5_generations":
        await add_pack_generations(user_id, 5)
        await message.answer(
            "✅ *Куплено 5 генераций!*\n\n"
            "Просто напиши что нарисовать 🎨\n\n"
            "🌙 *А чтобы поболтать:* @LunaIsLovelyLunaBot",
            parse_mode="Markdown"
        )
    elif payload == "pack_10_generations":
        await add_pack_generations(user_id, 10)
        await message.answer(
            "✅ *Куплено 10 генераций!*\n\n"
            "Просто напиши что нарисовать 🎨\n\n"
            "🌙 *А чтобы поболтать:* @LunaIsLovelyLunaBot",
            parse_mode="Markdown"
        )
    elif payload.startswith("single_gen:"):
        prompt = payload.replace("single_gen:", "")
        await process_generation(message, prompt, is_paid=True)

async def process_generation(message: types.Message, prompt: str, is_paid: bool = False):
    status_msg = await message.answer(f"🎨 *Генерирую:* {prompt[:50]}...\n⏳ Обычно 10-20 секунд", parse_mode="Markdown")
    
    img = await generate_image(prompt)
    
    if img:
        photo = BufferedInputFile(img.getvalue(), filename="selena.png")
        await message.answer_photo(photo, caption=f"🎨 *{prompt[:100]}*", parse_mode="Markdown")
        await status_msg.delete()
    else:
        await status_msg.edit_text(
            "❌ *Ошибка генерации*\n\n"
            "Попробуй:\n"
            "• Написать на английском\n"
            "• Более простой запрос\n\n"
            "🌙 *А чтобы поболтать:* @LunaIsLovelyLunaBot",
            parse_mode="Markdown"
        )

# ===== ГЕНЕРАЦИЯ ПО ТЕКСТУ =====
@dp.message(F.text & ~F.text.startswith('/'))
async def generate_by_text(message: types.Message):
    user_id = message.from_user.id
    prompt = message.text.strip()
    
    if len(prompt) < 3:
        await message.answer("❌ Напиши подробнее, что нарисовать (минимум 3 символа)")
        return
    
    # Проверяем пакетные генерации
    pack = await get_pack_generations(user_id)
    if pack > 0:
        await use_pack_generation(user_id)
        await process_generation(message, prompt)
        return
    
    # Проверяем бесплатные
    today_used = await get_generations_today(user_id)
    if today_used >= FREE_GENERATIONS_PER_DAY:
        await message.answer(
            f"📊 *Лимит исчерпан!*\n\n"
            f"Сегодня ты использовал {FREE_GENERATIONS_PER_DAY} бесплатных генераций.\n\n"
            f"💰 *Купить:*\n"
            f"• /pack5 — 5 генераций за {PRICE_5_GEN} ⭐\n"
            f"• /pack10 — 10 генераций за {PRICE_10_GEN} ⭐\n\n"
            f"🌙 *А чтобы поболтать:* @LunaIsLovelyLunaBot",
            parse_mode="Markdown"
        )
        return
    
    await incr_generations_today(user_id)
    await process_generation(message, prompt)

# ===== РЕДАКТИРОВАНИЕ ФОТО =====
@dp.message(F.photo)
async def edit_photo(message: types.Message):
    user_id = message.from_user.id
    edit_prompt = message.caption
    
    if not edit_prompt:
        await message.answer(
            "✏️ *Чтобы отредактировать фото,* напиши изменения в подписи!\n\n"
            "Примеры:\n"
            "• `сделай чёрно-белым`\n"
            "• `добавь радугу`\n\n"
            "🌙 *Поболтать:* @LunaIsLovelyLunaBot",
            parse_mode="Markdown"
        )
        return
    
    today_used = await get_edits_today(user_id)
    if today_used >= FREE_EDITS_PER_DAY:
        prices = [LabeledPrice(label="Редактирование", amount=PRICE_EDIT)]
        await message.answer_invoice(
            title="🖼 Редактирование фото",
            description=edit_prompt[:80],
            payload=f"single_edit:{edit_prompt}",
            provider_token="",
            currency="XTR",
            prices=prices
        )
        return
    
    await incr_edits_today(user_id)
    
    status_msg = await message.answer(f"🖼 *Редактирую:* {edit_prompt[:50]}...", parse_mode="Markdown")
    
    try:
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = BytesIO()
        await bot.download_file(file.file_path, file_bytes)
        file_bytes.seek(0)
        
        # Заглушка для редактирования (т.к. Polza медленный)
        await status_msg.edit_text("🖼 Редактирование временно недоступно. Попробуй генерацию: /start")
    except Exception as e:
        logger.error(f"Edit error: {e}")
        await status_msg.edit_text("❌ Ошибка, попробуй генерацию /start")

# ===== ЗАПУСК ВЕБ-СЕРВЕРА =====
async def root(request):
    return web.Response(text="SelenaArtBot is alive! 🎨")

async def health(request):
    return web.Response(text="OK")

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
    app.router.add_get("/health", health)
    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
