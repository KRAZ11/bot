import aiosqlite 
import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import KeyboardButton
from sqlalchemy import Column, Integer, String, BigInteger, delete, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ ---
TOKEN = "8653033022:AAFvthAhlqpc9wSDwYoa99D_vl5SQB4XBKU"
DATABASE_URL = "sqlite+aiosqlite:///database.db"  # SQLite

Base = declarative_base()

# Модель базы данных
class Subscription(Base):
    __tablename__ = 'subscriptions'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger)
    anime_id = Column(Integer)
    title = Column(String)
    next_ep_at = Column(String, nullable=True)

# Инициализация базы и бота
engine = create_async_engine(DATABASE_URL)
async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_main_kb():
    """Создает главное меню с кнопками"""
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="➕ Добавить аниме"), KeyboardButton(text="📅 Мой список"))
    builder.row(KeyboardButton(text="❌ Удалить аниме"))
    return builder.as_markup(resize_keyboard=True)

def get_time_left(date_str):
    """Рассчитывает время до выхода серии"""
    if not date_str: return "Дата выхода серии неизвестна"
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        diff = dt - datetime.now(timezone.utc)
        if diff.total_seconds() <= 0: return "Серия уже вышла!"
        return f"Осталось: {diff.days} дн. и {diff.seconds // 3600} ч."
    except: return "Ошибка даты"

async def get_shikimori_data(path):
    headers = {
        'User-Agent': 'TukTukAnimeBot/1.0',
        'Accept': 'application/json'
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        clean_path = path.lstrip('/')
        url = f"https://shikimori.io/api/{clean_path}"
        
        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    return await resp.json()
                logging.error(f"Ошибка API: {resp.status}")
                return None
        except Exception as e:
            logging.error(f"Сетевая ошибка: {e}")
            return None

# --- ОБРАБОТКА КНОПОК МЕНЮ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Запуск бота и показ меню"""
    await message.answer(
        "👋 Привет! Я твой аниме-календарь ТУКТУК",
        reply_markup=get_main_kb()
    )

@dp.message(F.text == "➕ Добавить аниме")
async def add_btn_handler(message: types.Message):
    await message.answer("Введите название аниме для поиска (например: Адский рай):")

@dp.message(F.text == "📅 Мой список")
async def list_btn_handler(message: types.Message):
    async with async_session() as session:
        res = await session.execute(select(Subscription).where(Subscription.user_id == message.from_user.id))
        subs = res.scalars().all()
        if not subs: 
            return await message.answer("🗓 Ваш список пуст. Нажмите кнопку «Добавить аниме».")
        
        response = "📅 **Ваши подписки:**\n\n"
        for s in subs:
            response += f"🔹 **{s.title}**\n└ {get_time_left(s.next_ep_at)}\n\n"
        await message.answer(response, parse_mode="Markdown")

@dp.message(F.text == "❌ Удалить аниме")
async def delete_btn_handler(message: types.Message):
    async with async_session() as session:
        res = await session.execute(select(Subscription).where(Subscription.user_id == message.from_user.id))
        subs = res.scalars().all()
        if not subs: 
            return await message.answer("Список пуст, удалять нечего.")
        
        builder = InlineKeyboardBuilder()
        for s in subs:
            builder.row(types.InlineKeyboardButton(text=f"❌ {s.title}", callback_data=f"del_{s.id}"))
        await message.answer("Выберите аниме для удаления:", reply_markup=builder.as_markup())

# --- ЛОГИКА ПОИСКА И ПОДПИСКИ ---

@dp.message(F.text & ~F.text.startswith("/"))
async def smart_search(message: types.Message):
    """Умный поиск по любому тексту (кроме команд и кнопок)"""
    if message.text in ["➕ Добавить аниме", "📅 Мой список", "❌ Удалить аниме"]:
        return

    query = message.text.strip()
    results = await get_shikimori_data(f"animes?search={query}&limit=5")
    
    if not results:
        return await message.answer("Ничего не найдено. Попробуйте другое название.")

    builder = InlineKeyboardBuilder()
    for a in results:
        year = a['aired_on'][:4] if a.get('aired_on') else "????"
        builder.row(types.InlineKeyboardButton(
            text=f"{a['russian'] or a['name']} ({year})", 
            callback_data=f"sub_{a['id']}")
        )
    await message.answer(f"Результаты по запросу «{query}»:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("sub_"))
async def save_sub(callback: types.CallbackQuery):
    """Сохранение подписки"""
    anime_id = int(callback.data.split("_")[1])
    anime = await get_shikimori_data(f"animes/{anime_id}")
    
    async with async_session() as session:
        stmt = select(Subscription).where(Subscription.user_id == callback.from_user.id, Subscription.anime_id == anime_id)
        existing = await session.execute(stmt)
        if existing.scalar():
            return await callback.message.edit_text("Вы уже подписаны на этот сезон!")

        new_sub = Subscription(
            user_id=callback.from_user.id,
            anime_id=anime_id,
            title=anime['russian'] or anime['name'],
            next_ep_at=anime.get('next_episode_at')
        )
        session.add(new_sub)
        await session.commit()

    await callback.message.edit_text(f"✅ Подписка оформлена на:\n**{new_sub.title}**", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("del_"))
async def process_deletion(callback: types.CallbackQuery):
    """Удаление подписки"""
    sub_id = int(callback.data.split("_")[1])
    async with async_session() as session:
        await session.execute(delete(Subscription).where(Subscription.id == sub_id))
        await session.commit()
    await callback.message.edit_text("✅ Аниме удалено из вашего списка.")

# --- ФОНОВАЯ ЗАДАЧА УВЕДОМЛЕНИЙ ---

async def check_updates():
    """Раз в 3 часа проверяет изменения дат выхода"""
    async with async_session() as session:
        res = await session.execute(select(Subscription))
        for sub in res.scalars().all():
            data = await get_shikimori_data(f"animes/{sub.anime_id}")
            if data and data.get('next_episode_at') != sub.next_ep_at:
                await bot.send_message(
                    sub.user_id, 
                    f"🌟 **Обновление!**\nВ аниме «{sub.title}» изменилась дата выхода серии или серия вышла!\nПроверь график: {get_time_left(data.get('next_episode_at'))}"
                )
                sub.next_ep_at = data.get('next_episode_at')
                await session.commit()

# --- ЗАПУСК ---

async def main():
    # Создаем таблицы в БД
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Запускаем планировщик уведомлений
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_updates, 'interval', minutes=5)
    scheduler.start()

    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")