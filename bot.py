#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
🌙 SelenaArtBot — AI генератор и редактор изображений
С интеграцией Polza.ai API (Qwen/Image-2)
Автономная работа (SQLite вместо Redis)
Admin ID: 532229128
"""

import asyncio
import logging
import os
import json
import sqlite3
import base64
import aiohttp
import urllib.parse
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional, Dict, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import LabeledPrice, BufferedInputFile
from dotenv import load_dotenv

# ============================================================
# ЗАГРУЗКА ПЕРЕМЕННЫХ
# ============================================================

load_dotenv()

TELEGRAM_TOKEN = "8663720990:AAG1lJDYHfX12tKZuQ_BOIIjnXSyxLsYHok"
POLZA_API_KEY = "pza_FiV3Pscoe4xKEor8l42rfOnNQ5baXMwM"
POLZA_API_URL = "https://polza.ai/api"

BOT_NAME = "Selena Art Bot 🌙"
ADMIN_ID = 532229128  # ТВОЙ ADMIN ID

# ============================================================
# НАСТРОЙКИ
# ============================================================

FREE_GENERATIONS_PER_DAY = 3      # Бесплатных генераций в день
FREE_EDITS_PER_DAY = 3            # Бесплатных редактирований в день
PRICE_GENERATION = 10             # Цена генерации в Stars
PRICE_EDIT = 15                   # Цена редактирования в Stars
PREMIUM_PRICE = 50                # Цена премиума в Stars
PREMIUM_DAYS = 30                 # Дней премиума

DB_PATH = "selena_bot.db"

# ============================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================
# БАЗА ДАННЫХ (SQLite)
# ============================================================

def init_database():
    """Создаём таблицы"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            premium_until INTEGER DEFAULT 0,
            registered_at INTEGER,
            total_generations INTEGER DEFAULT 0,
            total_edits INTEGER DEFAULT 0
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_generations (
            user_id INTEGER,
            date TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, date)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_edits (
            user_id INTEGER,
            date TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, date)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            operation_type TEXT,
            data TEXT,
            created_at TEXT
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

