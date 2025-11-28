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
    Использует поиск через обычную HTML страницу Pinterest.
    Возвращает (список URL, bookmark для следующей страницы)
    """
    try:
        # Используем обычный поиск через веб-страницу
        search_url = f"https://www.pinterest.com/search/pins/?q={query.replace(' ', '%20')}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(search_url, headers=headers, timeout=30, allow_redirects=True) as response:
                if response.status != 200:
                    logging.error(f"Pinterest вернул статус {response.status}")
                    text = await response.text()
                    logging.error(f"Ответ: {text[:500]}")
                    return [], None
                
                html = await response.text()
                logging.info(f"Получена HTML страница, длина: {len(html)}")
                
                # Ищем JSON данные в HTML (Pinterest встраивает данные в скрипты)
                import re
                
                # Pinterest хранит данные в window.__PWS_DATA__
                pattern = r'<script[^>]*>window\.__PWS_DATA__\s*=\s*(\{.*?\});</script>'
                match = re.search(pattern, html, re.DOTALL)
                
                if not match:
                    # Пробуем другой паттерн
                    pattern = r'"props":\s*(\{.*?"initialReduxState".*?\})'
                    matches = re.finditer(pattern, html, re.DOTALL)
                    for m in matches:
                        try:
                            data = json.loads(m.group(1))
                            if "initialReduxState" in data:
                                match = m
                                break
                        except:
                            continue
                
                if not match:
                    logging.error("Не удалось найти данные в HTML")
                    # Пробуем парсить img теги напрямую
                    return await parse_html_images(html, already_seen), None
                
                # Парсим JSON
                try:
                    json_str = match.group(1)
                    data = json.loads(json_str)
                    json_str = match.group(1)
                    data = json.loads(json_str)
                    
                    results = []
                    
                    # Ищем пины в разных местах структуры
                    pins = []
                    if "props" in data and "initialReduxState" in data["props"]:
                        redux = data["props"]["initialReduxState"]
                        if "pins" in redux:
                            pins = list(redux["pins"].values())
                    
                    logging.info(f"Найдено {len(pins)} пинов в JSON")
                    
                    for pin in pins:
                        try:
                            if isinstance(pin, dict) and "images" in pin:
                                images = pin["images"]
                                
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
                    
                    if results:
                        return results, None
                    else:
                        # Если JSON не дал результатов, парсим HTML
                        return await parse_html_images(html, already_seen), None
                        
                except json.JSONDecodeError as e:
                    logging.error(f"Ошибка парсинга JSON: {e}")
                    return await parse_html_images(html, already_seen), None
                
    except asyncio.TimeoutError:
        logging.error("Таймаут запроса к Pinterest")
        return [], None
    except Exception as e:
        logging.error(f"Ошибка при запросе к Pinterest: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return [], None


async def parse_html_images(html: str, already_seen: Set[str]) -> List[str]:
    """
    Парсит img теги из HTML напрямую как запасной вариант.
    """
    import re
    results = []
    
    # Ищем все img теги с pinimg.com
    img_pattern = r'<img[^>]+src="([^"]*pinimg\.com[^"]*)"'
    matches = re.finditer(img_pattern, html)
    
    for match in matches:
        url = match.group(1)
        
        # Фильтруем превью
        if any(x in url for x in ['60x60', '75x75', '236x', 'avatar', 'profile']):
            continue
        
        # Преобразуем в оригинал
        if '/474x/' in url:
            url = url.replace('/474x/', '/originals/')
        elif '/736x/' in url:
            url = url.replace('/736x/', '/originals/')
        
        if url not in already_seen and url.startswith('http'):
            results.append(url)
            already_seen.add(url)
            logging.info(f"✓ Из HTML: {url[:80]}")
    
    # Также ищем в srcset
    srcset_pattern = r'srcset="([^"]*pinimg\.com[^"]*)"'
    matches = re.finditer(srcset_pattern, html)
    
    for match in matches:
        srcset = match.group(1)
        urls = re.findall(r'(https://[^\s,]+)', srcset)
        
        for url in urls:
            if any(x in url for x in ['60x60', '75x75', '236x', 'avatar', 'profile']):
                continue
            
            if '/474x/' in url:
                url = url.replace('/474x/', '/originals/')
            elif '/736x/' in url:
                url = url.replace('/736x/', '/originals/')
            
            if url not in already_seen and url.startswith('http'):
                results.append(url)
                already_seen.add(url)
                logging.info(f"✓ Из srcset: {url[:80]}")
    
    logging.info(f"Всего извлечено из HTML: {len(results)}")
    return results


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
