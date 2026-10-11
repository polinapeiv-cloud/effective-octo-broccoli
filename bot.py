# -*- coding: utf-8 -*-
"""
Бот-напоминалка и трекер по договору №22.
Запуск: python bot.py  (нужен токен в .env, см. README.md)
"""
import asyncio
import logging
import os
from datetime import datetime, date, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from dotenv import load_dotenv

import storage
import fines
from rules import RULES, RULES_BY_ID, PEOPLE, MENTAL_DAYS_PER_MONTH

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Задай BOT_TOKEN в .env (см. README.md)")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=os.getenv("BOT_TZ", "Europe/Moscow"))

storage.init_db()

STATUS_LABEL = {
    "done": "✅ выполнено",
    "missed": "❌ не выполнено",
    "force_medical": "🩹 форс-мажор: болею",
    "force_mental": "🌙 форс-мажор: стоп-день",
}


def other_person(person):
    return "polina" if person == "denis" else "denis"


def person_of(telegram_id):
    u = storage.get_user(telegram_id)
    return u["person"] if u else None


def short(label, n=26):
    return label if len(label) <= n else label[: n - 1].rstrip() + "…"


def parse_date_arg(text, fallback):
    """Принимает '11.10', '11', '2026-10-11' — возвращает YYYY-MM-DD или None."""
    text = (text or "").strip()
    if not text:
        return fallback
    try:
        if "-" in text and len(text) == 10:
            return date.fromisoformat(text).isoformat()
        parts = text.replace("/", ".").split(".")
        today = date.today()
        if len(parts) == 1:
            d = int(parts[0])
            return date(today.year, today.month, d).isoformat()
        if len(parts) >= 2:
            d, m = int(parts[0]), int(parts[1])
            y = int(parts[2]) if len(parts) > 2 and parts[2] else today.year
            if y < 100:
                y += 2000
            return date(y, m, d).isoformat()
    except Exception:
        return None
    return None


# ---------------- /start ----------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Я Денис", callback_data="setp:denis")],
        [InlineKeyboardButton(text="Я Полина", callback_data="setp:polina")],
    ])
    await message.answer("Кто ты?", reply_markup=kb)


@dp.callback_query(F.data.startswith("setp:"))
async def cb_setp(cb: CallbackQuery):
    person = cb.data.split(":", 1)[1]
    storage.register_user(cb.from_user.id, cb.message.chat.id, person)
    await cb.message.edit_text(f"Готово — {PEOPLE[person]}. /today /week /month /day /balance /poke")
    await cb.answer()


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "/today — чек-лист на сегодня\n"
        "/week — недельные пункты\n"
        "/month — месячная цель, события, ментальные выходные\n"
        "/day 11.10 — отчёт за любую дату (с фото)\n"
        "/balance — долг и журнал штрафов\n"
        "/poke текст — ткнуть\n"
        "/fastfood, /missedcall — только Денис\n\n"
        "Фото пришли просто в чат — спрошу, к какому пункту прикрепить."
    )


# ---------------- /today ----------------

def daily_rules_for(person):
    return [r for r in RULES if r["person"] == person and r["scope"] == "daily"]


