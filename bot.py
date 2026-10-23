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
import os, time, re, asyncio, html, random, json, hmac, hashlib, datetime, base64
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
POST_HISTORY_REPO = os.environ.get("POST_HISTORY_REPO", "Dikey123/football-prognozy").strip()
POST_HISTORY_FILE = os.environ.get("POST_HISTORY_FILE", "daily_posts.json").strip()
POST_HISTORY_BRANCH = os.environ.get("POST_HISTORY_BRANCH", "main").strip()
POST_HISTORY_TOKEN = os.environ.get("POST_HISTORY_TOKEN", "").strip()
_POST_HISTORY_MEMORY = []
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

AI_MIN_PROBABILITY = 75

def ai_probability(opinion):
    if not isinstance(opinion, dict):
        return None
    try:
        if opinion.get("probability") is not None:
            value = int(opinion["probability"])
            return value if 0 <= value <= 100 else None
    except (TypeError, ValueError):
        pass
    match = re.search(r"(?<!\d)(100|[1-9]?\d)\s*%", str(opinion.get("text") or ""))
    return int(match.group(1)) if match else None

def ai_bet_eligible(opinion):
    return (
        isinstance(opinion, dict)
        and opinion.get("eligible") is True
        and ai_probability(opinion) is not None
        and ai_probability(opinion) >= AI_MIN_PROBABILITY
        and bool(str(opinion.get("bet") or opinion.get("pick") or "").strip())
    )

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

def date_window(data):
    today = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=3))).date()
    return today, [today + datetime.timedelta(days=i) for i in range(-2, 5)]

def score_for_match(match, res):
    score = match.get("ft") or match.get("score")
    if not score and res:
        norm = lambda value: re.sub(r"[^a-zа-я0-9]", "", str(value or "").lower().replace("ё", "е"))
        for result in (res.get("recent") or []):
            if (result.get("date") == match.get("date")
                    and norm(result.get("home")) == norm(match.get("home"))
                    and norm(result.get("away")) == norm(match.get("away"))):
                score = result.get("score") or result.get("ft")
                if score:
                    break
    return re.sub(r"\s*[-:]\s*", ":", str(score)).strip() if score else ""

def date_button_label(day, today, count):
    offset = (day - today).days
    if offset == -2:
        label = "2 дня назад"
    elif offset == -1:
        label = "Вчера"
    elif offset == 0:
        label = "Сегодня"
    elif offset == 1:
        label = "Завтра"
    else:
        label = f"Через {offset} дн."
    return f"{label} · {count}"

# ---------- экранчики ----------
def main_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🟢 Открыть приложение", web_app=WebAppInfo(url=BASE))
    kb.button(text="⚽ Матчи по датам", callback_data="today")
    kb.button(text="🏆 Рейтинг", callback_data="rating")
    kb.button(text="⚡ Экспресс дня", callback_data="express")
    kb.adjust(1, 1, 2)
    return kb.as_markup()

async def screen_today(data, res=None, selected=None):
    today, dates = date_window(data)
    try:
        selected_day = datetime.date.fromisoformat(str(selected)) if selected else today
    except ValueError:
        selected_day = today
    if selected_day not in dates:
        selected_day = today
    selected_iso = selected_day.isoformat()
    rows = [(t, i, m) for (t, i, m) in all_matches(data) if m.get("date") == selected_iso]
    rows.sort(key=lambda row: (str(row[2].get("time") or ""), str(row[2].get("home") or "")))
    if selected_day == today:
        title = f"⚽ Матчи сегодня — {e(dlabel(selected_iso))}"
        hint = "Нажмите на матч, чтобы открыть карточку."
    elif selected_day < today:
        title = f"⚽ Результаты — {e(dlabel(selected_iso))}"
        hint = "Итоговый счёт указан на кнопке матча, если источник уже его прислал."
    else:
        title = f"⚽ Матчи — {e(dlabel(selected_iso))}"
        hint = "Нажмите на матч, чтобы открыть прогноз."
    txt = f"<b>{title}</b>\n<i>{hint}</i>"
    if not rows:
        txt += "\n\nНа эту дату матчей нет. Выберите другой день ниже."
    kb = InlineKeyboardBuilder()
    counts = {day.isoformat(): 0 for day in dates}
    for _, _, match in all_matches(data):
        if match.get("date") in counts:
            counts[match["date"]] += 1
    for day in dates:
        day_iso = day.isoformat()
        kb.button(text=date_button_label(day, today, counts[day_iso]), callback_data=f"d:{day_iso}")
    for t, i, m in rows:
        full = m.get("status") == "full"
        score = score_for_match(m, res)
        if selected_day < today:
            label = f"{score}  {m['home']} — {m['away']}" if score else f"{m['home']} — {m['away']} · счёт уточняется"
        else:
            label = f"{m.get('time') or 'Время уточняется'}  {m['home']} — {m['away']}" + ("" if full else "  (расписание)")
        kb.button(text=label[:60], callback_data=f"m:{t['id']}:{i}")
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(4, 3, *([1] * (len(rows) + 1)))
    return txt, kb.as_markup()

def bar(cnt):
    tot = cnt["1"] + cnt["X"] + cnt["2"]
    if not tot:
        return ""
    def blk(n): return "█" * round(n / tot * 10)
    return f"🟩{cnt['1']} ⬜{cnt['X']} 🟦{cnt['2']}"

