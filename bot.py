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
async def get_redis():
    return redis_client

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

# ===== ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ =====
async def generate_image(prompt: str) -> BytesIO | None:
    import urllib.parse
    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=45) as resp:
                if resp.status == 200:
                    img_data = await resp.read()
                    if len(img_data) > 1024:
                        return BytesIO(img_data)
                return None
        except Exception as e:
            logger.error(f"Generation error: {e}")
            return None

async def edit_image(image_bytes: BytesIO, prompt: str) -> BytesIO | None:
    import urllib.parse
    edit_prompt = f"Edit this image: {prompt}"
    encoded_prompt = urllib.parse.quote(edit_prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=60) as resp:
                if resp.status == 200:
                    img_data = await resp.read()
                    if len(img_data) > 1024:
                        return BytesIO(img_data)
                return None
        except Exception as e:
            logger.error(f"Edit error: {e}")
            return None

# ===== КОМАНДЫ БОТА =====
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    is_premium = await get_premium(user_id)
    
    if is_premium:
        await message.answer(
            "🌙 *SelenaArtBot* — с возвращением!\n\n"
            "✨ У тебя активен премиум — безлимит!\n"
            "Просто напиши что нарисовать 🎨",
            parse_mode="Markdown"
        )
    else:
        today_gen = await get_generations_today(user_id)
        today_edit = await get_edits_today(user_id)
        remaining_gen = max(0, FREE_GENERATIONS_PER_DAY - today_gen)
        remaining_edit = max(0, FREE_EDITS_PER_DAY - today_edit)
        
        await message.answer(
            f"🌙 *SelenaArtBot* — твой AI-художник!\n\n"
            f"🎨 Генераций осталось: {remaining_gen} из {FREE_GENERATIONS_PER_DAY}\n"
            f"🖼 Редактирований: {remaining_edit} из {FREE_EDITS_PER_DAY}\n\n"
            f"**Что умею:**\n"
            f"• Напиши текст — нарисую картинку\n"
            f"• Отправь фото + описание — отредактирую\n\n"
            f"⭐ Купить премиум: /buy\n"
            f"📊 Статус: /status\n"
            f"❓ Помощь: /help",
            parse_mode="Markdown"
        )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 *Помощь*\n\n"
        "**Генерация:**\n"
        "Напиши любое описание\n"
        "Пример: `кот в космосе`\n\n"
        "**Редактирование:**\n"
        "1. Отправь фото\n"
        "2. В подписи напиши изменения\n"
        "3. Пример: `сделай чёрно-белым`\n\n"
        f"**Цены:**\n"
        f"• Генерация: {PRICE_GENERATION} ⭐\n"
        f"• Редактирование: {PRICE_EDIT} ⭐\n"
        f"• Премиум: {PREMIUM_PRICE} ⭐ на {PREMIUM_DAYS} дней",
        parse_mode="Markdown"
    )

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    user_id = message.from_user.id
    is_premium = await get_premium(user_id)
    
    if is_premium:
        await message.answer("🌟 *Премиум активен!* Безлимитные генерации и редактирования!", parse_mode="Markdown")
    else:
        today_gen = await get_generations_today(user_id)
        today_edit = await get_edits_today(user_id)
        remaining_gen = max(0, FREE_GENERATIONS_PER_DAY - today_gen)
        remaining_edit = max(0, FREE_EDITS_PER_DAY - today_edit)
        
        await message.answer(
            f"📊 *Твоя статистика*\n\n"
            f"🎨 Генераций: {remaining_gen} из {FREE_GENERATIONS_PER_DAY}\n"
            f"🖼 Редактирований: {remaining_edit} из {FREE_EDITS_PER_DAY}\n\n"
            f"Купи премиум за {PREMIUM_PRICE} ⭐: /buy",
            parse_mode="Markdown"
        )

@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    prices = [LabeledPrice(label=f"Безлимит {PREMIUM_DAYS} дней", amount=PREMIUM_PRICE)]
    
    await message.answer_invoice(
        title="SelenaArtBot Premium",
        description=f"Безлимитные генерации и редактирование на {PREMIUM_DAYS} дней!",
        payload="premium_30days",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="buy_premium"
    )