def today_keyboard(person):
    pk = storage.today_key()
    rows = []
    for r in daily_rules_for(person):
        e = storage.get_entry(r["id"], person, pk)
        cur = e["status"] if e else None
        mark = "·"
        if cur:
            mark = {"done": "✅", "missed": "❌", "force_medical": "🩹", "force_mental": "🌙"}.get(cur, "·")
        rows.append([InlineKeyboardButton(text=f"{mark} {short(r['label'])}", callback_data=f"day:{r['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("today"))
async def cmd_today(message: Message):
    person = person_of(message.from_user.id)
    if not person:
        await message.answer("Сначала /start.")
        return
    await message.answer(
        f"<b>{storage.today_key()}</b> — {PEOPLE[person]}",
        reply_markup=today_keyboard(person),
    )


@dp.callback_query(F.data.startswith("day:"))
async def cb_day_pick(cb: CallbackQuery):
    person = person_of(cb.from_user.id)
    if not person:
        await cb.answer("Сначала /start", show_alert=True)
        return
    rule_id = cb.data.split(":", 1)[1]
    rule = RULES_BY_ID[rule_id]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выполнено", callback_data=f"set:{rule_id}:done")],
        [InlineKeyboardButton(text=f"❌ −{rule['fine']}₽", callback_data=f"set:{rule_id}:missed")],
        [InlineKeyboardButton(text="🩹 Болею", callback_data=f"set:{rule_id}:force_medical")],
        [InlineKeyboardButton(text="🌙 Стоп-день", callback_data=f"set:{rule_id}:force_mental")],
        [InlineKeyboardButton(text="‹", callback_data="backtoday")],
    ])
    await cb.message.edit_text(f"<b>{rule['label']}</b> · п. {rule['clause']}", reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data == "backtoday")
async def cb_back_today(cb: CallbackQuery):
    person = person_of(cb.from_user.id)
    await cb.message.edit_text(
        f"<b>{storage.today_key()}</b> — {PEOPLE[person]}",
        reply_markup=today_keyboard(person),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("set:"))
async def cb_set_status(cb: CallbackQuery):
    person = person_of(cb.from_user.id)
    _, rule_id, status = cb.data.split(":")
    storage.set_entry_status(rule_id, person, storage.today_key(), status)
    rule = RULES_BY_ID[rule_id]
    note = "Записано"
    if status == "missed":
        note = f"−{rule['fine']}₽"
        await notify_other(person, f"⚠️ {PEOPLE[person]}: «{rule['label']}» не выполнено, −{rule['fine']}₽ (п. {rule['clause']}).")
    await cb.answer(note, show_alert=False)
    await cb.message.edit_text(
        f"<b>{storage.today_key()}</b> — {PEOPLE[person]}",
        reply_markup=today_keyboard(person),
    )


# ---------------- photo reports ----------------

PHOTO_RULES = [r for r in RULES if r.get("photo")]


@dp.message(F.photo)
async def on_photo(message: Message):
    person = person_of(message.from_user.id)
    if not person:
        await message.answer("Сначала /start и выбери, кто ты.")
        return
    candidates = [r for r in PHOTO_RULES if r["person"] == person]
    if not candidates:
        await message.answer("Для тебя нет пунктов с фото-отчётом.")
        return
    file_id = message.photo[-1].file_id
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r["label"], callback_data=f"photo:{r['id']}:{file_id}")]
        for r in candidates
    ])
    await message.answer("К какому пункту прикрепить это фото?", reply_markup=kb)


@dp.callback_query(F.data.startswith("photo:"))
async def cb_photo_attach(cb: CallbackQuery):
    person = person_of(cb.from_user.id)
    _, rule_id, file_id = cb.data.split(":", 2)
    storage.attach_photo(rule_id, person, storage.today_key(), file_id)
    rule = RULES_BY_ID[rule_id]
    await cb.message.edit_text(f"Фото прикреплено к «{rule['label']}» ✅")
    await cb.answer()


# ---------------- /day — отчёт за любую прошлую дату ----------------

def day_report_text(person, date_key):
    entries = storage.entries_on_date(person, date_key)
    events = storage.events_on_date(person, date_key)
    by_rule = {e["rule_id"]: e for e in entries}
    lines = [f"<b>{date_key}</b> — {PEOPLE[person]}"]
    any_data = False
    for r in daily_rules_for(person):
        e = by_rule.get(r["id"])
        if not e:
            continue
        any_data = True
        mark = {"done": "✅", "missed": "❌", "force_medical": "🩹", "force_mental": "🌙"}.get(e["status"], "·")
        photo = " 📎" if e["photo_file_id"] else ""
        lines.append(f"{mark} {r['label']}{photo}")
    for ev in events:
        any_data = True
        rule = RULES_BY_ID[ev["rule_id"]]
        lines.append(f"⚠️ {rule['label']}")
    if not any_data:
        lines.append("<i>Пусто — за этот день ничего не отмечено.</i>")
    return "\n".join(lines)