def opinion_block(src, emoji, name, match=None):
    """Показывает AI-ставку только с оценкой от 75% и доступным коэффициентом."""
    if not src:
        return ""
    if not ai_bet_eligible(src):
        return f"\n{emoji} <b>Мнение {name}</b>: нет ставки ИИ с оценкой модели от 75%."
    probability = ai_probability(src)
    bet = str(src.get("bet") or src.get("pick") or "").strip()
    price, estimated = _price_for_pick(match, bet) if match else (None, False)
    if not price:
        return f"\n{emoji} <b>Мнение {name}</b>: котировка для выбранного исхода не найдена, ставка пропущена."
    marker = "≈" if estimated else ""
    out = f"\n{emoji} <b>Мнение {name}</b>: счёт <b>{e(src.get('score',''))}</b>\n{e(src.get('text',''))}"
    out += f"\nСтавка {name}: <b>{e(bet)}</b> — кф <b>{marker}{price:.2f}</b> (оценка модели {probability}%)"
    sources = [s for s in (src.get("sources") or []) if isinstance(s, dict) and str(s.get("url") or "").startswith(("https://", "http://"))][:4]
    if sources:
        out += "\nИсточники: " + " · ".join(f'<a href="{e(s["url"])}">{e(s.get("title") or s["url"])}</a>' for s in sources)
    return out

def odds_summary(match):
    """Котировки 1X2 без списка букмекеров."""
    odds = match.get("odds") or []
    if not any(value not in (None, "") for value in odds):
        return ""
    def fmt(value):
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return "—"
    values = [fmt(odds[i] if i < len(odds) else None) for i in range(3)]
    lines = ["📊 <b>Коэффициенты 1X2</b>: П1 " + values[0] + " · Х " + values[1] + " · П2 " + values[2]]
    stamp = match.get("odds_updated") or max(
        (source.get("updated", "") for source in (match.get("odds_sources") or []) if source.get("updated")),
        default="",
    )
    if stamp:
        lines.append("🕘 Обновлено: " + e(_format_daily_timestamp(stamp)))
    return "\n".join(lines)

def screen_match(t, m, res=None):
    lines = [f"<b>{e(m['home'])} — {e(m['away'])}</b>",
             f"{e(t.get('name',''))} · {e(t.get('round',''))}",
             f"🗓 {e(dlabel(m['date']))}, {e(m['time'])}" + (f" · {e(m.get('venue',''))}" if m.get('venue') else "")]
    score = score_for_match(m, res)
    if score:
        lines.append(f"🏁 Итоговый счёт: <b>{e(score)}</b>")
    quote_block = odds_summary(m)
    if quote_block:
        lines.append("\n" + quote_block)
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
    for blk in (opinion_block(m.get("ai"), "🤖", "ИИ", m),
                opinion_block(m.get("gpt"), "🧠", "ChatGPT", m)):
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
    kb.button(text="⬅️ К матчам", callback_data=f"d:{m.get('date', '')}")
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
        price = _valid_decimal(tp.get("k"))
        if not price:
            continue
        reg = "🇷🇺" if tp.get("reg") == "ru" else "🇪🇺"
        res += f"{reg} <b>{e(tp.get('src',''))}</b>: {e(tp.get('pick',''))} — кф <b>{price:.2f}</b>\n"
    if res.endswith("\n\n"):
        return res + "Для остальных прогнозов пока нет коэффициентов."
    return res

# ---------- рейтинг ----------
def wilson(w, n):
    if not n:
        return 0
    z = 1.0
    p = w / n
    return (p + z*z/(2*n) - z*((p*(1-p)+z*z/(4*n))/n) ** 0.5) / (1 + z*z/n)

def rating_kb(page, total_pages):
    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(text="⬅️ Предыдущая", callback_data=f"r:{page-1}")
    if page + 1 < total_pages:
        kb.button(text="Следующая ➡️", callback_data=f"r:{page+1}")
    if page > 0 and page + 1 < total_pages:
        kb.adjust(2, 1)
    else:
        kb.adjust(1, 1)
    kb.button(text="⬅️ Главное меню", callback_data="menu")
    return kb.as_markup()