@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def payment_success(message: SuccessfulPayment):
    user_id = message.from_user.id
    await set_premium(user_id, days=30)
    await message.answer(
        "✅ *Премиум активирован!*\n\n"
        "Теперь у тебя безлимит на 30 дней!\n"
        "Генерируй и редактируй сколько хочешь 🎨",
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
    
    is_premium = await get_premium(user_id)
    today_used = await get_generations_today(user_id)
    
    if not is_premium and today_used >= FREE_GENERATIONS_PER_DAY:
        prices = [LabeledPrice(label="Одна генерация", amount=PRICE_GENERATION)]
        await message.answer_invoice(
            title="Генерация изображения",
            description=f"Запрос: {prompt[:80]}",
            payload=f"single_gen:{prompt}",
            provider_token="",
            currency="XTR",
            prices=prices
        )
        return
    
    if not is_premium:
        await incr_generations_today(user_id)
    
    status_msg = await message.answer(f"🎨 Рисую: {prompt[:50]}...")
    img = await generate_image(prompt)
    
    if img:
        photo = BufferedInputFile(img.getvalue(), filename="selena.jpg")
        await message.answer_photo(photo, caption=f"🌙 *{prompt[:100]}*", parse_mode="Markdown")
        await status_msg.delete()
    else:
        await status_msg.edit_text("❌ Ошибка генерации. Попробуй другой промпт")

# ===== РЕДАКТИРОВАНИЕ ФОТО =====
@dp.message(F.photo)
async def edit_photo(message: types.Message):
    user_id = message.from_user.id
    edit_prompt = message.caption
    
    if not edit_prompt:
        await message.answer("✏️ Напиши в подписи, что изменить на фото")
        return
    
    is_premium = await get_premium(user_id)
    today_used = await get_edits_today(user_id)
    
    if not is_premium and today_used >= FREE_EDITS_PER_DAY:
        prices = [LabeledPrice(label="Одно редактирование", amount=PRICE_EDIT)]
        await message.answer_invoice(
            title="Редактирование фото",
            description=edit_prompt[:80],
            payload=f"single_edit:{edit_prompt}",
            provider_token="",
            currency="XTR",
            prices=prices
        )
        return
    
    if not is_premium:
        await incr_edits_today(user_id)
    
    status_msg = await message.answer(f"🖼 Редактирую: {edit_prompt[:50]}...")
    
    try:
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = BytesIO()
        await bot.download_file(file.file_path, file_bytes)
        file_bytes.seek(0)
        
        edited = await edit_image(file_bytes, edit_prompt)
        
        if edited:
            photo = BufferedInputFile(edited.getvalue(), filename="edited.jpg")
            await message.answer_photo(photo, caption=f"✅ *{edit_prompt[:100]}*", parse_mode="Markdown")
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Ошибка редактирования")
    except Exception as e:
        logger.error(f"Edit error: {e}")
        await status_msg.edit_text("❌ Ошибка, попробуй ещё раз")

# ===== АДМИН-КОМАНДЫ =====
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для администратора")
        return
    
    await message.answer(
        "👑 *Админ-панель*\n\n"
        "/stats — статистика\n"
        "/users — список пользователей\n"
        "/prem [id] [дни] — выдать премиум\n"
        "/broadcast [текст] — рассылка",
        parse_mode="Markdown"
    )

@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    # Получаем всех пользователей из Redis
    keys = await redis_client.keys("selena:gen:*")
    users = set()
    for key in keys:
        parts = key.split(":")
        if len(parts) >= 3:
            users.add(parts[2])
    
    premium_keys = await redis_client.keys("selena:premium:*")
    
    await message.answer(f"👥 Пользователей: {len(users)}\n🌟 Премиум: {len(premium_keys)}")

@dp.message(Command("users"))
async def admin_users(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    keys = await redis_client.keys("selena:gen:*")
    users = set()
    for key in keys:
        parts = key.split(":")
        if len(parts) >= 3:
            users.add(parts[2])
    
    if not users:
        await message.answer("Нет пользователей")
        return
    
    user_list = list(users)[:30]
    await message.answer(f"👥 Пользователи:\n{', '.join(user_list)}")

@dp.message(Command("prem"))
async def admin_premium(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("❌ Использование: /prem user_id дни")
        return
    
    try:
        user_id = int(parts[1])
        days = int(parts[2])
        await set_premium(user_id, days)
        await message.answer(f"✅ Премиум выдан {user_id} на {days} дней")
    except:
        await message.answer("❌ Ошибка")

@dp.message(Command("broadcast"))
async def admin_broadcast(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("❌ Укажите текст рассылки")
        return
    
    keys = await redis_client.keys("selena:gen:*")
    users = set()
    for key in keys:
        parts = key.split(":")
        if len(parts) >= 3:
            users.add(int(parts[2]))
    
    await message.answer(f"📨 Рассылка для {len(users)} пользователей...")
    
    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 *Анонс от SelenaArtBot*\n\n{text}", parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)
        except:
            pass
    
    await message.answer(f"✅ Отправлено: {sent}")

# ===== ЗАПУСК ВЕБ-СЕРВЕРА ДЛЯ RENDER =====
async def root(request):
    return web.Response(text="SelenaArtBot is alive! 🎨")

async def ping(request):
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
    app.router.add_get("/ping", ping)
    app.router.add_get("/health", root)
    
    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
