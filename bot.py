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

# ================= КОНСТАНТЫ АНТИФРОДА =================
REFERRAL_PENDING = "pending"
REFERRAL_ACTIVE = "active"
REFERRAL_REWARDED = "rewarded"

MAX_REFERRALS_PER_HOUR = 5
MAX_REFERRALS_PER_DAY = 20
MIN_TIME_BETWEEN_REFERRALS = 60

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

async def remove_pack_generations(user_id: int, count: int):
    current = await get_pack_generations(user_id)
    if current > 0:
        new_count = max(0, current - count)
        await redis_client.set(f"selena:pack:gen:{user_id}", new_count)
        return new_count
    return 0

async def remove_pack_edits(user_id: int, count: int):
    current = await get_pack_edits(user_id)
    if current > 0:
        new_count = max(0, current - count)
        await redis_client.set(f"selena:pack:edit:{user_id}", new_count)
        return new_count
    return 0

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

async def decrement_referral_count(user_id: int, count: int = 1):
    current = await get_referral_count(user_id)
    new_count = max(0, current - count)
    await redis_client.set(f"selena:ref:count:{user_id}", new_count)
    return new_count

# ================= СТАТУСЫ РЕФЕРАЛОВ =================
async def get_referral_status(user_id: int, referrer_id: int = None) -> str:
    if referrer_id:
        key = f"selena:ref:status:{referrer_id}:{user_id}"
    else:
        keys = await redis_client.keys(f"selena:ref:status:*:{user_id}")
        if not keys:
            return None
        key = keys[0]
    return await redis_client.get(key)

async def set_referral_status(user_id: int, referrer_id: int, status: str):
    key = f"selena:ref:status:{referrer_id}:{user_id}"
    await redis_client.setex(key, 86400 * 30, status)

async def add_pending_referral(user_id: int, referrer_id: int) -> bool:
    existing_status = await get_referral_status(user_id, referrer_id)
    if existing_status:
        return False
    
    await set_referral_status(user_id, referrer_id, REFERRAL_PENDING)
    await set_referred_by(user_id, referrer_id)
    await log_referral(referrer_id, user_id, "pending", "Ожидает активации")
    return True

async def activate_referral(user_id: int) -> bool:
    referrer_id = await get_referred_by(user_id)
    if not referrer_id:
        return False
    
    status = await get_referral_status(user_id, referrer_id)
    
    if status in [REFERRAL_ACTIVE, REFERRAL_REWARDED]:
        return False
    
    if status is None or status == REFERRAL_PENDING:
        await set_referral_status(user_id, referrer_id, REFERRAL_ACTIVE)
        await log_referral(referrer_id, user_id, "active", "Пользователь активен")
        return True
    
    return False

async def reward_referral(user_id: int, by_action: bool = True) -> bool:
    referrer_id = await get_referred_by(user_id)
    if not referrer_id:
        return False
    
    status = await get_referral_status(user_id, referrer_id)
    
    if status == REFERRAL_REWARDED:
        return False
    
    if by_action and status in [REFERRAL_ACTIVE, REFERRAL_PENDING]:
        limits_ok, limit_msg = await check_referral_limits(referrer_id)
        if not limits_ok:
            await log_referral(referrer_id, user_id, "rejected", f"Лимиты: {limit_msg}")
            return False
        
        last_reward = await redis_client.get(f"selena:ref:last_reward:{referrer_id}")
        if last_reward:
            time_diff = datetime.now().timestamp() - float(last_reward)
            if time_diff < MIN_TIME_BETWEEN_REFERRALS:
                await log_referral(referrer_id, user_id, "rejected", f"Слишком быстро: {time_diff:.0f} сек")
                return False
        
        await add_pack_generations(referrer_id, REFERRAL_REWARD)
        await increment_referral_limits(referrer_id)
        await increment_referral_count(referrer_id)
        await redis_client.setex(f"selena:ref:last_reward:{referrer_id}", 3600, datetime.now().timestamp())
        await set_referral_status(user_id, referrer_id, REFERRAL_REWARDED)
        await log_referral(referrer_id, user_id, "rewarded", f"Бонус +{REFERRAL_REWARD} ген")
        
        try:
            total_refs = await get_referral_count(referrer_id)
            await bot.send_message(
                referrer_id,
                f"🎉 *Получен бонус за реферала!*\n\n"
                f"👥 Ваш друг завершил первое действие в боте\n"
                f"🎁 Вы получили +{REFERRAL_REWARD} генераций!\n"
                f"📊 Всего приглашено: {total_refs}",
                parse_mode="Markdown"
            )
        except:
            pass
        
        return True
    
    return False

