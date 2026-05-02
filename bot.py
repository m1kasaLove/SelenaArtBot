import asyncio
import logging
import os
import aiohttp
import base64
from io import BytesIO
from datetime import datetime
import random
import string

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import LabeledPrice, PreCheckoutQuery, SuccessfulPayment, BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from PIL import Image, ImageDraw, ImageFont

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

# ===== ЦЕНЫ =====
FREE_GENERATIONS_PER_DAY = 2
FREE_EDITS_PER_DAY = 1

PRICE_1_GEN = 2
PRICE_5_GEN = 8
PRICE_10_GEN = 15

PRICE_1_EDIT = 2
PRICE_5_EDIT = 8
PRICE_10_EDIT = 15

PRICE_COMBO_5 = 12
PRICE_COMBO_10 = 20

PREMIUM_PRICE = 30
PREMIUM_DAYS = 30

# Реферальная система
REFERRAL_REWARD = 3  # +3 генерации за приглашённого друга

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

# ===== ПРЕМИУМ =====
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

# ===== РЕФЕРАЛЬНАЯ СИСТЕМА =====
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

async def has_received_reward(user_id: int) -> bool:
    return await redis_client.get(f"selena:ref:rewarded:{user_id}") == "1"

async def mark_reward_received(user_id: int):
    await redis_client.set(f"selena:ref:rewarded:{user_id}", "1")

async def get_referral_count(user_id: int) -> int:
    val = await redis_client.get(f"selena:ref:count:{user_id}")
    return int(val) if val else 0

async def increment_referral_count(user_id: int):
    await redis_client.incr(f"selena:ref:count:{user_id}")

