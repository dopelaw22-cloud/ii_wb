#!/usr/bin/env python3
"""Анализ показателей и рекомендации -> wb.recommendation.

Каждое правило проверяет один показатель и, если он выходит за порог,
создаёт рекомендацию с severity: critical / warn / info.
Правило всегда объясняет, из каких цифр сделан вывод — иначе рекомендации
невозможно проверить.

Правила не выдумывают данные: если нужного показателя нет в БД, правило
молчит и сообщает об этом в блок «чего не хватает», а не делает вид,
что всё в порядке.

Запуск:  python analyze.py [--days 30] [--show]
"""
import sys, pathlib, argparse, json, statistics
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L

# Пороги. Вынесены наверх, чтобы владелец мог их поменять одним движением.
TH = {
    "margin_critical": 0.0,     # прибыль ниже нуля
    "margin_warn": 15.0,        # маржа ниже — тревога
    "roi_warn": 30.0,
    "drr_warn": 15.0,           # доля рекламных расходов, %
    "drr_critical": 30.0,
    "ctr_warn": 1.0,            # CTR ниже — креатив/запросы слабые
    "buyout_warn": 85.0,
    "photos_min": 10,
    "chars_min": 24,
    "desc_min": 1500,
    "stock_days_low": 14,       # остатка хватит меньше чем на столько дней
    "stock_days_high": 90,      # затоваривание
    "conv_cart_warn": 5.0,      # переход в карточку -> корзина, %
}


def add(recs, scope, severity, title, detail, nm_id=None, advert_id=None, metric=None):
    recs.append({"scope": scope, "severity": severity, "title": title,
                 "detail": detail, "nm_id": nm_id, "advert_id": advert_id,
                 "metric": metric or {}})


# ---------------------------------------------------------------- правила

def rule_unit_econ(recs, days):
    rows = L.qd("""
        SELECT u.nm_id, p.vendor_code, u.basis, u.price_seller, u.profit_rub,
               u.margin_pct, u.roi_pct, u.cost_rub, u.commission_rub,
               u.logistics_rub, u.storage_rub, u.advert_rub, u.detail
        FROM wb.unit_econ u JOIN wb.product p USING (nm_id)
        WHERE u.calc_date = (SELECT max(calc_date) FROM wb.unit_econ)
        ORDER BY u.margin_pct NULLS LAST
    """)
    if not rows:
        return ["юнит-экономика не рассчитана (unit_economics.py)"]
    gaps = []
    incomplete = [r for r in rows if (r["detail"] or {}).get("incomplete")]
    if incomplete:
        miss = sorted({m for r in incomplete for m in (r["detail"] or {}).get("missing", [])})
        gaps.append("прибыль не посчитана по %d товарам: нет %s"
                    % (len(incomplete), ", ".join(miss)))
    for r in rows:
        if r["margin_pct"] is None:
            continue
        # psycopg отдаёт numeric как Decimal, а он не делится на float
        for k in ("margin_pct", "roi_pct", "profit_rub"):
            if r[k] is not None:
                r[k] = float(r[k])
        vc = r["vendor_code"]
        if r["profit_rub"] is not None and r["profit_rub"] < TH["margin_critical"]:
            add(recs, "unit", "critical",
                "%s: продаётся в убыток" % vc,
                "Прибыль %s с единицы при цене %s. Себестоимость %s, комиссия %s, "
                "логистика %s, реклама %s. Либо поднимать цену, либо убирать рекламу, "
                "либо выводить товар."
                % (L.money(r["profit_rub"]), L.money(r["price_seller"]), L.money(r["cost_rub"]),
                   L.money(r["commission_rub"]), L.money(r["logistics_rub"]), L.money(r["advert_rub"])),
                nm_id=r["nm_id"], metric={"profit": float(r["profit_rub"]),
                                          "margin_pct": float(r["margin_pct"])})
        elif r["margin_pct"] < TH["margin_warn"]:
            add(recs, "unit", "warn",
                "%s: маржа %.1f%% — ниже порога %.0f%%" % (vc, r["margin_pct"], TH["margin_warn"]),
                "С единицы остаётся %s при цене %s. Крупнейшая статья расходов: %s."
                % (L.money(r["profit_rub"]), L.money(r["price_seller"]),
                   max([("комиссия", r["commission_rub"]), ("логистика", r["logistics_rub"]),
                        ("себестоимость", r["cost_rub"]), ("реклама", r["advert_rub"])],
                       key=lambda x: L.num(x[1]))[0]),
                nm_id=r["nm_id"], metric={"margin_pct": float(r["margin_pct"])})
        if r["roi_pct"] is not None and r["roi_pct"] < TH["roi_warn"]:
            add(recs, "unit", "info",
                "%s: ROI %.0f%% на вложенный рубль" % (vc, r["roi_pct"]),
                "На каждый рубль себестоимости (%s) возвращается %.2f ₽ прибыли."
                % (L.money(r["cost_rub"]), float(r["roi_pct"]) / 100.0),
                nm_id=r["nm_id"], metric={"roi_pct": float(r["roi_pct"])})
    return gaps


