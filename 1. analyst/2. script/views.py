#!/usr/bin/env python3
"""Текстовые блоки отчётов. Общие для Telegram-бота и файловых отчётов.

Каждая функция возвращает готовый текст и честно сообщает, если данных нет.
Пустой блок — это не ошибка, а состояние «ещё не собрали».
"""
import sys, pathlib, datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L

DASH = "—"


def m(v):
    return L.money(v) if v is not None else DASH


def p1(v):
    return ("%.1f%%" % float(v)) if v is not None else DASH


def freshness():
    """Когда какие данные обновлялись — чтобы отчёт нельзя было спутать со свежим."""
    rows = [
        ("товары",     L.q("SELECT max(synced_at) FROM wb.product", fetch="one")),
        ("цены",       L.q("SELECT max(captured_at) FROM wb.price", fetch="one")),
        ("себестоим.", L.q("SELECT max(created_at) FROM wb.cost", fetch="one")),
        ("продажи",    L.q("SELECT max(synced_at) FROM wb.sale", fetch="one")),
        ("финотчёт",   L.q("SELECT max(synced_at) FROM wb.finance_row", fetch="one")),
        ("воронка",    L.q("SELECT max(captured_at) FROM wb.funnel", fetch="one")),
        ("реклама",    L.q("SELECT max(synced_at) FROM wb.advert_stat", fetch="one")),
        ("остатки",    L.q("SELECT max(captured_at) FROM wb.stock", fetch="one")),
        ("тарифы",     L.q("SELECT max(captured_at) FROM wb.tariff", fetch="one")),
    ]
    out = []
    for name, r in rows:
        ts = r[0] if r else None
        if ts is None:
            out.append("%s: нет данных" % name)
        else:
            age = (datetime.datetime.now(ts.tzinfo) - ts)
            h = age.total_seconds() / 3600
            out.append("%s: %s" % (name, "только что" if h < 1 else
                                   ("%.0f ч назад" % h) if h < 48 else
                                   ("%.0f дн назад" % (h / 24))))
    return "\n".join(out)


def tax_note():
    """Человеческое описание налоговой схемы для шапки отчётов."""
    mode = L.setting("tax_mode", "usn6")
    note = L.q("SELECT note FROM wb.setting WHERE key='tax_mode'", fetch="one")
    unconfirmed = bool(note and note[0] and "НЕ ПОДТВЕРЖДЕНО" in note[0])

    if mode == "composite":
        usn = L.num(L.setting("tax_usn_pct", "0"))
        ins = L.num(L.setting("tax_insurance_pct", "0"))
        vat = L.num(L.setting("tax_vat_pct", "0"))
        in_price = (L.setting("tax_vat_in_price", "yes") or "yes").lower() == "yes"
        bits = []
        if usn:
            bits.append("УСН %g%%" % usn)
        if ins:
            bits.append("взносы %g%%" % ins)
        if vat:
            bits.append("НДС %g%%%s" % (vat, " (в цене)" if in_price else " (сверху)"))
        s = " + ".join(bits) or "без налога"
        if vat and in_price:
            s += ", фактически НДС %.2f%% от цены" % (vat / (100.0 + vat) * 100.0)
    else:
        rate = L.setting("tax_rate", "6")
        names = {"usn6": "УСН %s%% с выручки" % rate,
                 "usn15": "УСН %s%% с прибыли" % rate,
                 "none": "без налога"}
        s = names.get(mode, "%s %s%%" % (mode, rate))
    return s + (" (предположение, не подтверждено — /налог для смены)" if unconfirmed else "")


