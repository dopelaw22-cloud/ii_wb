#!/usr/bin/env python3
"""Единый сбор данных: прогоняет все сборщики, затем расчёт и анализ.

Методы под лимитом WB не считаются ошибкой: такой шаг помечается «лимит»,
данные в БД остаются от прошлого прогона, а в конце печатается, когда
окно откроется снова. Поэтому скрипт безопасно запускать по расписанию
сколько угодно часто — лишних запросов он не сделает.

Запуск:  python sync.py [--only продажи,реклама] [--quiet]
"""
import sys, pathlib, subprocess, argparse, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L
import views as V

HERE = pathlib.Path(__file__).resolve().parent
PY = "/home/wb/oplog/venv/bin/python"

STEPS = [
    ("товары",     "pull_products.py", []),
    ("себестоим.", "load_costs.py",    []),
    ("тарифы",     "pull_tariffs.py",  []),
    ("затраты рекл.", "pull_advert_spend.py", ["--days", "30"]),
    ("реклама",    "pull_adverts.py",  ["--days", "30"]),
    ("продажи",    "pull_sales.py",    []),
    ("воронка",    "pull_funnel.py",   []),
    ("остатки",    "pull_stocks.py",   []),
    ("финотчёт",   "pull_finance.py",  []),
    ("экономика",  "unit_economics.py", []),
    ("анализ",     "analyze.py",       []),
]


def run_step(script, args, timeout=900):
    path = HERE / script
    if not path.exists():
        return None, "скрипта нет"
    try:
        r = subprocess.run([PY, str(path)] + args, cwd=str(HERE),
                           capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        lines = [l for l in out.split("\n") if l.strip()]
        tail = lines[-1] if lines else ""
        throttled = ("под лимитом" in out) or ("лимит" in tail.lower())
        return ("limit" if throttled else ("ok" if r.returncode == 0 else "err")), tail[:140]
    except subprocess.TimeoutExpired:
        return "err", "не успел за %d мин" % (timeout // 60)
    except Exception as e:
        return "err", str(e)[:140]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="список шагов через запятую")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    only = {s.strip() for s in a.only.split(",")} if a.only else None
    rid = L.run_start("sync.py")
    mark = {"ok": "+", "limit": "~", "err": "!"}
    results = []
    for title, script, args in STEPS:
        if only and title not in only:
            continue
        status, msg = run_step(script, args)
        if status is None:
            continue
        results.append((title, status, msg))
        if not a.quiet:
            print("%s %-11s %s" % (mark.get(status, "?"), title, msg))

    n_ok = sum(1 for _, s, _ in results if s == "ok")
    n_lim = sum(1 for _, s, _ in results if s == "limit")
    n_err = sum(1 for _, s, _ in results if s == "err")

    if not a.quiet:
        print("\nготово: %d обновлено, %d под лимитом, %d с ошибкой" % (n_ok, n_lim, n_err))
        if n_lim:
            print("\n" + V.limits_block())

    L.run_end(rid, "ok" if not n_err else "partial",
              rows=n_ok, message="обновлено %d, лимит %d, ошибок %d" % (n_ok, n_lim, n_err))
    return 0 if not n_err else 1


if __name__ == "__main__":
    sys.exit(main())
