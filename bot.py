import asyncio
import logging
import os
import aiohttp
import base64
import random
import string
import uuid
from io import BytesIO
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    LabeledPrice, PreCheckoutQuery, SuccessfulPayment, BufferedInputFile,
    InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeDefault
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

import redis.asyncio as redis

# ================= CONFIG =================
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

# ================= PRICES =================
FREE_GENERATIONS_PER_DAY = 2
FREE_EDITS_PER_DAY = 1

PRICE_5_GEN = 8
PRICE_10_GEN = 15
PRICE_5_EDIT = 8
PRICE_10_EDIT = 15
PRICE_COMBO_5 = 12
PRICE_COMBO_10 = 20
PREMIUM_PRICE = 30
PREMIUM_DAYS = 30
REFERRAL_REWARD = 3

BOT_USERNAME = "SelenaArtBot"

# ================= SET MENU COMMANDS =================
async def set_commands():
    commands = [
        BotCommand(command="start", description="🎨 Главное меню"),
        BotCommand(command="status", description="📊 Моя статистика"),
        BotCommand(command="referral", description="🔥 Реферальная ссылка"),
        BotCommand(command="pack_gen5", description="🎨 5 генераций (8⭐)"),
        BotCommand(command="pack_gen10", description="🎨 10 генераций (15⭐)"),
        BotCommand(command="pack_edit5", description="🖼 5 редактирований (8⭐)"),
        BotCommand(command="pack_edit10", description="🖼 10 редактирований (15⭐)"),
        BotCommand(command="combo5", description="🔥 3 ген + 2 ред (12⭐)"),
        BotCommand(command="combo10", description="🔥 6 ген + 4 ред (20⭐)"),
        BotCommand(command="premium_buy", description="🌟 Безлимит (30⭐)"),
        BotCommand(command="help", description="❓ Помощь"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

# ================= REDIS HELPERS =================
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
    val = await redis_client.get(f"selena:pack:gen:{user_id}")
    return int(val) if val else 0

async def get_pack_edits(user_id: int) -> int:
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

# ================= PREMIUM =================
async def is_premium(user_id: int) -> bool:
    try:
        status = await redis_client.get(f"selena:premium:{user_id}")
        return status == "1"
    except:
        return False

async def set_premium(user_id: int, days: int = PREMIUM_DAYS):
    await redis_client.setex(f"selena:premium:{user_id}", days * 86400, "1")

async def remove_premium(user_id: int):
    await redis_client.delete(f"selena:premium:{user_id}")

# ================= REFERRAL =================
async def get_referral_code(user_id: int) -> str:
    code = await redis_client.get(f"selena:ref:code:{user_id}")
    if not code:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        await redis_client.setex(f"selena:ref:code:{user_id}", 86400 * 365, code)
    return code

async def get_referred_by(user_id: int) -> int:
    referrer = await redis_client.get(f"selena:ref:by:{user_id}")
    return int(referrer) if referrer else None

async def set_referred_by(user_id: int, referrer_id: int):
    await redis_client.set(f"selena:ref:by:{user_id}", referrer_id)

async def get_referral_count(user_id: int) -> int:
    val = await redis_client.get(f"selena:ref:count:{user_id}")
    return int(val) if val else 0

async def increment_referral_count(user_id: int):
    await redis_client.incr(f"selena:ref:count:{user_id}")

# ================= ENHANCE PROMPT =================
def enhance_prompt(prompt: str, is_edit: bool = False) -> str:
    if is_edit:
        preservation = (
            "CRITICAL: Keep the SAME people with IDENTICAL faces, body shapes, and poses. "
            "DO NOT change who they are. DO NOT replace them with different people. "
            "Change ONLY what is requested. Preserve the original identity completely. "
        )
    else:
        preservation = ""
    
    style = "ultra realistic, 4k, cinematic lighting, detailed, sharp focus, professional photography, high resolution"
    
    if is_edit:
        return f"{preservation} {style}: {prompt}"
    else:
        return f"{style}: {prompt}"

# ================= WATERMARK =================
async def add_watermark(image_bytes: BytesIO) -> BytesIO:
    image_bytes.seek(0)
    img = Image.open(image_bytes).convert("RGB")
    
    watermark = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(watermark)
    
    watermark_text = "SelenaArtBot"
    font_size = max(16, int(img.width / 35))
    
    try:
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        font = None
        for path in font_paths:
            if os.path.exists(path):
                font = ImageFont.truetype(path, font_size)
                break
        if font is None:
            font = ImageFont.load_default()
    except:
        font = ImageFont.load_default()
    
    bbox = draw.textbbox((0, 0), watermark_text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    x = img.width - text_width - 15
    y = img.height - text_height - 15
    
    draw.text((x + 2, y + 2), watermark_text, fill=(0, 0, 0, 100), font=font)
    draw.text((x, y), watermark_text, fill=(255, 255, 255, 180), font=font)
    
    img = img.convert("RGBA")
    img = Image.alpha_composite(img, watermark)
    
    result = Image.new('RGB', img.size, (255, 255, 255))
    result.paste(img, mask=img.split()[3])
    
    output = BytesIO()
    result.save(output, format="PNG", quality=90)
    output.seek(0)
    return output

# ================= SHARE BUTTON =================
def get_share_keyboard(image_id: str = None):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Поделиться результатом", callback_data="share")] if image_id else [],
        [InlineKeyboardButton(text="👥 Пригласить друга (+3 gen)", callback_data="referral_info")],
        [InlineKeyboardButton(text="📊 Мои рефералы", callback_data="my_referrals")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="close")]
    ])
    return keyboard

