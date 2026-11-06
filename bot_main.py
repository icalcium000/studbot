import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import BigInteger, Integer, String, UniqueConstraint, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ================= ЗАГРУЗКА КОНФИГУРАЦИИ =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Переменная BOT_TOKEN не задана в файле .env!")

raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(x.strip()) for x in raw_admins.split(",") if x.strip().isdigit()}
MAX_SLOTS = int(os.getenv("MAX_SLOTS", 20))

GROUPS = ["Группа 1", "Группа 2", "Группа 3"]

TRACKS = {
    "Коммуникационный": ["Мила", "Яна", "Рент"],
    "Промышленный": ["Мария", "Никита", "Родион"],
    "Продуктовый": ["Стася"],
}

TEACHER_TO_TRACK = {
    teacher: track for track, teachers in TRACKS.items() for teacher in teachers
}

# ================= БАЗА ДАННЫХ =================
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_URL = f"sqlite+aiosqlite:///{DATA_DIR / 'queue_bot.db'}"

engine = create_async_engine(DB_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(100))
    group_name: Mapped[str] = mapped_column(String(50))


class QueueSlot(Base):
    __tablename__ = "queue_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_name: Mapped[str] = mapped_column(String(50))
    track: Mapped[str] = mapped_column(String(50))
    teacher: Mapped[str] = mapped_column(String(50))
    slot_num: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_name: Mapped[str] = mapped_column(String(100))

    __table_args__ = (
        UniqueConstraint("group_name", "teacher", "slot_num", name="uq_slot"),
        UniqueConstraint("group_name", "teacher", "user_id", name="uq_user_teacher"),
    )


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ================= FSM (СОСТОЯНИЯ РЕГИСТРАЦИИ) =================
class RegState(StatesGroup):
    waiting_for_name = State()
    waiting_for_group = State()


# ================= КЛАВИАТУРЫ =================
def kb_groups_reg() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for g in GROUPS:
        builder.button(text=g, callback_data=f"reg_grp:{g}")
    builder.adjust(1)
    return builder.as_markup()


def kb_main_groups() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for g in GROUPS:
        builder.button(text=f"📁 {g}", callback_data=f"v_grp:{g}")
    builder.adjust(1)
    return builder.as_markup()


