#!/usr/bin/env python3
"""Справочник товаров и текущие цены -> wb.product, wb.price."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L


def pull_cards():
    cards, cur = [], {"limit": 100}
    while True:
        j = L.call("content", "/content/v2/get/cards/list", "POST",
                   body={"settings": {"cursor": cur, "filter": {"withPhoto": -1}}})
        batch = j.get("cards") or []
        cards += batch
        c = j.get("cursor") or {}
        if len(batch) < 100:
            break
        cur = {"limit": 100, "updatedAt": c.get("updatedAt"), "nmID": c.get("nmID")}
    return cards


def pull_prices():
    out, offset = [], 0
    while True:
        j = L.call("prices", "/api/v2/list/goods/filter", params={"limit": 1000, "offset": offset})
        goods = ((j.get("data") or {}).get("listGoods")) or []
        out += goods
        if len(goods) < 1000:
            break
        offset += 1000
    return out


def main():
    rid = L.run_start("pull_products.py")
    try:
        cards = pull_cards()
        with L.db() as c, c.cursor() as cur:
            for k in cards:
                sizes = k.get("sizes") or [{}]
                s0 = sizes[0]
                skus = s0.get("skus") or []
                d = k.get("dimensions") or {}
                cur.execute("""
                    INSERT INTO wb.product (nm_id, vendor_code, imt_id, subject_id, subject_name,
                        title, brand, chrt_id, barcode, photos, chars, desc_len,
                        width, height, length, weight_brutto, created_at, updated_at, synced_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (nm_id) DO UPDATE SET
                        vendor_code=EXCLUDED.vendor_code, subject_name=EXCLUDED.subject_name,
                        title=EXCLUDED.title, brand=EXCLUDED.brand, chrt_id=EXCLUDED.chrt_id,
                        barcode=EXCLUDED.barcode, photos=EXCLUDED.photos, chars=EXCLUDED.chars,
                        desc_len=EXCLUDED.desc_len, width=EXCLUDED.width, height=EXCLUDED.height,
                        length=EXCLUDED.length, weight_brutto=EXCLUDED.weight_brutto,
                        updated_at=EXCLUDED.updated_at, synced_at=now()
                """, (k.get("nmID"), k.get("vendorCode"), k.get("imtID"), k.get("subjectID"),
                      k.get("subjectName"), k.get("title"), k.get("brand"), s0.get("chrtID"),
                      skus[0] if skus else None, len(k.get("photos") or []),
                      len(k.get("characteristics") or []), len(k.get("description") or ""),
                      d.get("width"), d.get("height"), d.get("length"), d.get("weightBrutto"),
                      k.get("createdAt"), k.get("updatedAt")))

        goods = pull_prices()
        n_price = 0
        with L.db() as c, c.cursor() as cur:
            for g in goods:
                for s in (g.get("sizes") or []):
                    cur.execute("""
                        INSERT INTO wb.price (nm_id, size_id, price, discounted_price,
                                              club_price, discount, captured_at)
                        VALUES (%s,%s,%s,%s,%s,%s, date_trunc('hour', now()))
                        ON CONFLICT (nm_id, size_id, captured_at) DO UPDATE SET
                            price=EXCLUDED.price, discounted_price=EXCLUDED.discounted_price,
                            club_price=EXCLUDED.club_price, discount=EXCLUDED.discount
                    """, (g.get("nmID"), s.get("sizeID"), s.get("price"),
                          s.get("discountedPrice"), s.get("clubDiscountedPrice"), g.get("discount")))
                    n_price += 1

        print("товаров: %d, цен записано: %d" % (len(cards), n_price))
        L.run_end(rid, "ok", rows=len(cards))
    except Exception as e:
        L.run_end(rid, "error", message=str(e)[:400])
        raise


if __name__ == "__main__":
    main()