# ================= QWEN/IMAGE-2 (СТАБИЛЬНАЯ) =================
async def generate_with_qwen(prompt: str, reference_image: BytesIO = None, retry: bool = True) -> BytesIO | None:
    headers = {
        "Authorization": f"Bearer {POLZA_API_KEY}",
        "Content-Type": "application/json"
    }
    
    is_edit = reference_image is not None
    enhanced_prompt = enhance_prompt(prompt, is_edit=is_edit)
    
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
    
    if reference_image:
        reference_image.seek(0)
        img_base64 = base64.b64encode(reference_image.read()).decode('utf-8')
        payload["input"]["image"] = img_base64
        payload["input"]["strength"] = 0.7
        logger.info(f"[QWEN] 🖼 Редактирование: {prompt[:50]}")
    else:
        logger.info(f"[QWEN] 🎨 Генерация: {prompt[:50]}")
    
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        try:
            logger.info("[QWEN] Отправка запроса в Polza...")
            async with session.post("https://polza.ai/api/v1/media", headers=headers, json=payload) as resp:
                response_text = await resp.text()
                logger.info(f"[QWEN] Статус: {resp.status}")
                logger.info(f"[QWEN] Ответ: {response_text[:500]}")
                
                if resp.status != 200:
                    logger.error(f"[QWEN] Ошибка {resp.status}: {response_text[:200]}")
                    if retry:
                        await asyncio.sleep(2)
                        return await generate_with_qwen(prompt, reference_image, retry=False)
                    return None
                
                data = await resp.json()
                task_id = data.get("id")
                if not task_id:
                    logger.error(f"[QWEN] Нет ID задачи")
                    return None
                logger.info(f"[QWEN] Task ID: {task_id}")
            
            for attempt in range(60):
                await asyncio.sleep(2)
                
                async with session.get(f"https://polza.ai/api/v1/media/{task_id}", headers=headers) as resp:
                    if resp.status != 200:
                        continue
                    
                    status_data = await resp.json()
                    status = status_data.get("status")
                    logger.info(f"[QWEN] Попытка {attempt+1}/60, статус: {status}")
                    
                    if status == "completed":
                        image_url = None
                        
                        data_field = status_data.get("data", {})
                        if isinstance(data_field, dict):
                            image_url = data_field.get("url")
                        elif isinstance(data_field, list) and len(data_field) > 0:
                            image_url = data_field[0] if isinstance(data_field[0], str) else data_field[0].get("url")
                        
                        if not image_url:
                            output = status_data.get("output", {})
                            images = output.get("images", [])
                            if images:
                                image_url = images[0] if isinstance(images[0], str) else images[0].get("url")
                        
                        if image_url:
                            logger.info(f"[QWEN] Скачиваю...")
                            async with session.get(image_url) as img_resp:
                                if img_resp.status == 200:
                                    img_bytes = await img_resp.read()
                                    logger.info(f"[QWEN] ✅ Успех! Размер: {len(img_bytes)} байт")
                                    return BytesIO(img_bytes)
                        else:
                            logger.error(f"[QWEN] URL не найден")
                            if retry:
                                return await generate_with_qwen(prompt, reference_image, retry=False)
                            return None
                            
                    elif status == "failed":
                        error_msg = status_data.get("error", {}).get("message", "Unknown")
                        logger.error(f"[QWEN] ❌ Ошибка: {error_msg}")
                        if retry:
                            return await generate_with_qwen(prompt, reference_image, retry=False)
                        return None
            
            logger.error("[QWEN] ❌ Таймаут")
            return None
            
        except Exception as e:
            logger.error(f"[QWEN] Исключение: {e}")
            import traceback
            traceback.print_exc()
            if retry:
                return await generate_with_qwen(prompt, reference_image, retry=False)
            return None

