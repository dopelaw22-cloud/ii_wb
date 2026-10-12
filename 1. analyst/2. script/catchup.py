#!/usr/bin/env python3
"""Догоняющий сбор: ждёт открытия окон лимитов и забирает всё, что ещё не собрано.

Нужен для первого наполнения БД. Обычный sync.py пропускает методы под лимитом
и завершается, а этот терпеливо ждёт: проверяет окна раз в минуту и запускает
шаг, как только он становится доступен. Выходит, когда всё собрано или истёк
--max-hours.

Запускать в фоне:
    nohup /home/wb/oplog/venv/bin/python catchup.py --max-hours 9 \
        >> "../3. work/catchup.log" 2>&1 &
"""
import sys, pathlib, subprocess, time, datetime, argparse
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L

HERE = pathlib.Path(__file__).resolve().parent
PY = "/home/wb/oplog/venv/bin/python"

# шаг: (название, скрипт, аргументы, (host, path) для проверки окна, проверка «уже есть»)
PLAN = [
    ("тарифы комиссии", "pull_tariffs.py", [], ("common", "/api/v1/tariffs/commission"),
     "SELECT count(*) FROM wb.tariff WHERE kind='commission'"),
    ("тарифы логистики", "pull_tariffs.py", [], ("common", "/api/v1/tariffs/box"),
     "SELECT count(*) FROM wb.tariff WHERE kind='box'"),
    ("затраты рекламы", "pull_advert_spend.py", ["--days", "30"],
     ("advert", "/adv/v1/upd"), "SELECT count(*) FROM wb.advert_spend"),
    ("реклама", "pull_adverts.py", ["--days", "30"], ("advert", "/adv/v3/fullstats"),
     "SELECT count(*) FROM wb.advert_stat"),
    ("воронка", "pull_funnel.py", ["--days", "30"],
     ("analytics", "/api/analytics/v3/sales-funnel/products"),
     "SELECT count(*) FROM wb.funnel"),
    ("продажи", "pull_sales.py", ["--full"], ("stats", "/api/v1/supplier/sales"),
     "SELECT count(*) FROM wb.sale"),
    ("остатки", "pull_stocks.py", [], ("analytics", "/api/v1/warehouse_remains"),
     "SELECT count(*) FROM wb.stock"),
    ("финотчёт", "pull_finance.py", ["--weeks", "8"],
     ("finance", "/api/finance/v1/sales-reports/detailed"),
     "SELECT count(*) FROM wb.finance_row"),
]


def log(msg):
    print("%s  %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def have(sql):
    try:
        r = L.q(sql, fetch="one")
        return bool(r and r[0])
    except Exception:
        return False


def run(script, args):
    r = subprocess.run([PY, str(HERE / script)] + args, cwd=str(HERE),
                       capture_output=True, text=True, timeout=1800)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    lines = [l for l in out.split("\n") if l.strip()]
    return r.returncode, (lines[-1] if lines else ""), out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-hours", type=float, default=9.0)
    ap.add_argument("--tick", type=int, default=60, help="пауза между проверками, с")
    a = ap.parse_args()

    deadline = time.time() + a.max_hours * 3600
    done = set()
    # остатки собираются в два прогона: создать задание, потом забрать
    stocks_rounds = 0

    log("догоняющий сбор начат, лимит %.1f ч" % a.max_hours)
    while time.time() < deadline:
        pending = []
        for name, script, args, (host, path), check in PLAN:
            if name in done:
                continue
            if name == "остатки":
                if have(check) and stocks_rounds >= 2:
                    done.add(name); continue
            elif have(check):
                log("%s — уже собрано, пропускаю" % name)
                done.add(name); continue

            left = L.quota_left(host, path)
            if left > 0:
                pending.append((name, left))
                continue

            log("%s — окно открыто, запускаю" % name)
            try:
                code, tail, _ = run(script, args)
            except subprocess.TimeoutExpired:
                log("%s — не успел за 30 мин" % name); continue
            log("%s -> %s" % (name, tail[:160]))
            if name == "остатки":
                stocks_rounds += 1
                if stocks_rounds >= 2 and have(check):
                    done.add(name)
            elif have(check):
                done.add(name)

        if len(done) == len(PLAN):
            log("всё собрано")
            break
        if pending:
            nearest = min(p[1] for p in pending)
            log("ждём: %s" % ", ".join("%s через %s" % (n, L.hhmm(s))
                                       for n, s in sorted(pending, key=lambda x: x[1])))
            time.sleep(min(max(nearest + 2, 30), max(a.tick, 30)))
        else:
            time.sleep(a.tick)

    log("пересчёт экономики и анализ")
    for script in ("unit_economics.py", "analyze.py"):
        code, tail, _ = run(script, [])
        log("%s -> %s" % (script, tail[:160]))
    log("готово. Собрано: %s" % (", ".join(sorted(done)) or "ничего"))
    missing = [n for n, *_ in PLAN if n not in done]
    if missing:
        log("осталось под лимитом: %s" % ", ".join(missing))


if __name__ == "__main__":
    main()