def get_today_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def get_user_generations_today(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today = get_today_date()
    
    cursor.execute(
        "SELECT count FROM daily_generations WHERE user_id = ? AND date = ?",
        (user_id, today)
    )
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

def increment_user_generations(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today = get_today_date()
    
    cursor.execute('''
        INSERT INTO daily_generations (user_id, date, count)
        VALUES (?, ?, 1)
        ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1
    ''', (user_id, today))
    
    cursor.execute('''
        UPDATE users SET total_generations = total_generations + 1
        WHERE user_id = ?
    ''', (user_id,))
    
    conn.commit()
    
    cursor.execute("SELECT count FROM daily_generations WHERE user_id = ? AND date = ?", (user_id, today))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

def get_user_edits_today(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today = get_today_date()
    
    cursor.execute(
        "SELECT count FROM daily_edits WHERE user_id = ? AND date = ?",
        (user_id, today)
    )
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

def increment_user_edits(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today = get_today_date()
    
    cursor.execute('''
        INSERT INTO daily_edits (user_id, date, count)
        VALUES (?, ?, 1)
        ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1
    ''', (user_id, today))
    
    cursor.execute('''
        UPDATE users SET total_edits = total_edits + 1
        WHERE user_id = ?
    ''', (user_id,))
    
    conn.commit()
    
    cursor.execute("SELECT count FROM daily_edits WHERE user_id = ? AND date = ?", (user_id, today))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

def is_premium(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result and result[0]:
        return result[0] > int(datetime.now().timestamp())
    return False

def set_premium(user_id: int, days: int = PREMIUM_DAYS):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    premium_until = int((datetime.now() + timedelta(days=days)).timestamp())
    
    cursor.execute('''
        INSERT INTO users (user_id, premium_until, registered_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET premium_until = ?
    ''', (user_id, premium_until, int(datetime.now().timestamp()), premium_until))
    
    conn.commit()
    conn.close()
    logger.info(f"⭐ Премиум выдан пользователю {user_id} на {days} дней")

def remove_premium(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET premium_until = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def register_user(user_id: int, username: str = None, first_name: str = None, last_name: str = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO users (user_id, username, first_name, last_name, registered_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = COALESCE(?, username),
            first_name = COALESCE(?, first_name),
            last_name = COALESCE(?, last_name)
    ''', (user_id, username, first_name, last_name, int(datetime.now().timestamp()),
          username, first_name, last_name))
    
    conn.commit()
    conn.close()

def get_premium_days_left(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result and result[0]:
        remaining = result[0] - int(datetime.now().timestamp())
        return max(0, remaining // 86400)
    return 0

def get_total_users_count() -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

def get_premium_users_count() -> int:
    now = int(datetime.now().timestamp())
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE premium_until > ?", (now,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

def get_all_user_ids() -> list:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    results = cursor.fetchall()
    conn.close()
    return [r[0] for r in results]

# ============================================================
# ИНИЦИАЛИЗАЦИЯ БОТА
# ============================================================

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# ============================================================
# ПОЛЯЗА API (Qwen/Image-2)
# ============================================================

async def generate_with_polza(prompt: str, is_edit: bool = False, image_base64: str = None) -> Optional[BytesIO]:
    """
    Генерация/редактирование через Polza.ai API (Qwen/Image-2)
    
    Документация: https://polza.ai/docs
    """
    headers = {
        "Authorization": f"Bearer {POLZA_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Базовый payload для Qwen/Image-2
    payload = {
        "model": "qwen/image-2",
        "input": {
            "prompt": prompt,
            "aspect_ratio": "1:1",
            "output_format": "png",
            "guidance_scale": 7.5
        },
        "async": True
    }
    
    # Если это редактирование с референсом
    if is_edit and image_base64:
        payload["input"]["image"] = image_base64
        payload["input"]["strength"] = 0.8  # Сила влияния референса
    
    async with aiohttp.ClientSession() as session:
        try:
            # Шаг 1: Отправляем запрос
            logger.info(f"Отправка запроса в Polza: {prompt[:50]}...")
            async with session.post(f"{POLZA_API_URL}/v1/media", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Polza API ошибка {resp.status}: {error_text}")
                    return None
                
                data = await resp.json()
                task_id = data.get("id")
                
                if not task_id:
                    logger.error("Не получен task_id от Polza")
                    return None
                
                logger.info(f"Task ID: {task_id}")
            
            # Шаг 2: Ожидаем результат (polling)
            max_attempts = 30
            for attempt in range(max_attempts):
                await asyncio.sleep(2)  # Ждём 2 секунды между проверками
                
                async with session.get(f"{POLZA_API_URL}/v1/media/{task_id}", headers=headers) as status_resp:
                    if status_resp.status != 200:
                        continue
                    
                    status_data = await status_resp.json()
                    status = status_data.get("status")
                    
                    if status == "completed":
                        # Получаем URL изображения
                        output = status_data.get("output", {})
                        images = output.get("images", [])
                        
                        if images and len(images) > 0:
                            image_url = images[0].get("url")
                            if image_url:
                                # Скачиваем изображение
                                async with session.get(image_url) as img_resp:
                                    if img_resp.status == 200:
                                        img_data = await img_resp.read()
                                        logger.info(f"✅ Изображение получено, размер: {len(img_data)} байт")
                                        return BytesIO(img_data)
                    elif status == "failed":
                        error = status_data.get("error", "Неизвестная ошибка")
                        logger.error(f"Polza задача завершилась ошибкой: {error}")
                        return None
                    else:
                        logger.info(f"Ожидание завершения... статус: {status}")
            
            logger.error("Таймаут ожидания генерации")
            return None
            
        except asyncio.TimeoutError:
            logger.error("Таймаут Polza API")
            return None
        except Exception as e:
            logger.error(f"Ошибка при запросе к Polza: {e}")
            return None

async def generate_image(prompt: str) -> Optional[BytesIO]:
    """Генерация изображения из текста"""
    return await generate_with_polza(prompt, is_edit=False)

async def edit_image(image_bytes: BytesIO, prompt: str) -> Optional[BytesIO]:
    """Редактирование изображения"""
    # Конвертируем изображение в base64
    image_bytes.seek(0)
    image_base64 = base64.b64encode(image_bytes.read()).decode('utf-8')
    return await generate_with_polza(prompt, is_edit=True, image_base64=image_base64)

# ============================================================
# КОМАНДЫ БОТА
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user
    register_user(user.id, user.username, user.first_name, user.last_name)
    
    # Приветствие для админа
    if user.id == ADMIN_ID:
        await message.answer(
            f"👑 *Добро пожаловать, Администратор!*\n\n"
            f"🌙 *{BOT_NAME}* — твой личный AI-художник на Polza.ai\n\n"
            f"✅ *Твой Admin ID:* `{ADMIN_ID}`\n"
            f"🔧 *Доступны админ-команды:* /admin\n"
            f"💰 *API ключ Polza:* активен\n\n"
            f"Обычным пользователям доступно:\n"
            f"• {FREE_GENERATIONS_PER_DAY} генераций/день бесплатно\n"
            f"• {FREE_EDITS_PER_DAY} редактирований/день бесплатно",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"🌙 *{BOT_NAME}* — твой личный AI-художник!\n\n"
            f"✨ Привет, {user.first_name}! Я создаю и редактирую изображения через нейросеть Qwen/Image-2.\n\n"
            f"**🎨 Что я умею:**\n"
            f"• Генерировать картинки из текста\n"
            f"• Редактировать твои фотографии\n"
            f"• Работать с Telegram Stars 💫\n\n"
            f"**📝 Примеры:**\n"
            f"• `закат на море`\n"
            f"• `киберпанк город`\n"
            f"• `кот в космосе`\n\n"
            f"💎 *Бесплатно:* {FREE_GENERATIONS_PER_DAY} генераций и {FREE_EDITS_PER_DAY} редактирований в день!\n\n"
            f"/status — твоя статистика\n"
            f"/buy — купить премиум\n"
            f"/help — помощь",
            parse_mode="Markdown"
        )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        f"📖 *Помощь по {BOT_NAME}*\n\n"
        f"**✨ Генерация:** напиши текст — я нарисую\n"
        f"Пример: `розовый закат в горах`\n\n"
        f"**🖼 Редактирование:** отправь фото + описание в подписи\n"
        f"Пример: `сделай чёрно-белым`\n\n"
        f"**⭐ Цены (Telegram Stars):**\n"
        f"• Генерация: {PRICE_GENERATION} ⭐\n"
        f"• Редактирование: {PRICE_EDIT} ⭐\n"
        f"• Премиум ({PREMIUM_DAYS} дней): {PREMIUM_PRICE} ⭐\n\n"
        f"**🤖 Используемая модель:** Qwen/Image-2 (Polza.ai)\n\n"
        f"**📊 Команды:**\n"
        f"/start — начать\n"
        f"/status — моя статистика\n"
        f"/buy — купить премиум\n"
        f"/help — эта справка",
        parse_mode="Markdown"
    )

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    user_id = message.from_user.id
    premium = is_premium(user_id)
    
    if premium:
        days_left = get_premium_days_left(user_id)
        await message.answer(
            f"🌟 *Твой статус: ПРЕМИУМ* 🌟\n\n"
            f"✨ Безлимитные генерации\n"
            f"🖼 Безлимитные редактирования\n"
            f"📅 Осталось дней: {days_left}\n"
            f"🎨 Модель: Qwen/Image-2\n\n"
            f"Спасибо, что с нами! 🎨",
            parse_mode="Markdown"
        )
    else:
        today_gen = get_user_generations_today(user_id)
        today_edit = get_user_edits_today(user_id)
        remaining_gen = max(0, FREE_GENERATIONS_PER_DAY - today_gen)
        remaining_edit = max(0, FREE_EDITS_PER_DAY - today_edit)
        
        await message.answer(
            f"📊 *Твоя статистика*\n\n"
            f"🎨 Генераций осталось: {remaining_gen} из {FREE_GENERATIONS_PER_DAY}\n"
            f"🖼 Редактирований осталось: {remaining_edit} из {FREE_EDITS_PER_DAY}\n"
            f"🎨 Модель: Qwen/Image-2\n\n"
            f"💫 *Купи премиум и получи безлимит!*\n"
            f"/buy — {PREMIUM_PRICE} ⭐ на {PREMIUM_DAYS} дней",
            parse_mode="Markdown"
        )

@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    prices = [LabeledPrice(label=f"Безлимит на {PREMIUM_DAYS} дней", amount=PREMIUM_PRICE)]
    
    await message.answer_invoice(
        title="🌙 SelenaArtBot Premium",
        description=f"Безлимитные генерации и редактирование на {PREMIUM_DAYS} дней!\n🎨 Нейросеть Qwen/Image-2",
        payload="premium_purchase",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="buy_premium"
    )

@dp.pre_checkout_query()
async def pre_checkout_handler(query: types.PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def payment_success(message: types.Message):
    user_id = message.from_user.id
    payload = message.successful_payment.invoice_payload
    
    if payload == "premium_purchase":
        set_premium(user_id)
        await message.answer(
            f"✅ *Поздравляю! Премиум активирован!*\n\n"
            f"✨ Теперь у тебя безлимит на {PREMIUM_DAYS} дней\n"
            f"🎨 Генерируй и редактируй сколько хочешь с Qwen/Image-2!",
            parse_mode="Markdown"
        )
    elif payload.startswith("generation:"):
        prompt = payload.replace("generation:", "")
        await process_generation(message, prompt, is_paid=True)
    elif payload.startswith("edit:"):
        prompt = payload.replace("edit:", "")
        await message.answer(f"🖼 Обрабатываю платное редактирование...")

async def process_generation(message: types.Message, prompt: str, is_paid: bool = False):
    """Обработка генерации"""
    status_msg = await message.answer(f"🎨 *Рисую через Qwen/Image-2:*\n`{prompt[:80]}`\n⏳ Обычно 5-15 секунд...", parse_mode="Markdown")
    
    img_bytes = await generate_image(prompt)
    
    if img_bytes:
        photo = BufferedInputFile(img_bytes.getvalue(), filename="selena_art.png")
        await message.answer_photo(
            photo,
            caption=f"🌙 *{BOT_NAME} нарисовала:*\n✨ _{prompt[:100]}_\n\n🎨 Модель: Qwen/Image-2",
            parse_mode="Markdown"
        )
        await status_msg.delete()
    else:
        await status_msg.edit_text(
            "❌ *Ошибка генерации через Polza API*\n\n"
            "Возможные причины:\n"
            "• Превышен лимит запросов\n"
            "• Проблемы с API ключом\n"
            "• Попробуй другой промпт\n\n"
            "Если ошибка повторяется, напиши @admin",
            parse_mode="Markdown"
        )

# ============================================================
# ОБРАБОТКА ТЕКСТА (ГЕНЕРАЦИЯ)
# ============================================================

@dp.message(F.text & ~F.text.startswith('/'))
async def generate_by_text(message: types.Message):
    user_id = message.from_user.id
    prompt = message.text.strip()
    
    register_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    
    if len(prompt) > 300:
        await message.answer("❌ Слишком длинный запрос (максимум 300 символов)")
        return
    
    if len(prompt) < 3:
        await message.answer("❌ Напиши подробнее, что я должна нарисовать (минимум 3 символа)")
        return
    
    premium = is_premium(user_id)
    today_used = get_user_generations_today(user_id)
    
    if not premium and today_used >= FREE_GENERATIONS_PER_DAY:
        prices = [LabeledPrice(label="Одна генерация", amount=PRICE_GENERATION)]
        await message.answer_invoice(
            title="🎨 Генерация изображения",
            description=f"Запрос: {prompt[:80]}",
            payload=f"generation:{prompt}",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="generate_one"
        )
        return
    
    if not premium:
        increment_user_generations(user_id)
    
    await process_generation(message, prompt)

# ============================================================
# ОБРАБОТКА ФОТО (РЕДАКТИРОВАНИЕ)
# ============================================================

@dp.message(F.photo)
async def edit_photo(message: types.Message):
    user_id = message.from_user.id
    edit_prompt = message.caption
    
    register_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    
    if not edit_prompt:
        await message.answer(
            "🖼 *Чтобы отредактировать фото,* напиши изменения в подписи!\n\n"
            "Примеры:\n"
            "• `сделай чёрно-белым`\n"
            "• `добавь радугу`\n"
            "• `сделай фон розовым`",
            parse_mode="Markdown"
        )
        return
    
    if len(edit_prompt) > 200:
        await message.answer("❌ Слишком длинное описание (максимум 200 символов)")
        return
    
    premium = is_premium(user_id)
    today_used = get_user_edits_today(user_id)
    
    if not premium and today_used >= FREE_EDITS_PER_DAY:
        prices = [LabeledPrice(label="Одно редактирование", amount=PRICE_EDIT)]
        await message.answer_invoice(
            title="🖼 Редактирование фото",
            description=f"Изменить: {edit_prompt[:80]}",
            payload=f"edit:{edit_prompt}",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="edit_one"
        )
        return
    
    if not premium:
        increment_user_edits(user_id)
    
    status_msg = await message.answer(f"🖼 *Редактирую через Qwen/Image-2:*\n_{edit_prompt[:80]}_\n⏳ Подожди...", parse_mode="Markdown")
    
    try:
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = BytesIO()
        await bot.download_file(file.file_path, file_bytes)
        file_bytes.seek(0)
        
        edited_img = await edit_image(file_bytes, edit_prompt)
        
        if edited_img:
            photo = BufferedInputFile(edited_img.getvalue(), filename="edited.png")
            await message.answer_photo(
                photo,
                caption=f"✅ *Отредактировано!*\n\n📝 _{edit_prompt[:100]}_\n\n🎨 Модель: Qwen/Image-2",
                parse_mode="Markdown"
            )
            await status_msg.delete()
        else:
            await status_msg.edit_text(
                "❌ *Не удалось отредактировать фото*\n\n"
                "Попробуй проще описать изменения",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Ошибка редактирования: {e}")
        await status_msg.edit_text("❌ Произошла ошибка. Попробуй ещё раз.")

# ============================================================
# АДМИН-КОМАНДЫ
# ============================================================

@dp.message(Command("admin"))
async def admin_help(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Только для администратора")
        return
    
    await message.answer(
        f"👑 *Админ-панель {BOT_NAME}*\n\n"
        f"**Твой Admin ID:** `{ADMIN_ID}`\n\n"
        f"**Доступные команды:**\n"
        f"/stats — статистика бота\n"
        f"/premium [ID] [дней] — выдать премиум\n"
        f"/unpremium [ID] — снять премиум\n"
        f"/broadcast [текст] — рассылка\n"
        f"/users — список пользователей (первые 20)\n\n"
        f"**Примеры:**\n"
        f"`/premium 532229128 30`\n"
        f"`/broadcast Всем привет!`\n\n"
        f"**API Polza:** ✅ активен\n"
        f"**Модель:** Qwen/Image-2",
        parse_mode="Markdown"
    )

@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Только для админа")
        return
    
    total_users = get_total_users_count()
    premium_users = get_premium_users_count()
    
    await message.answer(
        f"📊 *Статистика {BOT_NAME}*\n\n"
        f"👥 Всего пользователей: `{total_users}`\n"
        f"⭐ Премиум: `{premium_users}`\n"
        f"📈 Бесплатных генераций в день: `{FREE_GENERATIONS_PER_DAY}`\n"
        f"🖼 Бесплатных редактирований: `{FREE_EDITS_PER_DAY}`\n\n"
        f"💰 Цены:\n"
        f"• Генерация: {PRICE_GENERATION} ⭐\n"
        f"• Редактирование: {PRICE_EDIT} ⭐\n"
        f"• Премиум: {PREMIUM_PRICE} ⭐\n\n"
        f"🎨 Модель: Qwen/Image-2 (Polza.ai)",
        parse_mode="Markdown"
    )

@dp.message(Command("users"))
async def admin_users(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, first_name, premium_until FROM users ORDER BY registered_at DESC LIMIT 20")
    users = cursor.fetchall()
    conn.close()
    
    if not users:
        await message.answer("Нет пользователей")
        return
    
    text = "👥 *Последние 20 пользователей:*\n\n"
    for user in users:
        user_id, username, first_name, premium_until = user
        premium_status = "⭐" if premium_until > int(datetime.now().timestamp()) else "🆓"
        name_display = first_name or username or str(user_id)
        text += f"{premium_status} `{user_id}` — {name_display}\n"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("premium"))
async def give_premium(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: /premium [user_id] [дней]")
        return
    
    try:
        user_id = int(args[1])
        days = int(args[2]) if len(args) > 2 else PREMIUM_DAYS
        set_premium(user_id, days)
        await message.answer(f"✅ Премиум выдан пользователю `{user_id}` на {days} дней", parse_mode="Markdown")
        
        try:
            await bot.send_message(
                user_id,
                f"🌟 *Поздравляю!*\n\nВам выдан премиум-доступ к {BOT_NAME} на {days} дней!\n✨ Теперь безлимитные генерации и редактирования с Qwen/Image-2!",
                parse_mode="Markdown"
            )
        except:
            pass
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("unpremium"))
async def remove_premium_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: /unpremium [user_id]")
        return
    
    try:
        user_id = int(args[1])
        remove_premium(user_id)
        await message.answer(f"✅ Премиум снят с пользователя `{user_id}`", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("broadcast"))
async def broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("❌ Использование: /broadcast [текст рассылки]")
        return
    
    await message.answer("📢 Начинаю рассылку...")
    
    users = get_all_user_ids()
    sent = 0
    
    for user_id in users:
        try:
            await bot.send_message(
                user_id,
                f"📢 *Анонс от {BOT_NAME}*\n\n{text}",
                parse_mode="Markdown"
            )
            sent += 1
            await asyncio.sleep(0.05)
        except:
            pass
    
    await message.answer(f"✅ Рассылка отправлена {sent} пользователям")

# ============================================================
# ЗАПУСК БОТА
# ============================================================

async def main():
    """Запуск бота"""
    init_database()
    logger.info(f"🚀 Запуск {BOT_NAME}...")
    logger.info(f"👑 Admin ID: {ADMIN_ID}")
    logger.info(f"🎨 Модель: Qwen/Image-2 (Polza.ai)")
    
    # Удаляем вебхук и запускаем поллинг
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())