# ================= generate_image и edit_image =================
async def generate_image(prompt: str) -> BytesIO | None:
    result = await generate_with_qwen(prompt)
    if result:
        return result
    logger.warning("[GEN] Qwen не ответил, пробуем fallback")
    return await generate_image_fallback(prompt)

async def edit_image(image_bytes: BytesIO, prompt: str) -> BytesIO | None:
    return await generate_with_qwen(prompt, reference_image=image_bytes)

async def generate_image_fallback(prompt: str) -> BytesIO | None:
    import urllib.parse
    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=30) as resp:
                if resp.status == 200:
                    img_data = await resp.read()
                    if len(img_data) > 5000:
                        logger.info(f"[FALLBACK] ✅ Успех!")
                        return BytesIO(img_data)
        except Exception as e:
            logger.error(f"[FALLBACK] Ошибка: {e}")
    return None

# ================= LUNA AD =================
async def send_luna_ad(message: types.Message):
    await message.answer(
        "🌙 *Хочешь живое общение?*\n\n"
        "Попробуй моего другого бота — **Луна**!\n"
        "👉 @LunaIsLovelyLunaBot\n\n"
        "Она ждёт тебя! 🌙",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

# ================= COMMANDS =================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    pack_gen = await get_pack_generations(user_id)
    pack_edit = await get_pack_edits(user_id)
    premium = await is_premium(user_id)
    
    # Обработка реферальной ссылки
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        referrer_code = args[1].replace("ref_", "")
        keys = await redis_client.keys("selena:ref:code:*")
        for key in keys:
            code = await redis_client.get(key)
            if code == referrer_code:
                referrer_id = int(key.split(":")[-1])
                if referrer_id != user_id and not await get_referred_by(user_id):
                    await set_referred_by(user_id, referrer_id)
                    await increment_referral_count(referrer_id)
                    await add_pack_generations(user_id, REFERRAL_REWARD)
                    
                    try:
                        await bot.send_message(
                            referrer_id, 
                            f"🎉 *По вашей ссылке пришёл новый пользователь!*\n\n"
                            f"👥 Приглашённый: {message.from_user.first_name}\n"
                            f"🎁 Вы получили +{REFERRAL_REWARD} генераций!\n\n"
                            f"Всего приглашено: {await get_referral_count(referrer_id)}",
                            parse_mode="Markdown"
                        )
                    except:
                        pass
                    
                    await message.answer(
                        f"🎉 *Вы получили +{REFERRAL_REWARD} генераций за регистрацию по ссылке!*\n\n"
                        f"🔥 Теперь у вас есть бонусные генерации.\n"
                        f"Просто напишите что нарисовать!",
                        parse_mode="Markdown"
                    )
                break
    
    share_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Пригласить друга (+3 gen)", callback_data="referral_info")]
    ])
    
    menu = (
        f"🎨 *SelenaArtBot* — твой AI-художник!\n\n"
        f"🤖 Модель: Qwen Image 2.0\n\n"
        f"📦 У тебя: {pack_gen} ген | {pack_edit} ред\n"
        f"🔥 Пригласи друга → +{REFERRAL_REWARD} генераций тебе и ему!\n\n"
        f"⚠️ *Важно:*\n"
        f"• Пишите чёткие запросы\n"
        f"• Для редактирования: «сохрани лица и позы»\n\n"
        f"📝 Команды в меню слева от смайлика\n\n"
        f"🌙 @LunaIsLovelyLunaBot"
    )
    
    if premium:
        menu = "🌟 *У тебя ПРЕМИУМ!* Безлимит!\n\n" + menu
    
    await message.answer(menu, parse_mode="Markdown", reply_markup=share_kb)
    await asyncio.sleep(3)
    await send_luna_ad(message)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 *Помощь*\n\n"
        "🤖 *Модель:* Qwen Image 2.0\n\n"
        "✨ *Советы для лучшего редактирования:*\n"
        "• Пиши детально: «Сохрани лица и позы»\n"
        "• Указывай что именно нужно изменить\n"
        "• Пример: «Оставь лица теми же, одень их в пляжную одежду»\n\n"
        "**Генерация:** напиши описание\n"
        "**Редактирование:** отправь фото + подпись\n\n"
        "**Команды в меню:**\n"
        "• /start — главное меню\n"
        "• /status — статистика\n"
        "• /referral — реферальная ссылка\n"
        "• /pack_gen5 — 5 ген (8⭐)\n"
        "• /pack_gen10 — 10 ген (15⭐)\n"
        "• /pack_edit5 — 5 ред (8⭐)\n"
        "• /pack_edit10 — 10 ред (15⭐)\n"
        "• /combo5 — 3 ген+2 ред (12⭐)\n"
        "• /combo10 — 6 ген+4 ред (20⭐)\n"
        "• /premium_buy — безлимит (30⭐)\n\n"
        "🌙 @LunaIsLovelyLunaBot",
        parse_mode="Markdown"
    )

