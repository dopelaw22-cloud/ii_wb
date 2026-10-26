#!/usr/bin/env python3
"""Проверка выкупов WB за отчётный период и сообщение бухгалтеру в Telegram.

Задача владельца: до 5-го числа каждого месяца (и по кварталам) смотреть во
вкладке «Документы» категорию «Уведомления о выкупе» и, если документы за
период есть, получать в Telegram сообщение
«Инфа для бухгалтера: были выкупы WB на сумму Х руб.»

Откуда берётся сумма. В списке документов WB суммы нет вообще — только
название, категория и дата. Зато в отчёте реализации выкупы помечены полем
`reportType = 2` («по выкупам»), и там есть и количество, и деньги. Поэтому
сумма считается по финотчёту, который и так обновляется дважды в сутки,
а список документов используется как подтверждение: есть ли бумага за период.

Про лимиты. `/api/v1/documents/list` отдаётся **раз в сутки**, и неудачная
попытка расходует окно так же, как успешная. Поэтому: один вызов за прогон,
ответ пишется на диск до разбора, а если окно закрыто — скрипт не падает,
а сообщает сумму по финотчёту с пометкой, что документы проверить не удалось.

Запуск:
    python check_buyouts.py                  # прошлый месяц
    python check_buyouts.py --period 2026-09 # конкретный месяц
    python check_buyouts.py --quarter 2026-3 # квартал
    python check_buyouts.py --dry            # посчитать, но не отправлять
"""
import sys, pathlib, argparse, datetime, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L

DOC_CATEGORY_HINT = "выкуп"          # ищем по названию категории, а не по ID
SENT_KEY = "buyout_notice_sent"      # какие периоды уже отправлены
BUYOUT_REPORT_TYPE = "2"


# ---------------------------------------------------------------- период

def prev_month(today=None):
    today = today or datetime.date.today()
    first = today.replace(day=1)
    last_prev = first - datetime.timedelta(days=1)
    return last_prev.replace(day=1), last_prev


