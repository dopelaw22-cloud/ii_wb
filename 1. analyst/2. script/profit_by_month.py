#!/usr/bin/env python3
"""Прибыль по месяцам из отчёта реализации.

Считает ровно по той же логике, что unit_economics.py в режиме fact, но
без разноса расходов по товарам: для месячного итога это не нужно, поэтому
хранение, реклама и прочие удержания без привязки к товару берутся целиком.

Важно про полноту: WB публикует отчёт реализации недельными периодами, и
текущий месяц почти всегда закрыт не до конца. Скрипт показывает, по какую
дату есть данные, и считает, сколько дней месяца не покрыто, — иначе
неполный месяц легко принять за провал продаж.

Запуск:  python profit_by_month.py [--months 6]
"""
import sys, pathlib, argparse, datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L
import unit_economics as U


def monthly(months=6):
    rows = L.qd("""
        WITH cost AS (
            SELECT p.nm_id,
                   (SELECT c.cost_rub + c.extra_rub FROM wb.cost c
                     WHERE c.vendor_code = lower(p.vendor_code)
                     ORDER BY c.valid_from DESC LIMIT 1) AS unit_cost
            FROM wb.product p)
        SELECT to_char(f.rr_date, 'YYYY-MM')                      AS month,
               min(f.rr_date)                                     AS first_op,
               max(f.rr_date)                                     AS last_op,
               sum(CASE WHEN f.seller_oper_name='Продажа' THEN f.quantity ELSE 0 END) AS qty_sold,
               sum(CASE WHEN f.seller_oper_name='Возврат' THEN f.quantity ELSE 0 END) AS qty_return,
               sum(CASE WHEN f.seller_oper_name='Продажа'
                            THEN f.retail_price_with_disc*f.quantity
                        WHEN f.seller_oper_name='Возврат'
                            THEN -f.retail_price_with_disc*f.quantity ELSE 0 END)  AS revenue,
               sum(CASE WHEN f.seller_oper_name='Продажа' THEN f.for_pay
                        WHEN f.seller_oper_name='Возврат' THEN -f.for_pay
                        ELSE 0 END)                               AS for_pay,
               sum(CASE WHEN f.seller_oper_name='Продажа' THEN f.acquiring_fee
                        WHEN f.seller_oper_name='Возврат' THEN -f.acquiring_fee
                        ELSE 0 END)                               AS acquiring,
               sum(f.delivery_rub + f.rebill_logistic_cost)       AS logistics,
               sum(f.paid_storage)                                AS storage,
               sum(f.paid_acceptance)                             AS acceptance,
               sum(f.penalty)                                     AS penalty,
               sum(CASE WHEN f.seller_oper_name='Удержание'
                         AND coalesce(f.raw->>'bonusTypeName','') LIKE %s
                        THEN f.deduction ELSE 0 END)              AS advert,
               sum(CASE WHEN f.seller_oper_name='Удержание'
                         AND coalesce(f.raw->>'bonusTypeName','') NOT LIKE %s
                        THEN f.deduction ELSE 0 END)              AS other_deduction,
               sum(CASE WHEN f.seller_oper_name='Продажа'
                            THEN coalesce(c.unit_cost,0)*f.quantity
                        WHEN f.seller_oper_name='Возврат'
                            THEN -coalesce(c.unit_cost,0)*f.quantity ELSE 0 END)   AS cost,
               count(*) FILTER (WHERE f.seller_oper_name='Продажа'
                                  AND c.unit_cost IS NULL)        AS no_cost_rows
        FROM wb.finance_row f LEFT JOIN cost c USING (nm_id)
        WHERE f.rr_date >= date_trunc('month', current_date) - (%s || ' months')::interval
        GROUP BY 1 ORDER BY 1
    """, ("%Продвижение%", "%Продвижение%", months))

    cfg = U.tax_config()
    out = []
    for r in rows:
        rev = L.num(r["revenue"])
        costs = (L.num(r["logistics"]) + L.num(r["storage"]) + L.num(r["acceptance"])
                 + L.num(r["penalty"]) + L.num(r["advert"])
                 + L.num(r["other_deduction"]) + L.num(r["cost"]))
        before_tax = L.num(r["for_pay"]) - costs
        tax, parts = U.tax_breakdown(rev, before_tax, cfg)
        qty = L.num(r["qty_sold"]) - L.num(r["qty_return"])

        # покрытие месяца: до какого числа закрыт отчёт
        first = r["first_op"]
        last = r["last_op"]
        month_start = first.replace(day=1)
        nxt = (month_start + datetime.timedelta(days=32)).replace(day=1)
        month_end = nxt - datetime.timedelta(days=1)
        today = datetime.date.today()
        expected_end = min(month_end, today)
        gap_days = (expected_end - last).days

        out.append({
            "month": r["month"], "last_op": last, "gap_days": max(0, gap_days),
            "month_end": month_end, "partial": gap_days > 0,
            "qty": qty, "qty_sold": L.num(r["qty_sold"]),
            "qty_return": L.num(r["qty_return"]),
            "revenue": rev, "for_pay": L.num(r["for_pay"]),
            "commission": rev - L.num(r["for_pay"]) - L.num(r["acquiring"]),
            "acquiring": L.num(r["acquiring"]),
            "logistics": L.num(r["logistics"]), "storage": L.num(r["storage"]),
            "penalty": L.num(r["penalty"]), "advert": L.num(r["advert"]),
            "other": L.num(r["other_deduction"]), "cost": L.num(r["cost"]),
            "tax": tax, "tax_parts": parts, "profit": before_tax - tax,
            "no_cost_rows": int(L.num(r["no_cost_rows"])),
        })
    return out


