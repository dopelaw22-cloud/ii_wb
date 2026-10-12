#!/usr/bin/env python3
"""Финансовый отчёт реализации -> wb.finance_row.

Это единственный источник, который показывает, сколько WB удержал на самом
деле: комиссию, логистику, обратную логистику, хранение, приёмку, штрафы,
эквайринг. Без него «прибыль» остаётся расчётной, а не фактической.

Старый GET /api/v5/supplier/reportDetailByPeriod задепрекейчен (404,
release-notes id=498). Текущий метод — POST на finance-api.

ВАЖНО: лимит метода — один вызов примерно в 8 часов (замерено:
X-Ratelimit-Reset = 27995 с). Поэтому скрипт запускается по расписанию
раз в сутки, а не по кнопке. Если окно закрыто — скрипт не тратит попытку
и сообщает, сколько осталось ждать.

Запуск:  python pull_finance.py [--weeks 8] [--from 2026-08-01 --to 2026-09-30]
"""
import sys, pathlib, argparse, datetime, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L

PAGE = 10000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=8, help="глубина, недель")
    ap.add_argument("--from", dest="dfrom")
    ap.add_argument("--to", dest="dto")
    a = ap.parse_args()

    d_to = datetime.date.fromisoformat(a.dto) if a.dto else datetime.date.today()
    d_from = (datetime.date.fromisoformat(a.dfrom) if a.dfrom
              else d_to - datetime.timedelta(weeks=a.weeks))

    rid = L.run_start("pull_finance.py")
    try:
        left = L.quota_left("finance", "/api/finance/v1/sales-reports/detailed")
        if left > 0:
            print("Финотчёт под лимитом: следующий запрос через %s.\n"
                  "Данные в БД остались прежними." % L.hhmm(left))
            L.run_end(rid, "partial", message="лимит, осталось %d с" % left)
            return

        rows, rrd, total = [], 0, 0
        while True:
            body = {"dateFrom": d_from.isoformat(), "dateTo": d_to.isoformat(),
                    "limit": PAGE, "period": "weekly"}
            if rrd:
                body["rrdId"] = rrd
            chunk = L.call("finance", "/api/finance/v1/sales-reports/detailed", "POST",
                           body=body)
            chunk = chunk or []
            rows += chunk
            total += len(chunk)
            if len(chunk) < PAGE:
                break
            rrd = max(r.get("rrdId") or 0 for r in chunk)
            # вторая страница уже упрётся в лимит — сохраняем что есть
            if L.quota_left("finance", "/api/finance/v1/sales-reports/detailed") > 0:
                print("забрали %d строк, дальше лимит — продолжим в следующий прогон" % total)
                L.set_setting("finance_rrd_cursor", rrd, "докуда дочитан финотчёт")
                break

        n = 0
        with L.db() as c, c.cursor() as cur:
            for r in rows:
                cur.execute("""
                    INSERT INTO wb.finance_row (rrd_id, report_id, date_from, date_to, rr_date,
                        order_dt, sale_dt, nm_id, vendor_code, subject_name, sku, doc_type,
                        seller_oper_name, quantity, retail_price, retail_price_with_disc,
                        retail_amount, for_pay, commission_percent, ppvz_sales_commission,
                        vw, vw_nds, acquiring_fee, delivery_amount, return_amount, delivery_rub,
                        rebill_logistic_cost, paid_storage, paid_acceptance, penalty, deduction,
                        additional_payment, office_name, srid, raw, synced_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (rrd_id) DO UPDATE SET
                        for_pay=EXCLUDED.for_pay, delivery_rub=EXCLUDED.delivery_rub,
                        paid_storage=EXCLUDED.paid_storage, penalty=EXCLUDED.penalty,
                        deduction=EXCLUDED.deduction, synced_at=now()
                """, (r.get("rrdId"), r.get("reportId"), r.get("dateFrom"), r.get("dateTo"),
                      r.get("rrDate"), r.get("orderDt"), r.get("saleDt"), r.get("nmId"),
                      r.get("vendorCode"), r.get("subjectName"), r.get("sku"),
                      r.get("docTypeName"), r.get("sellerOperName"), r.get("quantity"),
                      L.num(r.get("retailPrice")), L.num(r.get("retailPriceWithDisc")),
                      L.num(r.get("retailAmount")), L.num(r.get("forPay")),
                      L.num(r.get("commissionPercent")), L.num(r.get("ppvzSalesCommission")),
                      L.num(r.get("vw")), L.num(r.get("vwNds")), L.num(r.get("acquiringFee")),
                      r.get("deliveryAmount"), r.get("returnAmount"),
                      L.num(r.get("deliveryService")), L.num(r.get("rebillLogisticCost")),
                      L.num(r.get("paidStorage")), L.num(r.get("paidAcceptance")),
                      L.num(r.get("penalty")), L.num(r.get("deduction")),
                      L.num(r.get("additionalPayment")), r.get("officeName"), r.get("srid"),
                      json.dumps(r, ensure_ascii=False)))
                n += 1

        print("финотчёт %s..%s: строк %d" % (d_from, d_to, n))
        if n:
            agg = L.qd("""
                SELECT count(DISTINCT nm_id) товаров, sum(quantity) штук,
                       sum(for_pay) к_перечислению, sum(delivery_rub) логистика,
                       sum(paid_storage) хранение, sum(penalty) штрафы
                FROM wb.finance_row WHERE rr_date >= %s""", (d_from,))
            for k, v in (agg[0] if agg else {}).items():
                print("   %-18s %s" % (k, L.money(v) if k not in ("товаров", "штук") else int(L.num(v))))
        L.run_end(rid, "ok", rows=n)
    except L.Throttled as e:
        print("под лимитом: %s" % e)
        L.run_end(rid, "partial", message=str(e)[:300])
    except Exception as e:
        L.run_end(rid, "error", message=str(e)[:400])
        raise


if __name__ == "__main__":
    main()
