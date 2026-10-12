#!/usr/bin/env python3
"""Юнит-экономика по каждому товару -> wb.unit_econ.

Два независимых основания расчёта:

  plan — «по тарифам»: цена из прайса, комиссия и логистика по действующим
         тарифам WB, себестоимость из таблицы владельца. Доступен всегда,
         отвечает на вопрос «сколько должно оставаться с продажи».

  fact — «по финотчёту»: берутся реальные удержания WB из wb.finance_row
         (комиссия, логистика, хранение, приёмка, штрафы, эквайринг).
         Отвечает на вопрос «сколько осталось на самом деле».

Расхождение plan и fact — само по себе находка: значит WB удержал больше,
чем предполагает тариф (штрафы, обратная логистика, хранение).

Формула plan для FBO (склад WB), на одну проданную единицу:

    выручка            = цена со скидкой продавца
  − комиссия           = выручка × кВВ%
  − логистика          = тариф(объём) / доля_выкупа
                         + обратная логистика × (1/доля_выкупа − 1)
  − хранение           = тариф_хранения(объём) × дней_оборачиваемости
  − реклама            = затраты на рекламу / проданных единиц
  − себестоимость
  − налог
  = прибыль

Логистика делится на долю выкупа потому, что WB берёт доставку за каждую
отправку, включая те, которые покупатель не выкупил.

Запуск:  python unit_economics.py [--days 30] [--basis plan|fact|both] [--show]
"""
import sys, pathlib, argparse, json, datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L

DEFAULT_RETURN_FEE = 50.0       # обратная логистика за невыкуп, ₽
DEFAULT_TURNOVER_DAYS = 30      # если оборачиваемость не посчитана
DEFAULT_BUYOUT = 0.90           # если нет данных воронки


# ---------------------------------------------------------------- тарифы

def latest_tariff(kind):
    r = L.q("""SELECT payload FROM wb.tariff WHERE kind=%s
               ORDER BY captured_at DESC LIMIT 1""", (kind,), fetch="one")
    return r[0] if r else None


def commission_for(subject_id, tariffs, scheme="fbo"):
    """кВВ в процентах по предмету из справочника тарифов (базовая ставка)."""
    if not tariffs:
        return None
    for t in tariffs:
        if t.get("subjectID") == subject_id:
            return L.num(t.get("kgvpSupplier") if scheme == "fbo" else t.get("kgvpMarketplace"))
    return None


def effective_commission(days=90):
    """Фактический кВВ по товару, выведенный из продаж: 1 − forPay/цена.

    Тариф из справочника — базовая ставка. WB снижает её за рейтинг продавца
    и участие в акциях (в финотчёте это поля supRatingUp и isKgvpV2), поэтому
    по предмету «Вентиляторы» справочник даёт 45%, а удерживается около 35%.
    Расчёт по справочной ставке завысил бы расходы и показал убыток там,
    где его нет, — поэтому если есть продажи, берём ставку из них.
    """
    rows = L.qd("""
        SELECT nm_id,
               sum(price_with_disc) AS base,
               sum(for_pay)         AS for_pay,
               count(*)             AS n
        FROM wb.sale
        WHERE sale_dt >= current_date - %s::int
          AND coalesce(is_return,false) = false
          AND price_with_disc > 0 AND for_pay > 0
        GROUP BY nm_id HAVING count(*) >= 3
    """, (days,))
    out = {}
    for r in rows:
        base, pay = L.num(r["base"]), L.num(r["for_pay"])
        if base > 0:
            pct_val = (1.0 - pay / base) * 100.0
            if 0 < pct_val < 70:          # за этими границами данные явно битые
                out[r["nm_id"]] = (pct_val, int(L.num(r["n"])))
    return out


