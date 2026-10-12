#!/usr/bin/env python3
"""Остатки на складах -> wb.stock.

Метод отчётный и трёхшаговый: создать задание, дождаться готовности, скачать.
Все три шага делят одну квоту (~20 мин), поэтому опрашивать статус в цикле
нельзя — это сжигает окно и задание так и не скачивается.

Вместо цикла задание запоминается в `wb.setting.stocks_task_id`, и следующий
прогон (через cron или /обновить) просто забирает готовый отчёт. То есть
остатки приходят со задержкой в один прогон — зато приходят.

Старый /api/v1/supplier/stocks задепрекейчен и отвечает 404 — не использовать.

Запуск:  python pull_stocks.py [--new]
"""
import sys, pathlib, argparse, datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L

FAMILY = "/api/v1/warehouse_remains"      # общий ключ квоты для всех трёх шагов
TASK_KEY = "stocks_task_id"
TASK_AT = "stocks_task_at"
TASK_TTL_H = 12


def create_task():
    j = L.call("analytics", FAMILY,
               params={"locale": "ru", "groupByNm": "true", "groupByBarcode": "false",
                       "groupBySize": "false", "groupBySubject": "false",
                       "groupByBrand": "false", "groupBySa": "true"})
    data = (j or {}).get("data") or {}
    return data.get("taskId") or data.get("task_id") or (j or {}).get("taskId")


def task_status(task_id):
    j = L.call("analytics", "%s/tasks/%s/status" % (FAMILY, task_id), quota_key=FAMILY)
    return ((j or {}).get("data") or {}).get("status")


def download(task_id):
    return L.call("analytics", "%s/tasks/%s/download" % (FAMILY, task_id), quota_key=FAMILY)


def store(rows):
    n = 0
    with L.db() as c, c.cursor() as cur:
        for r in rows:
            nm = r.get("nmId") or r.get("nmID")
            if not nm:
                continue
            whs = r.get("warehouses") or []
            if not whs:
                continue
            for w in whs:
                cur.execute("""
                    INSERT INTO wb.stock (nm_id, warehouse, quantity, in_way_to,
                                          in_way_from, captured_at)
                    VALUES (%s,%s,%s,%s,%s, date_trunc('hour', now()))
                    ON CONFLICT (nm_id, warehouse, captured_at) DO UPDATE SET
                        quantity=EXCLUDED.quantity, in_way_to=EXCLUDED.in_way_to,
                        in_way_from=EXCLUDED.in_way_from
                """, (nm, w.get("warehouseName"), w.get("quantity"),
                      r.get("inWayToClient"), r.get("inWayFromClient")))
                n += 1
    return n


def clear_task():
    L.set_setting(TASK_KEY, "", "задание отчёта остатков выполнено")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", action="store_true", help="создать задание заново")
    a = ap.parse_args()

    rid = L.run_start("pull_stocks.py")
    try:
        task_id = (L.setting(TASK_KEY) or "").strip()
        created = L.setting(TASK_AT)
        stale = False
        if task_id and created:
            try:
                age_h = (datetime.datetime.now()
                         - datetime.datetime.fromisoformat(created)).total_seconds() / 3600
                stale = age_h > TASK_TTL_H
            except Exception:
                stale = True
        if a.new or stale:
            task_id = ""

        # шаг 1: есть незабранное задание — пробуем забрать
        if task_id:
            st = task_status(task_id)
            print("задание %s: статус %s" % (task_id, st))
            if st == "done":
                rows = download(task_id) or []
                n = store(rows)
                clear_task()
                print("остатки записаны: %d строк по %d товарам" % (n, len(rows)))
                tot = L.qd("""SELECT p.vendor_code, sum(s.quantity) AS q FROM wb.stock s
                              JOIN wb.product p USING (nm_id)
                              WHERE s.captured_at=(SELECT max(captured_at) FROM wb.stock)
                              GROUP BY p.vendor_code ORDER BY p.vendor_code""")
                for t in tot:
                    print("   %-16s %s шт." % (t["vendor_code"][:16], int(L.num(t["q"]))))
                L.run_end(rid, "ok", rows=n)
                return
            if st in ("purged", "canceled"):
                print("задание отменено WB — создам новое в следующий прогон")
                clear_task()
                L.run_end(rid, "partial", message="задание отменено")
                return
            print("отчёт ещё готовится — заберу в следующий прогон")
            L.run_end(rid, "partial", message="отчёт готовится")
            return

        # шаг 2: заданий нет — создаём
        task_id = create_task()
        if not task_id:
            raise RuntimeError("WB не вернул taskId")
        L.set_setting(TASK_KEY, task_id, "ожидает скачивания")
        L.set_setting(TASK_AT, datetime.datetime.now().isoformat(timespec="seconds"),
                      "когда создано задание отчёта остатков")
        print("задание %s создано. Отчёт заберу следующим прогоном "
              "(квота метода ~20 мин)." % task_id)
        L.run_end(rid, "ok", rows=0, message="задание создано")
    except L.Throttled as e:
        print("под лимитом: %s" % e)
        L.run_end(rid, "partial", message=str(e)[:300])
    except Exception as e:
        L.run_end(rid, "error", message=str(e)[:400])
        raise


if __name__ == "__main__":
    main()
