
import os
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "487674664"))

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

class SellForm(StatesGroup):
    manufacturer = State()
    model = State()
    condition = State()
    комплект = State()
    photos = State()
    phone = State()

class RepairForm(StatesGroup):
    problem = State()
    model = State()
    photos = State()
    phone = State()

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Продать телефон", callback_data="sell")],
        [InlineKeyboardButton(text="🔧 Ремонт телефона", callback_data="repair")],
        [InlineKeyboardButton(text="💰 Узнать стоимость", callback_data="estimate")],
        [InlineKeyboardButton(text="🛒 Купить телефон", callback_data="buy")],
        [InlineKeyboardButton(text="📍 Адрес и контакты", callback_data="contacts")],
        [InlineKeyboardButton(text="👨‍💻 Связаться с менеджером", callback_data="manager")],
    ])

def back_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")]
    ])

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "<b>👋 Добро пожаловать в RM Service!</b>\n\n"
        "Ремонт, скупка и продажа телефонов в Москве.\n"
        "Выберите нужную услугу 👇",
        reply_markup=main_menu()
    )

@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "<b>🏠 RM Service</b>\n\nВыберите нужную услугу 👇",
        reply_markup=main_menu()
    )
    await call.answer()

@dp.callback_query(F.data == "sell")
async def sell_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(SellForm.manufacturer)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍎 iPhone", callback_data="man_iPhone"),
         InlineKeyboardButton(text="📱 Samsung", callback_data="man_Samsung")],
        [InlineKeyboardButton(text="📱 Xiaomi", callback_data="man_Xiaomi"),
         InlineKeyboardButton(text="📱 Honor", callback_data="man_Honor")],
        [InlineKeyboardButton(text="📱 Huawei", callback_data="man_Huawei"),
         InlineKeyboardButton(text="📱 Другое", callback_data="man_Другое")],
    ])
    await call.message.edit_text("📱 <b>Продажа телефона</b>\n\nВыберите производителя:", reply_markup=kb)
    await call.answer()

@dp.callback_query(SellForm.manufacturer, F.data.startswith("man_"))
async def sell_man(call: CallbackQuery, state: FSMContext):
    await state.update_data(manufacturer=call.data[4:])
    await state.set_state(SellForm.model)
    await call.message.edit_text("✍️ Напишите модель и объём памяти.\n\nНапример: <b>iPhone 15 Pro 256 GB</b>")
    await call.answer()

@dp.message(SellForm.model)
async def sell_model(message: Message, state: FSMContext):
    await state.update_data(model=message.text)
    await state.set_state(SellForm.condition)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Отличное", callback_data="c_Отличное"),
         InlineKeyboardButton(text="🟡 Хорошее", callback_data="c_Хорошее")],
        [InlineKeyboardButton(text="🟠 Есть царапины", callback_data="c_Есть царапины")],
        [InlineKeyboardButton(text="🔴 Есть повреждения", callback_data="c_Есть повреждения"),
         InlineKeyboardButton(text="💥 Неисправен", callback_data="c_Неисправен")],
    ])
    await message.answer("📊 В каком состоянии телефон?", reply_markup=kb)

@dp.callback_query(SellForm.condition, F.data.startswith("c_"))
async def sell_condition(call: CallbackQuery, state: FSMContext):
    await state.update_data(condition=call.data[2:])
    await state.set_state(SellForm.комплект)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Полный комплект", callback_data="set_Полный комплект")],
        [InlineKeyboardButton(text="🔌 Телефон + кабель", callback_data="set_Телефон + кабель")],
        [InlineKeyboardButton(text="📱 Только телефон", callback_data="set_Только телефон")],
    ])
    await call.message.edit_text("📦 Что есть в комплекте?", reply_markup=kb)
    await call.answer()

@dp.callback_query(SellForm.комплект, F.data.startswith("set_"))
async def sell_set(call: CallbackQuery, state: FSMContext):
    await state.update_data(комплект=call.data[4:])
    await state.set_state(SellForm.photos)
    await call.message.edit_text(
        "📸 Пришлите 2–4 фотографии телефона.\n\n"
        "Желательно показать экран, заднюю крышку и корпус.\n\n"
        "Когда закончите, отправьте сообщение <b>Готово</b>."
    )
    await call.answer()

