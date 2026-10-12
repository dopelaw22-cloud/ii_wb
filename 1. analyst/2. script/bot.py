#!/usr/bin/env python3
"""Telegram-бот агента-аналитика: отчёты в чат и обратная связь владельцу.

Что умеет:
  — команды отвечают мгновенно из кеша в БД, без обращений к WB API;
  — свободный вопрос текстом уходит Claude: бот запускает headless-сессию
    в каталоге проекта и присылает ответ в тот же чат;
  — любое сообщение владельца сохраняется в wb.tg_message, так что переписка
    не теряется между запусками.

Доступ: первый, кто напишет /start, становится владельцем (chat_id пишется
в wb.setting). Остальные получают отказ — бот не отвечает посторонним.

Запуск вручную:  python bot.py
Как служба:      systemctl --user start wb-analyst-bot   (см. 5. service/)
"""
import sys, os, pathlib, time, json, subprocess, html, traceback
import requests
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L
import views as V

API = "https://api.telegram.org/bot%s" % L.ENV.get("WB_TG_BOT_TOKEN", "")
POLL_TIMEOUT = 50
CLAUDE_TIMEOUT = 420
MAX_MSG = 3900          # лимит Telegram — 4096, оставляем запас на разметку

HELP = """Я аналитик вашего магазина на Wildberries.

Команды:
/сводка — главные цифры за 30 дней
/прибыль — юнит-экономика по каждому товару
/месяцы — прибыль по месяцам, «/месяцы 2026-09» — расшифровка
/реклама — кампании: затраты, ДРР, CTR, CPC
/воронка — переходы, корзины, заказы, выкуп
/карточки — фото, характеристики, описания
/остатки — склад и на сколько дней хватит
/советы — рекомендации по улучшению
/отчет — полный отчёт файлом
/данные — что когда обновлялось
/лимиты — когда WB снова отдаст данные
/обновить — собрать свежие данные с WB
/налог — налоговая схема, смена: /налог усн 1 взносы 1 ндс 5

Или просто напишите вопрос своими словами — я разберусь в данных и отвечу.
Например: «почему у d125black маржа ниже, чем у d125white?»"""


# ---------------------------------------------------------------- Telegram

def tg(method, **kw):
    try:
        r = requests.post("%s/%s" % (API, method), json=kw, timeout=POLL_TIMEOUT + 15)
        return r.json()
    except Exception as e:
        print("tg %s: %s" % (method, e), file=sys.stderr)
        return {}


def send(chat_id, text, mono=True):
    """Длинные таблицы режем по строкам, чтобы не ломать вид."""
    chunks, cur = [], ""
    for line in str(text).split("\n"):
        if len(cur) + len(line) + 1 > MAX_MSG:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for ch in chunks or [""]:
        if mono:
            tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
               text="<pre>%s</pre>" % html.escape(ch))
        else:
            tg("sendMessage", chat_id=chat_id, text=ch)
        time.sleep(0.2)


def send_file(chat_id, path, caption=None):
    try:
        with open(path, "rb") as f:
            requests.post("%s/sendDocument" % API, data={"chat_id": chat_id,
                          "caption": (caption or "")[:1000]},
                          files={"document": (os.path.basename(path), f)}, timeout=120)
    except Exception as e:
        send(chat_id, "файл не ушёл: %s" % e, mono=False)


# ---------------------------------------------------------------- владелец

def owner():
    v = L.setting("owner_chat_id")
    return int(v) if v else None


def bind_owner(chat_id, name):
    L.set_setting("owner_chat_id", chat_id, "владелец магазина, привязан по первому /start")
    L.event("telegram: владелец привязан (%s)" % name)


# ---------------------------------------------------------------- мост к Claude

BRIDGE_SETTING = "claude_bridge"      # "on" / "off", по умолчанию включён


def bridge_enabled():
    return (L.setting(BRIDGE_SETTING, "on") or "on").lower() != "off"