def box_tariff(tariffs, geo="Россия", warehouse=None):
    """Тариф логистики и хранения — строго по стране продаж.

    Проверено на живом ответе 2026-10-07: по этому аккаунту WB отдаёт в
    /api/v1/tariffs/box **ни одного российского FBO-склада**. Для России там
    только два собственных склада продавца с FBS-тарифом (boxDeliveryMarketplace*),
    а поля boxDelivery* заполнены лишь у складов СНГ — Казахстан, Армения,
    Грузия, Узбекистан, Беларусь.

    Поэтому брать медиану по всем складам нельзя: получится средний тариф
    зарубежной логистики, к российским продажам не относящийся. Фильтруем по
    geoName и берём то, что для этой страны реально задано: сначала FBO, при
    его отсутствии — FBS, с пометкой, какая схема использована.

    Для FBO в России тарифа в этом ответе нет вовсе — фактическая логистика
    приходит из финотчёта (поле deliveryService), и расчёт fact её учитывает.
    """
    if not tariffs:
        return None

    def pick(w, scheme):
        pre = "boxDelivery" if scheme == "fbo" else "boxDeliveryMarketplace"
        return {
            "name": w.get("warehouseName"),
            "geo": w.get("geoName"),
            "scheme": scheme,
            "delivery_base": L.num(w.get(pre + "Base"), None),
            "delivery_liter": L.num(w.get(pre + "Liter"), None),
            "storage_base": L.num(w.get("boxStorageBase"), None),
            "storage_liter": L.num(w.get("boxStorageLiter"), None),
        }

    pool = [w for w in tariffs
            if not geo or (w.get("geoName") or "").lower() == geo.lower()]

    # Отбрасываем склады сверхгабаритных товаров: у «Свой склад СГТ РФ» тариф
    # 46+14 против 78,2+23,8 у обычного, и если взять его по ошибке, логистика
    # окажется занижена вдвое. Вытяжки 24×11×24 см к СГТ не относятся.
    def is_oversize(w):
        n = (w.get("warehouseName") or "").upper()
        return "СГТ" in n or "КГТ" in n or "KGT" in n
    normal = [w for w in pool if not is_oversize(w)]
    if normal:
        pool = normal
    if not pool:
        return None

    if warehouse:
        for w in pool:
            if warehouse.lower() in (w.get("warehouseName") or "").lower():
                for scheme in ("fbo", "fbs"):
                    p = pick(w, scheme)
                    if p["delivery_base"] is not None:
                        p["source"] = "склад «%s», %s" % (p["name"], scheme.upper())
                        return p

    # сначала FBO, потом FBS — и всегда самый дешёвый вариант по стране,
    # чтобы расчёт не завышал расходы без оснований
    for scheme in ("fbo", "fbs"):
        cand = [pick(w, scheme) for w in pool]
        cand = [p for p in cand if p["delivery_base"] is not None
                and p["delivery_liter"] is not None]
        if not cand:
            continue
        best = min(cand, key=lambda p: p["delivery_base"])
        best["source"] = "%s, склад «%s» (%s)" % (
            "FBO" if scheme == "fbo" else "FBS, свой склад", best["name"], best["geo"])
        return best
    return None


def logistics_rub(volume_l, box):
    """Логистика за одну отправку: база за первый литр + за каждый следующий."""
    if not box or box.get("delivery_base") is None:
        return None
    extra = max(0.0, volume_l - 1.0)
    return box["delivery_base"] + box["delivery_liter"] * extra


def storage_rub_day(volume_l, box):
    if not box or box.get("storage_base") is None:
        return None
    extra = max(0.0, volume_l - 1.0)
    return box["storage_base"] + box["storage_liter"] * extra


# ---------------------------------------------------------------- исходные данные

def products():
    return L.qd("""
        SELECT p.nm_id, p.vendor_code, p.subject_id, p.subject_name, p.title,
               p.width, p.height, p.length, p.weight_brutto,
               (SELECT cost_rub FROM wb.cost c WHERE c.vendor_code = lower(p.vendor_code)
                 ORDER BY valid_from DESC LIMIT 1) AS cost_rub,
               (SELECT extra_rub FROM wb.cost c WHERE c.vendor_code = lower(p.vendor_code)
                 ORDER BY valid_from DESC LIMIT 1) AS extra_rub,
               (SELECT discounted_price FROM wb.price pr WHERE pr.nm_id = p.nm_id
                 ORDER BY captured_at DESC LIMIT 1) AS price,
               (SELECT price FROM wb.price pr WHERE pr.nm_id = p.nm_id
                 ORDER BY captured_at DESC LIMIT 1) AS price_before_disc
        FROM wb.product p ORDER BY p.vendor_code
    """)


def advert_per_nm(days):
    """Затраты на рекламу и заказы с рекламы по товару за период."""
    rows = L.qd("""
        SELECT nm_id, sum(sum_rub) AS spend, sum(orders) AS orders,
               sum(views) AS views, sum(clicks) AS clicks, sum(atbs) AS atbs
        FROM wb.advert_stat
        WHERE nm_id <> 0 AND stat_date >= current_date - %s::int
        GROUP BY nm_id
    """, (days,))
    return {r["nm_id"]: r for r in rows}


def sales_per_nm(days):
    rows = L.qd("""
        SELECT nm_id, count(*) AS qty, sum(for_pay) AS for_pay,
               sum(price_with_disc) AS revenue, avg(spp) AS spp
        FROM wb.sale
        WHERE sale_dt >= current_date - %s::int AND coalesce(is_return,false) = false
        GROUP BY nm_id
    """, (days,))
    return {r["nm_id"]: r for r in rows}


