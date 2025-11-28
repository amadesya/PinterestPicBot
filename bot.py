import asyncio
import logging
from aiogram import Bot, Dispatcher, Router, types
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.filters import Command
from playwright.async_api import async_playwright
import os

logging.basicConfig(
    filename="bot_errors.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
TOKEN = os.environ.get("TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

user_queries = {}  
user_logs = {}      
user_history = {}   

async def search_pinterest(query: str, limit: int = 50):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            await page.goto(
                f"https://www.pinterest.com/search/pins/?q={query.replace(' ', '%20')}",
                timeout=60000
            )

            await page.wait_for_selector("img[srcset]", timeout=20000)

            srcsets = await page.eval_on_selector_all(
                "img[srcset]",
                "imgs => imgs.map(img => img.srcset.split(', ').map(s => s.split(' ')[0]).pop())"
            )

            await browser.close()
            return srcsets

    except Exception as e:
        logging.error(f"Ошибка в Pinterest парсере: {e}")
        return []
        
def get_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅ Назад", callback_data="prev"),
                InlineKeyboardButton(text="➡ Далее", callback_data="next"),
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="reset")
            ]
        ]
    )
    
async def send_images(user_id: int, call: CallbackQuery = None):
    state = user_queries.get(user_id)
    if not state:
        return

    images = state["images"]
    offset = state["offset"]

    next_images = images[offset:offset + 5]

    if not next_images:
        next_images = images[0:5]
        state["offset"] = 0

    if user_id not in user_logs:
        user_logs[user_id] = []

    for img in next_images:
        try:
            await bot.send_photo(user_id, img, caption=f"🔗 {img}")
            user_logs[user_id].append(img)
        except Exception as e:
            logging.error(f"Ошибка отправки фото: {img}\n{e}")
            await bot.send_message(user_id, f"❌ Не удалось загрузить картинку:\n{img}")

    state["offset"] += 5
    if state["offset"] >= len(images):
        state["offset"] = 0 

    keyboard = get_keyboard()

    if call:
        await call.message.edit_text(
            "Показаны изображения. Листай!",
            reply_markup=keyboard
        )
    else:
        await bot.send_message(
            user_id,
            "Показаны изображения. Листай!",
            reply_markup=keyboard
        )

@router.message()
async def get_images(message: Message):
    query = message.text.strip()
    user_id = message.from_user.id

    await message.answer("Ищу изображения... 🔍")

    images = await search_pinterest(query)

    if not images:
        await message.answer("❌ Не найдено ни одной картинки. Попробуйте другой запрос.")
        return

    if user_id not in user_history:
        user_history[user_id] = []

    user_history[user_id].append(query)

    user_queries[user_id] = {"query": query, "images": images, "offset": 0}

    await send_images(user_id)
    
@router.callback_query(lambda c: c.data == "next")
async def next_btn(callback: CallbackQuery):
    await send_images(callback.from_user.id, call=callback)
    await callback.answer()

@router.callback_query(lambda c: c.data == "prev")
async def prev_btn(callback: CallbackQuery):
    user_id = callback.from_user.id
    state = user_queries.get(user_id)

    state["offset"] -= 10
    if state["offset"] < 0:
        state["offset"] = len(state["images"]) - 5

    await send_images(user_id, call=callback)
    await callback.answer()

@router.callback_query(lambda c: c.data == "reset")
async def reset_btn(callback: CallbackQuery):
    user_id = callback.from_user.id
    user_queries[user_id]["offset"] = 0
    await send_images(user_id, call=callback)
    await callback.answer()

@router.message(Command("start"))
async def start_cmd(message: Message):
    await message.answer("Привет! Введи запрос — я пришлю картинки из Pinterest 📸")


async def main():
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
