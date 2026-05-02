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

# ===== НОВЫЕ ЦЕНЫ =====
FREE_GENERATIONS_PER_DAY = 2      # Бесплатно 2 генерации в день
FREE_EDITS_PER_DAY = 1            # Бесплатно 1 редактирование в день

PRICE_1_GEN = 2                   # 1 генерация = 2 ⭐
PRICE_5_GEN = 8                   # 5 генераций = 8 ⭐
PRICE_10_GEN = 15                 # 10 генераций = 15 ⭐

PRICE_1_EDIT = 2                  # 1 редактирование = 2 ⭐
PRICE_5_EDIT = 8                  # 5 редактирований = 8 ⭐
PRICE_10_EDIT = 15                # 10 редактирований = 15 ⭐

# ОБЩИЙ ТАРИФ (генерации + редактирования)
PRICE_COMBO_5 = 12                # 3 генерации + 2 редактирования = 12 ⭐
PRICE_COMBO_10 = 20               # 6 генераций + 4 редактирования = 20 ⭐

# ===== ФУНКЦИИ REDIS =====
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
    val = await redis_client.get(f"selena:pack:gen:{user_id}")
    return int(val) if val else 0

async def get_pack_edits(user_id: int) -> int:
    """Сколько купленных редактирований осталось"""
    val = await redis_client.get(f"selena:pack:edit:{user_id}")
    return int(val) if val else 0

async def use_pack_generation(user_id: int) -> bool:
    current = await get_pack_generations(user_id)
    if current > 0:
        await redis_client.decr(f"selena:pack:gen:{user_id}")
        return True
    return False

async def use_pack_edit(user_id: int) -> bool:
    current = await get_pack_edits(user_id)
    if current > 0:
        await redis_client.decr(f"selena:pack:edit:{user_id}")
        return True
    return False

async def add_pack_generations(user_id: int, count: int):
    await redis_client.incrby(f"selena:pack:gen:{user_id}", count)

async def add_pack_edits(user_id: int, count: int):
    await redis_client.incrby(f"selena:pack:edit:{user_id}", count)

async def add_combo_pack(user_id: int, gens: int, edits: int):
    await add_pack_generations(user_id, gens)
    await add_pack_edits(user_id, edits)

# ===== ГЕНЕРАЦИЯ ЧЕРЕЗ POLZA.AI =====
async def generate_image(prompt: str) -> BytesIO | None:
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
                    logger.error(f"Edit start error: {resp.status}")
                    return None
                data = await resp.json()
                task_id = data.get("id")
                logger.info(f"Edit task: {task_id}")
            
            for attempt in range(30):
                await asyncio.sleep(2)
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
            logger.error(f"Edit error: {e}")
            return None

