#!/usr/bin/env python3
"""Тарифы WB: комиссия по предметам и логистика/хранение по складам -> wb.tariff.

Храним снимком на дату: тарифы меняются, и прошлые расчёты должны оставаться
объяснимыми. Каждый блок тянется независимо — если один под лимитом, второй
всё равно обновится, а скрипт скажет, через сколько вернуться.

Запуск:  python pull_tariffs.py
"""
import sys, pathlib, json, datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L


def save(kind, payload):
    with L.db() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO wb.tariff (kind, captured_at, payload)
                       VALUES (%s, date_trunc('day', now()), %s)
                       ON CONFLICT (kind, captured_at) DO UPDATE SET payload=EXCLUDED.payload""",
                    (kind, json.dumps(payload, ensure_ascii=False)))


def get_commission():
    comm = L.call("common", "/api/v1/tariffs/commission", params={"locale": "ru"}, wait=True)
    report = comm.get("report") or []
    subj = {s[0] for s in L.q(
        "SELECT DISTINCT subject_id FROM wb.product WHERE subject_id IS NOT NULL")}
    mine = [r for r in report if r.get("subjectID") in subj]
    save("commission", mine)
    print("комиссии по нашим предметам: %d из %d в справочнике" % (len(mine), len(report)))
    for r in mine:
        print("   предмет %s «%s»: FBO %s%%, FBS %s%%, доля хранения %s%%"
              % (r.get("subjectID"), r.get("subjectName"), r.get("kgvpSupplier"),
                 r.get("kgvpMarketplace"), r.get("paidStorageKgvp")))
    return len(mine)


def get_box():
    box = L.call("common", "/api/v1/tariffs/box",
                 params={"date": datetime.date.today().isoformat()})
    wh = (((box or {}).get("response") or {}).get("data") or {}).get("warehouseList") or []
    save("box", wh)
    print("\nсклады в тарифах логистики: %d" % len(wh))
    for w in wh:
        if "Коледино" in (w.get("warehouseName") or "") or "Россия" in (w.get("geoName") or ""):
            print("   %-32s логистика: база %s / литр %s, хранение: база %s / литр %s"
                  % ((w.get("warehouseName") or "")[:32], w.get("boxDeliveryBase"),
                     w.get("boxDeliveryLiter"), w.get("boxStorageBase"), w.get("boxStorageLiter")))
    return len(wh)


def main():
    rid = L.run_start("pull_tariffs.py")
    n, errs = 0, []
    for name, fn in (("комиссия", get_commission), ("логистика/хранение", get_box)):
        try:
            n += fn()
        except L.Throttled as e:
            errs.append("%s: %s" % (name, e))
            print("   %s под лимитом — %s" % (name, e))
        except Exception as e:
            errs.append("%s: %s" % (name, str(e)[:160]))
            print("   %s: ошибка %s" % (name, str(e)[:160]))
    L.run_end(rid, "ok" if not errs else "partial", rows=n,
              message="; ".join(errs)[:400] or None)
    if errs:
        print("\nНе обновлено: %d блок(а). Повторите позже — данные в БД остались прежними." % len(errs))


if __name__ == "__main__":
    main()
