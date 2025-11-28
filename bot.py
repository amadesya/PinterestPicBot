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
# user_state[user_id] = {
#   "query": str,
#   "queue": List[str],         # подготовленные ссылки к отправке
#   "shown": Set[str],          # ссылки уже показанные (чтобы не повторять)
#   "history": List[str],       # история запросов
#   "is_fetching": bool         # флаг чтобы не запускать параллельные парсеры
# }
user_state = {}

# Параметры работы
BLOCK_SIZE = 5           # сколько картинок отправляем за раз
SCRROLLS_PER_FETCH = 4   # сколько раз скроллить при каждом доп.запросе
MIN_QUEUE_THRESHOLD = 8  # когда в очереди меньше этого числа — подгружать ещё


# ---------------- Вспомогательные клавиатуры ----------------
def get_more_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Показать ещё", callback_data="more")]]
    )


# ---------------- Парсер Pinterest ----------------
async def fetch_images_from_pinterest(query: str, page: Page, already_seen: Set[str]) -> List[str]:
    """
    Парсит страницу Pinterest с данным query в контексте уже открытой страницы.
    Скроллит несколько раз, собирает ссылки картинок и возвращает новые (не из already_seen).
    """
    try:
        # Убедимся, что мы на правильной странице
        await page.goto(f"https://www.pinterest.com/search/pins/?q={query.replace(' ', '%20')}", timeout=60000)
        await page.wait_for_selector("img[srcset]", timeout=20000)
    except Exception as e:
        logging.error(f"Ошибка при заходе на страницу Pinterest: {e}")
        # продолжим — возможно некоторые данные всё же есть
    # Скроллим страницу, чтобы подгрузились новые картинки
    for _ in range(SCRROLLS_PER_FETCH):
        try:
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await asyncio.sleep(1.2)
        except Exception as e:
            logging.error(f"Ошибка при скролле Pinterest: {e}")

    # Получаем все подходящие img элементы и извлекаем src/srcset
    try:
        imgs = await page.query_selector_all("img[srcset], img[src]")
        results = []
        for el in imgs:
            src = None
            try:
                src = await el.get_attribute("src")
                if not src:
                    # Если нет src — пробуем взять srcset и вытянуть первую ссылку
                    srcset = await el.get_attribute("srcset")
                    if srcset:
                        # srcset -> "url1 100w, url2 200w" -> взять первый url (или последний)
                        parts = [p.strip().split(' ')[0] for p in srcset.split(',') if p.strip()]
                        if parts:
                            src = parts[-1]  # беру последний т.к. он часто больше
                if src and src not in already_seen:
                    results.append(src)
                    already_seen.add(src)
            except Exception:
                # отдельный элемент мог упасть — пропускаем
                continue
        return results
    except Exception as e:
        logging.error(f"Ошибка при извлечении изображений: {e}")
        return []


async def search_and_enqueue_more(user_id: int):
    """
    Открывает браузер, парсит Pinterest и добавляет новые ссылки в очередь пользователя.
    Защищено флагом is_fetching для предотвращения параллельных fetch'ей.
    """
    state = user_state.get(user_id)
    if not state:
        return

    # Если уже идёт загрузка — не делаем второй запрос
    if state.get("is_fetching"):
        return

    state["is_fetching"] = True
    query = state["query"]
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                new_imgs = await fetch_images_from_pinterest(query, page, state["shown"])
                # добавляем только уникальные и не показанные (fetch_images_from_pinterest уже исключил shown)
                # но нужно следить, чтобы не добавлять то, что уже в очереди
                queued_set = set(state["queue"])
                appended = 0
                for img in new_imgs:
                    if img not in queued_set:
                        state["queue"].append(img)
                        queued_set.add(img)
                        appended += 1
                if appended == 0:
                    # попробуем ещё один проход с большим скроллом (редкая ситуация)
                    extra_imgs = await fetch_images_from_pinterest(query, page, state["shown"])
                    for img in extra_imgs:
                        if img not in queued_set:
                            state["queue"].append(img)
                            queued_set.add(img)
            except Exception as e:
                logging.error(f"Ошибка во время парсинга для user {user_id}: {e}")
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
    except Exception as e:
        logging.error(f"Ошибка запуска playwright: {e}")
    finally:
        state["is_fetching"] = False


