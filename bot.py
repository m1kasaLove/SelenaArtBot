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

# ===== НОВЫЕ ЦЕНЫ (с учетом себестоимости 4 руб) =====
FREE_GENERATIONS_PER_DAY = 2      # Бесплатно 2 генерации в день
FREE_EDITS_PER_DAY = 1            # Бесплатно 1 редактирование в день

PRICE_1_GEN = 2                   # 1 генерация = 2 ⭐ (~20 руб)
PRICE_3_GEN = 5                   # 3 генерации = 5 ⭐ (экономия 1⭐)
PRICE_EDIT = 3                    # Редактирование = 3 ⭐

PREMIUM_PRICE = 20                # Безлимит 30 дней = 20 ⭐
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

async def get_pack_generations(user_id: int) -> int:
    """Сколько купленных генераций осталось в пакете"""
    val = await redis_client.get(f"selena:pack:{user_id}")
    return int(val) if val else 0

async def use_pack_generation(user_id: int) -> bool:
    """Использовать одну генерацию из пакета"""
    current = await get_pack_generations(user_id)
    if current > 0:
        await redis_client.decr(f"selena:pack:{user_id}")
        return True
    return False

async def add_pack_generations(user_id: int, count: int):
    """Добавить купленные генерации"""
    await redis_client.incrby(f"selena:pack:{user_id}", count)

# ===== ГЕНЕРАЦИЯ ЧЕРЕЗ POLZA.AI =====
async def generate_image(prompt: str) -> BytesIO | None:
    """Генерация через Polza.ai с правильным ожиданием"""
    
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
            # 1. Запускаем генерацию
            async with session.post("https://api.polza.ai/v1/media", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Polza start error {resp.status}: {error_text}")
                    return None
                data = await resp.json()
                task_id = data.get("id")
                logger.info(f"Task started: {task_id}")
            
            # 2. Ожидаем результат (до 60 секунд)
            for attempt in range(40):
                await asyncio.sleep(1.5)
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
                                        img_bytes = await img_resp.read()
                                        logger.info(f"Image received, size: {len(img_bytes)} bytes")
                                        return BytesIO(img_bytes)
                    elif status == "failed":
                        logger.error(f"Generation failed: {status_data}")
                        return None
                    else:
                        logger.info(f"Waiting for generation, status: {status}")
            
            logger.error("Generation timeout after 60 seconds")
            return None
            
        except Exception as e:
            logger.error(f"Polza generation error: {e}")
            return None

# ===== РЕДАКТИРОВАНИЕ =====
async def edit_image(image_bytes: BytesIO, prompt: str) -> BytesIO | None:
    """Редактирование через Polza.ai"""
    
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
        }
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://api.polza.ai/v1/media", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    logger.error(f"Polza edit start error: {resp.status}")
                    return None
                data = await resp.json()
                task_id = data.get("id")
            
            for attempt in range(40):
                await asyncio.sleep(1.5)
                async with session.get(f"https://api.polza.ai/v1/media/{task_id}", headers=headers) as status_resp:
                    status_data = await status_resp.json()
                    if status_data.get("status") == "completed":
                        images = status_data.get("output", {}).get("images", [])
                        if images:
                            image_url = images[0].get("url")
                            async with session.get(image_url) as img_resp:
                                img_bytes = await img_resp.read()
                                return BytesIO(img_bytes)
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
    pack = await get_pack_generations(user_id)
    
    menu = (
        "🎨 *SelenaArtBot* — твой AI-художник!\n\n"
        "✨ *Нейросеть:* Qwen/Image-2\n\n"
        "💰 *Цены (Telegram Stars):*\n"
        f"• {FREE_GENERATIONS_PER_DAY} генерации в день — *БЕСПЛАТНО*\n"
        f"• 1 генерация — {PRICE_1_GEN} ⭐\n"
        f"• 3 генерации — {PRICE_3_GEN} ⭐ (экономия)\n"
        f"• Безлимит {PREMIUM_DAYS} дней — {PREMIUM_PRICE} ⭐\n\n"
        f"📦 Купленных генераций: {pack}\n\n"
        "📝 *Команды:*\n"
        "/status — моя статистика\n"
        "/buy — купить безлимит\n"
        "/pack — купить 3 генерации\n"
        "/help — помощь\n\n"
        "⭐ 1 Star ≈ 10 рублей"
    )
    
    if is_premium:
        menu += "\n\n🌟 *У тебя ПРЕМИУМ!* Безлимит!"
    
    await message.answer(menu, parse_mode="Markdown")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 *Помощь*\n\n"
        "**Генерация:**\n"
        "Просто напиши описание картинки\n"
        "Пример: `кот в сапогах на закате`\n\n"
        "**Редактирование:**\n"
        "1. Отправь фото\n"
        "2. В подписи напиши изменения\n"
        "Пример: `сделай чёрно-белым`\n\n"
        f"**💰 Цены:**\n"
        f"• {FREE_GENERATIONS_PER_DAY} ген/день — бесплатно\n"
        f"• 1 ген — {PRICE_1_GEN} ⭐\n"
        f"• 3 ген — {PRICE_3_GEN} ⭐\n"
        f"• Безлимит — {PREMIUM_PRICE} ⭐\n\n"
        "**Команды:**\n"
        "/start — главное меню\n"
        "/status — статистика\n"
        "/buy — безлимит\n"
        "/pack — 3 генерации",
        parse_mode="Markdown"
    )

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    user_id = message.from_user.id
    is_premium = await get_premium(user_id)
    
    if is_premium:
        await message.answer("🌟 *Премиум активен!* Безлимитные генерации!", parse_mode="Markdown")
        return
    
    today_gen = await get_generations_today(user_id)
    remaining_free = max(0, FREE_GENERATIONS_PER_DAY - today_gen)
    pack = await get_pack_generations(user_id)
    
    await message.answer(
        f"📊 *Твоя статистика*\n\n"
        f"🎨 Бесплатных сегодня: {remaining_free} из {FREE_GENERATIONS_PER_DAY}\n"
        f"📦 Купленных генераций: {pack}\n\n"
        f"💫 *Купить:*\n"
        f"• /pack — 3 ген за {PRICE_3_GEN} ⭐\n"
        f"• /buy — безлимит за {PREMIUM_PRICE} ⭐",
        parse_mode="Markdown"
    )

