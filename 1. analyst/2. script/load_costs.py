#!/usr/bin/env python3
"""Себестоимость из Google-таблицы владельца -> wb.cost.

Источник: лист, заданный COST_SHEET_ID/COST_SHEET_GID в .env.
Колонка COST_SHEET_COL (по умолчанию AJ, «Итого закуп. цена»), название товара — колонка A.
Названия в таблице (D100Black+) приводятся к артикулам WB (d100blackplus).

Запуск:  python load_costs.py [--dry]
"""
import sys, csv, io, re, pathlib, argparse
import requests
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L

NAME_COL = 0


def col_index(name):
    """AJ -> 35"""
    n = 0
    for ch in name.strip().upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def normalize(name):
    """«D100Black+» -> «d100blackplus», «D125Gray» -> «d125gray»."""
    s = (name or "").strip().lower()
    s = s.replace(" ", "")
    if s.endswith("+"):
        s = s[:-1] + "plus"
    return s


def fetch_sheet(sheet_id, gid):
    url = ("https://docs.google.com/spreadsheets/d/%s/export?format=csv&gid=%s"
           % (sheet_id, gid))
    r = requests.get(url, timeout=60)
    if r.status_code != 200:
        raise RuntimeError("таблица недоступна: HTTP %s. Открыт ли доступ по ссылке?" % r.status_code)
    if r.text.lstrip().startswith("<"):
        raise RuntimeError("вместо CSV пришёл HTML — таблица закрыта, нужен доступ «по ссылке»")
    return list(csv.reader(io.StringIO(r.text)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="показать, но не записывать")
    a = ap.parse_args()

    sheet_id = L.ENV.get("COST_SHEET_ID")
    gid = L.ENV.get("COST_SHEET_GID", "0")
    ci = col_index(L.ENV.get("COST_SHEET_COL", "AJ"))
    if not sheet_id:
        sys.exit("COST_SHEET_ID не задан в .env")

    rid = L.run_start("load_costs.py")
    try:
        rows = fetch_sheet(sheet_id, gid)
        # артикулы WB, под которые ищем себестоимость
        products = {vc.lower(): nm for nm, vc in
                    L.q("SELECT nm_id, vendor_code FROM wb.product")}
        if not products:
            sys.exit("справочник wb.product пуст — сначала запустите pull_products.py")

        found, missing = {}, []
        for r in rows:
            name = (r[NAME_COL] if len(r) > NAME_COL else "").strip()
            raw = (r[ci] if len(r) > ci else "").strip()
            if not name or not raw or raw.startswith("#"):
                continue
            key = normalize(name)
            val = L.num(raw, default=None)
            if val is None or val <= 0:
                continue
            if key in products:
                found[key] = val

        for vc in products:
            if vc not in found:
                missing.append(vc)

        print("колонка %s, строк в таблице: %d" % (L.ENV.get("COST_SHEET_COL", "AJ"), len(rows)))
        print("\nсопоставлено %d из %d артикулов:" % (len(found), len(products)))
        for vc in sorted(found):
            print("   %-18s %10s   nmID %s" % (vc, L.money(found[vc]), products[vc]))
        if missing:
            print("\nНЕ НАЙДЕНО в таблице (%d): %s" % (len(missing), ", ".join(sorted(missing))))

        if a.dry:
            print("\n--dry: в БД ничего не записано")
            L.run_end(rid, "ok", rows=0, message="dry-run")
            return

        with L.db() as c, c.cursor() as cur:
            for vc, val in found.items():
                cur.execute("""
                    INSERT INTO wb.cost (vendor_code, cost_rub, source, valid_from)
                    VALUES (%s,%s,%s, current_date)
                    ON CONFLICT (vendor_code, valid_from) DO UPDATE SET
                        cost_rub=EXCLUDED.cost_rub, source=EXCLUDED.source
                """, (vc, val, "google-sheet:%s" % L.ENV.get("COST_SHEET_COL", "AJ")))
        print("\nзаписано в wb.cost: %d" % len(found))
        if missing:
            L.event("себестоимость: не найдено %d артикулов" % len(missing), severity="warning")
        L.run_end(rid, "ok", rows=len(found))
    except Exception as e:
        L.run_end(rid, "error", message=str(e)[:400])
        raise


if __name__ == "__main__":
    main()
