#!/usr/bin/env python3
"""Продажи и заказы из раздела «Статистика» -> wb.sale, wb.order_row.

WB отдаёт данные инкрементально по lastChangeDate. Храним курсор в wb.setting,
чтобы при каждом прогоне забирать только новое.

Запуск:  python pull_sales.py [--from 2026-09-01] [--full]
"""
import sys, pathlib, argparse, datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L

CURSOR_SALES = "cursor_sales"
CURSOR_ORDERS = "cursor_orders"
DEFAULT_DEPTH = 90


def fetch(path, date_from):
    """WB отдаёт максимум 80 000 строк за вызов; идём по lastChangeDate.

    Возвращает (строки, причина_остановки). Если квота кончилась на середине,
    уже скачанные страницы возвращаются, а не теряются: окно у этого метода
    около двух часов, и выбросить полученное означало бы ждать ещё два часа
    ради тех же данных.
    """
    out, seen_cursor = [], None
    for _ in range(40):
        try:
            chunk = L.call("stats", path, params={"dateFrom": date_from}, wait=True)
        except L.Throttled as e:
            return out, "лимит после %d строк: %s" % (len(out), e)
        if not chunk:
            break
        out += chunk
        last = max(c.get("lastChangeDate") or "" for c in chunk)
        if len(chunk) < 80000 or last == seen_cursor:
            break
        seen_cursor = last
        date_from = last
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dfrom", help="дата начала, YYYY-MM-DD")
    ap.add_argument("--full", action="store_true", help="перезабрать за 90 дней, игнорируя курсор")
    a = ap.parse_args()

    rid = L.run_start("pull_sales.py")
    try:
        if a.dfrom:
            start_s = start_o = a.dfrom + "T00:00:00"
        elif a.full:
            d = (datetime.date.today() - datetime.timedelta(days=DEFAULT_DEPTH)).isoformat()
            start_s = start_o = d + "T00:00:00"
        else:
            d = (datetime.date.today() - datetime.timedelta(days=DEFAULT_DEPTH)).isoformat() + "T00:00:00"
            start_s = L.setting(CURSOR_SALES, d)
            start_o = L.setting(CURSOR_ORDERS, d)

        sales, s_note = fetch("/api/v1/supplier/sales", start_s)
        orders, o_note = fetch("/api/v1/supplier/orders", start_o)

        with L.db() as c, c.cursor() as cur:
            for s in sales:
                cur.execute("""
                    INSERT INTO wb.sale (srid, sale_id, sale_dt, last_change, nm_id, vendor_code,
                        barcode, warehouse_name, region, total_price, discount_pct, spp,
                        price_with_disc, finished_price, for_pay, is_realization, is_return, synced_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (srid) DO UPDATE SET
                        last_change=EXCLUDED.last_change, for_pay=EXCLUDED.for_pay,
                        finished_price=EXCLUDED.finished_price, is_return=EXCLUDED.is_return,
                        synced_at=now()
                """, (s.get("srid"), s.get("saleID"), s.get("date"), s.get("lastChangeDate"),
                      s.get("nmId"), s.get("supplierArticle"), s.get("barcode"),
                      s.get("warehouseName"), s.get("regionName"), s.get("totalPrice"),
                      s.get("discountPercent"), s.get("spp"), s.get("priceWithDisc"),
                      s.get("finishedPrice"), s.get("forPay"), s.get("isRealization"),
                      str(s.get("saleID") or "").startswith("R")))
            for o in orders:
                cur.execute("""
                    INSERT INTO wb.order_row (srid, order_dt, last_change, nm_id, vendor_code,
                        warehouse_name, region, total_price, discount_pct, spp,
                        price_with_disc, is_cancel, synced_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (srid) DO UPDATE SET
                        last_change=EXCLUDED.last_change, is_cancel=EXCLUDED.is_cancel, synced_at=now()
                """, (o.get("srid"), o.get("date"), o.get("lastChangeDate"), o.get("nmId"),
                      o.get("supplierArticle"), o.get("warehouseName"), o.get("regionName"),
                      o.get("totalPrice"), o.get("discountPercent"), o.get("spp"),
                      o.get("priceWithDisc"), o.get("isCancel")))

        if sales and not s_note:
            L.set_setting(CURSOR_SALES, max(s.get("lastChangeDate") or "" for s in sales),
                          "последняя забранная продажа")
        if orders and not o_note:
            L.set_setting(CURSOR_ORDERS, max(o.get("lastChangeDate") or "" for o in orders),
                          "последний забранный заказ")

        print("продаж: %d, заказов: %d" % (len(sales), len(orders)))
        for note in (s_note, o_note):
            if note:
                print("   %s" % note)
        if s_note or o_note:
            L.run_end(rid, "partial", rows=len(sales) + len(orders),
                      message=(s_note or o_note)[:300])
            return
        L.run_end(rid, "ok", rows=len(sales) + len(orders))
    except Exception as e:
        L.run_end(rid, "error", message=str(e)[:400])
        raise


if __name__ == "__main__":
    main()