MIN_ORDERS_FOR_BUYOUT = 10     # меньше — доля выкупа слишком шумная


def funnel_per_nm(days):
    rows = L.qd("""
        SELECT nm_id, sum(order_count) AS orders, sum(buyout_count) AS buyouts,
               sum(open_count) AS opens, sum(cart_count) AS carts,
               max(stocks_wb) AS stocks, max(conv_buyout_pct) AS wb_buyout_pct
        FROM wb.funnel
        -- только последний период: окна по 30 дней пересекаются,
        -- и суммирование нескольких снимков удвоило бы заказы и переходы
        WHERE (period_start, period_end) = (SELECT period_start, period_end FROM wb.funnel
                               ORDER BY period_end DESC, period_start DESC LIMIT 1)
        GROUP BY nm_id
    """)
    return {r["nm_id"]: r for r in rows}


def buyout_rate(f, fallback):
    """Доля выкупа и источник цифры.

    Своё buyouts/orders систематически занижает выкуп: заказы, сделанные в
    конце периода, ещё едут к покупателю и в выкупы не попали. По d100blackplus
    это даёт 61% против 89% у WB. Поэтому сначала берём показатель WB
    (conv_buyout_pct), своё считаем только при достаточном числе заказов,
    а при единичных заказах используем среднее по ассортименту — иначе
    логистика, которая делится на долю выкупа, утраивается от шума.
    """
    if not f:
        return fallback, "среднее по ассортименту"
    wb_pct = L.num(f.get("wb_buyout_pct"))
    if wb_pct > 0:
        return min(max(wb_pct / 100.0, 0.3), 1.0), "показатель WB"
    orders = L.num(f.get("orders"))
    if orders >= MIN_ORDERS_FOR_BUYOUT:
        b = L.num(f.get("buyouts")) / orders
        return min(max(b, 0.3), 1.0), "свой расчёт (%d заказов)" % orders
    return fallback, "среднее по ассортименту (заказов %d — мало)" % orders


def portfolio_buyout(fun):
    """Средняя доля выкупа по товарам, где она достоверна."""
    vals = []
    for f in (fun or {}).values():
        wb_pct = L.num(f.get("wb_buyout_pct"))
        if wb_pct > 0:
            vals.append(wb_pct / 100.0)
        elif L.num(f.get("orders")) >= MIN_ORDERS_FOR_BUYOUT:
            vals.append(L.num(f.get("buyouts")) / L.num(f.get("orders")))
    if not vals:
        return DEFAULT_BUYOUT
    vals.sort()
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    return min(max(med, 0.3), 1.0)


def finance_per_nm(days):
    """Фактические удержания WB по товару за период.

    Отчёт реализации — это не список продаж, а журнал всех операций. На 107
    продаж в нём приходится 1081 строка: логистика, хранение, удержания,
    возмещения издержек, баллы за отзывы. У многих служебных строк заполнено
    поле quantity, поэтому считать проданные единицы по «quantity > 0» нельзя:
    получается 848 вместо 107, и выручка на единицу падает в восемь раз.
    Количество и выручку берём только со строк «Продажа» и «Возврат».

    Возвраты в отчёте записаны положительными суммами, но означают деньги,
    забранные назад, — поэтому вычитаются явно по docTypeName.

    База выручки — retail_price_with_disc × quantity (цена со скидкой продавца),
    а не retail_amount. retail_amount — это то, что заплатил покупатель после
    платформенной скидки СПП, и при СПП 41% он меньше, чем причитается продавцу:
    491 346 ₽ против 521 275 ₽ к перечислению. Отсюда и выходила отрицательная
    комиссия, когда она считалась как retail_amount − for_pay.
    """
    rows = L.qd("""
        SELECT nm_id,
               sum(CASE WHEN seller_oper_name='Продажа' THEN quantity ELSE 0 END) AS qty_sold,
               sum(CASE WHEN seller_oper_name='Возврат' THEN quantity ELSE 0 END) AS qty_return,
               sum(CASE WHEN seller_oper_name='Продажа' THEN retail_price_with_disc*quantity
                        WHEN seller_oper_name='Возврат' THEN -retail_price_with_disc*quantity
                        ELSE 0 END)                                      AS revenue_base,
               sum(CASE WHEN seller_oper_name='Продажа' THEN for_pay
                        WHEN seller_oper_name='Возврат' THEN -for_pay
                        ELSE 0 END)                                      AS for_pay_net,
               sum(CASE WHEN seller_oper_name='Продажа' THEN acquiring_fee
                        WHEN seller_oper_name='Возврат' THEN -acquiring_fee
                        ELSE 0 END)                                      AS acquiring,
               sum(delivery_rub)            AS logistics,
               sum(rebill_logistic_cost)    AS logistics_rebill,
               sum(paid_storage)            AS storage,
               sum(paid_acceptance)         AS acceptance,
               sum(penalty)                 AS penalty,
               sum(deduction)               AS deduction,
               avg(commission_percent) FILTER (WHERE seller_oper_name='Продажа')
                                            AS commission_pct
        FROM wb.finance_row
        WHERE rr_date >= current_date - %s::int AND nm_id <> 0
        GROUP BY nm_id
    """, (days,))
    return {r["nm_id"]: r for r in rows}