def screen_rating(res, page=0):
    if not res:
        return ("Статистика ещё собирается — рейтинг появится после первых сыгранных матчей.", main_kb())
    rows = []
    for src, v in (res.get("sources") or {}).items():
        wins, losses = int(v.get("win", 0) or 0), int(v.get("lose", 0) or 0)
        pushes = int(v.get("push", 0) or 0)
        n = wins + losses
        rows.append({"src": src, "reg": v.get("reg","ru"), "win": wins,
                     "lose": losses, "push": pushes,
                     "pct": round(wins/n*100) if n else 0,
                     "w": wilson(wins,n), "n": n})
    for key, label in (("gpt75", "🧠 ChatGPT ≥75%"), ("ai75", "🤖 Claude ≥75%")):
        value = res.get(key) or {"win":0,"lose":0,"push":0}
        wins = int(value.get("win",0) or 0)
        losses = int(value.get("lose",0) or 0)
        pushes = int(value.get("push",0) or 0)
        n = wins + losses
        rows.append({"src": label, "reg": "ai", "win": wins, "lose": losses,
                     "push": pushes, "pct": round(wins/n*100) if n else 0,
                     "w": wilson(wins,n), "n": n, "ai": True})
    rows.sort(key=lambda r: (-r["w"], -r["n"], -r["pct"]))
    page_size = 15
    total_pages = max(1, (len(rows) + page_size - 1) // page_size)
    try:
        page = max(0, min(int(page), total_pages - 1))
    except (TypeError, ValueError):
        page = 0
    first = page * page_size
    last = min(first + page_size, len(rows))
    out = [f"🏆 <b>Рейтинг прогнозистов</b>\n<i>% по {res.get('matches',0)} сыгранным матчам; всего строк: {len(rows)} · страница {page+1}/{total_pages}. ИИ отдельно: только новые ставки от 75%</i>\n"]
    for i, r in enumerate(rows[first:last], first + 1):
        tag = "" if r.get("ai") else (" 🇷🇺" if r["reg"] == "ru" else " 🇪🇺")
        small = " · мало данных" if r["n"] < 3 else ""
        push = f" · пуш {r['push']}" if r["push"] else ""
        out.append(f"{i}. <b>{e(r['src'])}</b>{tag} — <b>{r['pct']}%</b> ({r['win']}–{r['lose']}{push}{small})")
    out.append("\n<i>Пуши не входят в процент; прошлые результаты не гарантируют будущие. 18+</i>")
    return "\n".join(out), rating_kb(page, total_pages)

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

def _pick_key(pick, match=None):
    """Нормализует прогноз до рынка, чтобы не подставлять цену от другого исхода."""
    raw = str(pick or "").strip().lower().replace("ё", "е")
    norm = re.sub(r"\s+", " ", raw.replace("х", "x"))
    if not norm:
        return None

    if re.search(r"(?<!\d)1\s*x(?!\d)|двойн.*1\s*x|1\s*х", raw) or ("не проигра" in raw and "хозя" in raw):
        return "dc_1x"
    if re.search(r"(?<!\d)x\s*2(?!\d)|двойн.*x\s*2|х\s*2", norm) or ("не проигра" in raw and ("гост" in raw or "выезд" in raw)):
        return "dc_x2"
    if re.search(r"(?<!\d)1\s*2(?!\d)", norm) or "двойн шанс 12" in norm:
        return "dc_12"

    if "обе заб" in raw or re.search(r"(?<![а-я])оз(?![а-я])", raw):
        return "btts_no" if re.search(r"\b(нет|no)\b|не забьют|оз нет", raw) else "btts_yes"

    total_side = None
    if re.search(r"\b(тб|больше|over)\b", norm) or "тотал больше" in norm:
        total_side = "over"
    elif re.search(r"\b(тм|меньше|under)\b", norm) or "тотал меньше" in norm:
        total_side = "under"
    if total_side:
        match_line = re.search(r"(?:тб|тм|больше|меньше|over|under|тотал\s+(?:больше|меньше))\s*([0-9]+(?:[.,][0-9]+)?)", norm)
        line = match_line.group(1).replace(",", ".") if match_line else ""
        return f"total_{total_side}_{line}"

    if "ничья" in raw or "draw" in raw or re.fullmatch(r"[xх]", raw):
        return "result_x"
    if re.search(r"\bп1\b", norm) or "победа хозяев" in raw or (match and match.get("home") and "победа" in raw and str(match["home"]).lower() in raw):
        return "result_1"
    if re.search(r"\bп2\b", norm) or "победа гостей" in raw or (match and match.get("away") and "победа" in raw and str(match["away"]).lower() in raw):
        return "result_2"
    return None


def _valid_decimal(value):
    try:
        number = float(str(value).replace(",", "."))
        return number if 1.01 < number < 30 else None
    except (TypeError, ValueError):
        return None


def _explicit_pick_price(pick):
    text = str(pick or "")
    found = re.search(r"(≈|~|кф\.?|коэффициент(?:ом)?\s*)\s*([0-9]+(?:[.,][0-9]+)?)", text, re.I)
    if not found:
        return None, False
    price = _valid_decimal(found.group(2))
    return price, bool(price and found.group(1) in ("≈", "~"))


def _price_for_pick(match, pick):
    """Возвращает (коэффициент, расчётный), только для совпадающего рынка."""
    price, estimated = _explicit_pick_price(pick)
    if price:
        return price, estimated

    key = _pick_key(pick, match)
    tip_prices = []
    for tip in (match.get("tips") or []):
        if key and _pick_key(tip.get("pick"), match) == key:
            value = _valid_decimal(tip.get("k"))
            if value:
                tip_prices.append(value)
    if tip_prices:
        return max(tip_prices), False

    odds = match.get("odds") or []
    if key in ("result_1", "result_x", "result_2"):
        index = {"result_1": 0, "result_x": 1, "result_2": 2}[key]
        value = _valid_decimal(odds[index] if index < len(odds) else None)
        if value:
            return value, False

    if key in ("dc_1x", "dc_x2", "dc_12") and len(odds) >= 3:
        indexes = {"dc_1x": (0, 1), "dc_x2": (1, 2), "dc_12": (0, 2)}[key]
        first = _valid_decimal(odds[indexes[0]])
        second = _valid_decimal(odds[indexes[1]])
        if first and second:
            return 1 / (1 / first + 1 / second), True
    return None, False


def _safe_double_chance(match):
    """Самый вероятный двойной шанс по рынку 1X2; точная котировка берётся из tip, если есть."""
    odds = match.get("odds") or []
    p1 = _valid_decimal(odds[0] if len(odds) > 0 else None)
    px = _valid_decimal(odds[1] if len(odds) > 1 else None)
    p2 = _valid_decimal(odds[2] if len(odds) > 2 else None)

    if p1 and px and p2:
        key = "dc_1x" if p1 <= p2 else "dc_x2"
        label = "1Х (хозяева не проиграют)" if key == "dc_1x" else "Х2 (гости не проиграют)"
        price, estimated = _price_for_pick(match, label)
        return (label, price, estimated) if price else (None, None, False)

    # Если 1X2 не пришли, используем опубликованную котировку двойного шанса.
    options = []
    for tip in (match.get("tips") or []):
        key = _pick_key(tip.get("pick"), match)
        price = _valid_decimal(tip.get("k"))
        if key in ("dc_1x", "dc_x2") and price:
            options.append((price, str(tip.get("pick") or ""), False))
    if options:
        price, label, _ = min(options, key=lambda item: item[0])
        return label, price, False
    return None, None, False


def parse_k(k):
    return _valid_decimal(k)


def _upcoming_today(match, today, res=None):
    if match.get("date") != today or score_for_match(match, res):
        return False
    if match.get("ft") or match.get("score"):
        return False
    time_text = str(match.get("time") or "")
    found = re.search(r"\b([0-2]?\d):([0-5]\d)\b", time_text)
    if found:
        zone = datetime.timezone(datetime.timedelta(hours=3))
        kickoff = datetime.datetime.combine(
            datetime.date.fromisoformat(today),
            datetime.time(int(found.group(1)), int(found.group(2))),
            tzinfo=zone,
        )
        if kickoff <= datetime.datetime.now(zone):
            return False
    return True


def express(data, n=3, res=None, today=None):
    if not data:
        return []
    today = today or data.get("today") or datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=3))
    ).date().isoformat()
    candidates = []
    for tournament, _, match in all_matches(data):
        if not _upcoming_today(match, today, res):
            continue
        label, price, estimated = _safe_double_chance(match)
        if not label or not price:
            continue
        odds = match.get("odds") or []
        p1 = _valid_decimal(odds[0] if len(odds) > 0 else None)
        p2 = _valid_decimal(odds[2] if len(odds) > 2 else None)
        market_probability = (1 / price) if estimated else (1 / price)
        candidates.append({
            "m": match,
            "pick": label,
            "odd": price,
            "estimated": estimated,
            "mk": "Двойной шанс",
            "tour": tournament.get("short") or tournament.get("name", ""),
            "probability": market_probability,
            "ai_confidence": max(
                int((match.get("ai") or {}).get("conf", 0) or 0),
                int((match.get("gpt") or {}).get("conf", 0) or 0),
            ),
        })

    # Берём матчи с более высокой вероятностью события и не повторяем одну игру.
    candidates.sort(key=lambda item: (-item["probability"], -item["ai_confidence"], item["m"].get("time") or "99:99"))
    selected, seen = [], set()
    for item in candidates:
        key = (item["m"].get("date"), item["m"].get("home"), item["m"].get("away"))
        if key in seen:
            continue
        selected.append(item)
        seen.add(key)
        if len(selected) >= n:
            break
    return selected


