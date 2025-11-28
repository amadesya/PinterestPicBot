import asyncio
import logging
import os
from typing import List, Set

from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from playwright.async_api import async_playwright, Page

# ---------------- Конфигурация логов ----------------
logging.basicConfig(
    filename="bot_errors.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ---------------- Инициализация Telegram ----------------
TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN environment variable is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ---------------- Состояние (в памяти) ----------------
user_state = {}

# Параметры работы
BLOCK_SIZE = 5           # сколько картинок отправляем за раз
SCRROLLS_PER_FETCH = 5   # сколько раз скроллить при каждом доп.запросе
MIN_QUEUE_THRESHOLD = 8  # когда в очереди меньше этого числа — подгружать ещё
MAX_FETCH_ATTEMPTS = 3   # максимум попыток fetch до остановки


# ---------------- Вспомогательные клавиатуры ----------------
def get_more_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Показать ещё", callback_data="more")]]
    )


def extract_highest_resolution_url(srcset: str) -> str:
    """
    Извлекает URL с максимальным разрешением из srcset.
    srcset формат: "url1 100w, url2 200w, url3 500w"
    """
    try:
        parts = [p.strip() for p in srcset.split(',') if p.strip()]
        # Каждая часть: "url width"
        max_width = 0
        best_url = None
        
        for part in parts:
            tokens = part.split()
            if len(tokens) >= 2:
                url = tokens[0]
                width_str = tokens[1].rstrip('w')
                try:
                    width = int(width_str)
                    if width > max_width:
                        max_width = width
                        best_url = url
                except ValueError:
                    continue
        
        return best_url if best_url else parts[-1].split()[0]
    except Exception:
        return None


# ---------------- Парсер Pinterest ----------------
async def fetch_images_from_pinterest(query: str, page: Page, already_seen: Set[str]) -> List[str]:
    """
    Парсит страницу Pinterest с данным query в контексте уже открытой страницы.
    Скроллит несколько раз, собирает ссылки картинок в максимальном разрешении.
    """
    try:
        await page.goto(f"https://www.pinterest.com/search/pins/?q={query.replace(' ', '%20')}", timeout=60000)
        await page.wait_for_selector("img[srcset]", timeout=20000)
    except Exception as e:
        logging.error(f"Ошибка при заходе на страницу Pinterest: {e}")
    
    # Скроллим страницу для подгрузки новых картинок
    for _ in range(SCRROLLS_PER_FETCH):
        try:
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)
        except Exception as e:
            logging.error(f"Ошибка при скролле Pinterest: {e}")

    # Получаем все img элементы и извлекаем URL в максимальном разрешении
    try:
        imgs = await page.query_selector_all("img[srcset]")
        results = []
        
        for el in imgs:
            try:
                # Приоритет: srcset с максимальным разрешением
                srcset = await el.get_attribute("srcset")
                if srcset:
                    url = extract_highest_resolution_url(srcset)
                    if url and url not in already_seen and url.startswith('http'):
                        # Фильтруем очевидные превью и иконки
                        if '60x60' not in url and '75x75' not in url and '236x' not in url:
                            results.append(url)
                            already_seen.add(url)
            except Exception:
                continue
        
        return results
    except Exception as e:
        logging.error(f"Ошибка при извлечении изображений: {e}")
        return []


async def search_and_enqueue_more(user_id: int):
    """
    Открывает браузер, парсит Pinterest и добавляет новые ссылки в очередь пользователя.
    """
    state = user_state.get(user_id)
    if not state:
        return

    # Если уже идёт загрузка — не делаем второй запрос
    if state.get("is_fetching"):
        return

    # Проверяем количество неудачных попыток
    if state.get("fetch_attempts", 0) >= MAX_FETCH_ATTEMPTS:
        state["fetch_exhausted"] = True
        return

    state["is_fetching"] = True
    query = state["query"]
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                new_imgs = await fetch_images_from_pinterest(query, page, state["shown"])
                
                queued_set = set(state["queue"])
                appended = 0
                
                for img in new_imgs:
                    if img not in queued_set:
                        state["queue"].append(img)
                        queued_set.add(img)
                        appended += 1
                
                # Если ничего не добавилось — увеличиваем счётчик неудач
                if appended == 0:
                    state["fetch_attempts"] = state.get("fetch_attempts", 0) + 1
                else:
                    # Сбрасываем счётчик при успехе
                    state["fetch_attempts"] = 0
                    
            except Exception as e:
                logging.error(f"Ошибка во время парсинга для user {user_id}: {e}")
                state["fetch_attempts"] = state.get("fetch_attempts", 0) + 1
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
    except Exception as e:
        logging.error(f"Ошибка запуска playwright: {e}")
        state["fetch_attempts"] = state.get("fetch_attempts", 0) + 1
    finally:
        state["is_fetching"] = False