def advert_from_finance(days):
    """Реклама, удержанная WB по отчёту реализации, и её разнос по товарам.

    WB списывает «Оказание услуг WB Продвижение» строкой «Удержание» с
    nm_id = 0 — то есть без привязки к товару. Группировка по товару такие
    строки теряла, и реклама вообще не попадала в расчёт: 101 009 ₽ расходов
    проходили мимо прибыли.

    Разносим по товарам пропорционально затратам из истории списаний
    (/adv/v1/upd), где в названии кампании есть артикул. Так сумма сходится
    с финотчётом, а доли берутся из фактических кампаний, а не из выручки.
    """
    total = L.num((L.q("""SELECT sum(deduction) FROM wb.finance_row
                          WHERE rr_date >= current_date - %s::int
                            AND seller_oper_name='Удержание'
                            AND raw->>'bonusTypeName' LIKE %s""",
                       (days, "%Продвижение%"), fetch="one") or [0])[0])
    if total <= 0:
        return {}, 0.0, "нет"

    known = {vc.lower(): nm for nm, vc in L.q("SELECT nm_id, vendor_code FROM wb.product")}
    spend = L.qd("""SELECT camp_name, sum(upd_sum) AS s FROM wb.advert_spend
                    WHERE upd_time >= current_date - %s::int
                    GROUP BY camp_name""", (days,))

    by_nm, unmatched = {}, 0.0
    for r in spend:
        nm = known.get(normalize_campaign(r["camp_name"]))
        if nm:
            by_nm[nm] = by_nm.get(nm, 0.0) + L.num(r["s"])
        else:
            unmatched += L.num(r["s"])

    base = sum(by_nm.values())
    if base <= 0:
        return {}, total, "не разнесена: кампании не сопоставились с артикулами"
    alloc = {nm: total * v / base for nm, v in by_nm.items()}
    note = "разнесена по кампаниям" + (
        " (не сопоставлено %s)" % L.money(unmatched) if unmatched else "")
    return alloc, total, note


def account_costs(days):
    """Расходы, которые WB пишет без привязки к товару (nm_id = 0).

    Так приходят хранение (1 964 ₽ за период), утилизация и прочие удержания,
    кроме рекламы — её разносит advert_from_finance(). Раньше фильтр nm_id <> 0
    выбрасывал эти строки целиком, и хранение в прибыль не попадало.

    Разносим пропорционально выручке товара: привязать хранение к конкретному
    товару по данным отчёта невозможно, а выручка — ближайший честный прокси.
    """
    r = L.q("""SELECT coalesce(sum(paid_storage),0), coalesce(sum(paid_acceptance),0),
                      coalesce(sum(penalty),0),
                      coalesce(sum(delivery_rub + rebill_logistic_cost),0),
                      coalesce(sum(CASE WHEN seller_oper_name='Удержание'
                                        AND coalesce(raw->>'bonusTypeName','') NOT LIKE %s
                                   THEN deduction ELSE 0 END),0)
               FROM wb.finance_row
               WHERE rr_date >= current_date - %s::int AND nm_id = 0""",
            ("%Продвижение%", days), fetch="one") or (0, 0, 0, 0)
    return {"storage": L.num(r[0]), "acceptance": L.num(r[1]),
            "penalty": L.num(r[2]), "logistics": L.num(r[3]), "deduction": L.num(r[4])}


def allocate_by_revenue(total, fin):
    """Разнести общую сумму по товарам пропорционально их выручке."""
    if total <= 0:
        return {}
    shares = {nm: L.num(f.get("revenue_base")) for nm, f in (fin or {}).items()
              if L.num(f.get("revenue_base")) > 0}
    base = sum(shares.values())
    if base <= 0:
        return {}
    return {nm: total * v / base for nm, v in shares.items()}


