#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Телеграм-бот «Капер ИИ» — прогнозы на футбол.
Этап 1 (MVP): читает данные нашего сайта с GitHub Pages и показывает:
  /start   — приветствие и меню
  /today   — матчи на сегодня (все турниры)
  /match   — карточка матча (через кнопки): коэффициенты, составы, новости, мнение ИИ
  /rating  — рейтинг прогнозистов (% проходимости) + строка ИИ
  /express — экспресс дня (3–5 событий, общий коэффициент от 2.00)

Токен НЕ хранится в коде. Передаётся через переменную окружения BOT_TOKEN.
Запуск локально:   BOT_TOKEN=xxxx python bot.py
"""
import os, time, re, asyncio, html, random, json, hmac, hashlib, datetime, itertools, math
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
# Адрес страницы приложения: отдаём её с самого бота (/app), чтобы не зависеть от публичного GitHub Pages.
_rext = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
APP_URL = (_rext + "/app") if _rext else (os.environ.get("APP_URL", "").strip() or BASE)
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

_cache = {"t": 0, "data": None, "res": None, "source": None}

# ---------- база данных (Supabase Postgres); всё отказоустойчиво ----------
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
_pool = None

async def db_init():
    """Подключение к базе и создание схемы. Любая ошибка → работаем без базы (от сайта)."""
    global _pool
    if not DATABASE_URL:
        print("DB: DATABASE_URL не задан — источник данных: публичный сайт.")
        return
    try:
        import ssl as _ssl, asyncpg
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        _pool = await asyncpg.create_pool(DATABASE_URL, ssl=ctx, min_size=1, max_size=3, command_timeout=20)
        async with _pool.acquire() as c:
            await c.execute("""
                CREATE TABLE IF NOT EXISTS snapshots(
                    key text PRIMARY KEY,
                    payload text NOT NULL,
                    updated_at timestamptz NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS users(
                    user_id bigint PRIMARY KEY,
                    username text,
                    first_name text,
                    first_seen timestamptz NOT NULL DEFAULT now(),
                    last_seen timestamptz NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS picks(
                    id bigserial PRIMARY KEY,
                    user_id bigint NOT NULL,
                    match_key text NOT NULL,
                    pick text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    result text,
                    UNIQUE(user_id, match_key)
                );
            """)
        print("DB: подключено, схема готова.")
    except Exception as ex:
        print("DB init error (работаем без базы):", ex)
        _pool = None

async def db_get_snapshot(key):
    if not _pool:
        return None
    try:
        async with _pool.acquire() as c:
            row = await c.fetchrow("SELECT payload FROM snapshots WHERE key=$1", key)
        if row and row["payload"]:
            return json.loads(row["payload"])
    except Exception as ex:
        print("DB get error:", ex)
    return None

async def db_get_text(key):
    if not _pool:
        return None
    try:
        async with _pool.acquire() as c:
            row = await c.fetchrow("SELECT payload FROM snapshots WHERE key=$1", key)
        if row and row["payload"]:
            return row["payload"]
    except Exception as ex:
        print("DB text error:", ex)
    return None

async def db_touch_user(user):
    if not _pool or user is None:
        return
    try:
        async with _pool.acquire() as c:
            await c.execute(
                """INSERT INTO users(user_id, username, first_name, last_seen)
                   VALUES($1,$2,$3, now())
                   ON CONFLICT(user_id) DO UPDATE
                   SET username=EXCLUDED.username, first_name=EXCLUDED.first_name, last_seen=now()""",
                user.id, getattr(user, "username", None), getattr(user, "first_name", None))
    except Exception as ex:
        print("DB user error:", ex)

async def db_status():
    if not _pool:
        return {"connected": False}
    out = {"connected": True, "snapshots": {}, "users": None}
    try:
        async with _pool.acquire() as c:
            for r in await c.fetch("SELECT key, updated_at FROM snapshots"):
                out["snapshots"][r["key"]] = r["updated_at"].isoformat()
            out["users"] = await c.fetchval("SELECT count(*) FROM users")
    except Exception as ex:
        out["error"] = str(ex)
    return out

MON = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
DAYS = ["воскресенье","понедельник","вторник","среда","четверг","пятница","суббота"]

def e(s):  # экранирование HTML
    return html.escape(str(s if s is not None else ""))

AI_MIN_PROBABILITY = 70

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
    # Сначала закрытая база; если там пусто/недоступно — публичный сайт (переходный период).
    data = await db_get_snapshot("data")
    res  = await db_get_snapshot("results")
    gpt  = await db_get_snapshot("gpt")
    src = "db" if data else None
    if not data:
        async with aiohttp.ClientSession() as s:
            data = await fetch_json(s, BASE + "data.json")
            res  = await fetch_json(s, BASE + "results.json")
            gpt  = await fetch_json(s, BASE + "gpt.json")
        src = "site"
    if data:
        merge_gpt(data, gpt)
        _cache.update(t=now, data=data, res=res, source=src)
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
    kb.button(text="🟢 Открыть приложение", web_app=WebAppInfo(url=APP_URL))
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
    """Всегда показывает мнение и счёт; ставку — только с порогом и котировкой."""
    if not src:
        return ""
    bet = str(src.get("bet") or src.get("pick") or "").strip()
    eligible = ai_bet_eligible(src)
    price, estimated = _price_for_pick(match, bet) if match and eligible else (None, False)
    opinion_text = str(src.get("text") or "")
    opinion_text = re.sub(r"Выбор модели\s*[—:-]\s*[^\n]*", "", opinion_text, flags=re.I)
    opinion_text = re.sub(r"Модель не выбрала ставку\.?", "", opinion_text, flags=re.I)
    opinion_text = re.sub(
        r"(?:Оценка модели|уверенность модели)\s*[—:-]?\s*\d{1,3}\s*%[^.\n]*(?:\.|$)",
        "", opinion_text, flags=re.I,
    ).strip()
    opinion_text = re.sub(r"\s{2,}", " ", opinion_text)
    if not opinion_text:
        opinion_text = "Аналитическое мнение по матчу сформировано."
    score = str(src.get("score") or "").strip()
    out = f"\n{emoji} <b>Мнение {name}</b>"
    if score:
        out += f": счёт <b>{e(score)}</b>"
    out += "\n" + e(opinion_text)
    if eligible and price:
        marker = "≈" if estimated else ""
        out += f"\nСтавка {name}: <b>{e(bet)}</b> — кф <b>{marker}{price:.2f}</b>"
    else:
        out += "\nСтавка не отобрана."
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

def ai_bucket(res, key):
    value = (res or {}).get(key) or {}
    try:
        wins = int(value.get("win", 0) or 0)
        losses = int(value.get("lose", 0) or 0)
        pushes = int(value.get("push", 0) or 0)
    except (TypeError, ValueError):
        wins = losses = pushes = 0
    total = wins + losses
    return {
        "win": wins,
        "lose": losses,
        "push": pushes,
        "n": total,
        "pct": round(wins / total * 100) if total else None,
    }

def ai_bucket_text(label, value):
    pct = f"{value['pct']}%" if value["pct"] is not None else "—"
    details = f"{value['win']}–{value['lose']}, {value['n']} ставок"
    if value["push"]:
        details += f", возвратов {value['push']}"
    if 0 < value["n"] < 3:
        details += ", мало данных"
    return f"{label}: {pct} ({details})"

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
    for current_key, legacy_key, archive_key, label in (
        ("gpt70", "gpt60", "gpt75", "🧠 Локальная модель"),
        ("ai70", "ai60", "ai75", "🤖 Claude"),
    ):
        current = ai_bucket(res, current_key)
        legacy = ai_bucket(res, legacy_key)
        archive = ai_bucket(res, archive_key)
        ranked = current if current["n"] else (legacy if legacy["n"] else archive)
        rows.append({
            "src": label,
            "reg": "ai",
            "win": ranked["win"],
            "lose": ranked["lose"],
            "push": ranked["push"],
            "pct": ranked["pct"] or 0,
            "w": wilson(ranked["win"], ranked["n"]),
            "n": ranked["n"],
            "ai": True,
            "ai_current": current,
            "ai_legacy": legacy,
            "ai_archive": archive,
        })
    rows.sort(key=lambda r: (-r["w"], -r["n"], -r["pct"]))
    page_size = 15
    total_pages = max(1, (len(rows) + page_size - 1) // page_size)
    try:
        page = max(0, min(int(page), total_pages - 1))
    except (TypeError, ValueError):
        page = 0
    first = page * page_size
    last = min(first + page_size, len(rows))
    out = [f"🏆 <b>Рейтинг прогнозистов</b>\n<i>% по {res.get('matches',0)} сыгранным матчам; всех строк: {len(rows)} · страница {page+1}/{total_pages}</i>\n"]
    for i, r in enumerate(rows[first:last], first + 1):
        if r.get("ai"):
            out.append(
                f"{i}. <b>{e(r['src'])}</b> — "
                f"{ai_bucket_text('новая выборка', r['ai_current'])}; "
                f"{ai_bucket_text('прежняя выборка', r['ai_legacy'])}; "
                f"{ai_bucket_text('архив', r['ai_archive'])}"
            )
            continue
        tag = " 🇷🇺" if r["reg"] == "ru" else " 🇪🇺"
        small = " · мало данных" if r["n"] < 3 else ""
        push = f" · возвратов {r['push']}" if r["push"] else ""
        pct = f"{r['pct']}%" if r["n"] else "—"
        sample = f"{r['n']} ставок" if r["n"] else "нет рассчитанных ставок"
        out.append(f"{i}. <b>{e(r['src'])}</b>{tag} — <b>{pct}</b> ({r['win']}–{r['lose']} · {sample}{push}{small})")
    out.append("\n<i>18+. Исторические результаты не гарантируют будущие.</i>")
    return "\n".join(out), rating_kb(page, total_pages)


# ---------- экспресс ----------
def mkt(pick):
    p = " " + str(pick).lower().strip() + " "
    if "угл" in p: return "Угловые"
    if re.search(r"удал|красн|карточ|желт|фол", p): return "Карточки"
    if "обе заб" in p or " оз " in p: return "Обе забьют"
    if "тотал больш" in p or re.search(r" тб[ 0-9(]", p) or "больше" in p: return "Тотал больше"
    if "тотал мен" in p or re.search(r" тм[ 0-9(]", p) or "меньше" in p: return "Тотал меньше"
    if "фора" in p or " ф1" in p or " ф2" in p or re.search(r"\bф[12]\s*\(", p): return "Фора"
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
    """Нормализует ставки, поддерживаемые расчётом результата матча."""
    raw = str(pick or "").strip().lower().replace("ё", "е").replace("−", "-").replace("–", "-")
    norm = re.sub(r"\s+", " ", raw.replace("х", "x").replace(",", "."))
    if not norm:
        return None

    if re.search(r"(?<!\d)1\s*x(?!\d)|двойн.*1\s*x|1\s*х", raw) or ("не проигра" in raw and ("хозя" in raw or "дома" in raw)):
        return "dc_1x"
    if re.search(r"(?<!\d)x\s*2(?!\d)|двойн.*x\s*2|х\s*2", norm) or ("не проигра" in raw and ("гост" in raw or "выезд" in raw)):
        return "dc_x2"
    if re.search(r"(?<!\d)1\s*2(?!\d)", norm) or "двойн шанс 12" in norm:
        return "dc_12"

    if "обе заб" in raw or "оба заб" in raw or re.search(r"(?<![а-я])оз(?![а-я])", raw):
        return "btts_no" if re.search(r"\b(нет|no)\b|не забьют|оз нет", raw) else "btts_yes"

    total_side = None
    if re.search(r"\b(тб|больше|over)\b", norm) or "тотал больше" in norm:
        total_side = "over"
    elif re.search(r"\b(тм|меньше|under)\b", norm) or "тотал меньше" in norm:
        total_side = "under"
    if total_side:
        match_line = re.search(r"(?:тб|тм|больше|меньше|over|under|тотал(?:\s+голов?)?\s+(?:больше|меньше))\s*([0-9]+(?:\.[0-9]+)?)", norm)
        if not match_line:
            return None
        return f"total_{total_side}_{match_line.group(1)}"

    side = None
    if re.search(r"(?:фора\s*(?:1|хозяев|дома)|ф\s*1|ф1)", raw):
        side = 1
    elif re.search(r"(?:фора\s*(?:2|гостей|в гостях)|ф\s*2|ф2)", raw):
        side = 2
    if side:
        handicap = re.search(r"(?:фора\s*(?:1|2|хозяев|гостей|дома|в гостях)|ф\s*[12]|ф[12])\s*\(?\s*([+-]?[0-9]+(?:\.[0-9]+)?)", norm)
        if not handicap:
            return None
        return f"handicap_{side}_{handicap.group(1)}"

    if raw in ("1", "п1") or re.search(r"\bп1\b", norm) or "победа хозяев" in raw or (match and match.get("home") and "победа" in raw and str(match["home"]).lower() in raw):
        return "result_1"
    if raw in ("2", "п2") or re.search(r"\bп2\b", norm) or "победа гостей" in raw or (match and match.get("away") and "победа" in raw and str(match["away"]).lower() in raw):
        return "result_2"
    if "ничья" in raw or "draw" in raw or re.fullmatch(r"[xх]", raw):
        return "result_x"
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
    """Подбирает подтверждённые ставки разных рынков из 3–5 матчей; купон не ниже 2.00."""
    if not data:
        return []
    today = today or data.get("today") or datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=3))
    ).date().isoformat()
    candidates = []
    for tournament, _, match in all_matches(data):
        if not _upcoming_today(match, today, res):
            continue
        options = {}

        def add_option(pick, price, estimated=False, support=0, ai_confidence=0):
            key = _pick_key(pick, match)
            if not key or not price or estimated:
                return
            item = options.get(key)
            if item is None:
                item = {
                    "m": match, "pick": str(pick), "odd": price, "estimated": False,
                    "mk": mkt(pick), "tour": tournament.get("short") or tournament.get("name", ""),
                    "support": 0, "ai_confidence": 0, "probability": 1 / price,
                }
                options[key] = item
            item["support"] += support
            item["ai_confidence"] = max(item["ai_confidence"], ai_confidence)
            if price > item["odd"]:
                item["odd"] = price
                if ai_confidence:
                    item["pick"] = str(pick)
            item["probability"] = 1 / item["odd"]

        for tip in (match.get("tips") or []):
            pick = str(tip.get("pick") or "").strip()
            price = _valid_decimal(tip.get("k"))
            if pick and price:
                add_option(pick, price, support=1)

        # Общие котировки 1X2 используются только внутри подбора;
        # в карточке матча список букмекеров и их таблица не показываются.
        raw_odds = match.get("odds") or []
        for index, pick in enumerate(("П1", "Х", "П2")):
            price = _valid_decimal(raw_odds[index] if index < len(raw_odds) else None)
            if price:
                add_option(pick, price)

        for opinion in (match.get("ai") or {}, match.get("gpt") or {}):
            if not ai_bet_eligible(opinion):
                continue
            pick = str(opinion.get("bet") or opinion.get("pick") or "").strip()
            price, estimated = _price_for_pick(match, pick)
            try:
                confidence = int(ai_probability(opinion) or 0)
            except (TypeError, ValueError):
                confidence = 0
            if pick and price and confidence >= AI_MIN_PROBABILITY:
                add_option(pick, price, estimated=estimated, ai_confidence=confidence)

        if options:
            # На матч оставляем один рынок: приоритет подтверждённому прогнозу ИИ,
            # затем консенсусу источников и более вероятному исходу.
            chosen = sorted(
                options.values(),
                key=lambda item: (-item["ai_confidence"], -item["support"], -item["probability"]),
            )[0]
            candidates.append(chosen)

    candidates.sort(key=lambda item: (
        -item["ai_confidence"],
        -item["support"],
        -item["probability"],
        item["m"].get("time") or "99:99",
    ))
    pool = candidates[:30]
    try:
        min_events = max(3, min(5, int(n)))
    except (TypeError, ValueError):
        min_events = 3
    for count in range(min_events, min(5, len(pool)) + 1):
        best = None
        best_key = None
        for combo in itertools.combinations(pool, count):
            total = 1.0
            for item in combo:
                total *= item["odd"]
            if total < 2.0:
                continue
            estimated_count = sum(bool(item.get("estimated")) for item in combo)
            confidence = sum(item["ai_confidence"] for item in combo)
            key = (
                estimated_count,
                total,
                -confidence,
                tuple(item["m"].get("time") or "99:99" for item in combo),
            )
            if best_key is None or key < best_key:
                best, best_key = combo, key
        if best:
            return sorted(best, key=lambda item: item["m"].get("time") or "99:99")
    return []


def _format_pick_price(item):
    marker = "≈" if item.get("estimated") else ""
    return f"{marker}{item['odd']:.2f}"


def screen_express(data, res=None, today=None):
    sel = express(data, 3, res=res, today=today)
    if len(sel) < 3:
        return (
            "⚡ <b>Экспресс дня</b>\n"
            "Пока не нашлось 3–5 предстоящих матчей с коэффициентами, "
            "которые дают общий кф не ниже 2.00. Неподтверждённый экспресс не публикуем."
        )

    total = 1.0
    out = [
        f"⚡ <b>Экспресс дня — {len(sel)} событий</b>",
        "<i>Подобраны рынки с подтверждёнными коэффициентами: исходы, тоталы, форы и другие доступные ставки.</i>",
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
    out.append("<i>Минимальный общий коэффициент 2.00; максимум 5 событий. "
               "Экспресс рискованнее одиночной ставки: должны пройти все выборы. 18+.</i>")
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
        btn = (MenuButtonWebApp(text="Приложение", web_app=WebAppInfo(url=APP_URL))
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
    await db_touch_user(msg.from_user)
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
    kb.button(text="🟢 Открыть приложение", web_app=WebAppInfo(url=APP_URL))
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

async def _status(request):
    data, _ = await get_data()
    st = await db_status()
    return web.json_response({
        "source": _cache.get("source"),   # "db" = читаем из базы, "site" = запасной вариант
        "has_data": bool(data),
        "db": st,
    }, headers=CORS)

async def _app(request):
    """Страница Mini App: отдаём из базы; если там нет — на время перехода проксируем с сайта."""
    page = await db_get_text("page")
    if not page:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(BASE + "index.html", timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        page = await r.text()
        except Exception as ex:
            print("app proxy error:", ex)
    if not page:
        return web.Response(text="Приложение временно недоступно, попробуйте позже.", status=503)
    return web.Response(text=page, content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-store"})

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
    app.router.add_get("/status", _status)
    app.router.add_get("/app", _app)
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
    """Берёт AI-ставки с текущим внутренним порогом и котировкой выбранного рынка."""
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


def _daily_predictions_block(data, res, today, express_legs=None):
    candidates = _daily_model_candidates(data, res, today)
    out = []
    if candidates:
        best = candidates[0]
        meta = []
        if best["score"]:
            meta.append(f"счёт ИИ {e(best['score'])}")
        if best["time"]:
            meta.append(e(best["time"]))
        suffix = " · " + " · ".join(meta) if meta else ""
        price = ("≈" if best["coefficient_estimated"] else "") + f"{best['coefficient']:.2f}"
        out.extend([
            "🎯 <b>Лучший прогноз дня</b>",
            f"<b>{e(best['home'])} — {e(best['away'])}</b> · {e(best['tournament'])}",
            f"Выбор ИИ: <b>{e(best['bet'])}</b> — кф <b>{price}</b>{suffix}",
        ])
    else:
        out.extend([
            "🎯 <b>Прогноз дня</b>",
            "Пока нет прогноза на предстоящий матч с проверяемым коэффициентом.",
        ])

    legs = express_legs if express_legs is not None else express(data, 3, res=res, today=today)
    if len(legs) >= 3:
        total = 1.0
        out.append(f"\n⚡ <b>Экспресс дня — {len(legs)} события</b>")
        for index, item in enumerate(legs, 1):
            total *= item["odd"]
            price = _format_pick_price(item)
            out.append(
                f"{index}. <b>{e(item['m']['home'])} — {e(item['m']['away'])}</b> "
                f"({e(item['tour'])}, {e(item['m'].get('time') or 'время уточняется')})\n"
                f"   {e(item['pick'])} — кф <b>{price}</b>"
            )
        marker = "≈" if any(item.get("estimated") for item in legs) else ""
        out.append(f"<b>Общий коэффициент: {marker}{total:.2f}</b>")
        if any(item.get("estimated") for item in legs):
            out.append("<i>≈ — расчётный коэффициент двойного шанса по котировкам 1X2, "
                       "а не прямая котировка букмекера.</i>")
    else:
        out.append("\n⚡ <b>Экспресс дня</b>")
        out.append("Нет подходящей комбинации из 3–5 событий с общим коэффициентом от 2.00; "
                   "непроверенные цены не публикуем.")
    out.append("<i>Экспресс рискованнее одиночной ставки: должны пройти все выбранные события. 18+</i>")
    return "\n".join(out)


def _daily_rating_block(res):
    """Ежедневный пост показывает топ-25 источников; статистика ИИ идёт отдельно."""
    if not res:
        return "🏆 <b>Рейтинг прогнозистов</b>\nСтатистика пока недоступна."

    sources = []
    for name, value in (res.get("sources") or {}).items():
        try:
            wins = int(value.get("win", 0) or 0)
            losses = int(value.get("lose", 0) or 0)
            pushes = int(value.get("push", 0) or 0)
        except (TypeError, ValueError):
            continue
        total = wins + losses
        sources.append({
            "name": name,
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "total": total,
            "pct": round(wins / total * 100) if total else None,
            "wilson": wilson(wins, total),
        })

    sources.sort(key=lambda row: (-row["wilson"], -row["total"], -(row["pct"] or 0)))
    ranked_sources = [row for row in sources if row["total"] > 0]
    total_ranked = len(ranked_sources)
    sources = ranked_sources[:25]
    out = ["🏆 <b>Топ-25 прогнозистов</b>"]
    done = res.get("done")
    if isinstance(done, list):
        out.append(
            f"<i>По {len(done)} матчам с результатом · показано {len(sources)} из {total_ranked}</i>"
        )

    if sources:
        for index, row in enumerate(sources, 1):
            if row["total"]:
                details = f"({row['wins']}–{row['losses']}, {row['total']} ставок"
                if row["pushes"]:
                    details += f", возвратов {row['pushes']}"
                details += ")"
                pct = f"{row['pct']}%"
                note = " · мало данных" if row["total"] < 3 else ""
            else:
                details = "(нет рассчитанных ставок)"
                pct = "—"
                note = ""
            out.append(f"{index}. <b>{e(row['name'])}</b> — {pct} {details}{note}")
    else:
        out.append("Прогнозов с рассчитанным исходом пока нет.")

    ai_rows = (
        ("gpt70", "gpt60", "gpt75", "🧠 Локальная модель"),
        ("ai70", "ai60", "ai75", "🤖 Claude"),
    )
    out.append("\n<b>ИИ — отдельная статистика</b>")
    for current_key, legacy_key, archive_key, label in ai_rows:
        current = ai_bucket(res, current_key)
        legacy = ai_bucket(res, legacy_key)
        archive = ai_bucket(res, archive_key)
        out.append(
            f"• <b>{e(label)}</b> — "
            f"{ai_bucket_text('новая выборка', current)}; "
            f"{ai_bucket_text('прежняя выборка', legacy)}; "
            f"{ai_bucket_text('архив', archive)}"
        )


    return "\n".join(out)


def _daily_ai_results_block(res, today):
    """Поматчевый разбор: какие ставки ИИ зашли/не зашли во вчерашний (или последний сыгранный) день."""
    recent = (res or {}).get("recent") or []
    if not recent:
        return ""

    def picks_for(date):
        return [
            r for r in recent
            if r.get("date") == date
            and (r.get("gpt_bet") or "").strip()
            and (r.get("gpt70") or r.get("gpt60")) in ("win", "lose", "push")
        ]

    try:
        yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    except (TypeError, ValueError):
        return ""
    day = yesterday
    picks = picks_for(yesterday)
    if not picks:
        past = sorted(
            {r.get("date") for r in recent if r.get("date") and r.get("date") < today},
            reverse=True,
        )
        for d in past:
            p = picks_for(d)
            if p:
                day, picks = d, p
                break
    if not picks:
        return ""

    result_of = lambda row: row.get("gpt70") or row.get("gpt60")
    win = sum(1 for r in picks if result_of(r) == "win")
    total = len(picks)
    out = [f"📋 <b>Итоги ставок ИИ за {e(dlabel(day))}:</b>"]
    for r in picks[:8]:
        result = result_of(r)
        icon = "✅" if result == "win" else ("↩️" if result == "push" else "❌")
        out.append(f"{icon} {e(r.get('home'))} {e(r.get('score', ''))} {e(r.get('away'))} · {e(r.get('gpt_bet'))}")
    if total > 8:
        out.append(f"<i>…и ещё {total - 8}</i>")
    out.append(f"<b>Зашло {win} из {total}</b>")
    return "\n".join(out)


EXPRESS_HISTORY_FILE = os.environ.get(
    "EXPRESS_HISTORY_FILE",
    os.path.join(os.path.dirname(__file__), ".express_history.json"),
)


def _load_express_history():
    try:
        with open(EXPRESS_HISTORY_FILE, encoding="utf-8") as f:
            value = json.load(f)
        if isinstance(value, dict) and isinstance(value.get("days"), list):
            return value
    except (OSError, json.JSONDecodeError):
        pass
    return {"days": []}


def _norm_team_name(value):
    return re.sub(r"[^a-zа-яё0-9]", "", str(value or "").lower().replace("ё", "е"))


def _express_settlement(record, res):
    legs = record.get("legs") or []
    recent = (res or {}).get("recent") or []
    by_key = {
        (str(item.get("date") or ""), _norm_team_name(item.get("home")),
         _norm_team_name(item.get("away"))): item
        for item in recent
    }
    settled = []
    for leg in legs:
        key = (str(leg.get("date") or record.get("date") or ""),
               _norm_team_name(leg.get("home")), _norm_team_name(leg.get("away")))
        result = by_key.get(key)
        score = str((result or {}).get("score") or (result or {}).get("ft") or "")
        score_match = re.match(r"^\s*(\d+)\s*[:\-]\s*(\d+)", score)
        if not score_match:
            settled.append({"leg": leg, "status": "pending", "score": ""})
            continue
        home_goals, away_goals = int(score_match.group(1)), int(score_match.group(2))
        market = _pick_key(leg.get("pick"), {"home": leg.get("home"), "away": leg.get("away")})
        total_goals = home_goals + away_goals
        if market == "dc_1x":
            status = "win" if home_goals >= away_goals else "lose"
        elif market == "dc_x2":
            status = "win" if away_goals >= home_goals else "lose"
        elif market == "dc_12":
            status = "win" if home_goals != away_goals else "lose"
        elif market == "result_1":
            status = "win" if home_goals > away_goals else "lose"
        elif market == "result_x":
            status = "win" if home_goals == away_goals else "lose"
        elif market == "result_2":
            status = "win" if away_goals > home_goals else "lose"
        elif market and market.startswith(("total_over_", "total_under_")):
            side, _, raw_line = market.split("_", 2)
            line = float(raw_line)
            value = total_goals - line
            status = "push" if abs(value) < 1e-9 else ("win" if (value > 0) == (side == "over") else "lose")
        elif market and market.startswith("handicap_"):
            _, side, raw_line = market.split("_", 2)
            line = float(raw_line)
            margin = home_goals + line - away_goals if side == "1" else away_goals + line - home_goals
            status = "push" if abs(margin) < 1e-9 else ("win" if margin > 0 else "lose")
        elif market == "btts_yes":
            status = "win" if home_goals > 0 and away_goals > 0 else "lose"
        elif market == "btts_no":
            status = "win" if home_goals == 0 or away_goals == 0 else "lose"
        else:
            status = "pending"
        settled.append({
            "leg": leg,
            "status": status,
            "score": f"{home_goals}:{away_goals}",
        })
    if not legs:
        overall = "not_posted"
    elif any(item["status"] == "lose" for item in settled):
        overall = "lose"
    elif settled and all(item["status"] in ("win", "push") for item in settled):
        overall = "push" if all(item["status"] == "push" for item in settled) else "win"
    else:
        overall = "pending"
    record["settlement"] = overall
    record["settled_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    return settled, overall


def _daily_express_result_block(history, res, today):
    days = history.get("days") or []
    yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    record = next((item for item in days if item.get("date") == yesterday), None)
    title_date = yesterday
    if record is None:
        older = sorted(
            (item for item in days if item.get("date", "") < today and item.get("legs")),
            key=lambda item: item.get("date", ""),
            reverse=True,
        )
        record = next((item for item in older if item.get("settlement") == "pending"), None)
        if record is None:
            return "📊 <b>Результат вчерашнего экспресса</b>\nСохранённого купона за вчера нет; начинаем вести историю с сегодняшнего поста."
        title_date = record.get("date") or yesterday

    if not record.get("legs"):
        return (
            f"📊 <b>Экспресс за {e(dlabel(title_date))}</b>\n"
            "Не публиковался: не нашлось подходящей комбинации из 3–5 событий с общим коэффициентом от 2.00."
        )

    settled, overall = _express_settlement(record, res)
    total = float(record.get("total") or 1)
    approx = "≈" if record.get("estimated") else ""
    out = [f"📊 <b>Результат экспресса за {e(dlabel(title_date))}</b>"]
    for item in settled:
        leg = item["leg"]
        icon = "✅" if item["status"] == "win" else ("❌" if item["status"] == "lose" else ("↩️" if item["status"] == "push" else "⏳"))
        score = f" · {e(item['score'])}" if item["score"] else " · результат ожидается"
        out.append(
            f"{icon} {e(leg.get('home'))} — {e(leg.get('away'))}{score}; "
            f"{e(leg.get('pick'))} · кф {('≈' if leg.get('estimated') else '')}{float(leg.get('odd') or 0):.2f}"
        )
    if overall == "win":
        effective_total = math.prod(float(item["leg"].get("odd") or 1) for item in settled if item["status"] == "win")
        payout = round(1000 * effective_total)
        out.append(f"<b>Экспресс прошёл · расчётный кф {effective_total:.2f} · при ставке 1 000 ₽ выплата ≈{payout:,} ₽, чистый результат +{payout - 1000:,} ₽.</b>".replace(",", " "))
    elif overall == "push":
        out.append("<b>Возврат по экспрессу: все события рассчитаны как возврат.</b>")
    elif overall == "lose":
        out.append("<b>Экспресс не прошёл · при ставке 1 000 ₽ выплата 0 ₽, результат −1 000 ₽.</b>")
    else:
        completed = sum(item["status"] in ("win", "lose", "push") for item in settled)
        out.append(f"<b>Пока рассчитано {completed} из {len(settled)} событий; полный итог появится после завершения всех матчей.</b>")
    return "\n".join(out)


def _split_telegram_text(text, limit=3800):
    lines = str(text).splitlines()
    chunks, current = [], ""
    for line in lines:
        addition = line if not current else "\n" + line
        if current and len(current) + len(addition) > limit:
            chunks.append(current)
            current = line
        else:
            current += addition
    if current:
        chunks.append(current)
    return chunks or [str(text)]


def _save_express_history(history, today, legs):
    days = [item for item in history.get("days", []) if item.get("date") != today]
    record_legs = []
    total = 1.0
    estimated = False
    for item in legs:
        total *= float(item["odd"])
        estimated = estimated or bool(item.get("estimated"))
        match = item["m"]
        record_legs.append({
            "date": today,
            "home": str(match.get("home") or ""),
            "away": str(match.get("away") or ""),
            "pick": str(item.get("pick") or ""),
            "odd": float(item["odd"]),
            "estimated": bool(item.get("estimated")),
            "tour": str(item.get("tour") or ""),
            "time": str(match.get("time") or ""),
        })
    days.append({
        "date": today,
        "legs": record_legs,
        "total": round(total, 4) if record_legs else None,
        "estimated": estimated,
        "posted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "settlement": "pending" if record_legs else "not_posted",
    })
    history["days"] = sorted(days, key=lambda item: item.get("date", ""))[-30:]
    directory = os.path.dirname(EXPRESS_HISTORY_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = EXPRESS_HISTORY_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, EXPRESS_HISTORY_FILE)


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
    express_legs = express(data, 3, res=res, today=today)
    history = _load_express_history()
    previous_result = _daily_express_result_block(history, res, today)

    parts = [
        "🔥 <b>Капер ИИ</b> — прогнозы на сегодня",
        f"⚽ Матчей сегодня: <b>{len(rows)}</b> · {e(dlabel(today))}",
        f"🔄 Данные обновлены: {e(_format_daily_timestamp(data.get('updated')) or 'время не указано')}",
        "",
        _daily_predictions_block(data, res, today, express_legs),
        "",
    ] + ([_daily_ai_results_block(res, today), ""] if _daily_ai_results_block(res, today) else []) + [
        previous_result,
        "",
        _daily_rating_block(res),
    ]
    if not rows:
        parts.insert(2, "На сегодня матчей нет — опубликовали актуальный рейтинг.")
    parts.append("\nВсе матчи и подробные прогнозы — в приложении 👇\n<i>18+. Аналитика, не гарантия.</i>")
    kb = InlineKeyboardBuilder()
    kb.button(text="⚽ Открыть в боте", url=bot_link())
    chunks = _split_telegram_text("\n".join(parts))
    for index, chunk in enumerate(chunks):
        kwargs = {"disable_web_page_preview": True}
        if index == len(chunks) - 1:
            kwargs["reply_markup"] = kb.as_markup()
        await bot.send_message(REQUIRED_CHAT, chunk, **kwargs)

    _save_express_history(history, today, express_legs)
    print(f"Ежедневный пост отправлен: {today}, матчей {len(rows)}, экспрессов {len(express_legs)} событий.")
    return True


POST_TIMES = [(12, 0)]  # ежедневный пост в канал в 12:00 МСК
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
    await db_init()
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
                menu_button=MenuButtonWebApp(text="Приложение", web_app=WebAppInfo(url=APP_URL)))
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