def _format_pick_price(item):
    marker = "≈" if item.get("estimated") else ""
    return f"{marker}{item['odd']:.2f}"


def screen_express(data, res=None, today=None):
    sel = express(data, 3, res=res, today=today)
    if len(sel) < 3:
        return "⚡ <b>Экспресс дня — 3 события</b>\nПока недостаточно матчей с доступными коэффициентами двойного шанса. Экспресс появится, когда будут котировки минимум на три предстоящих матча."

    total = 1.0
    out = [
        "⚡ <b>Экспресс дня — 3 события</b>",
        "<i>Выбраны наиболее вероятные двойные шансы по котировкам 1X2. ≈ — расчётный коэффициент.</i>",
    ]
    for index, item in enumerate(sel, 1):
        total *= item["odd"]
        out.append(
            f"\n{index}. <b>{e(item['m']['home'])} — {e(item['m']['away'])}</b> "
            f"({e(item['tour'])}, {e(item['m'].get('time') or 'время уточняется')})\n"
            f"   Выбор: <b>{e(item['pick'])}</b> — кф <b>{_format_pick_price(item)}</b>"
        )
    total_marker = "≈" if any(item.get("estimated") for item in sel) else ""
    out.append(f"\n<b>Общий коэффициент: {total_marker}{total:.2f}</b>")
    out.append("<i>Вероятность экспресса ниже вероятности каждого отдельного события. Аналитика, не гарантия. 18+</i>")
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
        "• Матчи за 2 дня назад — 4 дня вперёд, с результатами сыгранных\n"
        "• Коэффициенты, составы, новости и мнение ИИ по матчам\n"
        "• Рейтинг прогнозистов по реальной проходимости\n"
        "• Конструктор экспресса\n\n"
        "<i>18+. Это аналитика, а не гарантия результата.</i>",
        reply_markup=main_kb())

@dp.message(Command("app"))
async def cmd_app(msg: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🟢 Открыть приложение", web_app=WebAppInfo(url=BASE))
    await msg.answer("Открыть «Капер ИИ» как приложение — на весь экран:", reply_markup=kb.as_markup())

@dp.message(Command("today", "matches"))
async def cmd_today(msg: Message):
    data, res = await get_data()
    if not data:
        return await msg.answer("Не удалось загрузить данные. Попробуйте позже.")
    txt, kb = await screen_today(data, res)
    await msg.answer(txt, reply_markup=kb)

@dp.message(Command("rating"))
async def cmd_rating(msg: Message):
    _, res = await get_data()
    txt, kb = screen_rating(res)
    await msg.answer(txt, reply_markup=kb, disable_web_page_preview=True)

@dp.message(Command("express"))
async def cmd_express(msg: Message):
    data, _ = await get_data()
    await msg.answer(screen_express(data) if data else "Нет данных.", reply_markup=main_kb())

@dp.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    await c.message.edit_text("Главное меню:", reply_markup=main_kb()); await c.answer()

@dp.callback_query(F.data == "today")
async def cb_today(c: CallbackQuery):
    data, res = await get_data()
    if not data:
        return await c.answer("Не удалось загрузить данные. Попробуйте позже.", show_alert=True)
    txt, kb = await screen_today(data, res)
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("d:"))
async def cb_date(c: CallbackQuery):
    data, res = await get_data()
    if not data:
        return await c.answer("Не удалось загрузить данные. Попробуйте позже.", show_alert=True)
    txt, kb = await screen_today(data, res, c.data[2:])
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@dp.callback_query(F.data == "rating")
async def cb_rating(c: CallbackQuery):
    _, res = await get_data()
    txt, kb = screen_rating(res)
    await c.message.edit_text(txt, reply_markup=kb, disable_web_page_preview=True); await c.answer()