def normalize_campaign(name):
    """«d100 white +» и «d100grayplus / поиск / stop» -> «d100whiteplus»/«d100grayplus».

    Названия кампаний владелец пишет по-разному: с пробелами, с заглавной
    буквы, с плюсом в конце, иногда с типом и статусом через слеш.
    """
    if not name:
        return ""
    head = str(name).split("/")[0]
    s = head.strip().lower().replace(" ", "").replace(" ", "")
    if s.endswith("+"):
        s = s[:-1] + "plus"
    return s


# ---------------------------------------------------------------- налог

def tax_config():
    """Налоговая схема владельца из wb.setting.

    mode:
      usn6 / usn15 — простые режимы: ставка от выручки или от прибыли.
      composite    — составная схема: УСН + страховые взносы + НДС,
                     ставки берутся из отдельных настроек.
    """
    mode = L.setting("tax_mode", "usn6")
    return {
        "mode": mode,
        "rate": L.num(L.setting("tax_rate", "6")),
        "usn_pct": L.num(L.setting("tax_usn_pct", "0")),
        "insurance_pct": L.num(L.setting("tax_insurance_pct", "0")),
        "vat_pct": L.num(L.setting("tax_vat_pct", "0")),
        "vat_in_price": (L.setting("tax_vat_in_price", "yes") or "yes").lower() == "yes",
        "insurance_reduces_usn": (L.setting("tax_insurance_reduces_usn", "no") or "no").lower() == "yes",
    }


def tax_breakdown(revenue, profit_before_tax, cfg):
    """Налоги с одной продажи. Возвращает (итого, расшифровка).

    Для составной схемы важны две вещи, которые легко посчитать неверно:

    1. НДС уже внутри розничной цены. Покупатель платит цену с налогом,
       поэтому к уплате не «цена × 5%», а «цена × 5/105» — иначе НДС
       завышается на 5% от себя же.
    2. База УСН — доход без НДС (ст. 248 НК: предъявленные покупателю
       суммы налогов в доход не включаются). Считать УСН от цены с НДС
       означало бы платить налог с налога.
    """
    mode = cfg["mode"]
    if mode == "none":
        return 0.0, {}
    if mode == "usn15":
        t = max(0.0, profit_before_tax) * cfg["rate"] / 100.0
        return t, {"УСН %g%% с прибыли" % cfg["rate"]: t}
    if mode != "composite":
        t = revenue * cfg["rate"] / 100.0
        return t, {"УСН %g%% с выручки" % cfg["rate"]: t}

    vat_pct = cfg["vat_pct"]
    if vat_pct > 0 and cfg["vat_in_price"]:
        vat = revenue * vat_pct / (100.0 + vat_pct)
    else:
        vat = revenue * vat_pct / 100.0
    net = revenue - vat                       # доход без НДС — база для УСН и взносов

    usn = net * cfg["usn_pct"] / 100.0
    insurance = net * cfg["insurance_pct"] / 100.0

    # У ИП без работников взносы уменьшают налог по УСН «доходы».
    # По умолчанию выключено: схема владельцем задана как сложение.
    if cfg["insurance_reduces_usn"]:
        usn = max(0.0, usn - insurance)

    parts = {}
    if vat:       parts["НДС %g%% (в цене)" % vat_pct] = vat
    if usn:       parts["УСН %g%%" % cfg["usn_pct"]] = usn
    if insurance: parts["взносы %g%%" % cfg["insurance_pct"]] = insurance
    return vat + usn + insurance, parts


def tax_of(revenue, profit_before_tax, cfg):
    return tax_breakdown(revenue, profit_before_tax, cfg)[0]


# ---------------------------------------------------------------- расчёт

