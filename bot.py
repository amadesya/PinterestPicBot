import asyncio
import logging
import os
import json
from typing import List, Set
import aiohttp

from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command

# ---------------- Конфигурация логов ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot_errors.log"),
        logging.StreamHandler()
    ]
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
BLOCK_SIZE = 5
MIN_QUEUE_THRESHOLD = 8
MAX_FETCH_ATTEMPTS = 3


# ---------------- Вспомогательные клавиатуры ----------------
def get_more_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Показать ещё", callback_data="more")]]
    )


# ---------------- Парсер Pinterest через API ----------------
async def fetch_images_from_pinterest_api(query: str, already_seen: Set[str], bookmark: str = None) -> tuple[List[str], str]:
    """
    Использует внутреннее API Pinterest для получения изображений.
    Возвращает (список URL, bookmark для следующей страницы)
    """
    try:
        # Pinterest использует GraphQL API
        url = "https://www.pinterest.com/resource/BaseSearchResource/get/"
        
        # Параметры запроса
        options = {
            "query": query,
            "scope": "pins",
            "page_size": 25
        }
        
        if bookmark:
            options["bookmarks"] = [bookmark]
        
        params = {
            "source_url": f"/search/pins/?q={query}",
            "data": json.dumps({
                "options": options,
                "context": {}
            })
        }
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers, timeout=30) as response:
                if response.status != 200:
                    logging.error(f"Pinterest API вернул статус {response.status}")
                    return [], None
                
                data = await response.json()
                
                # Извлекаем результаты
                results = []
                next_bookmark = None
                
                if "resource_response" in data:
                    resource = data["resource_response"]
                    
                    # Получаем bookmark для следующей страницы
                    if "bookmark" in resource.get("data", {}):
                        next_bookmark = resource["data"]["bookmark"]
                    
                    # Извлекаем пины
                    pins = resource.get("data", {}).get("results", [])
                    
                    logging.info(f"Получено {len(pins)} пинов от API")
                    
                    for pin in pins:
                        try:
                            # Получаем изображение в максимальном качестве
                            images = pin.get("images", {})
                            
                            # Приоритет: orig > originals > 736x > 474x
                            img_url = None
                            if "orig" in images:
                                img_url = images["orig"].get("url")
                            elif "originals" in images:
                                img_url = images["originals"].get("url")
                            elif "736x" in images:
                                img_url = images["736x"].get("url")
                            elif "474x" in images:
                                img_url = images["474x"].get("url")
                            
                            if img_url and img_url not in already_seen:
                                results.append(img_url)
                                already_seen.add(img_url)
                                logging.info(f"✓ Добавлено: {img_url[:80]}")
                        
                        except Exception as e:
                            logging.error(f"Ошибка обработки пина: {e}")
                            continue
                
                return results, next_bookmark
                
    except asyncio.TimeoutError:
        logging.error("Таймаут запроса к Pinterest API")
        return [], None
    except Exception as e:
        logging.error(f"Ошибка при запросе к Pinterest API: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return [], None


async def search_and_enqueue_more(user_id: int):
    """
    Получает изображения через API Pinterest и добавляет их в очередь.
    """
    state = user_state.get(user_id)
    if not state:
        return

    if state.get("is_fetching"):
        return

    if state.get("fetch_attempts", 0) >= MAX_FETCH_ATTEMPTS:
        state["fetch_exhausted"] = True
        return

    state["is_fetching"] = True
    query = state["query"]
    
    try:
        # Используем bookmark для пагинации
        bookmark = state.get("next_bookmark")
        
        new_imgs, next_bookmark = await fetch_images_from_pinterest_api(
            query, 
            state["shown"],
            bookmark
        )
        
        # Сохраняем bookmark для следующего запроса
        state["next_bookmark"] = next_bookmark
        
        queued_set = set(state["queue"])
        appended = 0
        
        for img in new_imgs:
            if img not in queued_set:
                state["queue"].append(img)
                queued_set.add(img)
                appended += 1
        
        if appended == 0:
            state["fetch_attempts"] = state.get("fetch_attempts", 0) + 1
            logging.warning(f"Попытка {state['fetch_attempts']}/{MAX_FETCH_ATTEMPTS}: не найдено новых изображений")
        else:
            state["fetch_attempts"] = 0
            logging.info(f"Успешно добавлено {appended} изображений для user {user_id}")
            
    except Exception as e:
        logging.error(f"Ошибка во время получения данных для user {user_id}: {e}")
        state["fetch_attempts"] = state.get("fetch_attempts", 0) + 1
    finally:
        state["is_fetching"] = False


# ---------------- Отправка блока картинок ----------------
async def send_block(user_id: int, call: CallbackQuery = None):
    """
    Отправляет блок изображений пользователю.
    """
    state = user_state.get(user_id)
    if not state:
        return

    if len(state["queue"]) < MIN_QUEUE_THRESHOLD and not state.get("is_fetching") and not state.get("fetch_exhausted"):
        asyncio.create_task(search_and_enqueue_more(user_id))

    if not state["queue"] and state.get("is_fetching"):
        waited = 0.0
        while waited < 5.0 and not state["queue"]:
            await asyncio.sleep(0.5)
            waited += 0.5

    if not state["queue"]:
        if len(state["shown"]) == 0:
            try:
                msg = "❌ Не удалось найти картинки по этому запросу. Попробуй другой запрос."
                if call:
                    await call.message.answer(msg)
                else:
                    await bot.send_message(user_id, msg)
            except Exception as e:
                logging.error(f"Ошибка отправки сообщения: {e}")
        else:
            try:
                msg = "📭 Больше новых картинок не найдено. Попробуй другой запрос!"
                if call:
                    await call.message.answer(msg)
                else:
                    await bot.send_message(user_id, msg)
            except Exception as e:
                logging.error(f"Ошибка отправки сообщения: {e}")
        return

    to_send = []
    while state["queue"] and len(to_send) < BLOCK_SIZE:
        to_send.append(state["queue"].pop(0))

    success_count = 0
    for img in to_send:
        try:
            await bot.send_photo(user_id, img)
            state["shown"].add(img)
            success_count += 1
        except Exception as e:
            logging.error(f"Ошибка отправки фото {img}: {e}")
            # Пробуем отправить текстом
            try:
                await bot.send_message(user_id, f"🖼 {img}")
            except Exception:
                pass

    if success_count > 0 and not state.get("fetch_exhausted"):
        try:
            keyboard = get_more_keyboard()
            if call:
                await call.message.answer("Хотите ещё?", reply_markup=keyboard)
            else:
                await bot.send_message(user_id, "Хотите ещё?", reply_markup=keyboard)
        except Exception as e:
            logging.error(f"Ошибка отправки кнопки: {e}")


# ---------------- Обработчики команд ----------------
@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Привет! Введи запрос и я пришлю картинки из Pinterest в высоком разрешении.\n\n"
                        "Нажимай «Показать ещё» чтобы подгружать следующие изображения.")


@router.message()
async def handle_search(message: Message):
    query = message.text.strip()
    user_id = message.from_user.id

    logging.info(f"User {user_id} ищет: {query}")

    st = user_state.setdefault(user_id, {
        "query": query,
        "queue": [],
        "shown": set(),
        "history": [],
        "is_fetching": False,
        "fetch_attempts": 0,
        "fetch_exhausted": False,
        "next_bookmark": None
    })

    if st["query"] != query:
        st["query"] = query
        st["queue"].clear()
        st["shown"].clear()
        st["fetch_attempts"] = 0
        st["fetch_exhausted"] = False
        st["next_bookmark"] = None

    st["history"].append(query)

    await message.answer("Ищу изображения... 🔍")

    await search_and_enqueue_more(user_id)

    if not st["queue"]:
        await message.answer("❌ Ничего не найдено. Попробуй другой запрос.")
        return

    await send_block(user_id)


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
    
    if len(state["queue"]) < MIN_QUEUE_THRESHOLD and not state.get("is_fetching") and not state.get("fetch_exhausted"):
        asyncio.create_task(search_and_enqueue_more(user_id))

    await send_block(user_id, call=callback)


# ---------------- Запуск бота ----------------
async def main():
    logging.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Остановка бота")