@dp.callback_query(F.data.startswith("r:"))
async def cb_rating_page(c: CallbackQuery):
    try:
        page = int(c.data[2:])
    except (TypeError, ValueError):
        page = 0
    _, res = await get_data()
    txt, kb = screen_rating(res, page)
    await c.message.edit_text(txt, reply_markup=kb, disable_web_page_preview=True)
    await c.answer()

@dp.callback_query(F.data == "express")
async def cb_express(c: CallbackQuery):
    data, _ = await get_data()
    await c.message.edit_text(screen_express(data), reply_markup=main_kb()); await c.answer()

@dp.callback_query(F.data.startswith("m:"))
async def cb_match(c: CallbackQuery):
    _, tid, idx = c.data.split(":")
    data, res = await get_data()
    t, m = find_match(data, tid, int(idx))
    if not m:
        return await c.answer("Матч не найден", show_alert=True)
    await c.message.edit_text(
        screen_match(t, m, res),
        reply_markup=match_kb(t, int(idx), m),
        disable_web_page_preview=True,
    )
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

def _format_daily_timestamp(value):
    if not value:
        return ""
    try:
        stamp = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        msk = datetime.timezone(datetime.timedelta(hours=3))
        return stamp.astimezone(msk).strftime("%d.%m %H:%M МСК")
    except (TypeError, ValueError):
        return str(value)


def _daily_model_candidates(data, res, today):
    """Берёт только AI-ставки с вероятностью от 75% и котировкой этого рынка."""
    candidates = []
    for tournament, _, match in all_matches(data):
        if not _upcoming_today(match, today, res):
            continue
        opinions = []
        for key, label in (("ai", "Claude"), ("gpt", "ChatGPT")):
            opinion = match.get(key)
            if not ai_bet_eligible(opinion):
                continue
            probability = ai_probability(opinion)
            bet = str(opinion.get("bet") or opinion.get("pick") or "").strip()
            price, estimated = _price_for_pick(match, bet)
            if not price:
                continue
            opinions.append({
                "model": label,
                "bet": bet,
                "score": str(opinion.get("score") or "").strip(),
                "text": str(opinion.get("text") or ""),
                "probability": probability,
                "confidence": probability,
                "price": price,
                "estimated": estimated,
                "match": match,
            })
        if not opinions:
            continue
        opinion = max(opinions, key=lambda item: (item["probability"], item["model"] == "Claude"))
        candidates.append({
            "home": match.get("home", ""),
            "away": match.get("away", ""),
            "time": match.get("time", ""),
            "tournament": tournament.get("name", ""),
            "short_tournament": tournament.get("short") or tournament.get("name", ""),
            "experts": len(match.get("tips") or []),
            "probability": str(opinion["probability"]),
            "probability_value": opinion["probability"] / 100,
            "confidence": opinion["confidence"],
            "model": opinion["model"],
            "score": opinion["score"],
            "text": opinion["text"],
            "bet": opinion["bet"],
            "coefficient": opinion["price"],
            "coefficient_estimated": opinion["estimated"],
            "fallback": False,
            "match": match,
        })
    candidates.sort(key=lambda item: (
        -item["probability_value"],
        -(1 / item["coefficient"]),
        -item["experts"],
        item["time"] or "99:99",
    ))
    return candidates


def _daily_predictions_block(data, res, today):
    candidates = _daily_model_candidates(data, res, today)
    out = []
    snapshot = {"date": today, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "single": None, "express": []}
    if candidates:
        best = candidates[0]
        meta = []
        if best["score"]:
            meta.append(f"счёт ИИ {e(best['score'])}")
        if best["probability"]:
            meta.append(f"оценка модели {best['probability']}%")
        elif best["confidence"]:
            meta.append(f"уверенность {best['confidence']}/5")
        if best["time"]:
            meta.append(e(best["time"]))
        suffix = " · " + " · ".join(meta) if meta else ""
        title = "Выбор ИИ" if not best["fallback"] else "Осторожный выбор по котировкам для прогноза ИИ"
        price = ("≈" if best["coefficient_estimated"] else "") + f"{best['coefficient']:.2f}"
        snapshot["single"] = {
            "date": today, "home": best["home"], "away": best["away"],
            "tournament": best["tournament"], "time": best["time"],
            "bet": best["bet"], "coefficient": best["coefficient"],
            "coefficient_estimated": best["coefficient_estimated"],
            "model": best["model"], "probability": best["probability"],
        }
        out.extend([
            f"🎯 <b>Лучший прогноз дня</b>",
            f"<b>{e(best['home'])} — {e(best['away'])}</b> · {e(best['tournament'])}",
            f"{title}: <b>{e(best['bet'])}</b> — кф <b>{price}</b>{suffix}",
        ])
    else:
        out.extend([
            "🎯 <b>Прогноз дня</b>",
            "Пока нет прогноза на предстоящий матч с проверяемым коэффициентом.",
        ])

    today_value = data.get("today") or today
    legs = express(data, 3, res=res, today=today_value)
    if len(legs) == 3:
        total = 1.0
        out.append("\n⚡ <b>Экспресс дня — 3 события</b>")
        for index, item in enumerate(legs, 1):
            total *= item["odd"]
            price = _format_pick_price(item)
            snapshot["express"].append({
                "date": today, "home": item["m"]["home"], "away": item["m"]["away"],
                "tournament": item["tour"], "time": item["m"].get("time") or "",
                "bet": item["pick"], "coefficient": item["odd"],
                "coefficient_estimated": item["estimated"],
            })
            out.append(
                f"{index}. <b>{e(item['m']['home'])} — {e(item['m']['away'])}</b> "
                f"({e(item['tour'])}, {e(item['m'].get('time') or 'время уточняется')})\n"
                f"   {e(item['pick'])} — кф <b>{price}</b>"
            )
        snapshot["express_coefficient"] = total
        marker = "≈" if any(item.get("estimated") for item in legs) else ""
        out.append(f"<b>Общий коэффициент: {marker}{total:.2f}</b>")
        if any(item.get("estimated") for item in legs):
            out.append("<i>≈ — расчётный коэффициент двойного шанса по котировкам 1X2, не прямая котировка букмекера.</i>")
    else:
        out.append("\n⚡ <b>Экспресс дня — 3 события</b>")
        out.append("Сегодня пока нет коэффициентов минимум на три предстоящих матча, поэтому непроверенные цены не публикуем.")
    out.append("<i>Экспресс рискованнее одиночного события: должны пройти все три выбора. 18+</i>")
    return "\n".join(out), snapshot