def profit_table(basis=None):
    """Юнит-экономика по товарам. basis=None -> fact, если есть, иначе plan."""
    have_fact = L.q("""SELECT count(*) FROM wb.unit_econ
                       WHERE basis='fact' AND calc_date=(SELECT max(calc_date) FROM wb.unit_econ)""",
                    fetch="one")[0]
    basis = basis or ("fact" if have_fact else "plan")
    rows = L.qd("""
        SELECT p.vendor_code, u.price_seller, u.commission_rub, u.logistics_rub,
               u.storage_rub, u.advert_rub, u.cost_rub, u.tax_rub, u.profit_rub,
               u.margin_pct, u.roi_pct, u.buyout_pct, u.detail
        FROM wb.unit_econ u JOIN wb.product p USING (nm_id)
        WHERE u.basis=%s AND u.calc_date=(SELECT max(calc_date) FROM wb.unit_econ)
        ORDER BY u.profit_rub DESC NULLS LAST, p.vendor_code
    """, (basis,))
    if not rows:
        return "Юнит-экономика не рассчитана. Нужен прогон unit_economics.py."

    title = ("ЮНИТ-ЭКОНОМИКА — по факту финотчёта" if basis == "fact"
             else "ЮНИТ-ЭКОНОМИКА — расчёт по тарифам")
    out = [title, "налог: %s" % tax_note(), ""]
    out.append("%-15s %8s %7s %7s %7s %7s %8s %8s %6s"
               % ("артикул", "цена", "кВВ", "логист", "реклам", "себест", "налог", "прибыль", "марж"))
    out.append("-" * 82)
    incomplete = 0
    for r in rows:
        if r["profit_rub"] is None:
            incomplete += 1
        out.append("%-15s %8s %7s %7s %7s %7s %8s %8s %6s" % (
            r["vendor_code"][:15], m(r["price_seller"]), m(r["commission_rub"]),
            m(r["logistics_rub"]), m(r["advert_rub"]), m(r["cost_rub"]),
            m(r["tax_rub"]), m(r["profit_rub"]), p1(r["margin_pct"])))

    done = [r for r in rows if r["profit_rub"] is not None]
    if done:
        tot = sum(float(r["profit_rub"]) for r in done)
        out += ["", "средняя прибыль с единицы: %s, средняя маржа: %s"
                % (L.money(tot / len(done)),
                   p1(sum(float(r["margin_pct"]) for r in done if r["margin_pct"] is not None)
                      / max(1, sum(1 for r in done if r["margin_pct"] is not None))))]
    if incomplete:
        miss = sorted({x for r in rows for x in ((r["detail"] or {}).get("missing") or [])})
        out += ["", "по %d товарам прибыль не посчитана — не хватает: %s"
                % (incomplete, ", ".join(miss) or "данных")]
    return "\n".join(out)


def ads_block(days=30):
    rows = L.qd("""
        SELECT a.advert_id, a.name, a.status,
               sum(s.sum_rub) AS spend, sum(s.views) AS views, sum(s.clicks) AS clicks,
               sum(s.orders) AS orders, sum(s.sum_price) AS revenue
        FROM wb.advert a JOIN wb.advert_stat s USING (advert_id)
        WHERE s.nm_id=0 AND s.stat_date >= current_date - %s::int
        GROUP BY a.advert_id, a.name, a.status
        HAVING sum(s.sum_rub) > 0
        ORDER BY sum(s.sum_rub) DESC LIMIT 20
    """, (days,))
    if not rows:
        return "Статистика рекламы не загружена. Нужен прогон pull_adverts.py."
    st = {9: "активна", 11: "пауза", 7: "завершена", 4: "готова", -1: "удалена", 8: "отказана"}
    tot_spend = sum(L.num(r["spend"]) for r in rows)
    tot_rev = sum(L.num(r["revenue"]) for r in rows)
    out = ["РЕКЛАМА за %d дней" % days, ""]
    out.append("%-26s %9s %9s %6s %6s %6s %7s"
               % ("кампания", "затраты", "выручка", "ДРР", "CTR", "CPC", "заказы"))
    out.append("-" * 76)
    for r in rows:
        spend, rev = L.num(r["spend"]), L.num(r["revenue"])
        views, clicks = L.num(r["views"]), L.num(r["clicks"])
        name = (r["name"] or str(r["advert_id"]))[:26]
        out.append("%-26s %9s %9s %6s %6s %6s %7d" % (
            name, L.money(spend), L.money(rev),
            ("%.0f%%" % (spend / rev * 100)) if rev else DASH,
            ("%.2f" % (clicks / views * 100)) if views else DASH,
            ("%.0f" % (spend / clicks)) if clicks else DASH,
            int(L.num(r["orders"]))))
    out += ["", "итого затрат %s, выручка с рекламы %s, общий ДРР %s"
            % (L.money(tot_spend), L.money(tot_rev),
               ("%.0f%%" % (tot_spend / tot_rev * 100)) if tot_rev else DASH)]
    return "\n".join(out)