def kb_tracks(group_name: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for track in TRACKS.keys():
        builder.button(text=track, callback_data=f"v_trk:{group_name}:{track}")
    builder.button(text="⬅️ К выбору группы", callback_data="v_main")
    builder.adjust(1)
    return builder.as_markup()


def kb_teachers(group_name: str, track: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for teacher in TRACKS[track]:
        builder.button(text=f"👤 {teacher}", callback_data=f"v_tch:{group_name}:{teacher}")
    builder.button(text="⬅️ Назад к направлениям", callback_data=f"v_grp:{group_name}")
    builder.adjust(1)
    return builder.as_markup()


def kb_slots(
    group_name: str, teacher: str, occupied: dict[int, QueueSlot]
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i in range(1, MAX_SLOTS + 1):
        mark = "❌" if i in occupied else "🟢"
        builder.button(text=f"{mark} {i}", callback_data=f"slot:{group_name}:{teacher}:{i}")

    builder.adjust(5)

    track = TEACHER_TO_TRACK[teacher]
    builder.row(
        InlineKeyboardButton(
            text="🔄 Обновить", callback_data=f"v_tch:{group_name}:{teacher}"
        ),
        InlineKeyboardButton(
            text="🗑 Выйти из очереди", callback_data=f"leave:{group_name}:{teacher}"
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ К преподавателям", callback_data=f"v_trk:{group_name}:{track}"
        )
    )
    return builder.as_markup()


def kb_admin_reset() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔥 Сбросить ВСЕ группы", callback_data="adm_rst:ALL")
    for g in GROUPS:
        builder.button(text=f"Сбросить: {g}", callback_data=f"adm_rst:{g}")
    builder.adjust(1)
    return builder.as_markup()


# ================= ФОРМАТИРОВАНИЕ И БЕЗОПАСНЫЙ EDIT =================
async def render_queue_view(
    session: AsyncSession, group_name: str, teacher: str
) -> tuple[str, InlineKeyboardMarkup]:
    track = TEACHER_TO_TRACK[teacher]
    stmt = select(QueueSlot).where(
        QueueSlot.group_name == group_name, QueueSlot.teacher == teacher
    )
    result = await session.execute(stmt)
    slots = result.scalars().all()
    occupied = {s.slot_num: s for s in slots}

    lines = [
        "📋 <b>Очередь на защиту</b>",
        f"🎓 Группа: <b>{group_name}</b>",
        f"🧭 Направление: <b>{track}</b>",
        f"👤 Преподаватель: <b>{teacher}</b>\n",
    ]

    for i in range(1, MAX_SLOTS + 1):
        if i in occupied:
            lines.append(f"<b>{i}.</b> {occupied[i].user_name}")
        else:
            lines.append(f"<b>{i}.</b> <i>Свободно</i>")

    lines.append("\n👇 <i>Нажми на свободный номер ниже, чтобы занять или сменить место:</i>")
    return "\n".join(lines), kb_slots(group_name, teacher, occupied)


async def safe_edit_text(message: Message, text: str, reply_markup: InlineKeyboardMarkup = None):
    try:
        await message.edit_text(text=text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logging.warning(f"Ошибка при обновлении сообщения: {e}")


# ================= РОУТЕР И ХЭНДЛЕРЫ =================
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if message.chat.type != "private":
        await message.reply(
            "Для регистрации или смены ФИ напиши мне в личные сообщения! "
            "А чтобы открыть очередь прямо здесь, используй /queue."
        )
        return

    async with async_session() as session:
        user = await session.get(User, message.from_user.id)

    if user:
        await message.answer(
            f"Привет, <b>{user.full_name}</b> ({user.group_name})!\n"
            f"Используй /queue, чтобы открыть списки очередей.\n"
            f"Если хочешь изменить имя или группу — нажми /re_register."
        )
    else:
        await message.answer(
            "Привет! Давай зарегистрируемся для записи на защиты.\n"
            "Напиши свои <b>Фамилию и Имя</b> (например: <i>Иванов Иван</i> или <i>Иван И.</i>):"
        )
        await state.set_state(RegState.waiting_for_name)


@router.message(Command("re_register"))
async def cmd_reregister(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    await message.answer("Введи свои новые <b>Фамилию и Имя</b>:")
    await state.set_state(RegState.waiting_for_name)


@router.message(RegState.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    if not message.text:
        return
    full_name = message.text.strip()
    if len(full_name) < 3 or len(full_name) > 60:
        await message.answer("Пожалуйста, введи корректные Фамилию и Имя (от 3 до 60 символов).")
        return

    await state.update_data(full_name=full_name)
    await message.answer("Отлично! Теперь выбери свою учебную группу:", reply_markup=kb_groups_reg())
    await state.set_state(RegState.waiting_for_group)


@router.callback_query(RegState.waiting_for_group, F.data.startswith("reg_grp:"))
async def process_group(call: CallbackQuery, state: FSMContext):
    group_name = call.data.split(":")[1]
    data = await state.get_data()
    full_name = data["full_name"]

    async with async_session() as session:
        user = await session.get(User, call.from_user.id)
        if user:
            user.full_name = full_name
            user.group_name = group_name
        else:
            user = User(
                tg_id=call.from_user.id, full_name=full_name, group_name=group_name
            )
            session.add(user)
        await session.commit()

    await state.clear()
    await safe_edit_text(
        call.message,
        f"✅ Готово! Ты записан(а) как: <b>{full_name}</b> ({group_name}).\n\n"
        f"Теперь можешь записываться в очередь (команда /queue работает и здесь, и в общем чате).",
    )
    await call.message.answer(
        "Выбери группу для просмотра очередей:", reply_markup=kb_main_groups()
    )


@router.message(Command("queue"))
async def cmd_queue(message: Message):
    await message.answer(
        "📋 <b>Очереди на защиту</b>\nВыбери группу:",
        reply_markup=kb_main_groups(),
    )


@router.callback_query(F.data == "v_main")
async def cb_view_main(call: CallbackQuery):
    await safe_edit_text(
        call.message,
        "📋 <b>Очереди на защиту</b>\nВыбери группу:",
        reply_markup=kb_main_groups(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("v_grp:"))
async def cb_view_group(call: CallbackQuery):
    group_name = call.data.split(":")[1]
    await safe_edit_text(
        call.message,
        f"🎓 Группа: <b>{group_name}</b>\nВыбери направление:",
        reply_markup=kb_tracks(group_name),
    )
    await call.answer()


@router.callback_query(F.data.startswith("v_trk:"))
async def cb_view_track(call: CallbackQuery):
    _, group_name, track = call.data.split(":")
    await safe_edit_text(
        call.message,
        f"🎓 Группа: <b>{group_name}</b>\n🧭 Направление: <b>{track}</b>\nВыбери преподавателя:",
        reply_markup=kb_teachers(group_name, track),
    )
    await call.answer()


@router.callback_query(F.data.startswith("v_tch:"))
async def cb_view_teacher(call: CallbackQuery):
    _, group_name, teacher = call.data.split(":")
    async with async_session() as session:
        text, kb = await render_queue_view(session, group_name, teacher)

    await safe_edit_text(call.message, text, reply_markup=kb)
    await call.answer("Список актуален")


@router.callback_query(F.data.startswith("slot:"))
async def cb_take_slot(call: CallbackQuery):
    _, group_name, teacher, slot_str = call.data.split(":")
    slot_num = int(slot_str)
    track = TEACHER_TO_TRACK[teacher]

    async with async_session() as session:
        user = await session.get(User, call.from_user.id)
        if not user:
            await call.answer(
                "❗ Сначала зарегистрируйся! Напиши боту /start в личные сообщения.",
                show_alert=True,
            )
            return

        if user.group_name != group_name:
            await call.answer(
                f"⛔ Ты из группы «{user.group_name}»! Записываться можно только в очередь своей группы.",
                show_alert=True,
            )
            return

        stmt_slot = select(QueueSlot).where(
            QueueSlot.group_name == group_name,
            QueueSlot.teacher == teacher,
            QueueSlot.slot_num == slot_num,
        )
        existing_slot = (await session.execute(stmt_slot)).scalar_one_or_none()

        if existing_slot:
            if existing_slot.user_id == user.tg_id:
                await call.answer("Это уже твое место!", show_alert=False)
                return
            else:
                await call.answer(
                    f"❌ Место №{slot_num} уже занял(а) {existing_slot.user_name}!",
                    show_alert=True,
                )
                return

        stmt_user_slot = select(QueueSlot).where(
            QueueSlot.group_name == group_name,
            QueueSlot.teacher == teacher,
            QueueSlot.user_id == user.tg_id,
        )
        user_current_slot = (await session.execute(stmt_user_slot)).scalar_one_or_none()

        try:
            if user_current_slot:
                old_num = user_current_slot.slot_num
                user_current_slot.slot_num = slot_num
                user_current_slot.user_name = user.full_name
                await session.commit()
                await call.answer(f"✅ Перенес запись с №{old_num} на №{slot_num}!")
            else:
                new_slot = QueueSlot(
                    group_name=group_name,
                    track=track,
                    teacher=teacher,
                    slot_num=slot_num,
                    user_id=user.tg_id,
                    user_name=user.full_name,
                )
                session.add(new_slot)
                await session.commit()
                await call.answer(f"✅ Ты успешно занял(а) место №{slot_num}!")
        except IntegrityError:
            await session.rollback()
            await call.answer(
                "⚡ Кто-то оказался быстрее! Выбери другое место.",
                show_alert=True,
            )

        text, kb = await render_queue_view(session, group_name, teacher)
        await safe_edit_text(call.message, text, reply_markup=kb)


@router.callback_query(F.data.startswith("leave:"))
async def cb_leave_queue(call: CallbackQuery):
    _, group_name, teacher = call.data.split(":")

    async with async_session() as session:
        stmt = delete(QueueSlot).where(
            QueueSlot.group_name == group_name,
            QueueSlot.teacher == teacher,
            QueueSlot.user_id == call.from_user.id,
        )
        res = await session.execute(stmt)
        await session.commit()

        if res.rowcount > 0:
            await call.answer("🗑 Ты вышел(ла) из очереди к этому преподавателю.")
            text, kb = await render_queue_view(session, group_name, teacher)
            await safe_edit_text(call.message, text, reply_markup=kb)
        else:
            await call.answer("Тебя и так нет в этой очереди.", show_alert=True)


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        "⚙️ <b>Панель администратора</b>\nВыбери, какие списки нужно очистить:",
        reply_markup=kb_admin_reset(),
    )


@router.callback_query(F.data.startswith("adm_rst:"))
async def cb_admin_reset(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет прав администратора!", show_alert=True)
        return

    target = call.data.split(":")[1]
    async with async_session() as session:
        if target == "ALL":
            await session.execute(delete(QueueSlot))
            msg = "✅ Все очереди для всех групп полностью очищены!"
        else:
            await session.execute(
                delete(QueueSlot).where(QueueSlot.group_name == target)
            )
            msg = f"✅ Все очереди для «{target}» очищены!"
        await session.commit()

    await call.answer(msg, show_alert=True)
    await safe_edit_text(call.message, msg)


# ================= ЗАПУСК =================
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")