def calc_plan(p, box, comm_tariffs, adv, fun, sal, days, tax_cfg,
              eff_comm=None, port_buyout=None):
    vol = (L.num(p["width"]) * L.num(p["height"]) * L.num(p["length"])) / 1000.0
    price = L.num(p["price"])
    cost = L.num(p["cost_rub"]) + L.num(p["extra_rub"])
    tariff_pct = commission_for(p["subject_id"], comm_tariffs)
    eff = (eff_comm or {}).get(p["nm_id"])
    if eff:
        comm_pct, comm_src = eff[0], "по продажам (%d шт.)" % eff[1]
    else:
        comm_pct, comm_src = tariff_pct, "по справочнику тарифов"
    log1 = logistics_rub(vol, box)
    stor_day = storage_rub_day(vol, box)

    f = fun.get(p["nm_id"]) or {}
    orders = L.num(f.get("orders"))
    buyout, buyout_src = buyout_rate(f, port_buyout if port_buyout else DEFAULT_BUYOUT)

    # missing — без этого прибыль не считается вовсе;
    # caveats — учтено нулём, но владелец должен знать (суммы малы рядом с ценой)
    missing, caveats = [], []
    if price <= 0: missing.append("цена")
    if cost <= 0: missing.append("себестоимость")
    if comm_pct is None: missing.append("комиссия (нет тарифов)")
    if log1 is None: missing.append("логистика (нет тарифов)")
    if stor_day is None:
        caveats.append("хранение не учтено: WB не отдаёт тариф хранения для России")

    commission = price * (comm_pct or 0) / 100.0
    logistics = ((log1 or 0) / buyout) + DEFAULT_RETURN_FEE * (1.0 / buyout - 1.0)
    storage = (stor_day or 0) * DEFAULT_TURNOVER_DAYS

    a = adv.get(p["nm_id"]) or {}
    s = sal.get(p["nm_id"]) or {}
    qty = L.num(s.get("qty")) or L.num(f.get("buyouts"))
    advert = (L.num(a.get("spend")) / qty) if qty else 0.0

    before_tax = price - commission - logistics - storage - advert - cost
    tax, tax_parts = tax_breakdown(price, before_tax, tax_cfg)
    profit = before_tax - tax

    # Без комиссии и логистики прибыль посчитать нельзя. Показать «80% маржи»
    # вместо прочерка — хуже, чем не показать ничего: на такую цифру принимают
    # решения. Поэтому при нехватке тарифов прибыль остаётся пустой.
    incomplete = bool(missing)
    if incomplete:
        profit = None
        margin = roi = None
    else:
        margin = (profit / price * 100.0) if price else None
        roi = (profit / cost * 100.0) if cost else None

    return {
        "basis": "plan", "price_seller": price, "price_buyer": None,
        "commission_pct": comm_pct, "commission_rub": commission if comm_pct is not None else None,
        "logistics_rub": logistics if log1 is not None else None,
        "storage_rub": storage if stor_day is not None else None, "acceptance_rub": 0,
        "return_rub": DEFAULT_RETURN_FEE * (1.0 / buyout - 1.0),
        "advert_rub": advert, "cost_rub": cost,
        "tax_rub": tax if not incomplete else None,
        "profit_rub": profit, "margin_pct": margin, "roi_pct": roi,
        "buyout_pct": buyout * 100.0, "volume_l": vol,
        "detail": {"logistics_one_way": log1, "storage_per_day": stor_day,
                   "turnover_days": DEFAULT_TURNOVER_DAYS, "qty_sold": qty,
                   "advert_spend_total": L.num(a.get("spend")),
                   "buyout_source": buyout_src,
                   "commission_source": comm_src, "commission_tariff_pct": tariff_pct,
                   "logistics_source": (box or {}).get("source"),
                   "logistics_scheme": (box or {}).get("scheme"),
                   "caveats": caveats,
                   "tax_parts": {k: round(v, 2) for k, v in tax_parts.items()},
                   "missing": missing, "incomplete": incomplete, "days": days},
    }