def cards_block():
    rows = L.qd("""SELECT vendor_code, photos, chars, desc_len, brand,
                          width, height, length, weight_brutto
                   FROM wb.product ORDER BY vendor_code""")
    if not rows:
        return "Справочник товаров пуст."
    out = ["КАРТОЧКИ", ""]
    out.append("%-15s %5s %6s %7s %7s %9s" % ("артикул", "фото", "характ", "описан", "бренд", "объём, л"))
    out.append("-" * 56)
    for r in rows:
        vol = (L.num(r["width"]) * L.num(r["height"]) * L.num(r["length"])) / 1000.0
        out.append("%-15s %5d %6d %7d %7s %9.2f" % (
            r["vendor_code"][:15], L.num(r["photos"]), L.num(r["chars"]),
            L.num(r["desc_len"]), (r["brand"] or DASH)[:7], vol))
    return "\n".join(out)


def funnel_block(days=30):
    rows = L.qd("""
        SELECT p.vendor_code, sum(f.open_count) AS opens, sum(f.cart_count) AS carts,
               sum(f.order_count) AS orders, sum(f.buyout_count) AS buyouts,
               sum(f.order_sum) AS order_sum, max(f.feedback_rating) AS rating
        FROM wb.funnel f JOIN wb.product p USING (nm_id)
        WHERE (f.period_start, f.period_end) = (SELECT period_start, period_end FROM wb.funnel
                                   ORDER BY period_end DESC, period_start DESC LIMIT 1)
        GROUP BY p.vendor_code ORDER BY sum(f.order_sum) DESC NULLS LAST
    """)
    if not rows:
        return "Воронка не загружена. Нужен прогон pull_funnel.py."
    out = ["ВОРОНКА за %d дней" % days, ""]
    out.append("%-15s %8s %8s %8s %8s %7s %7s %6s"
               % ("артикул", "переходы", "корзина", "заказы", "выкупы", "в корз", "выкуп", "оценка"))
    out.append("-" * 76)
    for r in rows:
        op, ca = L.num(r["opens"]), L.num(r["carts"])
        od, bo = L.num(r["orders"]), L.num(r["buyouts"])
        out.append("%-15s %8d %8d %8d %8d %7s %7s %6s" % (
            r["vendor_code"][:15], op, ca, od, bo,
            ("%.1f%%" % (ca / op * 100)) if op else DASH,
            ("%.0f%%" % (bo / od * 100)) if od else DASH,
            ("%.1f" % float(r["rating"])) if r["rating"] else DASH))
    return "\n".join(out)


def stock_block(days=30):
    rows = L.qd("""
        WITH last AS (SELECT nm_id, sum(quantity) AS qty FROM wb.stock
                      WHERE captured_at=(SELECT max(captured_at) FROM wb.stock)
                      GROUP BY nm_id),
             sold AS (SELECT nm_id, count(*)::numeric/%s AS per_day FROM wb.sale
                      WHERE sale_dt >= current_date - %s::int
                        AND coalesce(is_return,false)=false GROUP BY nm_id)
        SELECT p.vendor_code, coalesce(l.qty,0) AS qty, coalesce(s.per_day,0) AS per_day
        FROM wb.product p LEFT JOIN last l USING (nm_id) LEFT JOIN sold s USING (nm_id)
        ORDER BY p.vendor_code
    """, (days, days))
    if not rows or not any(L.num(r["qty"]) for r in rows):
        return "Остатки не загружены. Нужен прогон pull_stocks.py."
    out = ["ОСТАТКИ", "", "%-15s %8s %11s %12s" % ("артикул", "шт.", "продаж/дн", "хватит на")]
    out.append("-" * 50)
    for r in rows:
        qty, pd = L.num(r["qty"]), L.num(r["per_day"])
        out.append("%-15s %8d %11s %12s" % (
            r["vendor_code"][:15], qty, ("%.1f" % pd) if pd else DASH,
            ("%.0f дн" % (qty / pd)) if pd else DASH))
    return "\n".join(out)