def rule_advert(recs, days):
    rows = L.qd("""
        SELECT a.advert_id, a.name, a.type, a.status,
               sum(s.sum_rub) AS spend, sum(s.views) AS views, sum(s.clicks) AS clicks,
               sum(s.orders) AS orders, sum(s.sum_price) AS revenue, sum(s.atbs) AS atbs
        FROM wb.advert a JOIN wb.advert_stat s USING (advert_id)
        WHERE s.nm_id = 0 AND s.stat_date >= current_date - %s::int
        GROUP BY a.advert_id, a.name, a.type, a.status
        HAVING sum(s.sum_rub) > 0
        ORDER BY sum(s.sum_rub) DESC
    """, (days,))
    if not rows:
        return ["статистика рекламы не загружена (pull_adverts.py)"]

    ctrs = [float(r["clicks"]) / float(r["views"]) * 100
            for r in rows if L.num(r["views"]) > 500]
    med_ctr = statistics.median(ctrs) if ctrs else None

    for r in rows:
        spend = L.num(r["spend"]); rev = L.num(r["revenue"])
        clicks = L.num(r["clicks"]); views = L.num(r["views"])
        orders = L.num(r["orders"])
        nm = r["name"] or ("кампания %s" % r["advert_id"])
        drr = (spend / rev * 100) if rev else None

        if spend > 0 and orders == 0:
            add(recs, "advert", "critical",
                "«%s»: %s затрат, ноль заказов" % (nm, L.money(spend)),
                "За %d дней: %d показов, %d кликов, 0 заказов. Деньги уходят без результата — "
                "останавливать или менять товар и запросы." % (days, views, clicks),
                advert_id=r["advert_id"], metric={"spend": spend, "clicks": clicks})
        elif drr is not None and drr > TH["drr_critical"]:
            add(recs, "advert", "critical",
                "«%s»: ДРР %.0f%%" % (nm, drr),
                "Затраты %s против выручки %s за %d дней. Реклама съедает почти всю маржу — "
                "снижать ставку или отключать." % (L.money(spend), L.money(rev), days),
                advert_id=r["advert_id"], metric={"drr": drr, "spend": spend})
        elif drr is not None and drr > TH["drr_warn"]:
            add(recs, "advert", "warn",
                "«%s»: ДРР %.0f%% выше порога %.0f%%" % (nm, drr, TH["drr_warn"]),
                "Затраты %s, выручка с рекламы %s, заказов %d. Проверить ставку и минус-фразы."
                % (L.money(spend), L.money(rev), orders),
                advert_id=r["advert_id"], metric={"drr": drr})

        if views > 1000:
            ctr = clicks / views * 100
            if ctr < TH["ctr_warn"]:
                add(recs, "advert", "warn",
                    "«%s»: CTR %.2f%% при %d показов" % (nm, ctr, views),
                    "Карточку видят, но не нажимают: слабая главная фотография или цена "
                    "вне рынка.%s" % ("" if med_ctr is None else
                                      " Медиана по вашим кампаниям — %.2f%%." % med_ctr),
                    advert_id=r["advert_id"], metric={"ctr": ctr})
    return []