def calc_fact(p, fin, days, tax_cfg, advert_alloc=None, acc_alloc=None):
    """Прибыль по факту финотчёта, на одну проданную единицу.

    Порядок вычитания важен, чтобы ничего не посчитать дважды:

      for_pay (к перечислению) уже за вычетом комиссии WB и эквайринга:
          827 871 − 288 099 (кВВ 34,8%) − 18 415 (эквайринг) ≈ 521 275 ✓
      поэтому комиссию и эквайринг отдельно вычитать НЕЛЬЗЯ;

      логистика, хранение, приёмка, штрафы и удержания идут отдельными
      строками отчёта с for_pay = 0, поэтому вычитаются;

      реклама приходит строкой «Удержание» без привязки к товару и
      разносится отдельно в advert_from_finance().
    """
    f = fin.get(p["nm_id"])
    if not f:
        return None
    qty_sold = L.num(f.get("qty_sold"))
    qty_ret = L.num(f.get("qty_return"))
    qty = qty_sold - qty_ret
    if qty_sold <= 0 or qty <= 0:
        # по товару не было продаж — служебные строки (логистика, хранение)
        # есть, но делить их не на что. Прибыль «минус себестоимость» была бы
        # не фактом, а артефактом расчёта.
        return None

    cost = L.num(p["cost_rub"]) + L.num(p["extra_rub"])
    nm = p["nm_id"]
    acc = acc_alloc or {}
    revenue = L.num(f.get("revenue_base"))          # цена со скидкой продавца
    for_pay = L.num(f.get("for_pay_net"))           # продажи минус возвраты
    acquiring = L.num(f.get("acquiring"))
    logistics = (L.num(f.get("logistics")) + L.num(f.get("logistics_rebill"))
                 + L.num(acc.get("logistics", {}).get(nm)))
    # к расходам товара добавляем его долю общехозяйственных (nm_id = 0)
    storage = L.num(f.get("storage")) + L.num(acc.get("storage", {}).get(nm))
    acceptance = L.num(f.get("acceptance")) + L.num(acc.get("acceptance", {}).get(nm))
    penalty = L.num(f.get("penalty")) + L.num(acc.get("penalty", {}).get(nm))
    deduction = L.num(f.get("deduction")) + L.num(acc.get("deduction", {}).get(nm))
    advert = L.num((advert_alloc or {}).get(nm))

    commission = revenue - for_pay - acquiring      # то, что удержал WB

    before_tax = (for_pay - logistics - storage - acceptance - penalty
                  - deduction - advert - cost * qty)
    tax, tax_parts = tax_breakdown(revenue, before_tax, tax_cfg)
    profit = before_tax - tax

    per = lambda v: v / qty
    return {
        "basis": "fact", "price_seller": per(revenue), "price_buyer": None,
        "commission_pct": L.num(f.get("commission_pct")),
        "commission_rub": per(commission),
        "logistics_rub": per(logistics), "storage_rub": per(storage),
        "acceptance_rub": per(acceptance),
        "return_rub": per(penalty + deduction),
        "advert_rub": per(advert), "cost_rub": cost, "tax_rub": per(tax),
        "profit_rub": per(profit),
        "margin_pct": (profit / revenue * 100.0) if revenue else None,
        "roi_pct": (per(profit) / cost * 100.0) if cost else None,
        "buyout_pct": (qty / qty_sold * 100.0) if qty_sold else None,
        "volume_l": (L.num(p["width"]) * L.num(p["height"]) * L.num(p["length"])) / 1000.0,
        "detail": {"qty_sold": qty_sold, "qty_return": qty_ret, "qty_net": qty,
                   "revenue_total": revenue, "for_pay_total": for_pay,
                   "commission_total": commission, "acquiring_total": acquiring,
                   "logistics_total": logistics, "storage_total": storage,
                   "penalty_total": penalty, "deduction_total": deduction,
                   "advert_total": advert, "cost_total": cost * qty,
                   "profit_total": profit, "days": days,
                   "tax_parts": {k: round(per(v), 2) for k, v in tax_parts.items()}},
    }