# ---------------- Отправка блока картинок ----------------
async def send_block(user_id: int, call: CallbackQuery = None):
    """
    Берёт из очереди BLOCK_SIZE картинок, отправляет их пользователю.
    """
    state = user_state.get(user_id)
    if not state:
        return

    # Если в очереди мало картинок и не исчерпан лимит попыток — подгружаем
    if len(state["queue"]) < MIN_QUEUE_THRESHOLD and not state.get("is_fetching") and not state.get("fetch_exhausted"):
        asyncio.create_task(search_and_enqueue_more(user_id))

    # Если очередь пустая — подождём
    if not state["queue"] and state.get("is_fetching"):
        waited = 0.0
        while waited < 5.0 and not state["queue"]:
            await asyncio.sleep(0.5)
            waited += 0.5

    # Если очередь пустая и больше нечего грузить
    if not state["queue"]:
        # Проверяем, было ли вообще что-то показано
        if len(state["shown"]) == 0:
            try:
                msg = "❌ Не удалось найти картинки по этому запросу. Попробуй другой запрос."
                if call:
                    await call.message.answer(msg)
                else:
                    await bot.send_message(user_id, msg)
            except Exception as e:
                logging.error(f"Ошибка отправки сообщения пользователю {user_id}: {e}")
        else:
            # Если что-то уже показывалось — просто сообщаем что больше нет
            try:
                msg = "📭 Больше новых картинок не найдено. Попробуй другой запрос!"
                if call:
                    await call.message.answer(msg)
                else:
                    await bot.send_message(user_id, msg)
            except Exception as e:
                logging.error(f"Ошибка отправки сообщения пользователю {user_id}: {e}")
        return

    # Формируем блок для отправки
    to_send = []
    while state["queue"] and len(to_send) < BLOCK_SIZE:
        to_send.append(state["queue"].pop(0))

    # Отправляем картинки
    success_count = 0
    for img in to_send:
        try:
            await bot.send_photo(user_id, img)
            state["shown"].add(img)
            success_count += 1
        except Exception as e:
            logging.error(f"Ошибка отправки фото {img} пользователю {user_id}: {e}")

    # Отправляем кнопку "Показать ещё" только если есть шанс найти ещё
    if success_count > 0 and not state.get("fetch_exhausted"):
        try:
            keyboard = get_more_keyboard()
            if call:
                await call.message.answer("Хотите ещё?", reply_markup=keyboard)
            else:
                await bot.send_message(user_id, "Хотите ещё?", reply_markup=keyboard)
        except Exception as e:
            logging.error(f"Ошибка отправки кнопки пользователю {user_id}: {e}")


# ---------------- Обработчики команд и сообщений ----------------
@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Привет! Введи запрос и я пришлю картинки из Pinterest в высоком разрешении. Нажимай «Показать ещё» чтобы подгружать следующие изображения.")


@router.message()
async def handle_search(message: Message):
    query = message.text.strip()
    user_id = message.from_user.id

    # Подготовим состояние пользователя
    st = user_state.setdefault(user_id, {
        "query": query,
        "queue": [],
        "shown": set(),
        "history": [],
        "is_fetching": False,
        "fetch_attempts": 0,
        "fetch_exhausted": False
    })

    # Если новый запрос — обновляем query, очищаем всё
    if st["query"] != query:
        st["query"] = query
        st["queue"].clear()
        st["shown"].clear()
        st["fetch_attempts"] = 0
        st["fetch_exhausted"] = False

    st["history"].append(query)

    await message.answer("Ищу изображения... 🔍")

    # Заполнение очереди
    await search_and_enqueue_more(user_id)

    # Если после fetch очередь пуста — сообщим
    if not st["queue"]:
        await message.answer("❌ Ничего не найдено. Попробуй другой запрос.")
        return

    await send_block(user_id)


# ---------------- Callback для кнопки "Показать ещё" ----------------
@router.callback_query(lambda c: c.data == "more")
async def more_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    
    if user_id not in user_state:
        try:
            await callback.message.answer("Отправь сначала запрос текстом.")
        except Exception:
            pass
        return

    state = user_state[user_id]
    
    # Подгружаем если нужно и не исчерпано
    if len(state["queue"]) < MIN_QUEUE_THRESHOLD and not state.get("is_fetching") and not state.get("fetch_exhausted"):
        asyncio.create_task(search_and_enqueue_more(user_id))

    await send_block(user_id, call=callback)


# ---------------- Запуск бота ----------------
async def main():
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Остановка бота")