def rule_cards(recs):
    rows = L.qd("""SELECT nm_id, vendor_code, title, brand, photos, chars, desc_len
                   FROM wb.product ORDER BY vendor_code""")
    if not rows:
        return ["справочник товаров пуст (pull_products.py)"]
    best_photos = max(L.num(r["photos"]) for r in rows)
    best_chars = max(L.num(r["chars"]) for r in rows)
    no_brand = [r for r in rows if not (r["brand"] or "").strip()]
    if no_brand:
        add(recs, "card", "warn",
            "Бренд не заполнен ни в одной карточке (%d шт.)" % len(no_brand),
            "Поле «бренд» пустое у всех товаров, хотя торговая марка — «Вытяжки скрытого "
            "монтажа». Заполненный бренд даёт переходы по брендовому запросу и собственную "
            "страницу бренда на WB.",
            metric={"count": len(no_brand)})
    for r in rows:
        ph, ch, dl = L.num(r["photos"]), L.num(r["chars"]), L.num(r["desc_len"])
        if ph < min(TH["photos_min"], best_photos):
            add(recs, "card", "warn",
                "%s: %d фото, у лучшей карточки %d" % (r["vendor_code"], ph, best_photos),
                "Добавить %d фото до уровня сильных карточек. Изображения — главный фактор "
                "конверсии в корзину." % (best_photos - ph),
                nm_id=r["nm_id"], metric={"photos": ph, "best": best_photos})
        if ch < min(TH["chars_min"], best_chars):
            add(recs, "card", "warn",
                "%s: %d характеристик, у лучшей карточки %d" % (r["vendor_code"], ch, best_chars),
                "Незаполненные характеристики исключают товар из фильтров поиска — "
                "покупатель его просто не найдёт. Довести до %d." % best_chars,
                nm_id=r["nm_id"], metric={"chars": ch, "best": best_chars})
        if dl < TH["desc_min"]:
            add(recs, "card", "info",
                "%s: описание %d знаков" % (r["vendor_code"], dl),
                "Короткое описание хуже индексируется поиском WB. Ориентир — %d знаков."
                % TH["desc_min"],
                nm_id=r["nm_id"], metric={"desc_len": dl})
    return []


def rule_funnel(recs, days):
    rows = L.qd("""
        SELECT f.nm_id, p.vendor_code, sum(f.open_count) AS opens, sum(f.cart_count) AS carts,
               sum(f.order_count) AS orders, sum(f.buyout_count) AS buyouts,
               max(f.product_rating) AS rating, max(f.feedback_rating) AS fb_rating
        FROM wb.funnel f JOIN wb.product p USING (nm_id)
        WHERE (f.period_start, f.period_end) = (SELECT period_start, period_end FROM wb.funnel
                                   ORDER BY period_end DESC, period_start DESC LIMIT 1)
        GROUP BY f.nm_id, p.vendor_code
    """)
    if not rows:
        return ["воронка не загружена (pull_funnel.py)"]
    for r in rows:
        opens, carts = L.num(r["opens"]), L.num(r["carts"])
        orders, buyouts = L.num(r["orders"]), L.num(r["buyouts"])
        if opens > 300:
            conv = carts / opens * 100
            if conv < TH["conv_cart_warn"]:
                add(recs, "card", "warn",
                    "%s: из карточки в корзину %.1f%%" % (r["vendor_code"], conv),
                    "%d переходов в карточку, только %d добавлений в корзину. Трафик есть, "
                    "карточка не убеждает: фото, цена, отзывы." % (opens, carts),
                    nm_id=r["nm_id"], metric={"conv_cart_pct": conv})
        if orders > 20:
            bo = buyouts / orders * 100
            if bo < TH["buyout_warn"]:
                add(recs, "unit", "warn",
                    "%s: выкуп %.0f%%" % (r["vendor_code"], bo),
                    "Из %d заказов выкуплено %d. Каждый невыкуп — двойная логистика за ваш "
                    "счёт. Причины: расхождение фото и товара, брак, долгая доставка."
                    % (orders, buyouts),
                    nm_id=r["nm_id"], metric={"buyout_pct": bo})
    return []


