#!/usr/bin/env python3
"""Воронка продаж по карточкам -> wb.funnel.

Отвечает на вопрос «где теряется покупатель»: сколько человек открыли карточку,
сколько положили в корзину, сколько заказали и сколько выкупили.

Лимит метода — один вызов примерно в 24 минуты, поэтому за прогон берём
весь период целиком, а не по дням.

Запуск:  python pull_funnel.py [--days 30] [--start 2026-09-01 --end 2026-09-30]
"""
import sys, pathlib, argparse, datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--start")
    ap.add_argument("--end")
    a = ap.parse_args()

    end = datetime.date.fromisoformat(a.end) if a.end else datetime.date.today()
    start = (datetime.date.fromisoformat(a.start) if a.start
             else end - datetime.timedelta(days=a.days))

    rid = L.run_start("pull_funnel.py")
    try:
        products, offset = [], 0
        while True:
            j = L.call("analytics", "/api/analytics/v3/sales-funnel/products", "POST",
                       body={"selectedPeriod": {"start": start.isoformat(),
                                                "end": end.isoformat()},
                             "orderBy": {"field": "orderSum", "mode": "desc"},
                             "limit": 1000, "offset": offset})
            batch = ((j or {}).get("data") or {}).get("products") or []
            products += batch
            if len(batch) < 1000:
                break
            offset += 1000

        n = 0
        with L.db() as c, c.cursor() as cur:
            for item in products:
                prod = item.get("product") or {}
                st = ((item.get("statistic") or {}).get("selected")) or {}
                conv = st.get("conversions") or {}
                stocks = prod.get("stocks") or {}
                cur.execute("""
                    INSERT INTO wb.funnel (nm_id, period_start, period_end, open_count,
                        cart_count, order_count, order_sum, buyout_count, buyout_sum,
                        cancel_count, cancel_sum, avg_price, add_to_wishlist,
                        conv_cart_pct, conv_order_pct, conv_buyout_pct,
                        product_rating, feedback_rating, stocks_wb, stocks_mp, captured_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (nm_id, period_start, period_end) DO UPDATE SET
                        open_count=EXCLUDED.open_count, cart_count=EXCLUDED.cart_count,
                        order_count=EXCLUDED.order_count, order_sum=EXCLUDED.order_sum,
                        buyout_count=EXCLUDED.buyout_count, buyout_sum=EXCLUDED.buyout_sum,
                        cancel_count=EXCLUDED.cancel_count, cancel_sum=EXCLUDED.cancel_sum,
                        avg_price=EXCLUDED.avg_price, add_to_wishlist=EXCLUDED.add_to_wishlist,
                        conv_cart_pct=EXCLUDED.conv_cart_pct,
                        conv_order_pct=EXCLUDED.conv_order_pct,
                        conv_buyout_pct=EXCLUDED.conv_buyout_pct,
                        product_rating=EXCLUDED.product_rating,
                        feedback_rating=EXCLUDED.feedback_rating,
                        stocks_wb=EXCLUDED.stocks_wb, stocks_mp=EXCLUDED.stocks_mp,
                        captured_at=now()
                """, (prod.get("nmId"), start, end, st.get("openCount"), st.get("cartCount"),
                      st.get("orderCount"), st.get("orderSum"), st.get("buyoutCount"),
                      st.get("buyoutSum"), st.get("cancelCount"), st.get("cancelSum"),
                      st.get("avgPrice"), st.get("addToWishlist"),
                      conv.get("addToCartPercent"), conv.get("cartToOrderPercent"),
                      conv.get("buyoutPercent"), prod.get("productRating"),
                      prod.get("feedbackRating"), stocks.get("wb"), stocks.get("mp")))
                n += 1

        print("воронка за %s..%s: карточек %d" % (start, end, n))
        for item in products[:14]:
            prod = item.get("product") or {}
            st = ((item.get("statistic") or {}).get("selected")) or {}
            cv = st.get("conversions") or {}
            print("   %-16s переходы %6s корзина %5s заказы %5s выкупы %5s  в корзину %s%% выкуп %s%%"
                  % ((prod.get("vendorCode") or "")[:16], st.get("openCount"),
                     st.get("cartCount"), st.get("orderCount"), st.get("buyoutCount"),
                     cv.get("addToCartPercent"), cv.get("buyoutPercent")))
        L.run_end(rid, "ok", rows=n)
    except L.Throttled as e:
        print("под лимитом: %s" % e)
        L.run_end(rid, "partial", message=str(e)[:300])
    except Exception as e:
        L.run_end(rid, "error", message=str(e)[:400])
        raise


if __name__ == "__main__":
    main()