async def get_referral_stats(user_id: int) -> dict:
    stats = {
        "pending": 0,
        "active": 0,
        "rewarded": 0,
        "total": 0
    }
    
    keys = await redis_client.keys(f"selena:ref:status:{user_id}:*")
    for key in keys:
        status = await redis_client.get(key)
        if status in stats:
            stats[status] += 1
            stats["total"] += 1
    
    return stats

# ================= ПРОВЕРКА ЛИМИТОВ =================
async def check_referral_limits(referrer_id: int) -> tuple[bool, str]:
    day_key = datetime.now().strftime("%Y-%m-%d")
    hour_key = datetime.now().strftime("%Y-%m-%d-%H")
    
    daily_count = await redis_client.get(f"selena:ref:rewarded:daily:{referrer_id}:{day_key}")
    daily_count = int(daily_count) if daily_count else 0
    if daily_count >= MAX_REFERRALS_PER_DAY:
        return False, f"Дневной лимит ({MAX_REFERRALS_PER_DAY})"
    
    hourly_count = await redis_client.get(f"selena:ref:rewarded:hourly:{referrer_id}:{hour_key}")
    hourly_count = int(hourly_count) if hourly_count else 0
    if hourly_count >= MAX_REFERRALS_PER_HOUR:
        return False, f"Часовой лимит ({MAX_REFERRALS_PER_HOUR})"
    
    return True, "OK"

async def increment_referral_limits(referrer_id: int):
    day_key = datetime.now().strftime("%Y-%m-%d")
    hour_key = datetime.now().strftime("%Y-%m-%d-%H")
    
    await redis_client.incr(f"selena:ref:rewarded:daily:{referrer_id}:{day_key}")
    await redis_client.incr(f"selena:ref:rewarded:hourly:{referrer_id}:{hour_key}")
    await redis_client.expire(f"selena:ref:rewarded:daily:{referrer_id}:{day_key}", 86400)
    await redis_client.expire(f"selena:ref:rewarded:hourly:{referrer_id}:{hour_key}", 3600)

