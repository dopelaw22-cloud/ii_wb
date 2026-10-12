#!/usr/bin/env python3
"""Рекламные кампании и их статистика -> wb.advert, wb.advert_stat.

Три шага:
  1. /adv/v1/promotion/count   — список всех ID кампаний с типом и статусом
  2. /api/advert/v2/adverts    — названия, ставки, список товаров (по 50 ID)
  3. /adv/v3/fullstats         — показы/клики/затраты/заказы по дням и товарам

Статистика раскладывается до уровня (кампания, день, артикул): затраты по
каждому товару внутри кампании видны отдельно, а nm_id=0 — агрегат по кампании.

Запуск:  python pull_adverts.py [--days 30] [--only-active]
"""
import sys, pathlib, argparse, datetime, time, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L

TYPE_NAME = {4: "каталог", 5: "карточка товара", 6: "поиск",
             7: "рекомендации", 8: "единая ставка (устар.)", 9: "АРК"}
STATUS_NAME = {-1: "удалена", 4: "готова к запуску", 7: "завершена",
               8: "отказана", 9: "активна", 11: "пауза"}


def all_campaigns():
    """Список кампаний. Если окно /promotion/count закрыто — берём из БД.

    Список кампаний меняется редко, а статистика по ним — каждый день.
    Ждать часовое окно только ради перечня ID бессмысленно, когда он уже
    лежит в wb.advert с прошлого прогона.
    """
    try:
        j = L.call("advert", "/adv/v1/promotion/count")
    except L.Throttled as e:
        cached = L.qd("""SELECT advert_id, type, status, change_time
                         FROM wb.advert ORDER BY advert_id""")
        if cached:
            print("перечень кампаний под лимитом (%s) — беру %d из БД"
                  % (L.hhmm(e.left), len(cached)))
            return [{"advert_id": c["advert_id"], "type": c["type"],
                     "status": c["status"], "change_time": c["change_time"]}
                    for c in cached]
        raise
    out = []
    for g in (j.get("adverts") or []):
        for a in (g.get("advert_list") or []):
            out.append({"advert_id": a.get("advertId"), "type": g.get("type"),
                        "status": g.get("status"), "change_time": a.get("changeTime")})
    return out


def campaign_info(ids):
    """Детали кампаний: имя, товары, ставки. По 50 ID за вызов.

    Метод под часовой квотой, а статистика — под минутной. Поэтому отсутствие
    названий не должно ронять сбор: без имён кампании всё равно сохранятся
    по ID, а статистика соберётся. Имена подтянутся следующим прогоном.
    """
    info = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            j = L.call("advert", "/api/advert/v2/adverts",
                       params={"ids": ",".join(str(x) for x in chunk)})
        except L.Throttled as e:
            print("   названия кампаний под лимитом (%s) — соберу статистику без них"
                  % L.hhmm(e.left))
            break
        except Exception as e:
            print("   названия кампаний: %s" % str(e)[:140])
            break
        for a in ((j or {}).get("adverts") or []):
            st = a.get("settings") or {}
            nms = [n.get("nm_id") for n in (a.get("nm_settings") or []) if n.get("nm_id")]
            ts = a.get("timestamps") or {}
            info[a.get("id")] = {
                "name": st.get("name"), "nm_ids": nms, "bid_type": a.get("bid_type"),
                "payment_type": st.get("payment_type"), "created": ts.get("created"),
                "updated": ts.get("updated"), "raw": a,
            }
        time.sleep(0.5)
    return info