@dp.message(Command("referral"))
async def cmd_referral(message: types.Message):
    user_id = message.from_user.id
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{await get_referral_code(user_id)}"
    count = await get_referral_count(user_id)
    
    await message.answer(
        f"🔥 *Реферальная программа*\n\n"
        f"👥 Приглашено друзей: {count}\n"
        f"🎁 За каждого: +{REFERRAL_REWARD} ген\n\n"
        f"🔗 Твоя ссылка:\n`{ref_link}`\n\n"
        f"Отправь ссылку другу → он получает +{REFERRAL_REWARD} ген → ты тоже!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=f"https://t.me/share/url?url={ref_link}&text=Привет! Попробуй SelenaArtBot — генератор картинок через ИИ!")]
        ])
    )

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    user_id = message.from_user.id
    premium = await is_premium(user_id)
    
    if premium:
        await message.answer("🌟 *У тебя ПРЕМИУМ!* Безлимит!", parse_mode="Markdown")
        return
    
    today_gen = await get_generations_today(user_id)
    remaining_gen = max(0, FREE_GENERATIONS_PER_DAY - today_gen)
    today_edit = await get_edits_today(user_id)
    remaining_edit = max(0, FREE_EDITS_PER_DAY - today_edit)
    pack_gen = await get_pack_generations(user_id)
    pack_edit = await get_pack_edits(user_id)
    ref_count = await get_referral_count(user_id)
    
    await message.answer(
        f"📊 *Статистика*\n\n"
        f"🎨 Бесплатных ген: {remaining_gen} из {FREE_GENERATIONS_PER_DAY}\n"
        f"🖼 Бесплатных ред: {remaining_edit} из {FREE_EDITS_PER_DAY}\n"
        f"📦 Куплено: {pack_gen} ген | {pack_edit} ред\n"
        f"👥 Приглашено друзей: {ref_count}\n"
        f"🎁 Получено бонусов: {ref_count * REFERRAL_REWARD} ген",
        parse_mode="Markdown"
    )