# ================= ЛОГИРОВАНИЕ =================
async def log_referral(referrer_id: int, new_user_id: int, status: str, reason: str = ""):
    log_key = f"selena:ref:log:{referrer_id}"
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "new_user_id": new_user_id,
        "status": status,
        "reason": reason
    }
    await redis_client.lpush(log_key, str(log_entry))
    await redis_client.ltrim(log_key, 0, 99)

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
    rows = []

    if image_id:
        rows.append([
            InlineKeyboardButton(text="🔥 Поделиться результатом", callback_data="share")
        ])

    rows.append([
        InlineKeyboardButton(text="👥 Пригласить друга (+3 gen)", callback_data="referral_info")
    ])

    rows.append([
        InlineKeyboardButton(text="📊 Мои рефералы", callback_data="my_referrals")
    ])

    rows.append([
        InlineKeyboardButton(text="❌ Закрыть", callback_data="close")
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)

# ================= FLUX.2-PRO =================
async def generate_with_flux(prompt: str, reference_image: BytesIO = None, retry: bool = True) -> BytesIO | None:
    headers = {
        "Authorization": f"Bearer {POLZA_API_KEY}",
        "Content-Type": "application/json"
    }

    is_edit = reference_image is not None
    enhanced_prompt = enhance_prompt(prompt, is_edit=is_edit)

    # 🔥 ВАЖНО: STRICT MODE Polza FLUX (без width/height)
    payload = {
        "model": "black-forest-labs/flux.2-pro",
        "input": {
            "prompt": enhanced_prompt,
            "aspect_ratio": "1:1",
            "output_format": "png"
        },
        "async": True
    }

    # 🖼 редактирование (image-to-image)
    if reference_image:
        reference_image.seek(0)
        img_base64 = base64.b64encode(reference_image.read()).decode("utf-8")

        # ⚠️ Polza чаще ожидает images[], а не image
        payload["input"]["images"] = [img_base64]
        payload["input"]["strength"] = 0.65

        logger.info(f"[FLUX] 🖼 Редактирование: {prompt[:50]}")
    else:
        logger.info(f"[FLUX] 🎨 Генерация: {prompt[:50]}")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        try:
            logger.info("[FLUX] Отправка запроса в Polza...")

            async with session.post(
                "https://polza.ai/api/v1/media",
                headers=headers,
                json=payload
            ) as resp:

                response_text = await resp.text()
                logger.info(f"[FLUX] Статус: {resp.status}")
                logger.info(f"[FLUX] Ответ: {response_text[:500]}")

                if resp.status != 200:
                    logger.error(f"[FLUX] Ошибка {resp.status}: {response_text[:200]}")
                    if retry:
                        await asyncio.sleep(2)
                        return await generate_with_flux(prompt, reference_image, retry=False)
                    return None

                data = await resp.json()
                task_id = data.get("id")

                if not task_id:
                    logger.error("[FLUX] Нет ID задачи")
                    return None

                logger.info(f"[FLUX] Task ID: {task_id}")

            # 🔄 polling
            for attempt in range(60):
                await asyncio.sleep(2)

                async with session.get(
                    f"https://polza.ai/api/v1/media/{task_id}",
                    headers=headers
                ) as resp:

                    if resp.status != 200:
                        continue

                    status_data = await resp.json()
                    status = status_data.get("status")

                    logger.info(f"[FLUX] Попытка {attempt+1}/60, статус: {status}")

                    if status == "completed":
                        image_url = None

                        data_field = status_data.get("data")

                        if isinstance(data_field, str):
                            image_url = data_field
                        elif isinstance(data_field, dict):
                            image_url = data_field.get("url")
                        elif isinstance(data_field, list) and data_field:
                            item = data_field[0]
                            image_url = item if isinstance(item, str) else item.get("url")

                        if not image_url:
                            output = status_data.get("output", {})
                            images = output.get("images", [])
                            if images:
                                item = images[0]
                                image_url = item if isinstance(item, str) else item.get("url")

                        if image_url:
                            logger.info("[FLUX] Скачиваю изображение...")

                            async with session.get(image_url) as img_resp:
                                if img_resp.status == 200:
                                    img_bytes = await img_resp.read()
                                    logger.info(f"[FLUX] ✅ Успех: {len(img_bytes)} байт")
                                    return BytesIO(img_bytes)

                        logger.error("[FLUX] URL не найден")
                        return None

                    elif status == "failed":
                        error_msg = status_data.get("error", {}).get("message", "Unknown")
                        logger.error(f"[FLUX] ❌ Failed: {error_msg}")

                        if retry:
                            return await generate_with_flux(prompt, reference_image, retry=False)
                        return None

            logger.error("[FLUX] ❌ Timeout")
            return None

        except Exception as e:
            logger.error(f"[FLUX] Exception: {e}")
            import traceback
            traceback.print_exc()

            if retry:
                return await generate_with_flux(prompt, reference_image, retry=False)
            return None

# ================= generate_image и edit_image =================
async def generate_image(prompt: str) -> BytesIO | None:
    result = await generate_with_flux(prompt)
    if result:
        return result
    logger.warning("[GEN] FLUX не ответил, пробуем fallback")
    return await generate_image_fallback(prompt)

async def edit_image(image_bytes: BytesIO, prompt: str) -> BytesIO | None:
    return await generate_with_flux(prompt, reference_image=image_bytes)

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
    
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        referrer_code = args[1].replace("ref_", "")
        keys = await redis_client.keys("selena:ref:code:*")
        for key in keys:
            code = await redis_client.get(key)
            if code == referrer_code:
                referrer_id = int(key.split(":")[-1])
                
                if referrer_id == user_id:
                    await log_referral(referrer_id, user_id, "rejected", "Самореферал")
                    break
                
                existing_referrer = await get_referred_by(user_id)
                if existing_referrer:
                    await log_referral(referrer_id, user_id, "rejected", "Уже имеет реферала")
                    break
                
                await add_pending_referral(user_id, referrer_id)
                
                await message.answer(
                    f"🎉 *Вы зарегистрировались по реферальной ссылке!*\n\n"
                    f"🔥 Чтобы активировать бонус, просто начните пользоваться ботом:\n"
                    f"• Сгенерируйте картинку\n"
                    f"• Или отредактируйте фото\n\n"
                    f"После первого действия бонус будет начислен вам и вашему другу!\n\n"
                    f"💰 Бонус: +{REFERRAL_REWARD} генераций",
                    parse_mode="Markdown"
                )
                break
    
    ref_stats = await get_referral_stats(user_id)
    
    share_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Пригласить друга (+3 gen)", callback_data="referral_info")],
        [InlineKeyboardButton(text="📊 Мои рефералы", callback_data="my_referrals")]
    ])
    
    menu = (
        f"🎨 *SelenaArtBot* — твой AI-художник!\n\n"
        f"🤖 Модель: FLUX.2 Pro\n\n"
        f"📦 У тебя: {pack_gen} ген | {pack_edit} ред\n\n"
        f"🔥 *Реферальная система:*\n"
        f"• В ожидании: {ref_stats['pending']}\n"
        f"• Активных: {ref_stats['active']}\n"
        f"• Награждено: {ref_stats['rewarded']}\n\n"
        f"Пригласи друга → +{REFERRAL_REWARD} ген за активного пользователя!\n\n"
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
        "🤖 *Модель:* FLUX.2 Pro\n\n"
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
        f"Отправь ссылку другу → он получает +{REFERRAL_REWARD} ген → ты тоже!\n\n"
        f"⚠️ Бонус начисляется только после первого действия друга!",
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
    ref_stats = await get_referral_stats(user_id)
    
    await message.answer(
        f"📊 *Статистика*\n\n"
        f"🎨 Бесплатных ген: {remaining_gen} из {FREE_GENERATIONS_PER_DAY}\n"
        f"🖼 Бесплатных ред: {remaining_edit} из {FREE_EDITS_PER_DAY}\n"
        f"📦 Куплено: {pack_gen} ген | {pack_edit} ред\n"
        f"👥 Приглашено друзей: {ref_count}\n"
        f"🎁 Получено бонусов: {ref_count * REFERRAL_REWARD} ген\n"
        f"⏳ В ожидании: {ref_stats['pending']}\n"
        f"✅ Активных: {ref_stats['active']}",
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
    status_msg = await message.answer(f"🎨 *Генерирую (FLUX.2 Pro):* {prompt[:50]}...\n⏳ 10-30 секунд", parse_mode="Markdown")
    
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
                        caption=f"🎨 *{prompt[:100]}*\n🤖 FLUX.2 Pro | SelenaArtBot",
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
    status_msg = await message.answer(f"🖼 *Редактирую (FLUX.2 Pro):* {prompt[:50]}...\n⏳ 10-30 секунд", parse_mode="Markdown")
    
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
                        caption=f"✅ *Отредактировано!*\n📝 {prompt[:100]}\n🤖 FLUX.2 Pro | SelenaArtBot",
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
    
    # Активация реферала при первом действии
    was_activated = await activate_referral(user_id)
    
    # Выдача бонуса
    was_rewarded = False
    if was_activated or await get_referral_status(user_id) == REFERRAL_ACTIVE:
        was_rewarded = await reward_referral(user_id, by_action=True)
    
    if was_rewarded:
        await message.answer(
            f"🎉 *Вы активировали бонус!*\n\n"
            f"Вы получили +{REFERRAL_REWARD} генераций на счёт!\n"
            f"Продолжайте пользоваться ботом 🎨",
            parse_mode="Markdown"
        )
    
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
    
    # Активация реферала при первом действии
    was_activated = await activate_referral(user_id)
    
    # Выдача бонуса
    was_rewarded = False
    if was_activated or await get_referral_status(user_id) == REFERRAL_ACTIVE:
        was_rewarded = await reward_referral(user_id, by_action=True)
    
    if was_rewarded:
        await message.answer(
            f"🎉 *Вы активировали бонус!*\n\n"
            f"Вы получили +{REFERRAL_REWARD} генераций на счёт!\n"
            f"Продолжайте пользоваться ботом 🎨",
            parse_mode="Markdown"
        )
    
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
    await callback.answer()

    user_id = callback.from_user.id
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{await get_referral_code(user_id)}"
    count = await get_referral_count(user_id)
    stats = await get_referral_stats(user_id)

    text = (
        f"🔥 *Реферальная программа*\n\n"
        f"👥 Приглашено друзей: {count}\n"
        f"🎁 За каждого активного: +{REFERRAL_REWARD} ген\n\n"
        f"📈 *Статус рефералов:*\n"
        f"• ⏳ В ожидании: {stats['pending']}\n"
        f"• ✅ Активных: {stats['active']}\n"
        f"• 💰 Награждено: {stats['rewarded']}\n\n"
        f"🔗 *Твоя ссылка:*\n`{ref_link}`\n\n"
        f"📤 Отправь другу — он получит +{REFERRAL_REWARD} ген за первое действие!\n"
        f"⚠️ Бонус начисляется только после первого действия друга!"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📤 Поделиться",
            url=f"https://t.me/share/url?url={ref_link}&text=Попробуй SelenaArtBot — генератор картинок через ИИ! 🎨"
        )],
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_start")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="close")]
    ])

    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=kb
    )