# ===== РЕКЛАМА LUNA BOT =====
async def send_luna_ad(message: types.Message):
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
    pack_gen = await get_pack_generations(user_id)
    pack_edit = await get_pack_edits(user_id)
    
    menu = (
        "🎨 *SelenaArtBot* — твой AI-художник!\n\n"
        "✨ *Нейросеть:* Qwen/Image-2\n\n"
        "💰 *Цены (Telegram Stars):*\n"
        f"• {FREE_GENERATIONS_PER_DAY} генерации в день — *БЕСПЛАТНО*\n"
        f"• {FREE_EDITS_PER_DAY} редактирование в день — *БЕСПЛАТНО*\n\n"
        "📦 *Пакеты:*\n"
        f"• 5 генераций — {PRICE_5_GEN} ⭐\n"
        f"• 10 генераций — {PRICE_10_GEN} ⭐\n"
        f"• 5 редактирований — {PRICE_5_EDIT} ⭐\n"
        f"• 10 редактирований — {PRICE_10_EDIT} ⭐\n\n"
        "🔥 *Комбо-пакеты (генерации + редактирования):*\n"
        f"• 3 ген + 2 ред — {PRICE_COMBO_5} ⭐\n"
        f"• 6 ген + 4 ред — {PRICE_COMBO_10} ⭐\n\n"
        f"📊 У тебя: {pack_gen} ген | {pack_edit} ред\n\n"
        "📝 *Команды:*\n"
        "/status — моя статистика\n"
        "/pack_gen5 — 5 генераций\n"
        "/pack_gen10 — 10 генераций\n"
        "/pack_edit5 — 5 редактирований\n"
        "/pack_edit10 — 10 редактирований\n"
        "/combo5 — комбо (3 ген + 2 ред)\n"
        "/combo10 — комбо (6 ген + 4 ред)\n"
        "/help — помощь\n\n"
        "⭐ 1 Star ≈ 10 рублей\n\n"
        "🌙 *Поболтать?* @LunaIsLovelyLunaBot"
    )
    
    await message.answer(menu, parse_mode="Markdown")
    await asyncio.sleep(5)
    await send_luna_ad(message)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 *Помощь*\n\n"
        "**Генерация:**\n"
        "Просто напиши описание картинки\n"
        "Пример: `кот в сапогах`\n\n"
        "**Редактирование:**\n"
        "1. Отправь фото\n"
        "2. В подписи напиши изменения\n"
        "Пример: `сделай чёрно-белым`\n\n"
        "**💰 Цены:**\n"
        f"• 2 ген/день — бесплатно\n"
        f"• 1 ред/день — бесплатно\n\n"
        f"**Команды:**\n"
        f"/status — статистика\n"
        f"/pack_gen5 — 5 ген за {PRICE_5_GEN}⭐\n"
        f"/pack_gen10 — 10 ген за {PRICE_10_GEN}⭐\n"
        f"/pack_edit5 — 5 ред за {PRICE_5_EDIT}⭐\n"
        f"/pack_edit10 — 10 ред за {PRICE_10_EDIT}⭐\n"
        f"/combo5 — 3 ген+2 ред за {PRICE_COMBO_5}⭐\n"
        f"/combo10 — 6 ген+4 ред за {PRICE_COMBO_10}⭐\n\n"
        "🌙 *Поболтать:* @LunaIsLovelyLunaBot",
        parse_mode="Markdown"
    )

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    user_id = message.from_user.id
    today_gen = await get_generations_today(user_id)
    remaining_gen = max(0, FREE_GENERATIONS_PER_DAY - today_gen)
    today_edit = await get_edits_today(user_id)
    remaining_edit = max(0, FREE_EDITS_PER_DAY - today_edit)
    pack_gen = await get_pack_generations(user_id)
    pack_edit = await get_pack_edits(user_id)
    
    await message.answer(
        f"📊 *Твоя статистика*\n\n"
        f"🎨 Бесплатных генераций: {remaining_gen} из {FREE_GENERATIONS_PER_DAY}\n"
        f"🖼 Бесплатных редактирований: {remaining_edit} из {FREE_EDITS_PER_DAY}\n\n"
        f"📦 Куплено: {pack_gen} генераций | {pack_edit} редактирований\n\n"
        f"💫 *Купить:*\n"
        f"• /pack_gen5 — 5 ген\n"
        f"• /pack_gen10 — 10 ген\n"
        f"• /pack_edit5 — 5 ред\n"
        f"• /pack_edit10 — 10 ред\n"
        f"• /combo5 — 3 ген + 2 ред\n"
        f"• /combo10 — 6 ген + 4 ред\n\n"
        f"🌙 *Поболтать:* @LunaIsLovelyLunaBot",
        parse_mode="Markdown"
    )

# ===== ПАКЕТЫ =====
@dp.message(Command("pack_gen5"))
async def cmd_pack_gen5(message: types.Message):
    prices = [LabeledPrice(label="5 генераций", amount=PRICE_5_GEN)]
    await message.answer_invoice(
        title="🎨 5 генераций",
        description=f"5 качественных картинок! Экономия {PRICE_1_GEN}⭐",
        payload="pack_5_generations",
        provider_token="",
        currency="XTR",
        prices=prices
    )

@dp.message(Command("pack_gen10"))
async def cmd_pack_gen10(message: types.Message):
    prices = [LabeledPrice(label="10 генераций", amount=PRICE_10_GEN)]
    await message.answer_invoice(
        title="🎨 10 генераций",
        description=f"10 качественных картинок! Экономия {PRICE_1_GEN * 2}⭐",
        payload="pack_10_generations",
        provider_token="",
        currency="XTR",
        prices=prices
    )

@dp.message(Command("pack_edit5"))
async def cmd_pack_edit5(message: types.Message):
    prices = [LabeledPrice(label="5 редактирований", amount=PRICE_5_EDIT)]
    await message.answer_invoice(
        title="🖼 5 редактирований",
        description=f"5 редактирований твоих фото!",
        payload="pack_5_edits",
        provider_token="",
        currency="XTR",
        prices=prices
    )

@dp.message(Command("pack_edit10"))
async def cmd_pack_edit10(message: types.Message):
    prices = [LabeledPrice(label="10 редактирований", amount=PRICE_10_EDIT)]
    await message.answer_invoice(
        title="🖼 10 редактирований",
        description=f"10 редактирований твоих фото!",
        payload="pack_10_edits",
        provider_token="",
        currency="XTR",
        prices=prices
    )

@dp.message(Command("combo5"))
async def cmd_combo5(message: types.Message):
    prices = [LabeledPrice(label="3 ген + 2 ред", amount=PRICE_COMBO_5)]
    await message.answer_invoice(
        title="🔥 Комбо-пакет 5",
        description=f"3 генерации + 2 редактирования! Экономия {PRICE_1_GEN + PRICE_1_EDIT}⭐",
        payload="combo_5",
        provider_token="",
        currency="XTR",
        prices=prices
    )