# ===== ВОДЯНОЙ ЗНАК =====
async def add_watermark(image_bytes: BytesIO) -> BytesIO:
    """Добавляет водяной знак SelenaArtBot на изображение"""
    image_bytes.seek(0)
    img = Image.open(image_bytes)
    
    # Конвертируем в RGB если нужно
    if img.mode in ('RGBA', 'LA', 'P'):
        rgb_img = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        rgb_img.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
        img = rgb_img
    
    draw = ImageDraw.Draw(img)
    
    # Текст водяного знака
    watermark_text = "✨ SelenaArtBot"
    
    # Размер шрифта в зависимости от размера изображения
    font_size = max(20, int(img.width / 25))
    
    try:
        # Попробуем загрузить шрифт
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
    except:
        font = ImageFont.load_default()
    
    # Позиция в правом нижнем углу с отступом
    bbox = draw.textbbox((0, 0), watermark_text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    x = img.width - text_width - 15
    y = img.height - text_height - 15
    
    # Полупрозрачный фон для текста
    draw.rectangle([x-5, y-3, x+text_width+5, y+text_height+5], fill=(0, 0, 0, 128))
    draw.text((x, y), watermark_text, fill=(255, 255, 255), font=font)
    
    # Сохраняем в BytesIO
    output = BytesIO()
    img.save(output, format="PNG")
    output.seek(0)
    return output

# ===== ГЕНЕРАЦИЯ =====
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
                    return None
                data = await resp.json()
                task_id = data.get("id")
            
            for attempt in range(30):
                await asyncio.sleep(2)
                async with session.get(f"https://api.polza.ai/v1/media/{task_id}", headers=headers) as status_resp:
                    if status_resp.status != 200:
                        continue
                    status_data = await status_resp.json()
                    if status_data.get("status") == "completed":
                        images = status_data.get("output", {}).get("images", [])
                        if images:
                            image_url = images[0].get("url")
                            async with session.get(image_url) as img_resp:
                                if img_resp.status == 200:
                                    return BytesIO(await img_resp.read())
            return None
        except:
            return None

async def edit_image(image_bytes: BytesIO, prompt: str) -> BytesIO | None:
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
                                return BytesIO(await img_resp.read())
            return None
        except:
            return None

# ===== КНОПКИ ДЛЯ ПОДЕЛИТЬСЯ =====
def get_share_keyboard(image_id: str = None):
    """Клавиатура для отправки результата"""
    buttons = []
    
    if image_id:
        buttons.append([InlineKeyboardButton(text="📤 Отправить в канал", callback_data=f"share_channel:{image_id}")])
        buttons.append([InlineKeyboardButton(text="👥 Отправить другу", switch_inline_query=f"Посмотри что сгенерировал {BOT_NAME}")])
    
    buttons.append([InlineKeyboardButton(text="🌟 Пригласить друга (+3 ген)", callback_data="referral_info")])
    buttons.append([InlineKeyboardButton(text="📊 Мои рефералы", callback_data="my_referrals")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ===== КОМАНДЫ БОТА =====
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
        # Находим кто пригласил
        keys = await redis_client.keys("selena:ref:code:*")
        for key in keys:
            code = await redis_client.get(key)
            if code == referrer_code:
                referrer_id = int(key.split(":")[-1])
                if referrer_id != user_id and not await get_referred_by(user_id):
                    await set_referred_by(user_id, referrer_id)
                    await increment_referral_count(referrer_id)
                    
                    # Начисляем бонус приглашённому
                    await add_pack_generations(user_id, REFERRAL_REWARD)
                    
                    # Уведомляем пригласившего
                    try:
                        await bot.send_message(referrer_id, f"🎉 *По вашей ссылке пришёл новый пользователь!*\n\nВы получили +{REFERRAL_REWARD} генераций!", parse_mode="Markdown")
                    except:
                        pass
                    
                    await message.answer(f"🎉 *Вы получили +{REFERRAL_REWARD} генераций за регистрацию по ссылке!*", parse_mode="Markdown")
                break
    
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{await get_referral_code(user_id)}"
    
    menu = (
        "🎨 *SelenaArtBot* — твой AI-художник!\n\n"
        "✨ *Нейросеть:* Qwen/Image-2\n\n"
        "💰 *Цены:*\n"
        f"• {FREE_GENERATIONS_PER_DAY} ген/день — БЕСПЛАТНО\n"
        f"• {FREE_EDITS_PER_DAY} ред/день — БЕСПЛАТНО\n\n"
        f"📦 У тебя: {pack_gen} ген | {pack_edit} ред\n"
    )
    
    if premium:
        menu += "🌟 *У тебя ПРЕМИУМ!* Безлимит!\n\n"
    
    menu += (
        f"🔥 *Реферальная система:*\n"
        f"Пригласи друга → +{REFERRAL_REWARD} генерации тебе и ему!\n"
        f"Твоя ссылка: `{ref_link}`\n\n"
        f"📝 Команды: /status | /help | /referral\n\n"
        f"🌙 *Поболтать:* @LunaIsLovelyLunaBot"
    )
    
    await message.answer(menu, parse_mode="Markdown")

@dp.message(Command("referral"))
async def cmd_referral(message: types.Message):
    user_id = message.from_user.id
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{await get_referral_code(user_id)}"
    count = await get_referral_count(user_id)
    
    await message.answer(
        f"🔥 *Реферальная программа*\n\n"
        f"👥 Приглашено друзей: {count}\n"
        f"🎁 За каждого друга: +{REFERRAL_REWARD} генераций\n\n"
        f"🔗 *Твоя ссылка:*\n`{ref_link}`\n\n"
        f"Поделись ссылкой с друзьями и получай бонусы!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Отправить другу", url=f"https://t.me/share/url?url={ref_link}&text=Привет! Попробуй SelenaArtBot — генератор картинок через ИИ!")]
        ])
    )