@dp.callback_query(lambda c: c.data == "back_to_start")
async def back_to_start(callback: types.CallbackQuery):
    await callback.answer()
    await cmd_start(callback.message)

@dp.callback_query(lambda c: c.data == "my_referrals")
async def my_referrals(callback: types.CallbackQuery):
    await callback.answer()

    user_id = callback.from_user.id
    count = await get_referral_count(user_id)
    stats = await get_referral_stats(user_id)

    text = (
        f"📊 *Мои рефералы*\n\n"
        f"👥 Приглашено друзей: {count}\n"
        f"🎁 Получено бонусов: {count * REFERRAL_REWARD} ген\n\n"
        f"📈 *Статус рефералов:*\n"
        f"• ⏳ В ожидании действия: {stats['pending']}\n"
        f"• ✅ Активных (бонус скоро): {stats['active']}\n"
        f"• 💰 Награждено: {stats['rewarded']}\n\n"
        f"🔥 Каждый новый друг → +{REFERRAL_REWARD} ген тебе и ему!\n"
        f"⚠️ Бонус только после первого действия друга!"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_start")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="close")]
    ])

    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=kb
    )

@dp.callback_query(lambda c: c.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    await callback.answer()
    await cmd_start(callback.message)

@dp.callback_query(lambda c: c.data == "close")
async def close_message(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.delete()

@dp.callback_query(lambda c: c.data == "share")
async def share_image(callback: types.CallbackQuery):
    await callback.answer("📤 Отправь этот результат другу!", show_alert=True)

# ================= АДМИН-КОМАНДЫ =================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "👑 *Админ-панель*\n\n"
        "📊 /stats — общая статистика\n"
        "👥 /users — список пользователей\n"
        "💰 /premium [id] [дни] — выдать премиум\n"
        "❌ /rmpremium [id] — снять премиум\n"
        "➕ /add_gen [id] [кол-во] — добавить генерации\n"
        "➕ /add_edit [id] [кол-во] — добавить редактирования\n"
        "➖ /rem_gen [id] [кол-во] — снять генерации\n"
        "➖ /rem_edit [id] [кол-во] — снять редактирования\n"
        "📢 /broadcast [текст] — рассылка\n"
        "⭐ /stars — баланс Stars\n\n"
        "👑 *Рефералы:*\n"
        "📊 /ref_stats [id] — статистика рефералов\n"
        "📋 /ref_logs [id] — логи рефералов\n"
        "❌ /ref_reset [id] — сбросить все рефералы пользователя\n"
        "🎁 /ref_reward [id] — принудительно выдать бонус рефереру\n"
        "⏰ /ref_pending [id] — показать pending рефералов\n"
        "🗑 /ref_clean [id] — очистить неактивных рефералов",
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
    total_gens = 0
    for key in keys:
        val = await redis_client.get(key)
        if val:
            total_gens += int(val)
    
    await message.answer(
        f"📊 *Статистика бота*\n\n"
        f"👥 Пользователей: {len(users)}\n"
        f"🌟 Премиум: {len(premium_keys)}\n"
        f"🎨 Всего генераций: {total_gens}",
        parse_mode="Markdown"
    )

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
            premium = "🌟" if await is_premium(int(user_id)) else ""
            users_data.append(f"{premium} `{user_id}` — {gens} ген, {edits} ред")
    if not users_data:
        await message.answer("Нет пользователей с пакетами")
        return
    text = "👥 *Пользователи:*\n\n" + "\n".join(users_data[:50])
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("rem_gen"))
async def admin_remove_gen(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("❌ Использование: /rem_gen user_id количество")
        return
    try:
        user_id = int(parts[1])
        count = int(parts[2])
        new_count = await remove_pack_generations(user_id, count)
        await message.answer(f"✅ Снято {count} генераций у {user_id}. Осталось: {new_count}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("rem_edit"))
async def admin_remove_edit(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("❌ Использование: /rem_edit user_id количество")
        return
    try:
        user_id = int(parts[1])
        count = int(parts[2])
        new_count = await remove_pack_edits(user_id, count)
        await message.answer(f"✅ Снято {count} редактирований у {user_id}. Осталось: {new_count}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("ref_reset"))
async def admin_ref_reset(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("❌ Использование: /ref_reset user_id")
        return
    try:
        user_id = int(parts[1])
        keys = await redis_client.keys(f"selena:ref:status:{user_id}:*")
        for key in keys:
            await redis_client.delete(key)
        await redis_client.set(f"selena:ref:count:{user_id}", 0)
        await redis_client.delete(f"selena:ref:log:{user_id}")
        await message.answer(f"✅ Все рефералы пользователя {user_id} сброшены")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("ref_reward"))
async def admin_force_reward(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("❌ Использование: /ref_reward user_id")
        return
    try:
        user_id = int(parts[1])
        referrer_id = await get_referred_by(user_id)
        if not referrer_id:
            await message.answer(f"❌ У пользователя {user_id} нет реферера")
            return
        await add_pack_generations(referrer_id, REFERRAL_REWARD)
        await increment_referral_count(referrer_id)
        await set_referral_status(user_id, referrer_id, REFERRAL_REWARDED)
        await message.answer(f"✅ Рефереру {referrer_id} начислен бонус +{REFERRAL_REWARD} ген за пользователя {user_id}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("ref_pending"))
async def admin_show_pending(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    user_id = int(parts[1]) if len(parts) > 1 else None
    if not user_id:
        await message.answer("❌ Использование: /ref_pending user_id")
        return
    keys = await redis_client.keys(f"selena:ref:status:{user_id}:*")
    pending = []
    for key in keys:
        status = await redis_client.get(key)
        if status == REFERRAL_PENDING:
            ref_user_id = key.split(":")[-1]
            pending.append(ref_user_id)
    if pending:
        await message.answer(f"⏳ *Pending рефералы {user_id}:*\n" + "\n".join(pending[:30]), parse_mode="Markdown")
    else:
        await message.answer(f"Нет pending рефералов у {user_id}")

@dp.message(Command("ref_clean"))
async def admin_clean_pending(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("❌ Использование: /ref_clean user_id")
        return
    try:
        user_id = int(parts[1])
        keys = await redis_client.keys(f"selena:ref:status:{user_id}:*")
        cleaned = 0
        for key in keys:
            status = await redis_client.get(key)
            if status == REFERRAL_PENDING:
                await redis_client.delete(key)
                cleaned += 1
        await message.answer(f"✅ Очищено {cleaned} неактивных pending рефералов у {user_id}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

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

@dp.message(Command("ref_stats"))
async def admin_ref_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    user_id = int(parts[1]) if len(parts) > 1 else message.from_user.id
    stats = await get_referral_stats(user_id)
    await message.answer(
        f"📊 *Реферальная статистика {user_id}:*\n\n"
        f"⏳ В ожидании: {stats['pending']}\n"
        f"✅ Активных: {stats['active']}\n"
        f"💰 Награждено: {stats['rewarded']}\n"
        f"📈 Всего: {stats['total']}",
        parse_mode="Markdown"
    )

@dp.message(Command("ref_logs"))
async def admin_ref_logs(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("❌ Использование: /ref_logs user_id")
        return
    try:
        user_id = int(parts[1])
        log_key = f"selena:ref:log:{user_id}"
        logs = await redis_client.lrange(log_key, 0, 19)
        if not logs:
            await message.answer(f"📋 Нет логов для пользователя {user_id}")
            return
        text = f"📋 *Логи рефералов для {user_id}:*\n\n"
        for log in logs:
            log_str = log.decode() if isinstance(log, bytes) else log
            try:
                import ast
                log_data = ast.literal_eval(log_str)
                text += f"• {log_data.get('timestamp', '?')[:16]} | {log_data.get('new_user_id')} | {log_data.get('status')} | {log_data.get('reason', '')}\n"
            except:
                text += f"• {log_str[:100]}\n"
        await message.answer(text[:4000], parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

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