def _daily_rating_block(res):
    """Показывает текущую проходимость источников и моделей по закрытым прогнозам."""
    if not res:
        return "🏆 <b>Рейтинг прогнозистов</b>\nСтатистика пока недоступна."

    sources = []
    for name, value in (res.get("sources") or {}).items():
        try:
            wins = int(value.get("win", 0) or 0)
            losses = int(value.get("lose", 0) or 0)
        except (TypeError, ValueError):
            continue
        total = wins + losses
        if total:
            sources.append({
                "name": name,
                "wins": wins,
                "losses": losses,
                "total": total,
                "pct": round(wins / total * 100),
                "wilson": wilson(wins, total),
            })

    sources.sort(key=lambda row: (-row["wilson"], -row["total"], -row["pct"]))
    qualified = [row for row in sources if row["total"] >= 3]
    shown = (qualified or sources)[:3]

    out = ["🏆 <b>Рейтинг прогнозистов</b>"]
    done = res.get("done")
    if isinstance(done, list):
        out.append(f"<i>По {len(done)} матчам с зафиксированным результатом</i>")

    if shown:
        for index, row in enumerate(shown, 1):
            note = " · мало данных" if row["total"] < 3 else ""
            out.append(
                f"{index}. <b>{e(row['name'])}</b> — {row['pct']}% "
                f"({row['wins']}–{row['losses']}){note}"
            )
    else:
        out.append("Рейтинг сайтов появится после первых рассчитанных прогнозов.")

    for key, label in (("ai", "ИИ-модель"), ("gpt", "ChatGPT")):
        value = res.get(key) or {}
        try:
            wins = int(value.get("win", 0) or 0)
            losses = int(value.get("lose", 0) or 0)
        except (TypeError, ValueError):
            wins = losses = 0
        total = wins + losses
        if total:
            out.append(
                f"{'🤖' if key == 'ai' else '🧠'} <b>{label}</b> — "
                f"{round(wins / total * 100)}% ({wins}–{losses})"
                + (" · мало данных" if total < 3 else "")
            )
        elif key == "ai":
            out.append("🤖 <b>ИИ-модель</b> — статистика накапливается.")
    return "\n".join(out)



_HISTORY_API = f"https://api.github.com/repos/{POST_HISTORY_REPO}/contents/{POST_HISTORY_FILE}"

