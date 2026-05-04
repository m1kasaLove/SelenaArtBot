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
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from PIL import Image, ImageDraw, ImageFont

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

# ================= PRICES & LIMITS =================
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
    cur = await get_pack_generations(user_id)
    if cur > 0:
        await redis_client.decr(f"selena:pack:gen:{user_id}")
        return True
    return False

async def use_pack_edit(user_id: int) -> bool:
    cur = await get_pack_edits(user_id)
    if cur > 0:
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

async def get_referral_count(user_id: int) -> int:
    val = await redis_client.get(f"selena:ref:count:{user_id}")
    return int(val) if val else 0

async def increment_referral_count(user_id: int):
    await redis_client.incr(f"selena:ref:count:{user_id}")

async def get_referrer_by_code(code: str) -> int | None:
    async for key in redis_client.scan_iter("selena:ref:code:*"):
        stored_code = await redis_client.get(key)
        if stored_code == code:
            return int(key.split(":")[-1])
    return None

async def has_referral(user_id: int) -> bool:
    return await redis_client.get(f"selena:ref:by:{user_id}") is not None

async def set_referrer(user_id: int, referrer_id: int):
    await redis_client.set(f"selena:ref:by:{user_id}", referrer_id)

# ================= GPT GENERATION =================
# ================= GPT GENERATION =================
def build_edit_prompt(user_prompt: str) -> str:
    return f"""Edit the provided image.

CRITICAL CONSTRAINTS (MUST FOLLOW):
- preserve the exact same person, same face, same identity
- do NOT change gender, age, or facial features
- keep the original pose and composition
- only modify: {user_prompt}

The result must look like the same person with only the requested changes."""

async def generate_with_gpt(prompt: str, reference_image: BytesIO = None, retry: bool = True) -> BytesIO | None:
    headers = {
        "Authorization": f"Bearer {POLZA_API_KEY}",
        "Content-Type": "application/json"
    }

    # Базовый payload для GPT
    payload = {
        "model": "openai/gpt-5.4-image-2",
        "input": {
            "prompt": prompt if not reference_image else build_edit_prompt(prompt),
            "aspect_ratio": "1:1",
            "output_format": "png"
        },
        "async": True
    }

    if reference_image:
        reference_image.seek(0)
        b64 = base64.b64encode(reference_image.read()).decode()
        payload["input"]["image"] = f"data:image/png;base64,{b64}"
        payload["input"]["strength"] = 0.5  # 0.3 — слабо, 1.0 — сильно
        payload["input"]["negative_prompt"] = "different person, changed face, other gender, distorted features"
        logger.info("[GPT] 🖼 EDIT MODE (сохраняем лицо)")
    else:
        logger.info("[GPT] 🎨 GENERATE MODE")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        try:
            async with session.post("https://polza.ai/api/v1/media", json=payload, headers=headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"[GPT] STATUS {resp.status}: {error_text}")
                    if retry:
                        await asyncio.sleep(2)
                        return await generate_with_gpt(prompt, reference_image, False)
                    return None

                data = await resp.json()
                task_id = data.get("id")
                if not task_id:
                    return None

            for _ in range(50):
                await asyncio.sleep(2)
                async with session.get(f"https://polza.ai/api/v1/media/{task_id}", headers=headers) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()
                    if data.get("status") == "completed":
                        url = None
                        d = data.get("data")
                        if isinstance(d, str):
                            url = d
                        elif isinstance(d, dict):
                            url = d.get("url")
                        elif isinstance(d, list) and d:
                            first = d[0]
                            url = first if isinstance(first, str) else first.get("url")
                        if not url:
                            output = data.get("output", {})
                            images = output.get("images", [])
                            if images:
                                item = images[0]
                                url = item if isinstance(item, str) else item.get("url")
                        if url:
                            async with session.get(url) as img:
                                if img.status == 200:
                                    return BytesIO(await img.read())
                    if data.get("status") == "failed":
                        if retry:
                            return await generate_with_gpt(prompt, reference_image, False)
                        return None
        except Exception as e:
            logger.error(f"[GPT] Exception: {e}")
            if retry:
                return await generate_with_gpt(prompt, reference_image, False)
    return None

# ================= FALLBACK =================
async def generate_fallback(prompt: str) -> BytesIO | None:
    import urllib.parse
    url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?width=1024&height=1024"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=30) as resp:
                if resp.status == 200:
                    return BytesIO(await resp.read())
        except:
            pass
    return None