def day_keyboard(date_key):
    prev_d = (date.fromisoformat(date_key) - timedelta(days=1)).isoformat()
    next_d = (date.fromisoformat(date_key) + timedelta(days=1)).isoformat()
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="‹ пред.", callback_data=f"dayn:{prev_d}"),
            InlineKeyboardButton(text="след. ›", callback_data=f"dayn:{next_d}"),
        ],
        [InlineKeyboardButton(text="📎 показать фото", callback_data=f"dayp:{date_key}")],
    ])


async def send_day_report(chat_id, person, date_key):
    await bot.send_message(chat_id, day_report_text(person, date_key), reply_markup=day_keyboard(date_key))


@dp.message(Command("day"))
async def cmd_day(message: Message):
    person = person_of(message.from_user.id)
    if not person:
        await message.answer("Сначала /start.")
        return
    arg = message.text.partition(" ")[2]
    date_key = parse_date_arg(arg, storage.today_key())
    if not date_key:
        await message.answer("Не понял дату. Пример: /day 11.10 или /day 2026-09-20")
        return
    await send_day_report(message.chat.id, person, date_key)


@dp.callback_query(F.data.startswith("dayn:"))
async def cb_day_nav(cb: CallbackQuery):
    person = person_of(cb.from_user.id)
    date_key = cb.data.split(":", 1)[1]
    await cb.message.edit_text(day_report_text(person, date_key), reply_markup=day_keyboard(date_key))
    await cb.answer()


@dp.callback_query(F.data.startswith("dayp:"))
async def cb_day_photos(cb: CallbackQuery):
    person = person_of(cb.from_user.id)
    date_key = cb.data.split(":", 1)[1]
    photos = storage.photos_on_date(person, date_key)
    if not photos:
        await cb.answer("Фото за этот день нет.", show_alert=True)
        return
    await cb.answer()
    if len(photos) == 1:
        e = photos[0]
        rule = RULES_BY_ID[e["rule_id"]]
        await bot.send_photo(cb.message.chat.id, e["photo_file_id"], caption=f"{rule['label']} · {date_key}")
    else:
        media = []
        for e in photos[:10]:
            rule = RULES_BY_ID[e["rule_id"]]
            media.append(InputMediaPhoto(media=e["photo_file_id"], caption=f"{rule['label']} · {date_key}"))
        await bot.send_media_group(cb.message.chat.id, media)


# ---------------- /week ----------------

def weekly_rules_for(person):
    return [r for r in RULES if r["person"] == person and r["scope"] == "weekly"]