@dp.message(SellForm.photos)
async def sell_photos(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text and message.text.lower() == "готово":
        await state.set_state(SellForm.phone)
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📲 Отправить номер", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
        await message.answer("📞 Оставьте номер телефона для связи с менеджером.", reply_markup=kb)
        return
    photos = data.get("photos", [])
    if message.photo:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        await message.answer(f"📸 Фото получено ({len(photos)}). Отправьте ещё или нажмите/напишите <b>Готово</b>.")
    else:
        await message.answer("Пришлите фотографию или напишите <b>Готово</b>.")

@dp.message(SellForm.phone, F.contact)
async def sell_phone(message: Message, state: FSMContext):
    await finish_sell(message, state, message.contact.phone_number)

@dp.message(SellForm.phone)
async def sell_phone_text(message: Message, state: FSMContext):
    await finish_sell(message, state, message.text)

async def finish_sell(message: Message, state: FSMContext, phone: str):
    data = await state.get_data()
    username = f"@{message.from_user.username}" if message.from_user.username else "не указан"
    text = (
        "🔔 <b>НОВАЯ ЗАЯВКА — СКУПКА</b>\n\n"
        f"📱 Производитель: {data.get('manufacturer','—')}\n"
        f"📱 Модель: {data.get('model','—')}\n"
        f"📊 Состояние: {data.get('condition','—')}\n"
        f"📦 Комплект: {data.get('комплект','—')}\n"
        f"📞 Телефон: {phone}\n"
        f"👤 Telegram: {username}"
    )
    await bot.send_message(ADMIN_ID, text)
    for fid in data.get("photos", []):
        await bot.send_photo(ADMIN_ID, fid)
    await state.clear()
    await message.answer(
        "✅ <b>Заявка принята!</b>\n\nМенеджер RM Service свяжется с вами для уточнения деталей и оценки телефона.",
        reply_markup=main_menu()
    )

@dp.callback_query(F.data == "repair")
async def repair_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(RepairForm.problem)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖥 Разбит экран", callback_data="p_Разбит экран")],
        [InlineKeyboardButton(text="🔋 Быстро разряжается", callback_data="p_Быстро разряжается")],
        [InlineKeyboardButton(text="📱 Не включается", callback_data="p_Не включается")],
        [InlineKeyboardButton(text="💧 Попала вода", callback_data="p_Попала вода")],
        [InlineKeyboardButton(text="📷 Камера", callback_data="p_Камера"),
         InlineKeyboardButton(text="🔊 Нет звука", callback_data="p_Нет звука")],
        [InlineKeyboardButton(text="🔌 Не заряжается", callback_data="p_Не заряжается")],
        [InlineKeyboardButton(text="❓ Другая проблема", callback_data="p_Другая проблема")],
    ])
    await call.message.edit_text("🔧 <b>Ремонт телефона</b>\n\nЧто случилось с телефоном?", reply_markup=kb)
    await call.answer()

@dp.callback_query(RepairForm.problem, F.data.startswith("p_"))
async def repair_problem(call: CallbackQuery, state: FSMContext):
    await state.update_data(problem=call.data[2:])
    await state.set_state(RepairForm.model)
    await call.message.edit_text("✍️ Напишите модель телефона.\n\nНапример: <b>iPhone 14 Pro</b>")
    await call.answer()

@dp.message(RepairForm.model)
async def repair_model(message: Message, state: FSMContext):
    await state.update_data(model=message.text)
    await state.set_state(RepairForm.photos)
    await message.answer("📸 Если можете, пришлите фото повреждения. Затем напишите <b>Готово</b>.")

@dp.message(RepairForm.photos)
async def repair_photos(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text and message.text.lower() == "готово":
        await state.set_state(RepairForm.phone)
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📲 Отправить номер", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
        await message.answer("📞 Оставьте номер телефона для связи с мастером.", reply_markup=kb)
        return
    photos = data.get("photos", [])
    if message.photo:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        await message.answer("📸 Фото получено. Отправьте ещё или напишите <b>Готово</b>.")
    else:
        await message.answer("Пришлите фото или напишите <b>Готово</b>.")

@dp.message(RepairForm.phone, F.contact)
async def repair_phone(message: Message, state: FSMContext):
    await finish_repair(message, state, message.contact.phone_number)

@dp.message(RepairForm.phone)
async def repair_phone_text(message: Message, state: FSMContext):
    await finish_repair(message, state, message.text)

async def finish_repair(message: Message, state: FSMContext, phone: str):
    data = await state.get_data()
    username = f"@{message.from_user.username}" if message.from_user.username else "не указан"
    text = (
        "🔔 <b>НОВАЯ ЗАЯВКА — РЕМОНТ</b>\n\n"
        f"📱 Модель: {data.get('model','—')}\n"
        f"🔧 Проблема: {data.get('problem','—')}\n"
        f"📞 Телефон: {phone}\n"
        f"👤 Telegram: {username}"
    )
    await bot.send_message(ADMIN_ID, text)
    for fid in data.get("photos", []):
        await bot.send_photo(ADMIN_ID, fid)
    await state.clear()
    await message.answer(
        "✅ <b>Заявка на ремонт принята!</b>\n\nМастер свяжется с вами для уточнения неисправности и стоимости.",
        reply_markup=main_menu()
    )

@dp.callback_query(F.data == "estimate")
async def estimate(call: CallbackQuery):
    await call.message.edit_text(
        "💰 <b>Узнать стоимость</b>\n\n"
        "Для предварительной оценки нажмите «Продать телефон» — бот соберёт модель, состояние, комплект и фотографии.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Заполнить заявку", callback_data="sell")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")]
        ])
    )
    await call.answer()

@dp.callback_query(F.data == "buy")
async def buy(call: CallbackQuery):
    await call.message.edit_text(
        "🛒 <b>Купить телефон</b>\n\n"
        "Актуальный ассортимент и цены уточняйте у менеджера RM Service.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨‍💻 Написать менеджеру", url="https://t.me/RMservice99")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")]
        ])
    )
    await call.answer()

@dp.callback_query(F.data == "contacts")
async def contacts(call: CallbackQuery):
    await call.message.edit_text(
        "📍 <b>RM Service</b>\n\n"
        "Москва, м. Лухмановская\n"
        "ул. Дмитриевского, 23\n\n"
        "📞 8 (977) 606-77-50",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📞 Позвонить", url="tel:+79776067750")],
            [InlineKeyboardButton(text="💬 Telegram", url="https://t.me/RMservice99")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")]
        ])
    )
    await call.answer()

@dp.callback_query(F.data == "manager")
async def manager(call: CallbackQuery):
    await call.message.edit_text(
        "👨‍💻 <b>Связаться с менеджером</b>\n\nНажмите кнопку ниже, чтобы открыть Telegram.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать менеджеру", url="https://t.me/RMservice99")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")]
        ])
    )
    await call.answer()

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Укажите новый токен бота в переменной окружения.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