@dp.message(Command("combo10"))
async def cmd_combo10(message: types.Message):
    prices = [LabeledPrice(label="6 ген + 4 ред", amount=PRICE_COMBO_10)]
    await message.answer_invoice(
        title="🔥 Комбо-пакет 10",
        description=f"6 генераций + 4 редактирования! Экономия {PRICE_1_GEN * 2 + PRICE_1_EDIT * 2}⭐",
        payload="combo_10",
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
    payload = message.successful_payment.invoice_payload
    
    if payload == "pack_5_generations":
        await add_pack_generations(user_id, 5)
        await message.answer("✅ *Куплено 5 генераций!*\n\nПросто напиши что нарисовать 🎨", parse_mode="Markdown")
    elif payload == "pack_10_generations":
        await add_pack_generations(user_id, 10)
        await message.answer("✅ *Куплено 10 генераций!*", parse_mode="Markdown")
    elif payload == "pack_5_edits":
        await add_pack_edits(user_id, 5)
        await message.answer("✅ *Куплено 5 редактирований!*\n\nОтправь фото и напиши изменения 🖼", parse_mode="Markdown")
    elif payload == "pack_10_edits":
        await add_pack_edits(user_id, 10)
        await message.answer("✅ *Куплено 10 редактирований!*", parse_mode="Markdown")
    elif payload == "combo_5":
        await add_combo_pack(user_id, 3, 2)
        await message.answer("✅ *Куплено 3 генерации + 2 редактирования!*\n\nИспользуй их когда захочешь! 🎨🖼", parse_mode="Markdown")
    elif payload == "combo_10":
        await add_combo_pack(user_id, 6, 4)
        await message.answer("✅ *Куплено 6 генераций + 4 редактирования!*", parse_mode="Markdown")
    elif payload.startswith("single_gen:"):
        prompt = payload.replace("single_gen:", "")
        await process_generation(message, prompt, is_paid=True)

async def process_generation(message: types.Message, prompt: str, is_paid: bool = False):
    status_msg = await message.answer(f"🎨 *Генерирую:* {prompt[:50]}...\n⏳ 10-20 секунд", parse_mode="Markdown")
    
    img = await generate_image(prompt)
    
    if img:
        photo = BufferedInputFile(img.getvalue(), filename="selena.png")
        await message.answer_photo(photo, caption=f"🎨 *{prompt[:100]}*", parse_mode="Markdown")
        await status_msg.delete()
    else:
        await status_msg.edit_text(
            "❌ *Ошибка генерации*\n\n"
            "Попробуй написать на английском\n\n"
            "🌙 *Поболтать:* @LunaIsLovelyLunaBot",
            parse_mode="Markdown"
        )

async def process_edit(message: types.Message, image_bytes: BytesIO, prompt: str):
    status_msg = await message.answer(f"🖼 *Редактирую:* {prompt[:50]}...\n⏳ 10-20 секунд", parse_mode="Markdown")
    
    edited = await edit_image(image_bytes, prompt)
    
    if edited:
        photo = BufferedInputFile(edited.getvalue(), filename="edited.png")
        await message.answer_photo(photo, caption=f"✅ *{prompt[:100]}*", parse_mode="Markdown")
        await status_msg.delete()
    else:
        await status_msg.edit_text(
            "❌ *Ошибка редактирования*\n\n"
            "🌙 *Поболтать:* @LunaIsLovelyLunaBot",
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
    
    # Проверяем купленные пакеты
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
            f"• /pack_gen5 — 5 ген за {PRICE_5_GEN} ⭐\n"
            f"• /pack_gen10 — 10 ген за {PRICE_10_GEN} ⭐\n"
            f"• /combo5 — 3 ген + 2 ред за {PRICE_COMBO_5} ⭐\n\n"
            f"🌙 *Поболтать:* @LunaIsLovelyLunaBot",
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
    
    if len(edit_prompt) < 3:
        await message.answer("❌ Напиши подробнее, что изменить (минимум 3 символа)")
        return
    
    # Проверяем купленные пакеты
    pack = await get_pack_edits(user_id)
    if pack > 0:
        await use_pack_edit(user_id)
        
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = BytesIO()
        await bot.download_file(file.file_path, file_bytes)
        file_bytes.seek(0)
        
        await process_edit(message, file_bytes, edit_prompt)
        return
    
    # Проверяем бесплатные
    today_used = await get_edits_today(user_id)
    if today_used >= FREE_EDITS_PER_DAY:
        await message.answer(
            f"📊 *Лимит редактирований исчерпан!*\n\n"
            f"💰 *Купить:*\n"
            f"• /pack_edit5 — 5 ред за {PRICE_5_EDIT} ⭐\n"
            f"• /pack_edit10 — 10 ред за {PRICE_10_EDIT} ⭐\n"
            f"• /combo5 — 3 ген + 2 ред за {PRICE_COMBO_5} ⭐\n\n"
            f"🌙 *Поболтать:* @LunaIsLovelyLunaBot",
            parse_mode="Markdown"
        )
        return
    
    await incr_edits_today(user_id)
    
    file = await bot.get_file(message.photo[-1].file_id)
    file_bytes = BytesIO()
    await bot.download_file(file.file_path, file_bytes)
    file_bytes.seek(0)
    
    await process_edit(message, file_bytes, edit_prompt)

# ===== ЗАПУСК =====
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