@dp.message(Command("premium_buy"))
async def cmd_premium_buy(message: types.Message):
    prices = [LabeledPrice(label=f"Безлимит {PREMIUM_DAYS} дней", amount=PREMIUM_PRICE)]
    await message.answer_invoice(
        title="🌟 SelenaArtBot Premium",
        description=f"Безлимитные генерации и редактирования на {PREMIUM_DAYS} дней!",
        payload="premium_purchase",
        provider_token="",
        currency="XTR",
        prices=prices
    )

@dp.message(Command("pack_gen5"))
async def cmd_pack_gen5(message: types.Message):
    prices = [LabeledPrice(label="5 генераций", amount=PRICE_5_GEN)]
    await message.answer_invoice(title="🎨 5 генераций", description="5 качественных картинок!", payload="pack_5_generations", provider_token="", currency="XTR", prices=prices)

@dp.message(Command("pack_gen10"))
async def cmd_pack_gen10(message: types.Message):
    prices = [LabeledPrice(label="10 генераций", amount=PRICE_10_GEN)]
    await message.answer_invoice(title="🎨 10 генераций", description="10 качественных картинок!", payload="pack_10_generations", provider_token="", currency="XTR", prices=prices)

@dp.message(Command("pack_edit5"))
async def cmd_pack_edit5(message: types.Message):
    prices = [LabeledPrice(label="5 редактирований", amount=PRICE_5_EDIT)]
    await message.answer_invoice(title="🖼 5 редактирований", description="5 редактирований твоих фото!", payload="pack_5_edits", provider_token="", currency="XTR", prices=prices)

@dp.message(Command("pack_edit10"))
async def cmd_pack_edit10(message: types.Message):
    prices = [LabeledPrice(label="10 редактирований", amount=PRICE_10_EDIT)]
    await message.answer_invoice(title="🖼 10 редактирований", description="10 редактирований твоих фото!", payload="pack_10_edits", provider_token="", currency="XTR", prices=prices)

@dp.message(Command("combo5"))
async def cmd_combo5(message: types.Message):
    prices = [LabeledPrice(label="3 ген + 2 ред", amount=PRICE_COMBO_5)]
    await message.answer_invoice(title="🔥 Комбо 5", description="3 генерации + 2 редактирования!", payload="combo_5", provider_token="", currency="XTR", prices=prices)

@dp.message(Command("combo10"))
async def cmd_combo10(message: types.Message):
    prices = [LabeledPrice(label="6 ген + 4 ред", amount=PRICE_COMBO_10)]
    await message.answer_invoice(title="🔥 Комбо 10", description="6 генераций + 4 редактирования!", payload="combo_10", provider_token="", currency="XTR", prices=prices)

@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def payment_success(message: SuccessfulPayment):
    user_id = message.from_user.id
    payload = message.successful_payment.invoice_payload
    
    if payload == "premium_purchase":
        await set_premium(user_id, PREMIUM_DAYS)
        await message.answer("✅ *Премиум активирован!*", parse_mode="Markdown")
    elif payload == "pack_5_generations":
        await add_pack_generations(user_id, 5)
        await message.answer("✅ *Куплено 5 генераций!*", parse_mode="Markdown")
    elif payload == "pack_10_generations":
        await add_pack_generations(user_id, 10)
        await message.answer("✅ *Куплено 10 генераций!*", parse_mode="Markdown")
    elif payload == "pack_5_edits":
        await add_pack_edits(user_id, 5)
        await message.answer("✅ *Куплено 5 редактирований!*", parse_mode="Markdown")
    elif payload == "pack_10_edits":
        await add_pack_edits(user_id, 10)
        await message.answer("✅ *Куплено 10 редактирований!*", parse_mode="Markdown")
    elif payload == "combo_5":
        await add_combo_pack(user_id, 3, 2)
        await message.answer("✅ *Куплено 3 ген + 2 ред!*", parse_mode="Markdown")
    elif payload == "combo_10":
        await add_combo_pack(user_id, 6, 4)
        await message.answer("✅ *Куплено 6 ген + 4 ред!*", parse_mode="Markdown")

