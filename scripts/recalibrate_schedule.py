#!/usr/bin/env python3
"""
Предлагает новые интервалы проверки (schedule_config.json) по факту находок,
которые считает calibration_report.py — вместо того чтобы поправить DAYS в
due_today.py один раз вручную и забыть, это повторяемая процедура: гоняешь
после накопления новых боевых прогонов, смотришь diff, применяешь, если
согласен. НИЧЕГО не пишет без --apply — по умолчанию только печатает, что
изменилось бы (решение владельца 20.08.2026: полуавтомат с diff на просмотр,
не тихая автозапись).

Правило (одно и то же для каждой ячейки network×importance×layer, где
проверок хватает — см. --min-sample в calibration_report.py):
  - new_hit-доля < 0.15  → интервал ×2.0  (почти никогда не находит — почти
                            наверняка так и с VK archived: 0 находок на 94+
                            проверках)
  - new_hit-доля < 0.25  → интервал ×1.4  (ниже целевого диапазона, но не
                            катастрофически)
  - new_hit-доля > 0.65  → интервал ×0.75 (стабильно много находок — можно
                            проверять чаще, не тратя чужое время впустую)
  - иначе                → без изменений (0.25-0.65 — не считаем, что есть
                            основания трогать)
Результат зажимается в [1, ABS_MAX_INTERVAL_DAYS] (тот же потолок 730д, что и
у backoff в due_today.py) и округляется до целых дней. После этого — проверка
на монотонность: интервал слоя НЕ может стать короче интервала более свежего
слоя той же важности/сети (active <= rare <= dormant <= archived) — если
предложенное значение это нарушает, поджимается до соседнего. Так исключены
абсурдные исходы вроде "rare проверяем реже, чем archived" из-за шумной
выборки в одной ячейке.

Ячейки с недостаточной выборкой не трогаются вовсе — остаются либо дефолтом
DAYS, либо тем, что уже есть в schedule_config.json от прошлого раза.

Запуск:
    python3 recalibrate_schedule.py [social.db] [--apply] [--min-sample N]
                                      [--max-count-per-day N] [--min-importance N]
"""
import json, os, sqlite3, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import due_today as D
import calibration_report as C
from _cli import positionals, require_db   # см. _cli.py

args = positionals(sys.argv[1:], ("--min-sample", "--max-count-per-day", "--min-importance"))
DB = require_db(args[0] if args else "social.db")
APPLY = "--apply" in sys.argv
MIN_IMPORTANCE = 2
MIN_SAMPLE = 15
MAX_COUNT_PER_DAY = 200
for i, a in enumerate(sys.argv):
    if a == "--min-sample" and i + 1 < len(sys.argv):
        MIN_SAMPLE = int(sys.argv[i + 1])
    if a == "--max-count-per-day" and i + 1 < len(sys.argv):
        MAX_COUNT_PER_DAY = int(sys.argv[i + 1])
    if a == "--min-importance" and i + 1 < len(sys.argv):
        MIN_IMPORTANCE = int(sys.argv[i + 1])

LAYER_ORDER = ["active", "rare", "dormant", "archived"]


def multiplier_for(rate):
    if rate < 0.15:
        return 2.0
    if rate < 0.25:
        return 1.4
    if rate > 0.65:
        return 0.75
    return 1.0


def propose(cells, min_sample):
    """Возвращает {(network, importance, layer): new_days} только для ячеек,
    где хватило выборки И итог отличается от того, что уже действует сейчас.

    Множитель считается ОТ ДЕФОЛТНОЙ D.DAYS (единая база, без network-override),
    а не от того, что уже лежит в schedule_config.json от прошлого раза. Это
    принципиально: если считать от уже применённого значения, повторный
    прогон на ТЕХ ЖЕ данных (наблюдения не изменились, значит и rate тот же)
    каждый раз давал бы новый множитель поверх предыдущего — 180→360→720→...
    без остановки, чисто из-за факта повторного запуска, а не новых данных.
    Отсчёт от фиксированной базы даёт то же самое new_days при том же rate —
    пересчёт идемпотентен, диф пуст, если реальных новых данных не появилось."""
    proposals = {}
    for (net, imp, lyr), c in cells.items():
        t = c["total"]
        if t < min_sample:
            continue
        baseline = D.DAYS.get(imp, {}).get(lyr)
        if baseline is None:
            continue
        rate = c["new_hit"] / t
        mult = multiplier_for(rate)
        candidate = min(D.ABS_MAX_INTERVAL_DAYS, max(1, round(baseline * mult)))
        current_effective = D.DAYS_BY_NETWORK.get(net, {}).get(imp, {}).get(lyr, baseline)
        if candidate != current_effective:
            proposals[(net, imp, lyr)] = candidate
    return proposals


