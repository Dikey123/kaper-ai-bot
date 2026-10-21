# Капер ИИ — Telegram-бот (Этап 1, MVP)

Бот читает данные нашего сайта с GitHub Pages
(`https://dikey123.github.io/football-prognozy/`) и показывает в Telegram:

- `/start` — меню
- `/today` — матчи сегодня (все турниры), по кнопке — карточка матча
- карточка матча: коэффициенты, составы, новости, мнение ИИ
- `/rating` — рейтинг прогнозистов (% проходимости) + строка ИИ
- `/express` — экспресс дня (самые вероятные события разных рынков)

Данные обновляются сами (нашими облачными задачами), бот всегда показывает свежее.

## Безопасность токена
Токен **не хранится в коде**. Он берётся из переменной окружения `BOT_TOKEN`.
Никогда не коммить токен в репозиторий и не вставляй его в переписку.
Если токен засветился — отзови его в @BotFather (`/revoke`) и выпусти новый.

## Локальный запуск (для проверки)
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
set BOT_TOKEN=ТВОЙ_ТОКЕН         # Windows (cmd)
# либо: $env:BOT_TOKEN="ТВОЙ_ТОКЕН"   (PowerShell)
# либо: export BOT_TOKEN=ТВОЙ_ТОКЕН   (Linux/Mac)
python bot.py
```
Бот запустится в режиме polling. Напиши ему в Telegram `/start`.

## Деплой на хостинг (always-on), чтобы бот работал 24/7

### Вариант A — Render (бесплатно, через веб-интерфейс)
1. render.com → New → **Web Service** → подключить GitHub-репозиторий `kaper-ai-bot`.
2. Runtime: **Python**. Build Command: `pip install -r requirements.txt`.
   Start Command: `python bot.py`.
3. В разделе **Environment** добавить переменную `BOT_TOKEN` = токен бота.
4. Create Web Service.
Бот встроенно поднимает мини веб-сервер (health-check на `/`), поэтому Render
видит его как живой сервис. Бесплатный Render «засыпает» без входящих запросов —
поэтому настройте бесплатный пингер (cron-job.org / UptimeRobot), который раз в
10–14 минут открывает URL сервиса. Это держит бота всегда онлайн.

### Вариант B — Railway (когда их сборки снова работают)
New Project → Deploy from GitHub repo → добавить переменную `BOT_TOKEN` → Deploy.

### Вариант C — VPS (стабильно, ~200–400 ₽/мес)
```bash
pip install -r requirements.txt
BOT_TOKEN=ТВОЙ_ТОКЕН python bot.py
# для автозапуска — systemd-сервис или screen/tmux
```

## Что дальше (по дорожной карте)
- Этап 2: пошаговый конструктор экспресса в боте (выбор рынков/коэффициента/кол-ва).
- Этап 3: подписки и рассылки (утренняя сводка, «за час до матча», результаты).
- Этап 4: платный доступ с реальной проверкой оплаты (Telegram Stars / ЮKassa).