async def _read_daily_post_history():
    if not POST_HISTORY_TOKEN:
        return {"version": 1, "posts": list(_POST_HISTORY_MEMORY)}, None, True
    headers = {
        "Authorization": f"Bearer {POST_HISTORY_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(_HISTORY_API, params={"ref": POST_HISTORY_BRANCH}, headers=headers) as response:
                if response.status == 404:
                    return {"version": 1, "posts": []}, None, True
                if response.status != 200:
                    print("Не удалось прочитать архив ежедневных постов, HTTP", response.status)
                    return None, None, False
                remote = await response.json(content_type=None)
        raw = base64.b64decode(remote.get("content", "")).decode("utf-8")
        document = json.loads(raw)
        if not isinstance(document, dict) or not isinstance(document.get("posts"), list):
            print("Архив ежедневных постов имеет неверный формат.")
            return None, None, False
        return document, remote.get("sha"), True
    except Exception as ex:
        print("Не удалось прочитать архив ежедневных постов:", ex)
        return None, None, False


async def load_daily_post_history():
    document, _, ok = await _read_daily_post_history()
    if ok and document is not None:
        return document
    return {"version": 1, "posts": list(_POST_HISTORY_MEMORY)}


async def save_daily_post_snapshot(snapshot):
    posts = [item for item in _POST_HISTORY_MEMORY if item.get("date") != snapshot.get("date")]
    posts.append(snapshot)
    _POST_HISTORY_MEMORY[:] = posts[-90:]
    if not POST_HISTORY_TOKEN:
        print("POST_HISTORY_TOKEN не задан: архив ежедневных постов пока хранится только в памяти процесса.")
        return False

    document, sha, ok = await _read_daily_post_history()
    if not ok or document is None:
        print("Снимок поста не сохранён: не удалось прочитать архив.")
        return False
    remote_posts = [item for item in document.get("posts", []) if item.get("date") != snapshot.get("date")]
    remote_posts.append(snapshot)
    document = {"version": 1, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "posts": remote_posts[-90:]}
    payload = {
        "message": f"chore: сохранить пост с прогнозами за {snapshot.get('date', '')}",
        "content": base64.b64encode(json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii"),
        "branch": POST_HISTORY_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    headers = {
        "Authorization": f"Bearer {POST_HISTORY_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.put(_HISTORY_API, json=payload, headers=headers) as response:
                if response.status not in (200, 201):
                    print("Не удалось сохранить архив ежедневных постов, HTTP", response.status)
                    return False
        print(f"Снимок ежедневного поста сохранён: {snapshot.get('date')}.")
        return True
    except Exception as ex:
        print("Не удалось сохранить архив ежедневных постов:", ex)
        return False


def _history_norm_team(value):
    return re.sub(r"[^a-zа-я0-9]", "", str(value or "").lower().replace("ё", "е"))


def _score_for_daily_pick(pick, data, res):
    date = str(pick.get("date") or "")
    home, away = _history_norm_team(pick.get("home")), _history_norm_team(pick.get("away"))
    for _, _, match in all_matches(data or {}):
        if (match.get("date") == date
                and _history_norm_team(match.get("home")) == home
                and _history_norm_team(match.get("away")) == away):
            score = score_for_match(match, res)
            if score:
                return score
    for result in (res or {}).get("recent", []) or []:
        if (result.get("date") == date
                and _history_norm_team(result.get("home")) == home
                and _history_norm_team(result.get("away")) == away):
            score = result.get("score") or result.get("ft")
            if score:
                return re.sub(r"\s*[-:]\s*", ":", str(score)).strip()
    return ""


def _settle_daily_pick_token(pick, home_goals, away_goals, home, away):
    text = str(pick or "").strip().lower().replace("ё", "е").replace("х", "x").replace("−", "-").replace("–", "-").replace(",", ".")
    total = home_goals + away_goals
    outcome = "1" if home_goals > away_goals else ("2" if home_goals < away_goals else "x")

    if "обе заб" in text or "оба заб" in text or re.search(r"(?<![а-я])оз(?![а-я])", text):
        yes = not any(word in text for word in ("нет", "не заб", "no"))
        return "win" if ((home_goals > 0 and away_goals > 0) == yes) else "lose"

    total_line = re.search(r"(?:тотал\s*(?:больше|меньше)|т[бм]|over|under|больше|меньше)\s*[:(]?\s*(\d+(?:\.\d+)?)", text)
    if total_line:
        line = float(total_line.group(1))
        over = any(word in text for word in ("тб", "больше", "over"))
        if total == line:
            return "push"
        return "win" if ((total > line) == over) else "lose"

    handicap = re.search(r"(?:фора\s*1|ф1)\s*\(?\s*([+-]?\d+(?:\.\d+)?)", text)
    if handicap:
        adjusted = home_goals + float(handicap.group(1)) - away_goals
        return "push" if adjusted == 0 else ("win" if adjusted > 0 else "lose")
    handicap = re.search(r"(?:фора\s*2|ф2)\s*\(?\s*([+-]?\d+(?:\.\d+)?)", text)
    if handicap:
        adjusted = away_goals + float(handicap.group(1)) - home_goals
        return "push" if adjusted == 0 else ("win" if adjusted > 0 else "lose")

    if re.search(r"(?<![a-zа-я0-9])1\s*x(?![a-zа-я0-9])", text):
        return "win" if outcome in ("1", "x") else "lose"
    if re.search(r"(?<![a-zа-я0-9])x\s*2(?![a-zа-я0-9])", text):
        return "win" if outcome in ("x", "2") else "lose"
    if re.search(r"(?<![a-zа-я0-9])1\s*2(?![a-zа-я0-9])", text):
        return "win" if outcome in ("1", "2") else "lose"

    exact = re.search(r"(\d+)\s*:\s*(\d+)", text)
    if exact and ("счет" in text or "точный" in text):
        return "win" if (int(exact.group(1)) == home_goals and int(exact.group(2)) == away_goals) else "lose"

    cleaned = re.sub(r"[^a-zа-я0-9]+", " ", text).strip()
    if cleaned in ("п1", "1", "победа 1", "победа хозяев"):
        return "win" if outcome == "1" else "lose"
    if cleaned in ("п2", "2", "победа 2", "победа гостей"):
        return "win" if outcome == "2" else "lose"
    if cleaned in ("х", "x", "ничья", "ничья x"):
        return "win" if outcome == "x" else "lose"

    home_norm, away_norm = _history_norm_team(home), _history_norm_team(away)
    if "не проигр" in text or "не уступ" in text:
        if home_norm and home_norm in _history_norm_team(text):
            return "win" if outcome in ("1", "x") else "lose"
        if away_norm and away_norm in _history_norm_team(text):
            return "win" if outcome in ("2", "x") else "lose"
    if "побед" in text or "выигр" in text:
        if home_norm and home_norm in _history_norm_team(text):
            return "win" if outcome == "1" else "lose"
        if away_norm and away_norm in _history_norm_team(text):
            return "win" if outcome == "2" else "lose"
    return None


def _settle_daily_pick(pick, score):
    score_match = re.match(r"\s*(\d+)\s*[:\-]\s*(\d+)", str(score or ""))
    if not score_match:
        return None
    return _settle_daily_pick_token(
        pick.get("bet", ""), int(score_match.group(1)), int(score_match.group(2)),
        pick.get("home", ""), pick.get("away", ""),
    )


def _rubles(value):
    return f"{int(round(value)):,}".replace(",", " ") + " ₽"


def _signed_rubles(value):
    if value > 0:
        return "+" + _rubles(value)
    if value < 0:
        return "−" + _rubles(abs(value))
    return _rubles(0)


def _settled_pick_report(pick, data, res, label):
    score = _score_for_daily_pick(pick, data, res)
    if not score:
        return {"line": f"⏳ {label}: счёт ещё не поступил.", "state": None}
    result = _settle_daily_pick(pick, score)
    coefficient = _valid_decimal(pick.get("coefficient"))
    match_name = f"{e(pick.get('home'))} — {e(pick.get('away'))}"
    bet = e(pick.get("bet") or "")
    if result is None or not coefficient:
        return {"line": f"⚠️ {label}: {match_name}, счёт {e(score)}; исход ставки или коэффициент не удалось надёжно рассчитать.", "state": None}
    if result == "win":
        payout = round(1000 * coefficient)
        net = payout - 1000
        status = "✅ ЗАШЁЛ"
    elif result == "push":
        payout = 1000
        net = 0
        status = "↩️ ВОЗВРАТ"
    else:
        payout = 0
        net = -1000
        status = "❌ НЕ ЗАШЁЛ"
    price = ("≈" if pick.get("coefficient_estimated") else "") + f"{coefficient:.2f}"
    line = (
        f"{status} {label}: {match_name}, выбор <b>{bet}</b>, счёт {e(score)}, кф {e(price)}.\n"
        f"   Ставка 1 000 ₽ → выплата {_rubles(payout)}, чистый итог {_signed_rubles(net)}."
    )
    return {"line": line, "state": result, "payout": payout, "net": net, "coefficient": coefficient}


def _daily_results_block(history, yesterday, data, res):
    posts = history.get("posts") if isinstance(history, dict) else []
    snapshot = next((item for item in (posts or []) if item.get("date") == yesterday), None)
    title = f"📊 <b>Результаты прогнозов за {e(dlabel(yesterday))}</b>"
    if not snapshot:
        return (
            title + "\nЗа этот день архив опубликованных прогнозов отсутствует. "
            "С сегодняшнего поста сохраняем ставки и коэффициенты, чтобы считать результат прозрачно."
        )

    lines = [title]
    single = snapshot.get("single")
    if single:
        report = _settled_pick_report(single, data, res, "Лучший прогноз")
        lines.append(report["line"])
    else:
        report = None
        lines.append("Вчера основной ИИ-прогноз с доступным коэффициентом не публиковался.")

    express_legs = snapshot.get("express") or []
    if len(express_legs) == 3:
        reports = [_settled_pick_report(leg, data, res, f"Экспресс {index}/3") for index, leg in enumerate(express_legs, 1)]
        lines.extend(item["line"] for item in reports)
        states = [item.get("state") for item in reports]
        if all(state in ("win", "lose", "push") for state in states):
            if "lose" in states:
                payout, net, result_text = 0, -1000, "❌ ЭКСПРЕСС НЕ ЗАШЁЛ"
            else:
                effective_coefficient = 1.0
                for leg, state in zip(express_legs, states):
                    if state == "win":
                        effective_coefficient *= float(leg["coefficient"])
                payout = round(1000 * effective_coefficient)
                net = payout - 1000
                result_text = "✅ ЭКСПРЕСС ЗАШЁЛ"
            lines.append(
                f"{result_text}: ставка 1 000 ₽ → выплата {_rubles(payout)}, "
                f"чистый итог {_signed_rubles(net)}."
            )
        else:
            lines.append("Итог экспресса пока не рассчитан: не по всем трём событиям есть подтверждённый результат.")
    else:
        lines.append("Экспресс из трёх событий вчера не публиковался.")

    lines.append("<i>Выплата включает сумму ставки; это расчёт по опубликованным коэффициентам, не обещание дохода. 18+.</i>")
    return "\n".join(lines)

async def post_daily(bot):
    if not REQUIRED_CHAT:
        return False
    # Для ежедневной публикации обходим кэш бота: берём свежие матчи и рейтинг.
    data, res = await get_data(force=True)
    if not data:
        return False
    today = data.get("today")
    if not today:
        today = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=3))
        ).date().isoformat()
    rows = [m for (t, i, m) in all_matches(data) if m.get("date") == today]
    predictions_text, snapshot = _daily_predictions_block(data, res, today)
    history = await load_daily_post_history()
    yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    results_text = _daily_results_block(history, yesterday, data, res)

    parts = [
        "🔥 <b>Капер ИИ</b> — прогнозы на сегодня",
        f"⚽ Матчей сегодня: <b>{len(rows)}</b> · {e(dlabel(today))}",
        f"🔄 Данные обновлены: {e(_format_daily_timestamp(data.get('updated')) or 'время не указано')}",
        "",
        predictions_text,
        "",
        _daily_rating_block(res),
    ]
    if not rows:
        parts.insert(2, "На сегодня матчей нет — опубликовали актуальный рейтинг.")
    parts.append("\nВсе матчи и подробные прогнозы — в приложении 👇\n<i>18+. Аналитика, не гарантия.</i>")
    kb = InlineKeyboardBuilder()
    kb.button(text="⚽ Открыть в боте", url=bot_link())
    posted = await bot.send_message(REQUIRED_CHAT, "\n".join(parts),
                                    reply_markup=kb.as_markup(), disable_web_page_preview=True)
    snapshot["message_id"] = posted.message_id
    await save_daily_post_snapshot(snapshot)
    try:
        await bot.send_message(REQUIRED_CHAT, results_text, disable_web_page_preview=True)
    except Exception as ex:
        print("Не удалось отправить отчёт за вчера:", ex)
    print(f"Ежедневный пост отправлен: {today}, матчей {len(rows)}.")
    return True

POST_TIMES = [(12, 30)]  # публикуем после сбора прогнозов в 12:00 МСК
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
        BotCommand(command="matches", description="Матчи и результаты по датам"),
        BotCommand(command="today", description="Матчи и результаты по датам"),
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