def ask_claude(question, chat_id):
    """Запускаем headless-сессию Claude в каталоге проекта.

    Права урезаны до чтения: разрешены только psql-запросы и чтение файлов,
    а --permission-mode dontAsk отклоняет всё остальное вместо того, чтобы
    спрашивать подтверждение (в фоновой службе спросить всё равно некого).
    Запись в WB и изменение файлов таким образом недоступны.

    Выключить мост целиком:
        UPDATE wb.setting SET value='off' WHERE key='claude_bridge';
    """
    if not bridge_enabled():
        return ("Свободные вопросы отключены. Доступны команды — /help.\n"
                "Включить: UPDATE wb.setting SET value='on' WHERE key='claude_bridge';")
    prompt = (
        "Ты агент-аналитик магазина WB «Вытяжки скрытого монтажа» (ИП Пермин Е. В.).\n"
        "Владелец спрашивает в Telegram. Ответь по-русски, коротко и по делу, "
        "обычным текстом без markdown-разметки, не длиннее 2500 знаков.\n"
        "Данные бери SQL-запросами к БД wb_ops, схема wb: product, cost, price, sale, "
        "order_row, finance_row, funnel, advert, advert_stat, stock, unit_econ, "
        "recommendation, tariff, setting.\n"
        "Команда для запросов: psql -d wb_ops -c \"<SQL>\"\n"
        "Если нужных данных в БД нет — так и скажи, какой сборщик надо запустить, "
        "и не придумывай цифры.\n\n"
        "Вопрос владельца: %s" % question
    )
    cmd = ["claude", "-p", prompt,
           "--permission-mode", "dontAsk",
           "--allowedTools", "Bash(psql*)", "Read", "Grep", "Glob",
           "--disallowedTools", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"]
    try:
        r = subprocess.run(cmd, cwd=str(L.ROOT), capture_output=True, text=True,
                           timeout=CLAUDE_TIMEOUT)
        out = (r.stdout or "").strip()
        if not out:
            err = (r.stderr or "").strip()[:500]
            return "Не удалось получить ответ.%s" % (("\n\n" + err) if err else "")
        return out
    except subprocess.TimeoutExpired:
        return "Слишком долго думаю — вопрос оказался тяжёлым. Попробуйте спросить уже."
    except FileNotFoundError:
        return "На сервере нет Claude CLI — свободные вопросы недоступны, команды работают."
    except Exception as e:
        return "Ошибка моста: %s" % str(e)[:300]


# ---------------------------------------------------------------- сбор данных

def do_sync(chat_id):
    """Собрать данные единым скриптом sync.py — та же логика, что по расписанию."""
    here = pathlib.Path(__file__).resolve().parent
    send(chat_id, "Собираю данные. Методы под лимитом WB пропущу — в них останутся "
                  "прошлые значения.", mono=False)
    try:
        r = subprocess.run(["/home/wb/oplog/venv/bin/python", str(here / "sync.py")],
                           cwd=str(here), capture_output=True, text=True, timeout=1800)
        out = (r.stdout or r.stderr or "").strip()
        send(chat_id, out[-3500:] if out else "sync.py ничего не вывел")
    except subprocess.TimeoutExpired:
        send(chat_id, "Сбор не уложился в 30 минут — прервал.", mono=False)
    except Exception as e:
        send(chat_id, "Ошибка сбора: %s" % str(e)[:300], mono=False)
    send(chat_id, V.summary())


# ---------------------------------------------------------------- налоги

TAX_HELP = """Сейчас: %s

Сменить составную схему (можно любые из трёх):
  /налог усн 1 взносы 1 ндс 5

Простой режим одной ставкой:
  /налог 6     — УСН 6%% с выручки
  /налог 15    — УСН 15%% с прибыли
  /налог 0     — без налога

НДС считается внутри цены: при ставке 5%% к уплате идёт цена x 5/105,
а база УСН — доход без НДС."""

TAX_WORDS = {
    "усн": "tax_usn_pct", "usn": "tax_usn_pct",
    "взносы": "tax_insurance_pct", "взнос": "tax_insurance_pct",
    "страховые": "tax_insurance_pct", "insurance": "tax_insurance_pct",
    "ндс": "tax_vat_pct", "nds": "tax_vat_pct", "vat": "tax_vat_pct",
}


def set_tax(arg):
    """Разбирает «усн 1 взносы 1 ндс 5» или одиночную ставку. True, если изменили."""
    a = (arg or "").strip().lower().replace("%", "").replace(",", ".")
    if not a:
        return False

    if a.replace(".", "", 1).isdigit():
        rate = float(a)
        mode = "none" if rate == 0 else ("usn6" if rate <= 6 else "usn15")
        L.set_setting("tax_mode", mode, "подтверждено владельцем в Telegram")
        L.set_setting("tax_rate", rate, "подтверждено владельцем в Telegram")
        for k in ("tax_usn_pct", "tax_insurance_pct", "tax_vat_pct"):
            L.set_setting(k, 0, "сброшено простым режимом")
        return True

    tokens = a.split()
    found = {}
    i = 0
    while i < len(tokens) - 1:
        key = TAX_WORDS.get(tokens[i])
        val = tokens[i + 1].replace(".", "", 1)
        if key and val.isdigit():
            found[key] = float(tokens[i + 1])
            i += 2
        else:
            i += 1
    if not found:
        return False

    L.set_setting("tax_mode", "composite", "подтверждено владельцем в Telegram")
    for k in ("tax_usn_pct", "tax_insurance_pct", "tax_vat_pct"):
        L.set_setting(k, found.get(k, 0), "подтверждено владельцем в Telegram")
    L.set_setting("tax_vat_in_price", "yes", "НДС внутри розничной цены")
    return True


# ---------------------------------------------------------------- команды

def norm(cmd):
    c = cmd.lower().lstrip("/").split("@")[0].strip()
    alias = {
        "start": "start", "help": "help", "помощь": "help",
        "сводка": "summary", "summary": "summary",
        "прибыль": "profit", "profit": "profit", "экономика": "profit",
        "месяцы": "months", "months": "months", "месяц": "months",
        "реклама": "ads", "ads": "ads",
        "воронка": "funnel", "funnel": "funnel",
        "карточки": "cards", "cards": "cards",
        "остатки": "stock", "stock": "stock",
        "советы": "recs", "recs": "recs", "рекомендации": "recs",
        "отчет": "report", "отчёт": "report", "report": "report",
        "данные": "fresh", "fresh": "fresh",
        "лимиты": "limits", "limits": "limits",
        "обновить": "sync", "sync": "sync",
        "налог": "tax", "tax": "tax",
    }
    return alias.get(c)


def handle(chat_id, text, name):
    raw = (text or "").strip()
    if not raw:
        return

    if raw.startswith("/"):
        parts = raw.split()
        cmd = norm(parts[0])
        arg = " ".join(parts[1:]).strip()

        if cmd in ("start", "help"):
            send(chat_id, HELP, mono=False)
            return
        if cmd == "summary":
            send(chat_id, V.summary()); return
        if cmd == "profit":
            send(chat_id, V.profit_table()); return
        if cmd == "months":
            # «/месяцы 2026-09» — расшифровка конкретного месяца
            send(chat_id, V.months_block(detail_month=arg or None)); return
        if cmd == "ads":
            send(chat_id, V.ads_block()); return
        if cmd == "funnel":
            send(chat_id, V.funnel_block()); return
        if cmd == "cards":
            send(chat_id, V.cards_block()); return
        if cmd == "stock":
            send(chat_id, V.stock_block()); return
        if cmd == "recs":
            send(chat_id, V.recs_block()); return
        if cmd == "fresh":
            send(chat_id, "ОБНОВЛЕНИЕ ДАННЫХ\n\n" + V.freshness()); return
        if cmd == "limits":
            send(chat_id, V.limits_block()); return
        if cmd == "sync":
            do_sync(chat_id); return
        if cmd == "tax":
            changed = set_tax(arg)
            if changed:
                if L.db_log:
                    L.db_log.owner_decision(kind="instruction", domain="taxes",
                                            subject="налоговый режим",
                                            decision=V.tax_note(), source="telegram")
                send(chat_id, "Принято: %s\n\nПересчитаю экономику при следующем "
                              "/обновить." % V.tax_note(), mono=False)
            else:
                send(chat_id, TAX_HELP % V.tax_note(), mono=False)
            return
        if cmd == "report":
            here = pathlib.Path(__file__).resolve().parent
            send(chat_id, "Собираю отчёт…", mono=False)
            try:
                r = subprocess.run(["/home/wb/oplog/venv/bin/python",
                                    str(here / "report.py"), "--quiet"],
                                   cwd=str(here), capture_output=True, text=True, timeout=600)
                paths = [l.strip() for l in (r.stdout or "").split("\n")
                         if l.strip().endswith((".md", ".xlsx"))]
                if not paths:
                    send(chat_id, "Отчёт не собрался:\n%s"
                         % (r.stderr or r.stdout or "")[:800]); return
                for p in paths:
                    send_file(chat_id, p, caption=os.path.basename(p))
            except Exception as e:
                send(chat_id, "Ошибка отчёта: %s" % str(e)[:300], mono=False)
            return

        send(chat_id, "Не знаю такую команду. /help", mono=False)
        return

    # свободный вопрос -> Claude
    send(chat_id, "Разбираюсь…", mono=False)
    answer = ask_claude(raw, chat_id)
    send(chat_id, answer, mono=False)
    with L.db() as c, c.cursor() as cur:
        cur.execute("""UPDATE wb.tg_message SET status='answered', answer=%s, answered_at=now()
                       WHERE chat_id=%s AND text=%s AND status='new'""",
                    (answer[:8000], chat_id, raw))


# ---------------------------------------------------------------- цикл

def main():
    if not L.ENV.get("WB_TG_BOT_TOKEN"):
        sys.exit("WB_TG_BOT_TOKEN не задан в .env")
    me = tg("getMe").get("result") or {}
    print("бот @%s запущен" % me.get("username"))
    L.event("telegram: бот запущен (@%s)" % me.get("username"))

    offset = int(L.setting("tg_offset", "0") or 0)
    while True:
        try:
            upd = tg("getUpdates", offset=offset, timeout=POLL_TIMEOUT,
                     allowed_updates=["message"])
            for u in (upd.get("result") or []):
                offset = max(offset, u["update_id"] + 1)
                L.set_setting("tg_offset", offset, "курсор Telegram getUpdates")
                msg = u.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                text = msg.get("text") or ""
                frm = msg.get("from") or {}
                name = (frm.get("first_name") or "") + " @" + (frm.get("username") or "")
                if not chat_id:
                    continue

                own = owner()
                if own is None:
                    if text.strip().lower().startswith("/start"):
                        bind_owner(chat_id, name)
                        own = chat_id
                        send(chat_id, "Готово, вы владелец. Дальше — по командам.", mono=False)
                    else:
                        send(chat_id, "Напишите /start, чтобы привязать бота.", mono=False)
                        continue
                if chat_id != own:
                    send(chat_id, "Бот приватный.", mono=False)
                    L.event("telegram: сообщение от посторонних (%s)" % name, severity="warning")
                    continue

                with L.db() as c, c.cursor() as cur:
                    cur.execute("""INSERT INTO wb.tg_message (tg_update_id, chat_id, from_name, text)
                                   VALUES (%s,%s,%s,%s) ON CONFLICT (tg_update_id) DO NOTHING""",
                                (u["update_id"], chat_id, name.strip(), text))
                try:
                    handle(chat_id, text, name)
                except Exception as e:
                    traceback.print_exc()
                    send(chat_id, "Сломался на этом запросе: %s" % str(e)[:300], mono=False)
        except KeyboardInterrupt:
            print("остановлен")
            return
        except Exception as e:
            print("цикл: %s" % e, file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    main()