# ================= PROCESS =================
async def process_generation(message: types.Message, prompt: str):
    status_msg = await message.answer(f"🎨 *Генерирую (Qwen 2.0):* {prompt[:50]}...\n⏳ 10-30 секунд", parse_mode="Markdown")
    
    img = await generate_image(prompt)
    
    if img:
        try:
            watermarked = await add_watermark(img)
            photo = BufferedInputFile(watermarked.getvalue(), filename="selena.png")
            image_id = str(uuid.uuid4())[:8]
            await redis_client.setex(f"selena:share:{image_id}", 3600, prompt)
            
            for attempt in range(3):
                try:
                    await message.answer_photo(
                        photo, 
                        caption=f"🎨 *{prompt[:100]}*\n🤖 Qwen Image 2.0 | SelenaArtBot",
                        parse_mode="Markdown",
                        reply_markup=get_share_keyboard(image_id),
                        timeout=60
                    )
                    await status_msg.delete()
                    break
                except asyncio.TimeoutError:
                    logger.warning(f"Таймаут при отправке, попытка {attempt+1}/3")
                    if attempt == 2:
                        await status_msg.edit_text("❌ *Ошибка отправки фото*\n\nПопробуй позже", parse_mode="Markdown")
                    await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Ошибка при отправке: {e}")
            await status_msg.edit_text("❌ *Ошибка при обработке картинки*", parse_mode="Markdown")
    else:
        await status_msg.edit_text(
            "❌ *Ошибка генерации*\n\n"
            "Попробуй написать на английском\n\n"
            "🌙 @LunaIsLovelyLunaBot",
            parse_mode="Markdown"
        )

async def process_edit(message: types.Message, image_bytes: BytesIO, prompt: str):
    status_msg = await message.answer(f"🖼 *Редактирую (Qwen 2.0):* {prompt[:50]}...\n⏳ 10-30 секунд", parse_mode="Markdown")
    
    edited = await edit_image(image_bytes, prompt)
    
    if edited:
        try:
            watermarked = await add_watermark(edited)
            photo = BufferedInputFile(watermarked.getvalue(), filename="edited.png")
            image_id = str(uuid.uuid4())[:8]
            await redis_client.setex(f"selena:share:{image_id}", 3600, prompt)
            
            for attempt in range(3):
                try:
                    await message.answer_photo(
                        photo, 
                        caption=f"✅ *Отредактировано!*\n📝 {prompt[:100]}\n🤖 Qwen Image 2.0 | SelenaArtBot",
                        parse_mode="Markdown",
                        reply_markup=get_share_keyboard(image_id),
                        timeout=60
                    )
                    await status_msg.delete()
                    break
                except asyncio.TimeoutError:
                    logger.warning(f"Таймаут при отправке, попытка {attempt+1}/3")
                    if attempt == 2:
                        await status_msg.edit_text("❌ *Ошибка отправки фото*\n\nПопробуй позже", parse_mode="Markdown")
                    await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Ошибка при отправке: {e}")
            await status_msg.edit_text("❌ *Ошибка при обработке картинки*", parse_mode="Markdown")
    else:
        await status_msg.edit_text(
            "❌ *Ошибка редактирования*\n\n"
            "Попробуй:\n"
            "• `сделай чёрно-белым`\n"
            "• `увеличь контраст`\n"
            "• Напиши детальнее: «сохрани лица и позы»\n\n"
            "🌙 @LunaIsLovelyLunaBot",
            parse_mode="Markdown"
        )

