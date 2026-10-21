#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Телеграм-бот «Капер ИИ» — прогнозы на футбол.
Этап 1 (MVP): читает данные нашего сайта с GitHub Pages и показывает:
  /start   — приветствие и меню
  /today   — матчи на сегодня (все турниры)
  /match   — карточка матча (через кнопки): коэффициенты, составы, новости, мнение ИИ
  /rating  — рейтинг прогнозистов (% проходимости) + строка ИИ
  /express — экспресс дня (3–4 самых вероятных события разных рынков)

Токен НЕ хранится в коде. Передаётся через переменную окружения BOT_TOKEN.
Запуск локально:   BOT_TOKEN=xxxx python bot.py
"""
import os, time, re, asyncio, html, random, json, hmac, hashlib, datetime
import aiohttp
from aiohttp import web
from urllib.parse import parse_qsl
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, BotCommand, WebAppInfo, MenuButtonWebApp, MenuButtonDefault
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# Источник данных — наш публичный сайт
BASE = os.environ.get("DATA_BASE", "https://dikey123.github.io/football-prognozy/")
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не задан BOT_TOKEN (переменная окружения).")

# Обязательная подписка: @username или -100id канала/группы. Пусто = проверка выключена.
REQUIRED_CHAT = os.environ.get("REQUIRED_CHAT", "").strip()
CHANNEL_URL = os.environ.get("CHANNEL_URL", "").strip()
def channel_link():
    if CHANNEL_URL:
        return CHANNEL_URL
    if REQUIRED_CHAT.startswith("@"):
        return "https://t.me/" + REQUIRED_CHAT[1:]
    return ""

_cache = {"t": 0, "data": None, "res": None}

MON = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
DAYS = ["воскресенье","понедельник","вторник","среда","четверг","пятница","суббота"]

def e(s):  # экранирование HTML
    return html.escape(str(s if s is not None else ""))

def dlabel(s):
    import datetime
    d = datetime.date.fromisoformat(s)
    return f"{DAYS[(d.weekday()+1)%7]}, {d.day} {MON[d.month-1]}"

# ---------- загрузка данных (кэш 5 мин) ----------
async def fetch_json(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
    except Exception:
        return None

def gpt_key(date, home, away):
    return f"{date}|{home}|{away}"

def merge_gpt(data, gpt):
    """Вливает мнения ChatGPT из отдельного gpt.json в матчи data.json.
    Ключ матча: 'дата|хозяева|гости'. Если в data.json мнение уже есть — не трогаем."""
    if not data or not gpt:
        return
    gm = gpt.get("matches") if isinstance(gpt, dict) else None
    if not isinstance(gm, dict):
        return
    for t in data.get("tournaments", []):
        for m in t.get("matches", []):
            if m.get("gpt"):
                continue
            op = gm.get(gpt_key(m.get("date",""), m.get("home",""), m.get("away","")))
            if isinstance(op, dict):
                m["gpt"] = op

async def get_data(force=False):
    now = time.time()
    if not force and _cache["data"] and now - _cache["t"] < 300:
        return _cache["data"], _cache["res"]
    async with aiohttp.ClientSession() as s:
        data = await fetch_json(s, BASE + "data.json")
        res  = await fetch_json(s, BASE + "results.json")
        gpt  = await fetch_json(s, BASE + "gpt.json")
    if data:
        merge_gpt(data, gpt)
        _cache.update(t=now, data=data, res=res)
    return _cache["data"], _cache["res"]

def all_matches(data):
    out = []
    for t in data.get("tournaments", []):
        for i, m in enumerate(t.get("matches", [])):
            out.append((t, i, m))
    return out

def find_match(data, tid, idx):
    for t in data.get("tournaments", []):
        if t["id"] == tid:
            ms = t.get("matches", [])
            if 0 <= idx < len(ms):
                return t, ms[idx]
    return None, None

# ---------- экранчики ----------
def main_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🟢 Открыть приложение", web_app=WebAppInfo(url=BASE))
    kb.button(text="⚽ Матчи сегодня", callback_data="today")
    kb.button(text="🏆 Рейтинг", callback_data="rating")
    kb.button(text="⚡ Экспресс дня", callback_data="express")
    kb.adjust(1, 1, 2)
    return kb.as_markup()

async def screen_today(data):
    today = data.get("today")
    rows = [(t, i, m) for (t, i, m) in all_matches(data) if m.get("date") == today]
    if not rows:
        return f"На сегодня ({dlabel(today)}) матчей нет. Нажмите /start и загляните позже.", None
    txt = f"<b>⚽ Матчи сегодня — {e(dlabel(today))}</b>\n<i>Нажмите на матч, чтобы открыть прогноз.</i>"
    kb = InlineKeyboardBuilder()
    for t, i, m in rows:
        full = m.get("status") == "full"
        label = f"{m['time']}  {m['home']} — {m['away']}" + ("" if full else "  (расписание)")
        kb.button(text=label[:60], callback_data=f"m:{t['id']}:{i}")
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(1)
    return txt, kb.as_markup()

def bar(cnt):
    tot = cnt["1"] + cnt["X"] + cnt["2"]
    if not tot:
        return ""
    def blk(n): return "█" * round(n / tot * 10)
    return f"🟩{cnt['1']} ⬜{cnt['X']} 🟦{cnt['2']}"

def opinion_block(src, emoji, name):
    """Единый блок мнения одного ИИ. src = {score, text, conf(0-5), bet}. Пусто → "" ."""
    if not src:
        return ""
    try:
        conf = max(0, min(5, int(src.get("conf", 0) or 0)))
    except (TypeError, ValueError):
        conf = 0
    dots = "●" * conf + "○" * (5 - conf)
    out = f"\n{emoji} <b>Мнение {name}</b>: счёт <b>{e(src.get('score',''))}</b>\n{e(src.get('text',''))}"
    if src.get("bet"):
        out += f"\nВыбор {name}: <b>{e(src['bet'])}</b>  (уверенность {dots})"
    return out

def screen_match(t, m):
    lines = [f"<b>{e(m['home'])} — {e(m['away'])}</b>",
             f"{e(t.get('name',''))} · {e(t.get('round',''))}",
             f"🗓 {e(dlabel(m['date']))}, {e(m['time'])}" + (f" · {e(m.get('venue',''))}" if m.get('venue') else "")]
    o = m.get("odds") or [None, None, None]
    if any(x is not None for x in o):
        def f(x): return "—" if x is None else f"{x:.2f}"
        lines.append(f"\n<b>Коэффициенты</b>: П1 {f(o[0])} · Х {f(o[1])} · П2 {f(o[2])}")
    if m.get("context"):
        lines.append("\n" + e(m["context"]))
    # эксперты
    tips = m.get("tips") or []
    cnt = {"1": 0, "X": 0, "2": 0}
    for tp in tips:
        if tp.get("lean") in cnt:
            cnt[tp["lean"]] += 1
    if sum(cnt.values()):
        lines.append(f"\n<b>Куда склоняются эксперты</b> ({sum(cnt.values())} прогнозов): " + bar(cnt))
    # Мнения ИИ — два источника (Claude в поле "ai", ChatGPT в поле "gpt")
    for blk in (opinion_block(m.get("ai"), "🤖", "ИИ"),
                opinion_block(m.get("gpt"), "🧠", "ChatGPT")):
        if blk:
            lines.append(blk)
    return "\n".join(lines)

def match_kb(t, idx, m):
    kb = InlineKeyboardBuilder()
    if m.get("lineups"):
        kb.button(text="👕 Составы", callback_data=f"lu:{t['id']}:{idx}")
    if m.get("news"):
        kb.button(text="📰 Новости матча", callback_data=f"nw:{t['id']}:{idx}")
    if m.get("tips"):
        kb.button(text="📊 Все прогнозы", callback_data=f"tp:{t['id']}:{idx}")
    kb.button(text="⬅️ К матчам", callback_data="today")
    kb.adjust(2, 1)
    return kb.as_markup()

def screen_lineups(m):
    def side(s, name):
        if not s:
            return ""
        xi = " / ".join(", ".join(line) for line in s.get("xi", []))
        out = ("Не сыграют: " + ", ".join(s["out"])) if s.get("out") else "Потерь нет"
        txt = f"<b>{e(name)}</b> ({e(s.get('formation',''))}" + (f", {e(s['coach'])}" if s.get('coach') else "") + ")\n"
        txt += e(xi) + "\n<i>" + e(out) + "</i>"
        if s.get("doubt"):
            txt += "\n<i>Под вопросом: " + e(", ".join(s["doubt"])) + "</i>"
        return txt
    lu = m["lineups"]
    res = f"👕 <b>Предполагаемые составы</b>\n{m['home']} — {m['away']}\n\n"
    res += side(lu.get("home"), m["home"]) + "\n\n" + side(lu.get("away"), m["away"])
    if lu.get("source"):
        res += f"\n\nИсточник: {e(lu['source'].get('name',''))}"
    return res

def screen_news(m):
    res = f"📰 <b>Новости матча</b>\n{m['home']} — {m['away']}\n\n"
    for n in m.get("news", []):
        res += f"• <a href=\"{e(n['u'])}\">{e(n['t'])}</a>\n  <i>{e(n['s'])}</i>\n"
    return res

def screen_tips(m):
    res = f"📊 <b>Прогнозы экспертов</b>\n{m['home']} — {m['away']}\n\n"
    for tp in m.get("tips", []):
        reg = "🇷🇺" if tp.get("reg") == "ru" else "🇪🇺"
        k = f" — кф {tp['k']}" if tp.get("k") else ""
        res += f"{reg} <b>{e(tp.get('src',''))}</b>: {e(tp.get('pick',''))}{e(k)}\n"
    return res

# ---------- рейтинг ----------
def wilson(w, n):
    if not n:
        return 0
    z = 1.0
    p = w / n
    return (p + z*z/(2*n) - z*((p*(1-p)+z*z/(4*n))/n) ** 0.5) / (1 + z*z/n)

def screen_rating(res):
    if not res or not res.get("sources"):
        return "Статистика ещё собирается — рейтинг появится после первых сыгранных матчей."
    rows = []
    for src, v in res["sources"].items():
        n = v["win"] + v["lose"]
        rows.append({"src": src, "reg": v.get("reg","ru"), "win": v["win"], "lose": v["lose"],
                     "pct": round(v["win"]/n*100) if n else 0, "w": wilson(v["win"], n), "n": n})
    ai = res.get("ai", {"win":0,"lose":0})
    an = ai["win"] + ai["lose"]
    rows.append({"src": "🤖 Мнение ИИ", "reg": "ai", "win": ai["win"], "lose": ai["lose"],
                 "pct": round(ai["win"]/an*100) if an else 0, "w": wilson(ai["win"], an), "n": an, "ai": True})
    gpt = res.get("gpt")
    if gpt:
        gn = gpt["win"] + gpt["lose"]
        rows.append({"src": "🧠 ChatGPT", "reg": "ai", "win": gpt["win"], "lose": gpt["lose"],
                     "pct": round(gpt["win"]/gn*100) if gn else 0, "w": wilson(gpt["win"], gn), "n": gn, "ai": True})
    rows.sort(key=lambda r: (-r["w"], -r["n"], -r["pct"]))
    out = [f"🏆 <b>Рейтинг прогнозистов</b>\n<i>% проходимости по {res.get('matches',0)} сыгранным матчам</i>\n"]
    for i, r in enumerate(rows[:25], 1):
        tag = "" if r.get("ai") else (" 🇷🇺" if r["reg"] == "ru" else " 🇪🇺")
        small = " · мало данных" if r["n"] < 3 else ""
        out.append(f"{i}. <b>{e(r['src'])}</b>{tag} — <b>{r['pct']}%</b> ({r['win']}–{r['lose']}{small})")
    out.append("\n<i>Комбо и экзотика не учитываются. Это аналитика, не гарантия. 18+</i>")
    return "\n".join(out)

# ---------- экспресс ----------
def mkt(pick):
    p = " " + str(pick).lower().strip() + " "
    if "угл" in p: return "Угловые"
    if re.search(r"удал|красн|карточ|желт|фол", p): return "Карточки"
    if "обе заб" in p or " оз " in p: return "Обе забьют"
    if "тотал больш" in p or re.search(r" тб[ 0-9(]", p) or "больше" in p: return "Тотал больше"
    if "тотал мен" in p or re.search(r" тм[ 0-9(]", p) or "меньше" in p: return "Тотал меньше"
    if "фора" in p or " ф1" in p or " ф2" in p: return "Фора"
    if "двойн" in p or "1х" in p or "х2" in p or "не проигр" in p: return "Двойной шанс"
    if "побед" in p or " п1 " in p or " п2 " in p: return "Исход"
    return "Ставка"

def parse_k(k):
    try:
        v = float(str(k).replace(",", "."))
        return v if 1.01 < v < 30 else None
    except Exception:
        return None

def express(data, n=4):
    import datetime
    cands = []
    for t, i, m in all_matches(data):
        if m.get("status") != "full":
            continue
        o = m.get("odds") or []
        if o and o[0] is not None and (len(o) < 3 or o[2] is None or o[0] <= o[2]):
            cands.append({"m": m, "pick": f"Победа «{m['home']}»", "odd": float(o[0]), "mk": "Исход", "tour": t.get("short","")})
        elif len(o) >= 3 and o[2] is not None:
            cands.append({"m": m, "pick": f"Победа «{m['away']}»", "odd": float(o[2]), "mk": "Исход", "tour": t.get("short","")})
        seen = set()
        for tp in m.get("tips", []):
            k = parse_k(tp.get("k"))
            if not k:
                continue
            key = re.sub(r"\s+", "", str(tp.get("pick","")).lower())
            if key in seen:
                continue
            seen.add(key)
            mk = mkt(tp.get("pick",""))
            if mk == "Ставка":  # неясный рынок — в экспресс не берём
                continue
            cands.append({"m": m, "pick": tp.get("pick",""), "odd": k, "mk": mk, "tour": t.get("short","")})
    cands.sort(key=lambda c: c["odd"])
    uM, uT, sel = set(), set(), []
    for c in cands:
        if len(sel) >= n: break
        mid = c["m"]["home"] + c["m"]["away"]
        if mid in uM or c["mk"] in uT: continue
        uM.add(mid); uT.add(c["mk"]); sel.append(c)
    if len(sel) < n:
        for c in cands:
            if len(sel) >= n: break
            mid = c["m"]["home"] + c["m"]["away"]
            if mid in uM: continue
            uM.add(mid); sel.append(c)
    return sel

def screen_express(data):
    sel = express(data, 4)
    if len(sel) < 2:
        return "Пока недостаточно матчей с коэффициентами для экспресса. Загляните ближе к игровому дню."
    total = 1.0
    out = ["⚡ <b>Экспресс дня от ИИ</b>\n<i>Самые вероятные события разных рынков</i>\n"]
    for c in sel:
        total *= c["odd"]
        out.append(f"• <b>{e(c['m']['home'])} — {e(c['m']['away'])}</b> ({e(c['tour'])})\n  {e(c['mk'])}: {e(c['pick'])} — кф <b>{c['odd']:.2f}</b>")
    out.append(f"\n<b>Итоговый коэффициент: {total:.2f}</b>  ·  1000 ₽ → {round(total*1000)} ₽")
    out.append("<i>Должны сыграть все события. Аналитика, не гарантия. 18+</i>")
    return "\n".join(out)

# ---------- обязательная подписка на канал/группу ----------
async def is_subscribed(bot, uid):
    if not REQUIRED_CHAT:
        return True
    try:
        m = await bot.get_chat_member(REQUIRED_CHAT, uid)
        return m.status in ("member", "administrator", "creator")
    except Exception as ex:
        print("Проверка подписки не удалась (бот — админ канала?):", ex)
        return True  # при неверной настройке не ломаем бота

def gate_kb():
    kb = InlineKeyboardBuilder()
    link = channel_link()
    if link:
        kb.button(text="📢 Подписаться", url=link)
    kb.button(text="✅ Я подписался", callback_data="check_sub")
    kb.adjust(1)
    return kb.as_markup()

GATE_TEXT = ("🔒 Доступ к боту «Капер ИИ» — только для подписчиков нашего канала (бесплатно).\n\n"
             "Подпишитесь по кнопке ниже и нажмите «✅ Я подписался».")

# Персональная кнопка-меню: подписчику — «Приложение», остальным — обычная (без приложения).
_menu_state = {}  # uid -> bool (True = кнопка приложения стоит)
async def set_user_menu(bot, uid, allow):
    if not REQUIRED_CHAT:
        return  # гейт выключен — приложением управляет глобальная кнопка
    if _menu_state.get(uid) == allow:
        return
    try:
        btn = (MenuButtonWebApp(text="Приложение", web_app=WebAppInfo(url=BASE))
               if allow else MenuButtonDefault())
        await bot.set_chat_menu_button(chat_id=uid, menu_button=btn)
        _menu_state[uid] = allow
    except Exception as ex:
        print("set_user_menu:", ex)

class SubGate(BaseMiddleware):
    async def __call__(self, handler, event, data):
        bot = data.get("bot")
        user = data.get("event_from_user")
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            ok = await is_subscribed(bot, user.id)
            await set_user_menu(bot, user.id, ok)
            if ok:
                await event.answer("Готово! Доступ открыт ✅")
                try:
                    await event.message.edit_text(
                        "👋 Доступ открыт. Это <b>Капер ИИ</b> — выберите раздел:",
                        reply_markup=main_kb())
                except Exception:
                    await event.message.answer("Доступ открыт.", reply_markup=main_kb())
            else:
                await event.answer("Пока не вижу подписку. Подпишитесь и нажмите ещё раз.", show_alert=True)
            return
        if user:
            ok = await is_subscribed(bot, user.id)
            await set_user_menu(bot, user.id, ok)
            if not ok:
                if isinstance(event, CallbackQuery):
                    await event.answer()
                    await event.message.answer(GATE_TEXT, reply_markup=gate_kb())
                else:
                    await event.answer(GATE_TEXT, reply_markup=gate_kb())
                return
        return await handler(event, data)

# ---------- хэндлеры ----------
dp = Dispatcher()
dp.message.outer_middleware(SubGate())
dp.callback_query.outer_middleware(SubGate())

@dp.message(CommandStart())
async def start(msg: Message):
    await msg.answer(
        "👋 Привет! Это <b>Капер ИИ</b> — прогнозы на футбол: Лига чемпионов, Лига Европы и топ-5 лиг.\n\n"
        "• Матчи дня с коэффициентами, составами и новостями\n"
        "• Мнение ИИ по каждому матчу\n"
        "• Рейтинг прогнозистов по реальной проходимости\n"
        "• Конструктор экспресса\n\n"
        "<i>18+. Это аналитика, а не гарантия результата.</i>",
        reply_markup=main_kb())

@dp.message(Command("app"))
async def cmd_app(msg: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🟢 Открыть приложение", web_app=WebAppInfo(url=BASE))
    await msg.answer("Открыть «Капер ИИ» как приложение — на весь экран:", reply_markup=kb.as_markup())

@dp.message(Command("today"))
async def cmd_today(msg: Message):
    data, _ = await get_data()
    if not data:
        return await msg.answer("Не удалось загрузить данные. Попробуйте позже.")
    txt, kb = await screen_today(data)
    await msg.answer(txt, reply_markup=kb)

@dp.message(Command("rating"))
async def cmd_rating(msg: Message):
    _, res = await get_data()
    await msg.answer(screen_rating(res), reply_markup=main_kb(), disable_web_page_preview=True)

@dp.message(Command("express"))
async def cmd_express(msg: Message):
    data, _ = await get_data()
    await msg.answer(screen_express(data) if data else "Нет данных.", reply_markup=main_kb())

@dp.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    await c.message.edit_text("Главное меню:", reply_markup=main_kb()); await c.answer()

@dp.callback_query(F.data == "today")
async def cb_today(c: CallbackQuery):
    data, _ = await get_data()
    txt, kb = await screen_today(data)
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@dp.callback_query(F.data == "rating")
async def cb_rating(c: CallbackQuery):
    _, res = await get_data()
    await c.message.edit_text(screen_rating(res), reply_markup=main_kb(), disable_web_page_preview=True); await c.answer()

@dp.callback_query(F.data == "express")
async def cb_express(c: CallbackQuery):
    data, _ = await get_data()
    await c.message.edit_text(screen_express(data), reply_markup=main_kb()); await c.answer()

@dp.callback_query(F.data.startswith("m:"))
async def cb_match(c: CallbackQuery):
    _, tid, idx = c.data.split(":")
    data, _ = await get_data()
    t, m = find_match(data, tid, int(idx))
    if not m:
        return await c.answer("Матч не найден", show_alert=True)
    if m.get("status") != "full":
        await c.message.edit_text(
            f"<b>{e(m['home'])} — {e(m['away'])}</b>\n{e(t.get('name',''))}\n🗓 {e(dlabel(m['date']))}, {e(m['time'])}\n\n"
            "Прогнозы, составы и новости появятся в день матча.",
            reply_markup=InlineKeyboardBuilder().button(text="⬅️ К матчам", callback_data="today").as_markup())
        return await c.answer()
    await c.message.edit_text(screen_match(t, m), reply_markup=match_kb(t, int(idx), m), disable_web_page_preview=True)
    await c.answer()

@dp.callback_query(F.data.startswith(("lu:", "nw:", "tp:")))
async def cb_sub(c: CallbackQuery):
    kind, tid, idx = c.data.split(":")
    data, _ = await get_data()
    t, m = find_match(data, tid, int(idx))
    if not m:
        return await c.answer("Матч не найден", show_alert=True)
    txt = screen_lineups(m) if kind == "lu" else screen_news(m) if kind == "nw" else screen_tips(m)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К матчу", callback_data=f"m:{tid}:{idx}")
    await c.message.edit_text(txt, reply_markup=kb.as_markup(), disable_web_page_preview=True)
    await c.answer()

# ---------- мини веб-сервер (для хостингов типа Render, чтобы сервис считался живым) ----------
# ---------- защищённая выдача данных для Mini App ----------
def check_init_data(init_data, token, max_age=86400):
    """Проверка подписи Telegram WebApp initData. Возвращает user_id или None."""
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except Exception:
        return None
    h = pairs.pop("hash", None)
    if not h:
        return None
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, h):
        return None
    try:
        if max_age and time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
    except Exception:
        pass
    try:
        return json.loads(pairs.get("user", "{}")).get("id")
    except Exception:
        return None

CORS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}

async def _health(request):
    return web.Response(text="Капер ИИ — бот работает. 18+")

async def api_data(request):
    uid = check_init_data(request.query.get("initData", ""), TOKEN)
    if not uid:
        return web.json_response({"error": "no_tg"}, status=403, headers=CORS)
    if not await is_subscribed(request.app["bot"], uid):
        return web.json_response({"error": "not_subscribed"}, status=403, headers=CORS)
    data, res = await get_data()
    if not data:
        return web.json_response({"error": "nodata"}, status=503, headers=CORS)
    return web.json_response({"data": data, "res": res}, headers=CORS)

async def api_options(request):
    return web.Response(status=204, headers={**CORS,
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*"})

async def start_web(bot):
    port = int(os.environ.get("PORT", "10000"))
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/api/data", api_data)
    app.router.add_options("/api/data", api_options)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Веб-сервер слушает порт {port} (health + /api/data)")

# ---------- авто-постинг в канал ----------
BOT_USERNAME = ""
def bot_link():
    return f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else (channel_link() or "https://t.me/")

def _leader_and_ai(res):
    if not res or not res.get("sources"):
        return None, None
    rr = []
    for src, v in res["sources"].items():
        n = v["win"] + v["lose"]
        rr.append((wilson(v["win"], n), src, round(v["win"]/n*100) if n else 0))
    rr.sort(reverse=True)
    leader = rr[0] if rr else None
    ai = res.get("ai", {"win": 0, "lose": 0})
    an = ai["win"] + ai["lose"]
    aistat = (round(ai["win"]/an*100), ai["win"], ai["lose"]) if an else None
    return leader, aistat

async def post_daily(bot):
    if not REQUIRED_CHAT:
        return False
    data, res = await get_data()
    if not data:
        return False
    today = data.get("today")
    rows = [m for (t, i, m) in all_matches(data) if m.get("date") == today]
    if not rows:
        return False  # нет матчей — не постим
    parts = ["🔥 <b>Капер ИИ</b> — прогнозы на сегодня",
             f"⚽ Матчей сегодня: <b>{len(rows)}</b> · {e(dlabel(today))}", ""]
    parts.append(screen_express(data))
    leader, aistat = _leader_and_ai(res)
    tail = []
    if leader:
        tail.append(f"🏆 Лидер прогнозистов: <b>{e(leader[1])}</b> — {leader[2]}%")
    if aistat:
        tail.append(f"🤖 ИИ: <b>{aistat[0]}%</b> проходимости ({aistat[1]}–{aistat[2]})")
    if tail:
        parts.append("\n" + "\n".join(tail))
    parts.append("\nВсе прогнозы, составы и свой экспресс — в приложении 👇\n<i>18+. Аналитика, не гарантия.</i>")
    kb = InlineKeyboardBuilder()
    kb.button(text="⚽ Открыть в боте", url=bot_link())
    await bot.send_message(REQUIRED_CHAT, "\n".join(parts),
                           reply_markup=kb.as_markup(), disable_web_page_preview=True)
    print("Пост в канал отправлен.")
    return True

POST_TIMES = [(10, 15)]  # часы:минуты МСК для авто-постов в канал
async def channel_poster(bot):
    if not REQUIRED_CHAT:
        return
    MSK = datetime.timezone(datetime.timedelta(hours=3))
    while True:
        now = datetime.datetime.now(MSK)
        cands = []
        for h, m in POST_TIMES:
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if t <= now:
                t += datetime.timedelta(days=1)
            cands.append(t)
        await asyncio.sleep(max(30, (min(cands) - now).total_seconds()))
        try:
            await post_daily(bot)
        except Exception as ex:
            print("channel_poster:", ex)
        await asyncio.sleep(70)

@dp.message(Command("post"))
async def cmd_post(msg: Message):
    if not REQUIRED_CHAT:
        return await msg.answer("Канал не настроен (REQUIRED_CHAT пуст).")
    try:
        m = await msg.bot.get_chat_member(REQUIRED_CHAT, msg.from_user.id)
        if m.status not in ("administrator", "creator"):
            return await msg.answer("Команда доступна только администраторам канала.")
    except Exception:
        return await msg.answer("Не удалось проверить права (бот — админ канала?).")
    ok = await post_daily(msg.bot)
    await msg.answer("Пост отправлен в канал ✅" if ok else "Сегодня нет матчей — пост не отправлен.")

async def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="start", description="Меню"),
        BotCommand(command="app", description="Открыть приложение"),
        BotCommand(command="today", description="Матчи сегодня"),
        BotCommand(command="rating", description="Рейтинг прогнозистов"),
        BotCommand(command="express", description="Экспресс дня"),
    ])
    try:
        if REQUIRED_CHAT:
            # Гейт включён: глобально убираем кнопку-приложение, подписчикам включим персонально.
            await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
        else:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Приложение", web_app=WebAppInfo(url=BASE)))
    except Exception as ex:
        print("Не удалось задать кнопку-меню:", ex)
    global BOT_USERNAME
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username or ""
    except Exception as ex:
        print("get_me:", ex)
    await start_web(bot)
    asyncio.create_task(channel_poster(bot))
    print("Бот запущен (polling)…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