async def generate_image(prompt: str) -> BytesIO | None:
    result = await generate_with_gpt(prompt)
    if result:
        return result
    logger.warning("[GEN] GPT не ответил, пробуем fallback")
    return await generate_fallback(prompt)

async def edit_image(image_bytes: BytesIO, prompt: str) -> BytesIO | None:
    return await generate_with_gpt(prompt, reference_image=image_bytes)

# ================= WATERMARK =================
async def add_watermark(image_bytes: BytesIO) -> BytesIO:
    image_bytes.seek(0)
    img = Image.open(image_bytes).convert("RGBA")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    draw.text((10, 10), "SelenaArtBot", fill=(255, 255, 255, 180), font=font)
    out = BytesIO()
    img.save(out, "PNG")
    out.seek(0)
    return out

# ================= KEYBOARDS =================
def get_share_keyboard(image_id: str = None):
    rows = []
    if image_id:
        rows.append([InlineKeyboardButton(text="🔥 Поделиться результатом", callback_data="share")])
    rows.append([InlineKeyboardButton(text="👥 Пригласить друга (+3 gen)", callback_data="referral_info")])
    rows.append([InlineKeyboardButton(text="📊 Мои рефералы", callback_data="my_referrals")])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ================= COMMANDS =================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    pack_gen = await get_pack_generations(user_id)
    pack_edit = await get_pack_edits(user_id)
    premium = await is_premium(user_id)

    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        ref_code = args[1].replace("ref_", "")
        referrer_id = await get_referrer_by_code(ref_code)

        if referrer_id and referrer_id != user_id and not await has_referral(user_id):
            await set_referrer(user_id, referrer_id)
            await increment_referral_count(referrer_id)
            await add_pack_generations(referrer_id, REFERRAL_REWARD)

            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 *По вашей ссылке пришёл новый пользователь!*\n"
                    f"👥 Приглашённый: {message.from_user.first_name}\n"
                    f"🎁 Вы получили +{REFERRAL_REWARD} генераций!",
                    parse_mode="Markdown"
                )
            except:
                pass

            await message.answer(
                f"🎉 *Вы получили +{REFERRAL_REWARD} генераций за регистрацию по ссылке!*\n\n"
                f"🔥 Просто напишите что нарисовать!",
                parse_mode="Markdown"
            )

    share_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Пригласить друга (+3 gen)", callback_data="referral_info")],
        [InlineKeyboardButton(text="📊 Мои рефералы", callback_data="my_referrals")]
    ])

    menu = (
        f"🎨 *SelenaArtBot* — твой AI-художник!\n\n"
        f"🤖 Модель: GPT-5.4-Image-2\n\n"
        f"📦 У тебя: {pack_gen} ген | {pack_edit} ред\n\n"
        f"🔥 Пригласи друга → +{REFERRAL_REWARD} ген!\n\n"
        f"📝 Команды в меню слева от смайлика\n\n"
        f"🌙 @LunaIsLovelyLunaBot"
    )

    if premium:
        menu = "🌟 *У тебя ПРЕМИУМ!* Безлимит!\n\n" + menu

    await message.answer(menu, parse_mode="Markdown", reply_markup=share_kb)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 *Помощь*\n\n"
        "🤖 *Модель:* GPT-5.4-Image-2\n\n"
        "**Генерация:** напиши описание\n"
        "**Редактирование:** отправь фото + подпись\n\n"
        "🌙 @LunaIsLovelyLunaBot",
        parse_mode="Markdown"
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
    pack_gen = await get_pack_generations(user_id)

    await message.answer(
        f"📊 *Статистика*\n\n"
        f"🎨 Бесплатных ген: {remaining_gen} из {FREE_GENERATIONS_PER_DAY}\n"
        f"📦 Куплено: {pack_gen} ген\n",
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
        f"Отправь ссылку другу → +{REFERRAL_REWARD} ген!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться", url=f"https://t.me/share/url?url={ref_link}&text=Попробуй SelenaArtBot!")]
        ])
    )