# ================= TEXT HANDLER =================
@dp.message(F.text & ~F.text.startswith('/'))
async def generate_by_text(message: types.Message):
    user_id = message.from_user.id
    prompt = message.text.strip()
    
    if len(prompt) < 3:
        await message.answer("❌ Напиши подробнее (минимум 3 символа)")
        return
    
    premium = await is_premium(user_id)
    if premium:
        await process_generation(message, prompt)
        return
    
    pack = await get_pack_generations(user_id)
    if pack > 0:
        await use_pack_generation(user_id)
        await process_generation(message, prompt)
        return
    
    today_used = await get_generations_today(user_id)
    if today_used >= FREE_GENERATIONS_PER_DAY:
        await message.answer(f"📊 *Лимит исчерпан!*\n💰 /pack_gen5 — 5 ген за {PRICE_5_GEN}⭐\n🔥 /referral — пригласи друга, получи +3 ген", parse_mode="Markdown")
        return
    
    await incr_generations_today(user_id)
    await process_generation(message, prompt)

# ================= PHOTO HANDLER =================
@dp.message(F.photo)
async def edit_photo(message: types.Message):
    user_id = message.from_user.id
    edit_prompt = message.caption
    
    if not edit_prompt:
        await message.answer("✏️ *Напиши в подписи, что изменить на фото*", parse_mode="Markdown")
        return
    
    if len(edit_prompt) < 3:
        await message.answer("❌ Напиши подробнее, что изменить (минимум 3 символа)")
        return
    
    premium = await is_premium(user_id)
    if premium:
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = BytesIO()
        await bot.download_file(file.file_path, file_bytes)
        file_bytes.seek(0)
        await process_edit(message, file_bytes, edit_prompt)
        return
    
    pack = await get_pack_edits(user_id)
    if pack > 0:
        await use_pack_edit(user_id)
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = BytesIO()
        await bot.download_file(file.file_path, file_bytes)
        file_bytes.seek(0)
        await process_edit(message, file_bytes, edit_prompt)
        return
    
    today_used = await get_edits_today(user_id)
    if today_used >= FREE_EDITS_PER_DAY:
        await message.answer(f"📊 *Лимит редактирований исчерпан!*\n💰 /pack_edit5 — 5 ред за {PRICE_5_EDIT}⭐\n🌟 /premium_buy — безлимит", parse_mode="Markdown")
        return
    
    await incr_edits_today(user_id)
    file = await bot.get_file(message.photo[-1].file_id)
    file_bytes = BytesIO()
    await bot.download_file(file.file_path, file_bytes)
    file_bytes.seek(0)
    await process_edit(message, file_bytes, edit_prompt)

# ================= CALLBACKS =================
@dp.callback_query(lambda c: c.data == "referral_info")
async def referral_info(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{await get_referral_code(user_id)}"
    count = await get_referral_count(user_id)
    
    await callback.message.delete()
    
    await callback.message.answer(
        f"🔥 *Реферальная программа*\n\n"
        f"👥 Приглашено друзей: {count}\n"
        f"🎁 За каждого: +{REFERRAL_REWARD} ген\n"
        f"🎉 Получено бонусов: {count * REFERRAL_REWARD} ген\n\n"
        f"🔗 *Твоя реферальная ссылка:*\n"
        f"`{ref_link}`\n\n"
        f"📤 Отправь ссылку другу → он получает +{REFERRAL_REWARD} ген → ты тоже!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=f"https://t.me/share/url?url={ref_link}&text=Привет! Попробуй SelenaArtBot — генератор картинок через ИИ!")],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_start")]
        ])
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "back_to_start")
async def back_to_start(callback: types.CallbackQuery):
    await callback.message.delete()
    await cmd_start(callback.message)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "my_referrals")
async def my_referrals(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    count = await get_referral_count(user_id)
    
    await callback.message.delete()
    
    await callback.message.answer(
        f"📊 *Мои рефералы*\n\n"
        f"👥 Приглашено друзей: {count}\n"
        f"🎁 Получено бонусов: {count * REFERRAL_REWARD} генераций\n\n"
        f"🔥 Каждый новый друг → +{REFERRAL_REWARD} ген тебе и ему!\n"
        f"Продолжай приглашать — бонусы безлимитны!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_start")]
        ])
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    await callback.message.delete()
    await cmd_start(callback.message)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "close")