def stats(ids, date_from, date_to):
    """fullstats по 50 кампаний за вызов. Лимит — 1 вызов в минуту."""
    rows = []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            j = L.call("advert", "/adv/v3/fullstats", wait=True,
                       params={"ids": ",".join(str(x) for x in chunk),
                               "beginDate": date_from, "endDate": date_to})
        except RuntimeError as e:
            # кампания без статистики за период отдаёт 400 — это нормально
            print("   пропуск блока %d-%d: %s" % (i, i + len(chunk), str(e)[:120]))
            continue
        for c in (j or []):
            aid = c.get("advertId")
            for day in (c.get("days") or []):
                d = (day.get("date") or "")[:10]
                # агрегат по кампании за день
                rows.append((aid, d, 0, day.get("views"), day.get("clicks"), day.get("ctr"),
                             day.get("cpc"), day.get("sum"), day.get("atbs"), day.get("orders"),
                             day.get("shks"), day.get("sum_price"), day.get("cr")))
                # разбивка по товарам: суммируем по платформам
                per_nm = {}
                for app in (day.get("apps") or []):
                    for nm in (app.get("nms") or []):
                        k = nm.get("nmId")
                        a = per_nm.setdefault(k, dict(views=0, clicks=0, sum=0.0, atbs=0,
                                                      orders=0, shks=0, sum_price=0.0))
                        a["views"] += nm.get("views") or 0
                        a["clicks"] += nm.get("clicks") or 0
                        a["sum"] += L.num(nm.get("sum"))
                        a["atbs"] += nm.get("atbs") or 0
                        a["orders"] += nm.get("orders") or 0
                        a["shks"] += nm.get("shks") or 0
                        a["sum_price"] += L.num(nm.get("sum_price"))
                for k, a in per_nm.items():
                    ctr = (a["clicks"] / a["views"] * 100) if a["views"] else 0
                    cpc = (a["sum"] / a["clicks"]) if a["clicks"] else 0
                    cr = (a["orders"] / a["clicks"] * 100) if a["clicks"] else 0
                    rows.append((aid, d, k, a["views"], a["clicks"], ctr, cpc, a["sum"],
                                 a["atbs"], a["orders"], a["shks"], a["sum_price"], cr))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="глубина статистики, дней")
    ap.add_argument("--only-active", action="store_true",
                    help="статистику тянуть только по активным и приостановленным")
    a = ap.parse_args()

    rid = L.run_start("pull_adverts.py")
    try:
        camps = all_campaigns()
        ids = [c["advert_id"] for c in camps]
        print("кампаний всего: %d" % len(ids))

        info = campaign_info(ids)
        with L.db() as c, c.cursor() as cur:
            for k in camps:
                i = info.get(k["advert_id"], {})
                cur.execute("""
                    INSERT INTO wb.advert (advert_id, name, type, status, create_time,
                                           change_time, nm_ids, raw, synced_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (advert_id) DO UPDATE SET
                        -- если имена были под лимитом, не затираем уже известное
                        name=COALESCE(EXCLUDED.name, wb.advert.name),
                        status=EXCLUDED.status, change_time=EXCLUDED.change_time,
                        nm_ids=CASE WHEN EXCLUDED.nm_ids = '{}' THEN wb.advert.nm_ids
                                    ELSE EXCLUDED.nm_ids END,
                        raw=CASE WHEN EXCLUDED.raw = '{}'::jsonb THEN wb.advert.raw
                                 ELSE EXCLUDED.raw END,
                        synced_at=now()
                """, (k["advert_id"], i.get("name"), k["type"], k["status"],
                      i.get("created"), k["change_time"], i.get("nm_ids") or [],
                      json.dumps(i.get("raw") or {}, ensure_ascii=False)))

        stat_ids = [c["advert_id"] for c in camps
                    if (not a.only_active) or c["status"] in (9, 11)]
        d_to = datetime.date.today()
        d_from = d_to - datetime.timedelta(days=a.days)
        print("статистика по %d кампаниям за %s..%s" % (len(stat_ids), d_from, d_to))
        rows = stats(stat_ids, d_from.isoformat(), d_to.isoformat())

        with L.db() as c, c.cursor() as cur:
            for r in rows:
                cur.execute("""
                    INSERT INTO wb.advert_stat (advert_id, stat_date, nm_id, views, clicks,
                        ctr, cpc, sum_rub, atbs, orders, shks, sum_price, cr, synced_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (advert_id, stat_date, nm_id) DO UPDATE SET
                        views=EXCLUDED.views, clicks=EXCLUDED.clicks, ctr=EXCLUDED.ctr,
                        cpc=EXCLUDED.cpc, sum_rub=EXCLUDED.sum_rub, atbs=EXCLUDED.atbs,
                        orders=EXCLUDED.orders, shks=EXCLUDED.shks,
                        sum_price=EXCLUDED.sum_price, cr=EXCLUDED.cr, synced_at=now()
                """, r)

        print("строк статистики: %d" % len(rows))
        L.run_end(rid, "ok", rows=len(rows))
    except L.Throttled as e:
        print("под лимитом: %s" % e)
        L.run_end(rid, "partial", message=str(e)[:300])
    except Exception as e:
        L.run_end(rid, "error", message=str(e)[:400])
        raise


if __name__ == "__main__":
    main()