def save(nm_id, r):
    with L.db() as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO wb.unit_econ (nm_id, calc_date, basis, price_seller, price_buyer,
                commission_pct, commission_rub, logistics_rub, storage_rub, acceptance_rub,
                return_rub, advert_rub, cost_rub, tax_rub, profit_rub, margin_pct, roi_pct,
                buyout_pct, volume_l, detail)
            VALUES (%s, current_date, %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (nm_id, calc_date, basis) DO UPDATE SET
                price_seller=EXCLUDED.price_seller, commission_pct=EXCLUDED.commission_pct,
                commission_rub=EXCLUDED.commission_rub, logistics_rub=EXCLUDED.logistics_rub,
                storage_rub=EXCLUDED.storage_rub, acceptance_rub=EXCLUDED.acceptance_rub,
                return_rub=EXCLUDED.return_rub, advert_rub=EXCLUDED.advert_rub,
                cost_rub=EXCLUDED.cost_rub, tax_rub=EXCLUDED.tax_rub,
                profit_rub=EXCLUDED.profit_rub, margin_pct=EXCLUDED.margin_pct,
                roi_pct=EXCLUDED.roi_pct, buyout_pct=EXCLUDED.buyout_pct,
                volume_l=EXCLUDED.volume_l, detail=EXCLUDED.detail
        """, (nm_id, r["basis"], r["price_seller"], r["price_buyer"], r["commission_pct"],
              r["commission_rub"], r["logistics_rub"], r["storage_rub"], r["acceptance_rub"],
              r["return_rub"], r["advert_rub"], r["cost_rub"], r["tax_rub"], r["profit_rub"],
              r["margin_pct"], r["roi_pct"], r["buyout_pct"], r["volume_l"],
              json.dumps(r["detail"], ensure_ascii=False, default=str)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--basis", choices=["plan", "fact", "both"], default="both")
    ap.add_argument("--show", action="store_true", help="напечатать таблицу")
    a = ap.parse_args()

    rid = L.run_start("unit_economics.py")
    try:
        comm_t = latest_tariff("commission")
        box_t = box_tariff(latest_tariff("box"))
        tax_cfg = tax_config()
        adv = advert_per_nm(a.days)
        fun = funnel_per_nm(a.days)
        sal = sales_per_nm(a.days)
        fin = finance_per_nm(a.days)
        eff_comm = effective_commission()
        port_buyout = portfolio_buyout(fun)
        adv_alloc, adv_total, adv_note = advert_from_finance(a.days)
        acc_tot = account_costs(a.days)
        acc_alloc = {k: allocate_by_revenue(v, fin) for k, v in acc_tot.items()}
        prods = products()

        warn = []
        if not comm_t:
            warn.append("нет тарифов комиссии — запустите pull_tariffs.py")
        if not box_t:
            warn.append("нет тарифов логистики — запустите pull_tariffs.py")
        if not fin:
            warn.append("нет финотчёта — fact недоступен, запустите pull_finance.py")
        elif adv_total:
            print("реклама по финотчёту: %s за %d дней, %s"
                  % (L.money(adv_total), a.days, adv_note))

        n = 0
        rows_plan = []
        computed = {"plan": set(), "fact": set()}
        for p in prods:
            if a.basis in ("plan", "both"):
                r = calc_plan(p, box_t, comm_t, adv, fun, sal, a.days, tax_cfg,
                              eff_comm, port_buyout)
                save(p["nm_id"], r); rows_plan.append((p, r)); n += 1
                computed["plan"].add(p["nm_id"])
            if a.basis in ("fact", "both"):
                r = calc_fact(p, fin, a.days, tax_cfg, adv_alloc, acc_alloc)
                if r:
                    save(p["nm_id"], r); n += 1
                    computed["fact"].add(p["nm_id"])

        # прошлый прогон мог записать строку по товару, который сейчас не считается
        # (например, продаж не стало) — такие строки надо убрать, иначе в отчёте
        # останутся устаревшие цифры
        stale = 0
        with L.db() as c, c.cursor() as cur:
            for basis, ids in computed.items():
                if a.basis not in (basis, "both"):
                    continue
                cur.execute("""DELETE FROM wb.unit_econ
                               WHERE calc_date = current_date AND basis = %s
                                 AND NOT (nm_id = ANY(%s))""", (basis, list(ids) or [0]))
                stale += cur.rowcount
        print("рассчитано строк: %d, товаров: %d%s"
              % (n, len(prods), (", убрано устаревших: %d" % stale) if stale else ""))
        import views as _V
        print("налог: %s" % _V.tax_note())
        if box_t:
            print("логистика: %s — база %s ₽ + %s ₽/л"
                  % (box_t.get("source"), box_t.get("delivery_base"),
                     box_t.get("delivery_liter")))
            if box_t.get("storage_base") is None:
                print("  ! тарифа хранения для России WB не отдаёт — в plan хранение не учтено")
        if fun:
            print("доля выкупа: среднее по ассортименту %.0f%% (при единичных заказах берётся оно)" % (port_buyout * 100))
        if eff_comm:
            vals = [v[0] for v in eff_comm.values()]
            print("кВВ по продажам: %.1f–%.1f%% (по %d товарам), справочник: %s%%"
                  % (min(vals), max(vals), len(eff_comm),
                     commission_for(prods[0]["subject_id"], comm_t) if prods else "?"))
        else:
            print("кВВ: по справочнику тарифов — продаж для уточнения пока нет")
        for w in warn:
            print("  ! %s" % w)

        if a.show and rows_plan:
            m = lambda v: L.money(v) if v is not None else "—"
            print("\n%-16s %9s %8s %8s %8s %8s %9s %9s %7s" %
                  ("артикул", "цена", "кВВ", "логист", "хранен", "реклама", "себест", "прибыль", "марж%"))
            for p, r in rows_plan:
                print("%-16s %9s %8s %8s %8s %8s %9s %9s %7s" % (
                    p["vendor_code"][:16], m(r["price_seller"]),
                    m(r["commission_rub"]), m(r["logistics_rub"]),
                    m(r["storage_rub"]), m(r["advert_rub"]),
                    m(r["cost_rub"]), m(r["profit_rub"]),
                    ("%.1f" % r["margin_pct"]) if r["margin_pct"] is not None else "—"))
            miss = {m for _, r in rows_plan for m in r["detail"]["missing"]}
            if miss:
                print("\nне хватает данных: %s" % ", ".join(sorted(miss)))
        L.run_end(rid, "ok", rows=n)
    except Exception as e:
        L.run_end(rid, "error", message=str(e)[:400])
        raise


if __name__ == "__main__":
    main()