def recs_block(limit=20, severity=None):
    where = "status='new'"
    params = []
    if severity:
        where += " AND severity=%s"
        params.append(severity)
    rows = L.qd("""SELECT severity, title, detail FROM wb.recommendation
                   WHERE %s
                   ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                            id LIMIT %%s""" % where, tuple(params) + (limit,))
    if not rows:
        return "Рекомендаций нет. Либо всё в норме, либо не хватает данных — /данные."
    mark = {"critical": "🔴", "warn": "🟡", "info": "⚪"}
    out = ["РЕКОМЕНДАЦИИ", ""]
    for r in rows:
        out.append("%s %s" % (mark.get(r["severity"], "·"), r["title"]))
        out.append("   %s" % r["detail"])
        out.append("")
    n = L.q("SELECT count(*) FROM wb.recommendation WHERE status='new'", fetch="one")[0]
    if n > len(rows):
        out.append("…и ещё %d. Полный список — в файловом отчёте." % (n - len(rows)))
    return "\n".join(out)


def limits_block():
    """Сколько осталось до следующего разрешённого вызова по каждому методу."""
    out = ["ЛИМИТЫ WB API", "",
           "У WB аналитические методы отдаются окнами по часам.",
           "Данные берутся из кеша в БД, API вызывается только когда окно открыто.", ""]
    names = {
        "finance:/api/finance/v1/sales-reports/detailed": "финотчёт реализации",
        "analytics:/api/analytics/v3/sales-funnel/products": "воронка продаж",
        "stats:/api/v1/supplier/sales": "продажи",
        "stats:/api/v1/supplier/orders": "заказы",
        "advert:/adv/v3/fullstats": "статистика рекламы",
        "common:/api/v1/tariffs/box": "тарифы логистики",
        "common:/api/v1/tariffs/commission": "тарифы комиссии",
        "analytics:/api/v1/warehouse_remains": "остатки",
    }
    for key, title in names.items():
        host, path = key.split(":", 1)
        left = L.quota_left(host, path)
        out.append("%-24s %s" % (title, "доступно" if left == 0 else "через %s" % L.hhmm(left)))
    return "\n".join(out)


def summary(days=30):
    """Короткая сводка — то, что уходит в Telegram первым сообщением."""
    out = ["СВОДКА за %d дней" % days, ""]

    s = L.qd("""SELECT count(*) AS qty, sum(for_pay) AS for_pay, sum(price_with_disc) AS rev
                FROM wb.sale WHERE sale_dt >= current_date - %s::int
                  AND coalesce(is_return,false)=false""", (days,))
    if s and L.num(s[0]["qty"]):
        out.append("Продаж: %d, к перечислению %s" % (int(s[0]["qty"]), L.money(s[0]["for_pay"])))
    else:
        out.append("Продажи: данных нет")

    a = L.qd("""SELECT sum(sum_rub) AS spend, sum(sum_price) AS rev, sum(orders) AS orders
                FROM wb.advert_stat WHERE nm_id=0 AND stat_date >= current_date - %s::int""", (days,))
    if a and L.num(a[0]["spend"]):
        spend, rev = L.num(a[0]["spend"]), L.num(a[0]["rev"])
        out.append("Реклама: %s затрат, ДРР %s" % (
            L.money(spend), ("%.0f%%" % (spend / rev * 100)) if rev else DASH))
    else:
        out.append("Реклама: данных нет")

    u = L.qd("""SELECT avg(margin_pct) AS marg, min(margin_pct) AS mn, count(*) AS n
                FROM wb.unit_econ
                WHERE calc_date=(SELECT max(calc_date) FROM wb.unit_econ)
                  AND profit_rub IS NOT NULL""")
    if u and L.num(u[0]["n"]):
        out.append("Маржа: средняя %s, худшая %s по %d товарам"
                   % (p1(u[0]["marg"]), p1(u[0]["mn"]), int(u[0]["n"])))
    else:
        out.append("Маржа: не посчитана (нет тарифов или финотчёта)")

    c = L.q("""SELECT count(*) FILTER (WHERE severity='critical'),
                      count(*) FILTER (WHERE severity='warn')
               FROM wb.recommendation WHERE status='new'""", fetch="one")
    if c:
        out.append("Рекомендации: %d критичных, %d предупреждений" % (c[0] or 0, c[1] or 0))
    return "\n".join(out)


def months_block(months=6, detail_month=None):
    """Прибыль по месяцам из отчёта реализации."""
    import profit_by_month as M
    rows = M.monthly(months)
    out = M.render(rows)
    if detail_month:
        for r in rows:
            if r["month"] == detail_month:
                return out + "\n\n" + M.detail(r)
        return out + "\n\nмесяц %s не найден" % detail_month
    return out