def enforce_monotonic(base_by_network, proposals):
    """active <= rare <= dormant <= archived, по каждой (network, importance).
    base_by_network — текущие DAYS_BY_NETWORK (то, что не в proposals, берём
    отсюда как есть). Возвращает финальную объединённую таблицу для ВСЕХ
    известных (network, importance, layer), не только изменённых — чтобы можно
    было и посчитать диф, и, если нужно, дампнуть целиком."""
    final = {}
    adjustments = []
    for net, by_imp in base_by_network.items():
        for imp, by_layer in by_imp.items():
            prev = 0
            for lyr in LAYER_ORDER:
                if lyr not in by_layer:
                    continue
                current = by_layer[lyr]
                val = proposals.get((net, imp, lyr), current)
                if val < prev:
                    adjustments.append((net, imp, lyr, prev, val, current))
                    val = prev
                final[(net, imp, lyr)] = val
                prev = val
    return final, adjustments


def load_existing_config(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    con = sqlite3.connect(DB)
    run_dates, _ = C.battle_run_dates(con, MAX_COUNT_PER_DAY)
    cells, _, _ = C.compute_cells(con, run_dates, MIN_IMPORTANCE)
    con.close()

    proposals = propose(cells, MIN_SAMPLE)
    final, adjustments = enforce_monotonic(D.DAYS_BY_NETWORK, proposals)

    if not proposals:
        print(f"по текущим данным (--min-sample {MIN_SAMPLE}) менять нечего — "
              f"либо выборки не хватает, либо все ячейки в целевом диапазоне 25-65%.")
        return

    print(f"предложенные изменения интервалов (боевых дат: {len(run_dates)}, "
          f"--min-sample {MIN_SAMPLE}):\n")
    print(f"{'сеть':<10} {'важн':>4} {'слой':<9} {'провер.':>7} {'new_hit%':>9} "
          f"{'было(д)':>8} {'станет(д)':>9}")
    for (net, imp, lyr), new_days in sorted(proposals.items()):
        c = cells[(net, imp, lyr)]
        t = c["total"]
        rate = c["new_hit"] / t
        current = D.DAYS_BY_NETWORK[net][imp][lyr]
        print(f"{net:<10} {imp:>4} {lyr:<9} {t:>7} {rate*100:8.1f}% "
              f"{current:>8} {new_days:>9}")

    if adjustments:
        print(f"\n{len(adjustments)} значени(е/й) дополнительно поджато(ы) ради монотонности "
              f"(слой не может проверяться реже, чем более свежий слой той же важности/сети):")
        for net, imp, lyr, val, was, _current in adjustments:
            print(f"  {net} imp={imp} {lyr}: {was}д → {val}д "
                  f"(не может быть меньше, чем более свежий слой)")

    if APPLY:
        path = D.SCHEDULE_CONFIG_PATH
        existing = load_existing_config(path)
        # Пишем значения из final, а не из proposals: поджатие ради монотонности
        # раньше только печаталось, а в файл уезжало исходное предложение — и
        # конфиг оставался ровно в том состоянии, которое докстринг обещает
        # исключить («rare проверяем реже, чем dormant»). Записываем все ячейки,
        # где final отличается от действующего значения: и сами предложения, и
        # соседние слои, которые пришлось подтянуть за ними.
        for (net, imp, lyr), new_days in sorted(final.items()):
            if new_days == D.DAYS_BY_NETWORK.get(net, {}).get(imp, {}).get(lyr):
                continue
            existing.setdefault(net, {})
            existing[net].setdefault(str(imp), {})
            existing[net][str(imp)][lyr] = new_days
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"\nзаписано в {path}")
    else:
        print("\n(пробный прогон — ничего не записано, добавь --apply)")


if __name__ == "__main__":
    main()