def week_keyboard(person):
    wk = storage.week_key()
    rows = []
    for r in weekly_rules_for(person):
        e = storage.get_entry(r["id"], person, wk)
        cur = e["status"] if e else None
        mark = {"done": "✅", "missed": "❌", "force_medical": "🩹", "force_mental": "🌙"}.get(cur, "·")
        rows.append([InlineKeyboardButton(text=f"{mark} {short(r['label'])}", callback_data=f"wk:{r['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("week"))
async def cmd_week(message: Message):
    person = person_of(message.from_user.id)
    if not person:
        await message.answer("Сначала /start.")
        return
    await message.answer(
        f"<b>Неделя с {storage.week_key()}</b> — {PEOPLE[person]}",
        reply_markup=week_keyboard(person),
    )


@dp.callback_query(F.data.startswith("wk:"))
async def cb_week_pick(cb: CallbackQuery):
    person = person_of(cb.from_user.id)
    rule_id = cb.data.split(":", 1)[1]
    rule = RULES_BY_ID[rule_id]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выполнено", callback_data=f"wset:{rule_id}:done")],
        [InlineKeyboardButton(text=f"❌ −{rule['fine']}₽", callback_data=f"wset:{rule_id}:missed")],
        [InlineKeyboardButton(text="🩹 Форс-мажор", callback_data=f"wset:{rule_id}:force_medical")],
        [InlineKeyboardButton(text="‹", callback_data="backweek")],
    ])
    await cb.message.edit_text(f"<b>{rule['label']}</b> · п. {rule['clause']}", reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data == "backweek")
async def cb_back_week(cb: CallbackQuery):
    person = person_of(cb.from_user.id)
    await cb.message.edit_text(
        f"<b>Неделя с {storage.week_key()}</b> — {PEOPLE[person]}",
        reply_markup=week_keyboard(person),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("wset:"))
async def cb_week_set(cb: CallbackQuery):
    person = person_of(cb.from_user.id)
    _, rule_id, status = cb.data.split(":")
    storage.set_entry_status(rule_id, person, storage.week_key(), status)
    rule = RULES_BY_ID[rule_id]
    if status == "missed":
        await notify_other(person, f"⚠️ {PEOPLE[person]}: «{rule['label']}» — не выполнено, −{rule['fine']}₽.")
    await cb.answer("Записано")
    await cb.message.edit_text(
        f"<b>Неделя с {storage.week_key()}</b> — {PEOPLE[person]}",
        reply_markup=week_keyboard(person),
    )


# ---------------- /month (monthly goal + events) ----------------

@dp.message(Command("month"))
async def cmd_month(message: Message):
    person = person_of(message.from_user.id)
    if not person:
        await message.answer("Сначала /start.")
        return
    mk = storage.month_key()
    lines = [f"<b>{mk}</b> — {PEOPLE[person]}"]

    monthly_rule = next((r for r in RULES if r["person"] == person and r["scope"] == "monthly"), None)
    if monthly_rule:
        e = storage.get_entry(monthly_rule["id"], person, mk)
        status = STATUS_LABEL.get(e["status"], "не отмечено") if e else "не отмечено"
        lines.append(f"\n<b>{monthly_rule['label']}</b>: {status}")
        kb_rows = [
            [InlineKeyboardButton(text="✅ Выполнена", callback_data=f"mset:{monthly_rule['id']}:done")],
            [InlineKeyboardButton(text=f"❌ −{monthly_rule['fine']}₽", callback_data=f"mset:{monthly_rule['id']}:missed")],
        ]
    else:
        kb_rows = []

    mental_used = sum(
        1 for e in storage.entries_for_person_period_prefix(person, "")
        if e["status"] == "force_mental" and e["period_key"].startswith(mk)
    )
    lines.append(f"\n🌙 Ментальных выходных: {mental_used} из {MENTAL_DAYS_PER_MONTH}")

    event_rules = [r for r in RULES if r["person"] == person and r["scope"] == "event"]
    if event_rules:
        lines.append("\n<b>События</b>")
        for r in event_rules:
            n = len(storage.events_for(r["id"], person))
            lines.append(f"— {r['label']}: {n}")
        lines.append("<i>/fastfood, /missedcall — записать новый случай</i>")

    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None,
    )


@dp.callback_query(F.data.startswith("mset:"))
async def cb_month_set(cb: CallbackQuery):
    person = person_of(cb.from_user.id)
    _, rule_id, status = cb.data.split(":")
    storage.set_entry_status(rule_id, person, storage.month_key(), status)
    rule = RULES_BY_ID[rule_id]
    if status == "missed":
        await notify_other(person, f"⚠️ {PEOPLE[person]}: месячная цель не выполнена, −{rule['fine']}₽.")
    await cb.answer("Записано")
    await cb.message.edit_text(f"{rule['label']}: {STATUS_LABEL.get(status)}")