@dp.callback_query(lambda c: c.data == "referral_info")
async def referral_info(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{await get_referral_code(user_id)}"
    
    await callback.message.edit_text(
        f"🔥 *Как получить бонусы?*\n\n"
        f"1. Отправь другу свою ссылку\n"
        f"2. Друг переходит и регистрируется\n"
        f"3. Вы оба получаете +{REFERRAL_REWARD} генераций!\n\n"
        f"🔗 *Твоя ссылка:*\n`{ref_link}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться", url=f"https://t.me/share/url?url={ref_link}&text=Привет! Попробуй SelenaArtBot — генератор картинок через ИИ!")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
        ])
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "my_referrals")
async def my_referrals(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    count = await get_referral_count(user_id)
    
    await callback.message.edit_text(
        f"📊 *Мои рефералы*\n\n"
        f"👥 Приглашено друзей: {count}\n"
        f"🎁 Получено бонусов: {count * REFERRAL_REWARD} генераций\n\n"
        f"Продолжай приглашать — бонусы безлимитны!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
        ])
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    await cmd_start(callback.message)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "close")
async def close_message(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.answer()

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 *Помощь*\n\n"
        "**Генерация:** напиши описание\n"
        "**Редактирование:** отправь фото + подпись\n\n"
        "**Команды:**\n"
        "/start — главное меню\n"
        "/status — статистика\n"
        "/referral — реферальная ссылка\n"
        "/pack_gen5 — 5 ген\n"
        "/pack_gen10 — 10 ген\n"
        "/pack_edit5 — 5 ред\n"
        "/pack_edit10 — 10 ред\n"
        "/combo5 — 3 ген+2 ред\n"
        "/combo10 — 6 ген+4 ред\n"
        "/premium_buy — безлимит\n\n"
        "🔥 *Рефералка:* пригласи друга → +3 ген\n\n"
        "🌙 *Поболтать:* @LunaIsLovelyLunaBot",
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
    today_edit = await get_edits_today(user_id)
    remaining_edit = max(0, FREE_EDITS_PER_DAY - today_edit)
    pack_gen = await get_pack_generations(user_id)
    pack_edit = await get_pack_edits(user_id)
    ref_count = await get_referral_count(user_id)
    
    await message.answer(
        f"📊 *Твоя статистика*\n\n"
        f"🎨 Бесплатных ген: {remaining_gen} из {FREE_GENERATIONS_PER_DAY}\n"
        f"🖼 Бесплатных ред: {remaining_edit} из {FREE_EDITS_PER_DAY}\n"
        f"📦 Куплено: {pack_gen} ген | {pack_edit} ред\n"
        f"👥 Приглашено друзей: {ref_count}\n\n"
        f"💫 /referral — твоя реферальная ссылка",
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

# ===== ПАКЕТЫ (команды остались те же) =====
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
    await message.answer_invoice(title="🔥 Комбо-пакет 5", description="3 генерации + 2 редактирования!", payload="combo_5", provider_token="", currency="XTR", prices=prices)

@dp.message(Command("combo10"))
async def cmd_combo10(message: types.Message):
    prices = [LabeledPrice(label="6 ген + 4 ред", amount=PRICE_COMBO_10)]
    await message.answer_invoice(title="🔥 Комбо-пакет 10", description="6 генераций + 4 редактирования!", payload="combo_10", provider_token="", currency="XTR", prices=prices)

@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def payment_success(message: SuccessfulPayment):
    user_id = message.from_user.id
    payload = message.successful_payment.invoice_payload
    
    if payload == "premium_purchase":
        await set_premium(user_id, PREMIUM_DAYS)
        await message.answer("✅ *Премиум активирован!* Безлимит на 30 дней!", parse_mode="Markdown")
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
        await message.answer("✅ *Куплено 3 генерации + 2 редактирования!*", parse_mode="Markdown")
    elif payload == "combo_10":
        await add_combo_pack(user_id, 6, 4)
        await message.answer("✅ *Куплено 6 генераций + 4 редактирования!*", parse_mode="Markdown")

# ===== ОСНОВНЫЕ ПРОЦЕССЫ =====
async def process_generation(message: types.Message, prompt: str):
    status_msg = await message.answer(f"🎨 *Генерирую:* {prompt[:50]}...\n⏳ 10-20 секунд", parse_mode="Markdown")
    
    img = await generate_image(prompt)
    
    if img:
        # Добавляем водяной знак
        watermarked = await add_watermark(img)
        photo = BufferedInputFile(watermarked.getvalue(), filename="selena.png")
        
        # Генерируем ID для кнопки поделиться
        import uuid
        image_id = str(uuid.uuid4())[:8]
        await redis_client.setex(f"selena:share:{image_id}", 3600, prompt)
        
        await message.answer_photo(
            photo, 
            caption=f"🎨 *{prompt[:100]}*\n✨ Создано в SelenaArtBot",
            parse_mode="Markdown",
            reply_markup=get_share_keyboard(image_id)
        )
        await status_msg.delete()
    else:
        await status_msg.edit_text("❌ *Ошибка генерации*\n\nПопробуй написать на английском", parse_mode="Markdown")

async def process_edit(message: types.Message, image_bytes: BytesIO, prompt: str):
    status_msg = await message.answer(f"🖼 *Редактирую:* {prompt[:50]}...\n⏳ 10-20 секунд", parse_mode="Markdown")
    
    edited = await edit_image(image_bytes, prompt)
    
    if edited:
        watermarked = await add_watermark(edited)
        photo = BufferedInputFile(watermarked.getvalue(), filename="edited.png")
        
        import uuid
        image_id = str(uuid.uuid4())[:8]
        await redis_client.setex(f"selena:share:{image_id}", 3600, prompt)
        
        await message.answer_photo(
            photo, 
            caption=f"✅ *{prompt[:100]}*\n✨ Отредактировано в SelenaArtBot",
            parse_mode="Markdown",
            reply_markup=get_share_keyboard(image_id)
        )
        await status_msg.delete()
    else:
        await status_msg.edit_text("❌ *Ошибка редактирования*", parse_mode="Markdown")

# ===== ГЕНЕРАЦИЯ ПО ТЕКСТУ =====
@dp.message(F.text & ~F.text.startswith('/'))
async def generate_by_text(message: types.Message):
    user_id = message.from_user.id
    prompt = message.text.strip()
    
    if len(prompt) < 3:
        await message.answer("❌ Напиши подробнее, что нарисовать (минимум 3 символа)")
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
        await message.answer(
            f"📊 *Лимит исчерпан!*\n\n"
            f"💰 /pack_gen5 — 5 ген за {PRICE_5_GEN}⭐\n"
            f"🔥 /referral — пригласи друга и получи +3 ген\n"
            f"🌟 /premium_buy — безлимит за {PREMIUM_PRICE}⭐",
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
        await message.answer("✏️ *Напиши в подписи, что изменить на фото*", parse_mode="Markdown")
        return
    
    if len(edit_prompt) < 3:
        await message.answer("❌ Напиши подробнее, что изменить")
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
        await message.answer(
            f"📊 *Лимит редактирований исчерпан!*\n\n"
            f"💰 /pack_edit5 — 5 ред за {PRICE_5_EDIT}⭐\n"
            f"🔥 /referral — пригласи друга и получи +3 ген\n"
            f"🌟 /premium_buy — безлимит за {PREMIUM_PRICE}⭐",
            parse_mode="Markdown"
        )
        return
    
    await incr_edits_today(user_id)
    file = await bot.get_file(message.photo[-1].file_id)
    file_bytes = BytesIO()
    await bot.download_file(file.file_path, file_bytes)
    file_bytes.seek(0)
    await process_edit(message, file_bytes, edit_prompt)

# ===== АДМИН-КОМАНДЫ =====
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("👑 *Админ-панель*\n\n/stats — статистика\n/users — список пользователей\n/premium [id] [дни]\n/rmpremium [id]\n/add_gen [id] [кол-во]\n/add_edit [id] [кол-во]\n/broadcast\n/stars", parse_mode="Markdown")

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
        await message.answer("❌ Ошибка")

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
                    await message.answer(f"⭐ *Баланс Stars:* {stars}\n\n1 Star ≈ 10 рублей", parse_mode="Markdown")
                else:
                    await message.answer("❌ Ошибка")
    except:
        await message.answer("❌ Ошибка")

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