def render(rows):
    if not rows:
        return "Финотчёт не загружен — прибыль по месяцам считать не из чего."
    o = ["ПРИБЫЛЬ ПО МЕСЯЦАМ (по отчёту реализации WB)",
         "налог: %s" % __import__("views").tax_note(), ""]
    o.append("%-9s %6s %10s %10s %9s %9s %9s %10s %7s"
             % ("месяц", "шт.", "выручка", "кВВ", "логист", "реклама", "себест", "прибыль", "маржа"))
    o.append("-" * 88)
    for r in rows:
        o.append("%-9s %6d %10s %10s %9s %9s %9s %10s %6.1f%%" % (
            r["month"], r["qty"], L.money(r["revenue"]), L.money(r["commission"]),
            L.money(r["logistics"]), L.money(r["advert"]), L.money(r["cost"]),
            L.money(r["profit"]),
            (r["profit"] / r["revenue"] * 100) if r["revenue"] else 0))
    partial = [r for r in rows if r["partial"]]
    if partial:
        o.append("")
        for r in partial:
            o.append("! %s неполный: отчёт закрыт по %s, не покрыто %d дн. до %s"
                     % (r["month"], r["last_op"].strftime("%d.%m"), r["gap_days"],
                        min(r["month_end"], datetime.date.today()).strftime("%d.%m")))
    nc = [r for r in rows if r["no_cost_rows"]]
    if nc:
        o.append("! нет себестоимости по части строк: %s"
                 % ", ".join("%s (%d)" % (r["month"], r["no_cost_rows"]) for r in nc))
    return "\n".join(o)


def detail(r):
    """Полная расшифровка одного месяца."""
    o = ["%s — расшифровка" % r["month"], ""]
    rev = r["revenue"]
    pct = lambda v: ("%5.1f%%" % (v / rev * 100)) if rev else "     —"
    o.append("продано %d шт. (продаж %d, возвратов %d)"
             % (r["qty"], r["qty_sold"], r["qty_return"]))
    o.append("")
    o.append("  выручка          %12s %s" % (L.money(rev), pct(rev)))
    for name, v in (("комиссия WB", r["commission"]), ("эквайринг", r["acquiring"]),
                    ("логистика", r["logistics"]), ("хранение", r["storage"]),
                    ("штрафы", r["penalty"]), ("реклама", r["advert"]),
                    ("прочие удержания", r["other"]), ("себестоимость", r["cost"])):
        if v:
            o.append("  %-16s %12s %s" % (name, "−" + L.money(v), pct(v)))
    for name, v in sorted(r["tax_parts"].items(), key=lambda x: -x[1]):
        o.append("  %-16s %12s %s" % (name, "−" + L.money(v), pct(v)))
    o.append("  " + "-" * 32)
    o.append("  ПРИБЫЛЬ          %12s %s" % (L.money(r["profit"]), pct(r["profit"])))
    if r["qty"]:
        o.append("  на единицу       %12s" % L.money(r["profit"] / r["qty"]))
    return "\n".join(o)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--detail", help="расшифровка месяца, например 2026-09")
    a = ap.parse_args()
    rows = monthly(a.months)
    print(render(rows))
    if a.detail:
        for r in rows:
            if r["month"] == a.detail:
                print("\n" + detail(r))
                break
        else:
            print("\nмесяц %s не найден" % a.detail)


if __name__ == "__main__":
    main()