@dp.message(Command("pack"))
async def cmd_pack(message: types.Message):
    """Пакет 3 генерации со скидкой"""
    prices = [LabeledPrice(label="3 генерации (эконом)", amount=PRICE_3_GEN)]
    
    await message.answer_invoice(
        title="🎨 Пакет 3 генерации",
        description=f"3 качественные картинки!\nЭкономия {PRICE_1_GEN} ⭐",
        payload="pack_3_generations",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="buy_pack"
    )

@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    """Безлимит на 30 дней"""
    prices = [LabeledPrice(label=f"Безлимит {PREMIUM_DAYS} дней", amount=PREMIUM_PRICE)]
    
    await message.answer_invoice(
        title="🌟 SelenaArtBot Premium",
        description=f"Безлимитные генерации на {PREMIUM_DAYS} дней!\nВсего {PREMIUM_PRICE} ⭐ — меньше 1⭐ в день!",
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
    payload = message.successful_payment.invoice_payload
    
    if payload == "premium_30days":
        await set_premium(user_id, days=30)
        await message.answer(
            "✅ *Премиум активирован!*\n\n"
            "Теперь у тебя безлимит на 30 дней!\n"
            "Генерируй сколько хочешь 🎨",
            parse_mode="Markdown"
        )
    elif payload == "pack_3_generations":
        await add_pack_generations(user_id, 3)
        await message.answer(
            "✅ *Куплено 3 генерации!*\n\n"
            "Просто напиши что нарисовать 🎨\n"
            "Пакетные генерации не сгорают и не зависят от дневного лимита.",
            parse_mode="Markdown"
        )
    elif payload.startswith("single_gen:"):
        prompt = payload.replace("single_gen:", "")
        await process_generation(message, prompt, is_paid=True)
    elif payload.startswith("single_edit:"):
        prompt = payload.replace("single_edit:", "")
        await message.answer(f"🖼 Обрабатываю платное редактирование...")

async def process_generation(message: types.Message, prompt: str, is_paid: bool = False):
    """Обработка генерации"""
    status_msg = await message.answer(f"🎨 *Генерирую:* {prompt[:50]}...\n⏳ Обычно 10-20 секунд", parse_mode="Markdown")
    
    img = await generate_image(prompt)
    
    if img:
        photo = BufferedInputFile(img.getvalue(), filename="selena.png")
        await message.answer_photo(
            photo, 
            caption=f"🎨 *{prompt[:100]}*\n✨ Qwen/Image-2",
            parse_mode="Markdown"
        )
        await status_msg.delete()
    else:
        await status_msg.edit_text(
            "❌ *Ошибка генерации*\n\n"
            "Попробуй:\n"
            "• Написать на английском\n"
            "• Более простой запрос\n"
            "• Пример: `cat with boots on sunset`\n\n"
            "Если ошибка повторяется — попробуй позже",
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
    
    # Проверяем премиум
    is_premium = await get_premium(user_id)
    if is_premium:
        await process_generation(message, prompt)
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
        # Предлагаем купить
        await message.answer(
            f"📊 *Лимит исчерпан!*\n\n"
            f"Сегодня ты использовал {FREE_GENERATIONS_PER_DAY} бесплатных генераций.\n\n"
            f"💰 *Купить:*\n"
            f"• /pack — 3 генерации за {PRICE_3_GEN} ⭐\n"
            f"• /buy — безлимит за {PREMIUM_PRICE} ⭐\n\n"
            f"Или завтра лимит обновится!",
            parse_mode="Markdown"
        )
        return
    
    # Используем бесплатную генерацию
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
            "• `добавь радугу`\n"
            "• `сделай фон розовым`",
            parse_mode="Markdown"
        )
        return
    
    # Проверяем премиум
    is_premium = await get_premium(user_id)
    if not is_premium:
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
    
    status_msg = await message.answer(f"🖼 *Редактирую:* {edit_prompt[:50]}...\n⏳ Подожди...", parse_mode="Markdown")
    
    try:
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = BytesIO()
        await bot.download_file(file.file_path, file_bytes)
        file_bytes.seek(0)
        
        edited = await edit_image(file_bytes, edit_prompt)
        
        if edited:
            photo = BufferedInputFile(edited.getvalue(), filename="edited.png")
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
    
    keys = await redis_client.keys("selena:gen:*")
    users = set()
    for key in keys:
        parts = key.split(":")
        if len(parts) >= 3:
            users.add(parts[2])
    
    premium_keys = await redis_client.keys("selena:premium:*")
    
    await message.answer(
        f"📊 *Статистика*\n\n"
        f"👥 Пользователей: {len(users)}\n"
        f"🌟 Премиум: {len(premium_keys)}",
        parse_mode="Markdown"
    )

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
