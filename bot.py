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
    level=logging.INFO,  # Изменено на INFO для отладки
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
    Парсит Pinterest и возвращает список ссылок на оригинальные изображения.
    """
    results = []
    try:
        url = f"https://www.pinterest.com/search/pins/?q={query.replace(' ', '%20')}"
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")

        # Маскируем headless
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        """)

        # Скроллим страницу несколько раз для подгрузки картинок
        for _ in range(SCRROLLS_PER_FETCH):
            await page.evaluate("window.scrollBy(0, window.innerHeight);")
            await asyncio.sleep(1.5)

        # Находим все img, содержащие pinimg (Pinterest)
        imgs = await page.query_selector_all("img[src*='pinimg.com']")
        logging.info(f"Найдено {len(imgs)} img элементов на странице")

        for el in imgs:
            try:
                # Получаем srcset или src
                srcset = await el.get_attribute("srcset")
                src = await el.get_attribute("src")
                url_candidate = None

                if srcset:
                    # Берём URL с наибольшим разрешением
                    parts = [p.strip() for p in srcset.split(',') if p.strip()]
                    max_w = 0
                    for part in parts:
                        tokens = part.split()
                        if len(tokens) >= 2:
                            u, w = tokens[0], int(tokens[1].rstrip('w'))
                            if w > max_w:
                                max_w = w
                                url_candidate = u
                elif src:
                    url_candidate = src

                if url_candidate and url_candidate.startswith("http") and url_candidate not in already_seen:
                    # Фильтруем миниатюры и служебные картинки
                    if all(x not in url_candidate for x in ["236x", "60x", "avatar", "profile", "user"]):
                        # Старайся брать оригинал
                        url_candidate = url_candidate.replace("/236x/", "/originals/").replace("/474x/", "/originals/").replace("/736x/", "/originals/")
                        results.append(url_candidate)
                        already_seen.add(url_candidate)
            except Exception as e:
                logging.error(f"Ошибка обработки img элемента: {e}")
                continue

        logging.info(f"Возвращено {len(results)} новых изображений для запроса '{query}'")
        return results

    except Exception as e:
        logging.error(f"Ошибка при fetch_images_from_pinterest: {e}")
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
            # Запускаем браузер с параметрами для обхода детекции
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox'
                ]
            )
            
            # Создаём контекст с реальным user agent
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            
            page = await context.new_page()
            
            # Скрываем признаки автоматизации
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)
            
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
                    logging.warning(f"Попытка {state['fetch_attempts']}/{MAX_FETCH_ATTEMPTS}: не найдено новых изображений для '{query}'")
                else:
                    # Сбрасываем счётчик при успехе
                    state["fetch_attempts"] = 0
                    logging.info(f"Успешно добавлено {appended} изображений для user {user_id}")
                    
            except Exception as e:
                logging.error(f"Ошибка во время парсинга для user {user_id}: {e}")
                state["fetch_attempts"] = state.get("fetch_attempts", 0) + 1
            finally:
                try:
                    await context.close()
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