async def close_message(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.answer()

@dp.callback_query(lambda c: c.data == "share")
async def share_image(callback: types.CallbackQuery):
    await callback.answer("📤 Отправь этот результат другу!", show_alert=True)

# ================= ADMIN COMMANDS =================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "👑 *Админ-панель*\n\n"
        "/stats — статистика\n"
        "/users — список пользователей\n"
        "/premium [id] [дни] — выдать премиум\n"
        "/rmpremium [id] — снять премиум\n"
        "/add_gen [id] [кол-во] — добавить генерации\n"
        "/add_edit [id] [кол-во] — добавить редактирования\n"
        "/broadcast [текст] — рассылка\n"
        "/stars — баланс Stars",
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
    await message.answer(f"📊 *Статистика*\n\n👥 Пользователей: {len(users)}\n🌟 Премиум: {len(premium_keys)}", parse_mode="Markdown")

@dp.message(Command("users"))
async def admin_users(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    keys = await redis_client.keys("selena:pack:gen:*")
    users_data = []
    for key in keys:
        parts = key.split(":")
        if len(parts) >= 4:
            user_id = parts[3]
            gens = await get_pack_generations(int(user_id))
            edits = await get_pack_edits(int(user_id))
            users_data.append(f"`{user_id}` — {gens} ген, {edits} ред")
    if not users_data:
        await message.answer("Нет пользователей с пакетами")
        return
    text = "👥 *Пользователи с пакетами:*\n\n" + "\n".join(users_data[:30])
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("premium"))
async def admin_premium(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        return
    try:
        user_id = int(parts[1])
        days = int(parts[2]) if len(parts) > 2 else PREMIUM_DAYS
        await set_premium(user_id, days)
        await message.answer(f"✅ Премиум выдан {user_id} на {days} дней")
    except:
        pass

@dp.message(Command("rmpremium"))
async def admin_rmpremium(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        return
    try:
        user_id = int(parts[1])
        await remove_premium(user_id)
        await message.answer(f"✅ Премиум снят с {user_id}")
    except:
        pass

@dp.message(Command("add_gen"))
async def admin_add_gen(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        return
    try:
        user_id = int(parts[1])
        count = int(parts[2])
        await add_pack_generations(user_id, count)
        await message.answer(f"✅ Добавлено {count} ген пользователю {user_id}")
    except:
        pass

@dp.message(Command("add_edit"))
async def admin_add_edit(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        return
    try:
        user_id = int(parts[1])
        count = int(parts[2])
        await add_pack_edits(user_id, count)
        await message.answer(f"✅ Добавлено {count} ред пользователю {user_id}")
    except:
        pass

@dp.message(Command("broadcast"))
async def admin_broadcast(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        return
    keys = await redis_client.keys("selena:gen:*")
    users = set()
    for key in keys:
        parts = key.split(":")
        if len(parts) >= 3:
            users.add(int(parts[2]))
    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 *Анонс от SelenaArtBot*\n\n{text}", parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(f"✅ Отправлено: {sent}")

@dp.message(Command("stars"))
async def admin_stars(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getStarBalance") as resp:
                data = await resp.json()
                if data.get("ok"):
                    stars = data.get("result", {}).get("balance", 0)
                    await message.answer(f"⭐ *Баланс Stars бота:* {stars}", parse_mode="Markdown")
                else:
                    await message.answer("❌ Ошибка")
    except:
        pass

# ================= STARTUP =================
async def root(request):
    return web.Response(text="SelenaArtBot is alive! 🎨")

async def health(request):
    return web.Response(text="OK")

async def on_startup(app):
    global redis_client
    redis_client = await redis.from_url(REDIS_URL, decode_responses=True)
    logger.info("✅ Redis connected")
    await set_commands()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"✅ Webhook set: {WEBHOOK_URL}")
    logger.info("✅ Menu commands set")

async def on_shutdown(app):
    if redis_client:
        await redis_client.aclose()
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