# ---------------- Отправка блока картинок ----------------
async def send_block(user_id: int, call: CallbackQuery = None):
    """
    Берёт из очереди BLOCK_SIZE картинок, отправляет их пользователю и добавляет кнопку 'Показать ещё'.
    Если очередь мала — запускает фоновой fetch, ждёт его (до короткого таймаута) и потом отправляет то, что есть.
    """
    state = user_state.get(user_id)
    if not state:
        return

    # Если в очереди мало картинок, инициируем подгрузку
    if len(state["queue"]) < MIN_QUEUE_THRESHOLD and not state.get("is_fetching"):
        # запускаем fetch без блокировки — он сам ставит is_fetching
        asyncio.create_task(search_and_enqueue_more(user_id))

    # Если очередь пустая — подождём короткое время, чтобы парсер успел что-то положить
    if not state["queue"] and state.get("is_fetching"):
        # подождём до 4 секунд, проверяя очередь каждые 0.5s
        waited = 0.0
        while waited < 4.0 and not state["queue"]:
            await asyncio.sleep(0.5)
            waited += 0.5

    # Если всё ещё ничего — уведомляем пользователя
    if not state["queue"]:
        try:
            if call:
                await call.message.answer("❌ Не удалось найти новые картинки прямо сейчас. Попробуй позже.")
            else:
                await bot.send_message(user_id, "❌ Не удалось найти новые картинки прямо сейчас. Попробуй позже.")
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения о пустой очереди пользователю {user_id}: {e}")
        return

    # формируем блок для отправки
    to_send = []
    while state["queue"] and len(to_send) < BLOCK_SIZE:
        to_send.append(state["queue"].pop(0))

    # отправляем картинки
    for img in to_send:
        try:
            await bot.send_photo(user_id, img, caption=f"🔗 {img}")
            state["shown"].add(img)
        except Exception as e:
            logging.error(f"Ошибка отправки фото {img} пользователю {user_id}: {e}")
            try:
                await bot.send_message(user_id, f"❌ Не удалось загрузить картинку:\n{img}")
            except Exception as e2:
                logging.error(f"Ошибка отправки fallback-сообщения: {e2}")

    # Отправляем кнопку "Показать ещё" прямо после блока
    try:
        keyboard = get_more_keyboard()
        if call:
            # пытаемся отредактировать callback message, но удобнее просто отправить новое сообщение с кнопкой
            await call.message.answer("Хотите ещё?", reply_markup=keyboard)
        else:
            await bot.send_message(user_id, "Хотите ещё?", reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Ошибка отправки кнопки пользователю {user_id}: {e}")


# ---------------- Обработчики команд и сообщений ----------------
@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Привет! Введи запрос и я пришлю картинки из Pinterest. Нажимай «Показать ещё» чтобы подгружать следующие изображения.")


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
        "is_fetching": False
    })

    # Если новый запрос — обновляем query, очищаем queue и shown (чтобы начать свежо)
    if st["query"] != query:
        st["query"] = query
        st["queue"].clear()
        st["shown"].clear()

    st["history"].append(query)

    await message.answer("Ищу изображения... 🔍")

    # Сразу заполнение очереди (синхронно, чтобы пользователь получил первый блок)
    # будем вызывать fetch, который обновит st["queue"]
    await search_and_enqueue_more(user_id)

    # Если после fetch очередь пуста — сообщим, иначе отправим первый блок
    if not st["queue"]:
        await message.answer("❌ Ничего не найдено. Попробуй другой запрос.")
        return

    await send_block(user_id)


# ---------------- Callback для кнопки "Показать ещё" ----------------
@router.callback_query(lambda c: c.data == "more")
async def more_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()  # убираем "часики" в интерфейсе
    # Если пользователь ещё не запрашивал — игнор
    if user_id not in user_state:
        try:
            await callback.message.answer("Отправь сначала запрос текстом.")
        except Exception:
            pass
        return

    # если в очереди мало — инициируем подгрузку
    state = user_state[user_id]
    if len(state["queue"]) < MIN_QUEUE_THRESHOLD and not state.get("is_fetching"):
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