# ================= PAYMENTS =================
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
    status_msg = await message.answer(f"🎨 *Генерирую (GPT-5.4):* {prompt[:50]}...\n⏳ 10-30 секунд", parse_mode="Markdown")

    img = await generate_image(prompt)

    if img:
        try:
            watermarked = await add_watermark(img)
            photo = BufferedInputFile(watermarked.getvalue(), filename="selena.png")
            image_id = str(uuid.uuid4())[:8]

            await message.answer_photo(
                photo,
                caption=f"🎨 *{prompt[:100]}*\n🤖 GPT-5.4-Image-2 | SelenaArtBot",
                parse_mode="Markdown",
                reply_markup=get_share_keyboard(image_id)
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке: {e}")
            await status_msg.edit_text("❌ *Ошибка при обработке картинки*", parse_mode="Markdown")
    else:
        await status_msg.edit_text(
            "❌ *Ошибка генерации*\n\nПопробуй написать на английском\n\n🌙 @LunaIsLovelyLunaBot",
            parse_mode="Markdown"
        )

    try:
        await status_msg.delete()
    except:
        pass

async def process_edit(message: types.Message, image_bytes: BytesIO, prompt: str):
    status_msg = await message.answer(f"🖼 *Редактирую (GPT-5.4):* {prompt[:50]}...\n⏳ 10-30 секунд", parse_mode="Markdown")

    edited = await edit_image(image_bytes, prompt)

    if edited:
        try:
            watermarked = await add_watermark(edited)
            photo = BufferedInputFile(watermarked.getvalue(), filename="edited.png")
            image_id = str(uuid.uuid4())[:8]

            await message.answer_photo(
                photo,
                caption=f"✅ *Отредактировано!*\n📝 {prompt[:100]}\n🤖 GPT-5.4-Image-2 | SelenaArtBot",
                parse_mode="Markdown",
                reply_markup=get_share_keyboard(image_id)
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке: {e}")
            await status_msg.edit_text("❌ *Ошибка при обработке картинки*", parse_mode="Markdown")
    else:
        await status_msg.edit_text(
            "❌ *Ошибка редактирования*\n\nПопробуй:\n• `сделай чёрно-белым`\n• `увеличь контраст`\n\n🌙 @LunaIsLovelyLunaBot",
            parse_mode="Markdown"
        )

    try:
        await status_msg.delete()
    except:
        pass

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
        await message.answer(f"📊 *Лимит исчерпан!*\n💰 /pack_gen5 — 5 ген за {PRICE_5_GEN}⭐", parse_mode="Markdown")
        return

    await incr_generations_today(user_id)
    await process_generation(message, prompt)

# ================= PHOTO HANDLER =================
@dp.message(F.photo)
async def edit_photo(message: types.Message):
    user_id = message.from_user.id
    edit_prompt = message.caption or ""

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
@dp.callback_query(F.data == "referral_info")
async def referral_info_cb(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{await get_referral_code(user_id)}"
    await callback.message.edit_text(f"🔥 *Твоя реферальная ссылка:*\n`{link}`", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "my_referrals")
async def my_referrals_cb(callback: types.CallbackQuery):
    count = await get_referral_count(callback.from_user.id)
    await callback.message.edit_text(f"📊 *Ты пригласил {count} друзей*\n🎁 Получено бонусов: {count * REFERRAL_REWARD} ген", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "back_to_start")
async def back_to_start_cb(callback: types.CallbackQuery):
    await callback.message.delete()
    await cmd_start(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "close")
async def close_cb(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.answer()

@dp.callback_query(F.data == "share")
async def share_cb(callback: types.CallbackQuery):
    await callback.answer("📤 Поделись с другом!", show_alert=True)

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
        "/premium [id] [дни] — выдать премиум\n"
        "/rmpremium [id] — снять премиум\n"
        "/add_gen [id] [кол-во] — добавить генерации\n"
        "/add_edit [id] [кол-во] — добавить редактирования\n"
        "/broadcast [текст] — рассылка",
        parse_mode="Markdown"
    )

@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    users = set()
    async for key in redis_client.scan_iter("selena:gen:*"):
        parts = key.split(":")
        if len(parts) >= 3:
            users.add(parts[2])
    premium_keys = await redis_client.keys("selena:premium:*")
    await message.answer(f"📊 *Статистика*\n\n👥 Пользователей: {len(users)}\n🌟 Премиум: {len(premium_keys)}", parse_mode="Markdown")

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
    users = set()
    async for key in redis_client.scan_iter("selena:gen:*"):
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

# ================= STARTUP =================
async def on_startup(app):
    global redis_client
    redis_client = await redis.from_url(REDIS_URL, decode_responses=True)
    logger.info("✅ Redis connected")

    await set_commands()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"✅ Webhook set: {WEBHOOK_URL}")
    logger.info("✅ Bot started properly")

async def on_shutdown(app):
    if redis_client:
        await redis_client.aclose()
    await bot.session.close()
    logger.info("✅ Bot shutdown")

def create_app():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="SelenaArtBot is alive! 🎨"))
    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