def month_range(ym):
    y, m = (int(x) for x in ym.split("-"))
    start = datetime.date(y, m, 1)
    end = (start + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
    return start, end


def quarter_range(yq):
    y, q = (int(x) for x in yq.split("-"))
    m = (q - 1) * 3 + 1
    start = datetime.date(y, m, 1)
    end = (datetime.date(y, m + 2, 1) + datetime.timedelta(days=32)).replace(day=1) \
        - datetime.timedelta(days=1)
    return start, end


# ---------------------------------------------------------------- данные

def buyouts_from_finance(start, end):
    """Выкупы по отчёту реализации: reportType = 2."""
    r = L.qd("""
        SELECT count(*) FILTER (WHERE quantity > 0)         AS rows_sale,
               coalesce(sum(quantity), 0)                   AS qty,
               coalesce(sum(retail_price_with_disc*quantity), 0) AS revenue,
               coalesce(sum(retail_amount), 0)              AS retail,
               coalesce(sum(for_pay), 0)                    AS for_pay,
               min(rr_date)                                 AS first_op,
               max(rr_date)                                 AS last_op
        FROM wb.finance_row
        WHERE (raw->>'reportType') = %s
          AND rr_date BETWEEN %s AND %s
          AND seller_oper_name = 'Продажа'
    """, (BUYOUT_REPORT_TYPE, start, end))
    return (r or [{}])[0]


def finance_coverage(start, end):
    """С какой и по какую дату финотчёт покрывает период.

    Проверять надо оба конца. Хвост очевиден: текущий месяц закрыт не до конца.
    А вот начало легко упустить — за 3-й квартал нужен июль, но в БД отчёт
    загружен с 12 августа, и сумма вышла бы заниженной без единого слова об этом.
    """
    r = L.q("""SELECT min(date_from), max(date_to) FROM wb.finance_row
               WHERE date_to >= %s AND date_from <= %s""", (start, end), fetch="one")
    have_from, have_to = (r or (None, None))
    notes = []
    if have_from is None:
        return ["за период в БД нет ни одной строки отчёта реализации"]
    if have_from > start:
        notes.append("отчёт загружен только с %s — не хватает %d дн. в начале периода"
                     % (have_from.strftime("%d.%m"), (have_from - start).days))
    today = datetime.date.today()
    expected_end = min(end, today)
    if have_to < expected_end:
        notes.append("отчёт закрыт по %s — не хватает %d дн. в конце периода"
                     % (have_to.strftime("%d.%m"), (expected_end - have_to).days))
    return notes


def buyout_documents(start, end):
    """Документы категории «…выкуп…» за период. Один вызов, ответ на диск.

    Возвращает (список документов, пояснение). Пояснение непустое, если
    проверить не удалось — тогда вызывающий код об этом сообщает, а не молчит.
    """
    raw_path = L.WORK / ("doc_list_%s_%s.json" % (start, end))
    if raw_path.exists():
        pages = json.loads(raw_path.read_text(encoding="utf-8"))
    else:
        left = L.quota_left("documents", "/api/v1/documents/list")
        if left > 0:
            return None, ("список документов под суточным лимитом WB, "
                          "следующая попытка через %s" % L.hhmm(left))
        try:
            j = L.call("documents", "/api/v1/documents/list",
                       params={"locale": "ru",
                               "beginTime": start.isoformat(), "endTime": end.isoformat(),
                               "sort": "date", "order": "desc", "limit": 50})
        except L.Throttled as e:
            return None, "список документов под лимитом: %s" % e
        except Exception as e:
            return None, "список документов не получен: %s" % str(e)[:160]
        pages = [j]
        L.WORK.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")

    docs = [d for p in pages for d in (((p or {}).get("data") or {}).get("documents") or [])]
    mine = [d for d in docs
            if DOC_CATEGORY_HINT in (d.get("category") or "").lower()
            or DOC_CATEGORY_HINT in (d.get("name") or "").lower()]
    return mine, None


# ---------------------------------------------------------------- отправка

def already_sent(period):
    done = (L.setting(SENT_KEY, "") or "").split(",")
    return period in [d.strip() for d in done if d.strip()]


def mark_sent(period):
    done = [d.strip() for d in (L.setting(SENT_KEY, "") or "").split(",") if d.strip()]
    if period not in done:
        done.append(period)
    L.set_setting(SENT_KEY, ",".join(done[-24:]), "периоды, по которым уже уходило "
                                                  "сообщение о выкупах")


def build_message(period_title, fin, docs, doc_note, coverage_notes):
    revenue = L.num(fin.get("revenue"))
    qty = int(L.num(fin.get("qty")))

    if qty <= 0 and docs:
        # документ о выкупе уже выложен, а в отчёт реализации сумма ещё не попала:
        # отчёт публикуется неделями и отстаёт от документов. Писать «на сумму 0 ₽»
        # нельзя — бухгалтер решит, что выкупов не было.
        msg = ["Инфа для бухгалтера: за %s есть документы о выкупах WB, "
               "но сумма в отчёт реализации ещё не попала." % period_title, ""]
        msg.append("документов: %d" % len(docs))
        for d in docs[:5]:
            msg.append("   • %s (%s)" % ((d.get("name") or "")[:60],
                                         (d.get("creationTime") or "")[:10]))
        msg.append("")
        msg.append("Сумму пришлю, когда WB закроет отчёт за период.")
        for n in (coverage_notes or []):
            msg.append("! %s" % n)
        return "\n".join(msg)

    msg = ["Инфа для бухгалтера: были выкупы WB на сумму %s" % L.money(revenue), ""]
    msg.append("период: %s" % period_title)
    msg.append("выкуплено: %d шт." % qty)
    msg.append("к перечислению: %s" % L.money(fin.get("for_pay")))
    if docs:
        msg.append("документов в разделе «Документы»: %d" % len(docs))
        for d in docs[:5]:
            msg.append("   • %s (%s)" % ((d.get("name") or "")[:60],
                                         (d.get("creationTime") or "")[:10]))
    elif docs == []:
        msg.append("документов с «выкуп» в разделе «Документы» за период не нашлось — "
                   "сумма посчитана по отчёту реализации")
    if doc_note:
        msg.append("! %s" % doc_note)
    for n in (coverage_notes or []):
        msg.append("! %s" % n)
    return "\n".join(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", help="месяц YYYY-MM")
    ap.add_argument("--quarter", help="квартал YYYY-Q, например 2026-3")
    ap.add_argument("--prev-quarter", action="store_true",
                    help="прошлый завершённый квартал (для запуска по расписанию)")
    ap.add_argument("--dry", action="store_true", help="посчитать и показать, не отправлять")
    ap.add_argument("--force", action="store_true", help="отправить, даже если уже отправляли")
    a = ap.parse_args()

    if a.prev_quarter:
        today = datetime.date.today()
        q = (today.month - 1) // 3          # 0..3; прошлый квартал
        y = today.year
        if q == 0:
            y, q = y - 1, 4
        a.quarter = "%d-%d" % (y, q)
    if a.quarter:
        start, end = quarter_range(a.quarter)
        period, title = a.quarter, "%s квартал %s" % (a.quarter.split("-")[1],
                                                      a.quarter.split("-")[0])
    elif a.period:
        start, end = month_range(a.period)
        period, title = a.period, a.period
    else:
        start, end = prev_month()
        period, title = start.strftime("%Y-%m"), start.strftime("%Y-%m")

    rid = L.run_start("check_buyouts.py", {"period": period})
    try:
        fin = buyouts_from_finance(start, end)
        qty = L.num(fin.get("qty"))

        coverage_notes = finance_coverage(start, end)

        docs, doc_note = buyout_documents(start, end)

        if qty <= 0 and not docs:
            print("за %s выкупов нет (строк reportType=2 с продажами не найдено)" % title)
            if doc_note:
                print("   %s" % doc_note)
            for n in coverage_notes:
                print("   ! %s" % n)
            L.run_end(rid, "ok", rows=0, message="выкупов нет")
            return

        msg = build_message(title, fin, docs, doc_note, coverage_notes)
        print(msg)

        if a.dry:
            print("\n--dry: сообщение не отправлено")
            L.run_end(rid, "ok", rows=int(qty), message="dry-run")
            return
        if already_sent(period) and not a.force:
            print("\nза %s сообщение уже отправляли — повтор не нужен (--force для повтора)"
                  % period)
            L.run_end(rid, "ok", rows=int(qty), message="уже отправлено")
            return

        import bot
        own = bot.owner()
        if not own:
            print("\nвладелец не привязан — напишите боту /start")
            L.run_end(rid, "partial", message="владелец не привязан")
            return
        bot.send(own, msg, mono=False)
        if qty > 0:
            mark_sent(period)
        else:
            print("период не помечен отправленным: сумма ещё не пришла, "
                  "следующий прогон сообщит её")
        L.event("выкупы %s: %s, отправлено бухгалтеру"
                % (title, L.money(fin.get("revenue"))))
        print("\nотправлено владельцу в Telegram")
        L.run_end(rid, "ok", rows=int(qty))
    except Exception as e:
        L.run_end(rid, "error", message=str(e)[:400])
        raise


if __name__ == "__main__":
    main()
