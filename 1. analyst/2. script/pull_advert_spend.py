#!/usr/bin/env python3
"""История затрат на рекламу -> wb.advert_spend, плюс названия кампаний.

Зачем отдельно от pull_adverts.py: /adv/v3/fullstats сидит под часовым окном
и отдаёт показы с кликами, а /adv/v1/upd почти не ограничен и отдаёт списания
с рекламного счёта. То есть на вопрос «сколько я трачу» можно ответить всегда,
даже когда подробная статистика недоступна.

Бонус: в поле campName у этого магазина зашит артикул — «d100grayplus / поиск /
stop». Это позволяет разложить затраты по товарам без fullstats и заодно
заполнить названия в wb.advert, которые отдаются по часовой квоте.

Запуск:  python pull_advert_spend.py [--days 30]
"""
import sys, pathlib, argparse, datetime, re
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L


def parse_article(camp_name, known):
    """Из «d100grayplus / поиск / stop» достаём артикул, если он нам известен."""
    if not camp_name:
        return None
    for part in re.split(r"[\s/,;|]+", camp_name.lower()):
        part = part.strip()
        if part in known:
            return part
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()

    d_to = datetime.date.today()
    d_from = d_to - datetime.timedelta(days=a.days)

    rid = L.run_start("pull_advert_spend.py")
    try:
        rows = L.call("advert", "/adv/v1/upd",
                      params={"from": d_from.isoformat(), "to": d_to.isoformat()}) or []

        known = {vc.lower() for (vc,) in L.q("SELECT vendor_code FROM wb.product")}
        n, names = 0, {}
        with L.db() as c, c.cursor() as cur:
            for r in rows:
                aid = r.get("advertId")
                if not aid:
                    continue
                cur.execute("""
                    INSERT INTO wb.advert_spend (advert_id, upd_time, upd_sum, upd_num,
                        payment_type, camp_name, advert_type, advert_status, synced_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (advert_id, upd_time, upd_sum) DO UPDATE SET
                        camp_name=EXCLUDED.camp_name, advert_status=EXCLUDED.advert_status,
                        synced_at=now()
                """, (aid, r.get("updTime"), L.num(r.get("updSum")), r.get("updNum"),
                      r.get("paymentType"), r.get("campName"), r.get("advertType"),
                      r.get("advertStatus")))
                n += 1
                if r.get("campName"):
                    names[aid] = r["campName"]

            # названия кампаний приходят здесь же — заполняем, если их ещё нет
            filled = 0
            for aid, nm in names.items():
                cur.execute("""UPDATE wb.advert SET name=%s
                               WHERE advert_id=%s AND (name IS NULL OR name='')""", (nm, aid))
                filled += cur.rowcount

        # баланс рекламного счёта — полезно знать, не кончились ли деньги
        try:
            bal = L.call("advert", "/adv/v1/balance") or {}
            L.set_setting("advert_balance", L.num(bal.get("net")), "счёт рекламного кабинета, ₽")
            L.set_setting("advert_bonus", L.num(bal.get("balance")), "бонусы рекламного кабинета, ₽")
            print("счёт рекламы: %s, бонусы: %s"
                  % (L.money(bal.get("net")), L.money(bal.get("balance"))))
        except Exception as e:
            print("баланс не получен: %s" % str(e)[:120])

        print("списаний за %s..%s: %d, названий заполнено: %d" % (d_from, d_to, n, filled))

        agg = L.qd("""
            SELECT camp_name, count(*) списаний, sum(upd_sum)::int затраты
            FROM wb.advert_spend WHERE upd_time::date >= %s
            GROUP BY camp_name ORDER BY sum(upd_sum) DESC LIMIT 12""", (d_from,))
        for r in agg:
            art = parse_article(r["camp_name"], known)
            print("   %-44s %8s ₽   %s" % ((r["camp_name"] or "")[:44], r["затраты"],
                                           art or "товар не распознан"))
        L.run_end(rid, "ok", rows=n)
    except L.Throttled as e:
        print("под лимитом: %s" % e)
        L.run_end(rid, "partial", message=str(e)[:300])
    except Exception as e:
        L.run_end(rid, "error", message=str(e)[:400])
        raise


if __name__ == "__main__":
    main()