def rule_stocks(recs, days):
    rows = L.qd("""
        WITH last AS (
            SELECT nm_id, sum(quantity) AS qty
            FROM wb.stock
            WHERE captured_at = (SELECT max(captured_at) FROM wb.stock)
            GROUP BY nm_id),
        sold AS (
            SELECT nm_id, count(*)::numeric / %s AS per_day
            FROM wb.sale WHERE sale_dt >= current_date - %s::int
              AND coalesce(is_return,false) = false
            GROUP BY nm_id)
        SELECT p.nm_id, p.vendor_code, coalesce(l.qty,0) AS qty, coalesce(s.per_day,0) AS per_day
        FROM wb.product p LEFT JOIN last l USING (nm_id) LEFT JOIN sold s USING (nm_id)
    """, (days, days))
    if not any(L.num(r["qty"]) for r in rows):
        return ["остатки не загружены (pull_stocks.py)"]
    for r in rows:
        qty, per_day = L.num(r["qty"]), L.num(r["per_day"])
        if per_day <= 0:
            if qty > 0:
                add(recs, "stock", "info",
                    "%s: %d шт. на складе, продаж нет" % (r["vendor_code"], qty),
                    "За %d дней ни одной продажи, а товар занимает склад и копит плату "
                    "за хранение." % days,
                    nm_id=r["nm_id"], metric={"qty": qty})
            continue
        days_left = qty / per_day
        if days_left < TH["stock_days_low"]:
            add(recs, "stock", "critical" if days_left < 7 else "warn",
                "%s: остатка на %.0f дней" % (r["vendor_code"], days_left),
                "%d шт. при расходе %.1f шт./день. Товар уйдёт в out-of-stock, позиции "
                "в поиске просядут — поставку нужно планировать сейчас." % (qty, per_day),
                nm_id=r["nm_id"], metric={"days_left": days_left, "qty": qty})
        elif days_left > TH["stock_days_high"]:
            add(recs, "stock", "info",
                "%s: остатка на %.0f дней — затоваривание" % (r["vendor_code"], days_left),
                "%d шт. при расходе %.1f шт./день. Деньги заморожены в товаре, плата "
                "за хранение растёт." % (qty, per_day),
                nm_id=r["nm_id"], metric={"days_left": days_left, "qty": qty})
    return []


# ---------------------------------------------------------------- сборка

def run(days=30, save=True):
    recs, gaps = [], []
    for fn in (lambda: rule_unit_econ(recs, days),
               lambda: rule_advert(recs, days),
               lambda: rule_cards(recs),
               lambda: rule_funnel(recs, days),
               lambda: rule_stocks(recs, days)):
        try:
            gaps += (fn() or [])
        except Exception as e:
            gaps.append("правило упало: %s" % str(e)[:140])

    order = {"critical": 0, "warn": 1, "info": 2}
    recs.sort(key=lambda r: order.get(r["severity"], 3))

    if save and recs:
        with L.db() as c, c.cursor() as cur:
            cur.execute("UPDATE wb.recommendation SET status='stale' WHERE status='new'")
            for r in recs:
                cur.execute("""INSERT INTO wb.recommendation
                        (scope, nm_id, advert_id, severity, title, detail, metric)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                            (r["scope"], r["nm_id"], r["advert_id"], r["severity"],
                             r["title"], r["detail"],
                             json.dumps(r["metric"], ensure_ascii=False, default=str)))
    return recs, gaps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args()

    rid = L.run_start("analyze.py")
    try:
        recs, gaps = run(a.days, save=not a.no_save)
        mark = {"critical": "!!", "warn": " !", "info": "  "}
        print("рекомендаций: %d (критичных %d)"
              % (len(recs), sum(1 for r in recs if r["severity"] == "critical")))
        if a.show:
            for r in recs:
                print("\n%s %s" % (mark.get(r["severity"], "  "), r["title"]))
                print("     %s" % r["detail"])
        if gaps:
            print("\nЧего не хватает для полного анализа:")
            for g in gaps:
                print("   — %s" % g)
        L.run_end(rid, "ok", rows=len(recs))
    except Exception as e:
        L.run_end(rid, "error", message=str(e)[:400])
        raise


if __name__ == "__main__":
    main()
