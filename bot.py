import asyncio
from aiogram import Bot, Dispatcher, Router, types
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from playwright.async_api import async_playwright
import os

TOKEN = os.environ.get("TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

user_queries = {}
user_logs = {}  # лог показанных картинок


async def search_pinterest(query: str, limit: int = 50):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.goto(
            f"https://www.pinterest.com/search/pins/?q={query.replace(' ', '%20')}",
            timeout=60000
        )

        await page.wait_for_selector("img[srcset]", timeout=20000)

        # собираем всё
        srcsets = await page.eval_on_selector_all(
            "img[srcset]",
            "imgs => imgs.map(img => img.srcset.split(', ').map(s => s.split(' ')[0]).pop())"
        )

        await browser.close()
        return srcsets


@router.message(Command("start"))
async def start_cmd(message: Message):
    await message.answer("Привет! Введи запрос — я пришлю картинки из Pinterest 📸")


async def send_next_images(user_id: int, call: CallbackQuery = None):
    state = user_queries.get(user_id)
    if not state:
        return

    offset = state["offset"]
    images = state["images"]
    next_images = images[offset:offset + 5]

    if user_id not in user_logs:
        user_logs[user_id] = []

    # отправляем 5 изображений
    for img in next_images:
        try:
            await bot.send_photo(user_id, img, caption=f"🔗 Ссылка: {img}")
            user_logs[user_id].append(img)
        except Exception:
            await bot.send_message(user_id, f"❌ Не удалось загрузить изображение:\n{img}")

    state["offset"] += 5

    # бесконечный цикл
    if state["offset"] >= len(images):
        state["offset"] = 0

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Показать ещё", callback_data="more")]]
    )

    # обновляем текст сообщения, если вызвано callback'ом
    if call:
        await call.message.edit_text("Показаны 5 изображений. Хочешь ещё?", reply_markup=keyboard)
    else:
        await bot.send_message(user_id, "Показаны 5 изображений. Хочешь ещё?", reply_markup=keyboard)


@router.message()
async def get_images(message: Message):
    query = message.text.strip()
    await message.answer("Ищу изображения... 🔍")

    images = await search_pinterest(query)

    if not images:
        await message.answer("❌ Ничего не найдено. Попробуй другой запрос.")
        return

    user_queries[message.from_user.id] = {"query": query, "images": images, "offset": 0}
    await send_next_images(message.from_user.id)


@router.callback_query(lambda c: c.data == "more")
async def more_callback(callback: CallbackQuery):
    await send_next_images(callback.from_user.id, call=callback)
    await callback.answer()


async def main():
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