@dp.message(Command("fastfood"))
async def cmd_fastfood(message: Message):
    person = person_of(message.from_user.id)
    if person != "denis":
        await message.answer("Этот пункт только у Дениса (п. 2.2).")
        return
    storage.log_event("d_fastfood", person)
    lines = fines.fastfood_fine_lines(person)
    last = lines[-1] if lines else None
    if last and last["amount"] > 0:
        await message.answer(f"Записано. Повтор раньше 14 дней — штраф −{last['amount']}₽.")
        await notify_other(person, "⚠️ Денис зафиксировал фастфуд раньше 14 дней с прошлого раза (п. 2.2, −500₽).")
    else:
        await message.answer("Записано. В пределах 14-дневного лимита — без штрафа.")


@dp.message(Command("missedcall"))
async def cmd_missed_call(message: Message):
    person = person_of(message.from_user.id)
    if person != "denis":
        await message.answer("Этот пункт только у Дениса (п. 2.10).")
        return
    storage.log_event("d_call", person)
    await message.answer("Записано: −200₽ (п. 2.10).")
    await notify_other(person, "⚠️ Денис зафиксировал пропущенный созвон без предупреждения (п. 2.10, −200₽).")


# ---------------- /balance ----------------

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    person = person_of(message.from_user.id)
    if not person:
        await message.answer("Сначала /start.")
        return
    lines = []
    for p in ("denis", "polina"):
        lines.append(f"{PEOPLE[p]}: <b>{fines.total_due(p)}₽</b>")
    lines.append("")
    journal = fines.all_fine_lines(person)[:15]
    if not journal:
        lines.append("Штрафов пока нет.")
    else:
        lines.append(f"<b>{PEOPLE[person]}</b>")
        for l in journal:
            mark = "✓" if l["paid"] else "—"
            when = l["when"][:10]
            lines.append(f"{mark} {when} · {l['rule']['label']} · −{l['amount']}₽")
    await message.answer("\n".join(lines))


# ---------------- /poke ----------------

@dp.message(Command("poke"))
async def cmd_poke(message: Message):
    person = person_of(message.from_user.id)
    if not person:
        await message.answer("Сначала /start.")
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.answer("Напиши текст после команды, например:\n/poke пришли фото обеда")
        return
    target = other_person(person)
    storage.add_poke(person, target, text)
    ok = await notify_other(person, f"👉 {PEOPLE[person]} тыкает: {text}")
    await message.answer("Отправлено." if ok else f"Записал тычок, но {PEOPLE[target]} ещё не запускал(а) бота (/start) — сообщение не доставлено.")


async def notify_other(person, text):
    target = other_person(person)
    u = storage.get_user_by_person(target)
    if not u:
        return False
    try:
        await bot.send_message(u["chat_id"], text)
        return True
    except Exception as e:
        logging.warning("notify failed: %s", e)
        return False


# ---------------- scheduled reminders ----------------

async def daily_reminder():
    for person in ("denis", "polina"):
        u = storage.get_user_by_person(person)
        if not u:
            continue
        pk = storage.today_key()
        missing = [r for r in daily_rules_for(person) if not storage.get_entry(r["id"], person, pk)]
        if not missing:
            continue
        names = "\n".join(f"— {r['label']}" for r in missing)
        try:
            await bot.send_message(
                u["chat_id"],
                f"Напоминание на сегодня, не отмечено:\n{names}\n\nОткрой /today, чтобы отметить.",
            )
        except Exception as e:
            logging.warning("reminder failed for %s: %s", person, e)


async def weekly_report_reminder():
    for person in ("denis", "polina"):
        u = storage.get_user_by_person(person)
        if not u:
            continue
        try:
            await bot.send_message(
                u["chat_id"],
                "Воскресенье — время недельного отчёта. Открой /week и /balance, чтобы подвести итоги.",
            )
        except Exception as e:
            logging.warning("weekly reminder failed for %s: %s", person, e)


async def main():
    hour = int(os.getenv("BOT_REMINDER_HOUR", "21"))
    minute = int(os.getenv("BOT_REMINDER_MINUTE", "0"))
    scheduler.add_job(daily_reminder, "cron", hour=hour, minute=minute)
    scheduler.add_job(weekly_report_reminder, "cron", day_of_week="sun", hour=hour, minute=minute)
    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
