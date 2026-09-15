#!/usr/bin/env python3
"""
Калибровка расписания: сколько реальных находок дают проверки в каждой ячейке
(сеть × важность × слой активности на момент проверки), и отдельно — сколько
FB/IG-проверок тратятся на чужой контент (authored_by_owner=0), а не на
реальные посты человека.

Появился 20.08.2026 по прямому запросу владельца: «низкий процент настоящих
находок, прогоны обходят аккаунты вхолостую». До этого скрипта такой вопрос
не считался вообще — DAYS в due_today.py подобрана на глаз 06-10.08.2026 и ни
разу не сверялась с тем, что реально находится. Первый прогон этого скрипта
(вручную, до его написания) показал: за 5 боевых прогонов (07,10,13,19,20.08,
528 проверок) VK — 15.2% настоящих новых находок (78.9% пусто), FB — 42.8%
(но 36.9% — чужой контент на стене), IG — 71.2%. Внутри VK разброс ещё резче:
слой active — 44-57% (расписание там в порядке), а rare+dormant+archived
вместе — 189 из 237 VK-проверок дают всего 7.4% находок (archived отдельно:
94 проверки, 0 находок).

Каждое наблюдение (проверка канала) относится ровно к одной категории:
  - new_hit       — по-настоящему новый собственный пост, которого раньше
                    у этого канала не было (сравнение по post_url, а если
                    его нет — по post_date).
  - repeat_known  — found_post=1, authored_by_owner=1, но тот же post_url/
                    post_date уже встречался у ЭТОГО ЖЕ канала раньше (закреп,
                    повторный обход того же поста). Известная отдельная
                    проблема (её ловят flag_repeat_posts.py и проверка повторов
                    в check_digest_completeness.py) — этот скрипт её только
                    СЧИТАЕТ, не чинит и не входит в задачу калибровки
                    расписания (по решению владельца 20.08.2026).
  - not_owner     — found_post=1, но authored_by_owner=0 (чужой контент:
                    поздравления на стене, гостевые посты, теги).
  - empty         — found_post=0.
  - blocked       — access_blocked=1 (капча/приватность — тоже не находка, но
                    это проблема сбора, не расписания).

Для расписания важна только "new_hit vs всё остальное" — репост/чужой
контент/пусто одинаково не стоили того, чтобы канал проверяли именно сегодня.

Дни группируются по (network, importance, layer), где layer считается ТЕМ ЖЕ
layer() из due_today.py, но на момент конкретной проверки (по последнему
СОБСТВЕННОМУ посту ДО этой даты) — не текущим слоем канала. Ячейки с выборкой
меньше --min-sample (по умолчанию 15) помечаются "недостаточно данных" и не
участвуют в рекомендациях recalibrate_schedule.py — на n=3 любое число это шум.

"Боевые" прогоны (due_today-driven) отделяются от разовых census/бэклог-
заливок автоматически: день считается боевым, если проверок за день не больше
--max-count-per-day (по умолчанию 200) — census-заливки 01-03.08 и 06.08.2026
дают 243-1212 в день, обычные прогоны — 6-176. Порог настраиваемый.

Запуск:
    python3 calibration_report.py [social.db] [--min-importance N] [--min-sample N]
                                    [--max-count-per-day N] [--not-owner] [--json]

    --not-owner   — вместо таблицы по расписанию показать каналы с высокой
                    долей чужого контента на стене (кандидаты на то, чтобы
                    владелец решил, что с ними делать — тема отдельная от
                    расписания, сама калибровка её не чинит).
"""
import collections, json, os, sqlite3, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import due_today as D
from _cli import positionals, require_db   # см. _cli.py

args = positionals(sys.argv[1:], ("--min-importance", "--min-sample", "--max-count-per-day"))
DB = args[0] if args else "social.db"
if __name__ == "__main__":   # модуль импортируют (recalibrate_schedule.py) — там нужен только код, не база
    DB = require_db(DB)
MIN_IMPORTANCE = 2
MIN_SAMPLE = 15
MAX_COUNT_PER_DAY = 200
SHOW_NOT_OWNER = "--not-owner" in sys.argv
AS_JSON = "--json" in sys.argv
for i, a in enumerate(sys.argv):
    if a == "--min-importance" and i + 1 < len(sys.argv):
        MIN_IMPORTANCE = int(sys.argv[i + 1])
    if a == "--min-sample" and i + 1 < len(sys.argv):
        MIN_SAMPLE = int(sys.argv[i + 1])
    if a == "--max-count-per-day" and i + 1 < len(sys.argv):
        MAX_COUNT_PER_DAY = int(sys.argv[i + 1])


def parse_date(d):
    try:
        y, m, dd = d.split("-")
        return date(int(y), int(m), int(dd))
    except Exception:
        return None


def battle_run_dates(con, max_count_per_day):
    """Даты, которые похожи на обычный due_today-прогон, а не разовую заливку
    (census/бэклог) — см. докстринг выше. Возвращает set дат (строки) плюс
    список (дата, count, включена?) для прозрачности."""
    counts = collections.Counter(
        row[0] for row in con.execute("SELECT checked_at FROM observations WHERE checked_at IS NOT NULL")
    )
    included = {d for d, c in counts.items() if c <= max_count_per_day}
    breakdown = sorted(counts.items())
    return included, breakdown


def compute_cells(con, run_dates, min_importance):
    """Идёт по всей истории ПОСЛЕДОВАТЕЛЬНО (нужно для реконструкции "что уже
    было известно каналу на момент этой проверки"), но в статистику попадают
    только проверки из run_dates. Возвращает:
      cells: {(network, importance, layer): Counter(total/new_hit/repeat_known/not_owner/empty/blocked)}
      not_owner_by_account: {account_id: Counter(found/not_owner)} — для --not-owner
    """
    rows = con.execute("""
        SELECT o.account_id, o.checked_at, o.found_post, o.post_date, o.post_url,
               COALESCE(o.access_blocked,0), COALESCE(o.authored_by_owner,1) AS owner,
               a.network, p.importance, p.display_name
        FROM observations o
        JOIN accounts a ON a.id = o.account_id
        JOIN people p ON p.id = a.person_id
        WHERE p.importance IS NOT NULL AND p.importance >= ?
        ORDER BY o.account_id, o.checked_at ASC, o.id ASC
    """, (min_importance,)).fetchall()

    seen_keys = collections.defaultdict(set)
    last_own_date = {}
    cells = collections.defaultdict(collections.Counter)
    not_owner_by_account = collections.defaultdict(lambda: collections.Counter())
    names = {}

    for aid, checked_at, found_post, post_date, post_url, blocked, owner, network, importance, name in rows:
        names[aid] = (name, network)
        key = post_url or post_date
        if checked_at in run_dates:
            today = parse_date(checked_at)
            lp = last_own_date.get(aid)
            lyr = D.layer(lp, today) if today else "unknown"
            cell = cells[(network, importance, lyr)]
            cell["total"] += 1
            if blocked:
                cell["blocked"] += 1
            elif found_post == 0:
                cell["empty"] += 1
            elif owner == 0:
                cell["not_owner"] += 1
            elif key and key in seen_keys[aid]:
                cell["repeat_known"] += 1
            else:
                cell["new_hit"] += 1
            if found_post == 1 and not blocked:
                not_owner_by_account[aid]["found"] += 1
                if owner == 0:
                    not_owner_by_account[aid]["not_owner"] += 1
        if found_post == 1 and owner == 1 and key:
            seen_keys[aid].add(key)
            last_own_date[aid] = post_date

    return cells, not_owner_by_account, names


LAYER_ORDER = ["active", "rare", "dormant", "archived"]


def report_cells(con, cells, min_sample):
    print(f"{'сеть':<10} {'важн':>4} {'слой':<9} {'провер.':>7} {'new_hit':>8} "
          f"{'повтор':>7} {'чужой':>6} {'пусто':>6}  тек.интервал(д)")
    for net in ("vk", "facebook", "instagram"):
        for imp in (5, 4, 3, 2):
            for lyr in LAYER_ORDER:
                c = cells.get((net, imp, lyr))
                if not c:
                    continue
                t = c["total"]
                cur_days = D.DAYS_BY_NETWORK.get(net, {}).get(imp, {}).get(lyr, "—")
                flag = "" if t >= min_sample else "  (мало данных)"
                print(f"{net:<10} {imp:>4} {lyr:<9} {t:>7} "
                      f"{c['new_hit']:>4}({c['new_hit']/t*100:4.0f}%) "
                      f"{c['repeat_known']:>3}({c['repeat_known']/t*100:3.0f}%) "
                      f"{c['not_owner']:>3}({c['not_owner']/t*100:3.0f}%) "
                      f"{c['empty']:>3}({c['empty']/t*100:3.0f}%)  {cur_days!s:>6}{flag}")


def report_not_owner(names, not_owner_by_account, min_found=3, min_rate=0.4):
    # VK исключён намеренно: export_vk_daily.py тянет wall.get(filter="owner"),
    # чужие посты на стене туда физически не попадают — authored_by_owner=0 на
    # VK означает "голый репост без своих слов" (см. import_daily.py,
    # is_bare_repost), а не чужой контент. Это другое явление и другая задача
    # (пересчитывается уже в empty_streak — голый репост и так не освежает
    # last_post), сюда его смешивать не нужно.
    rows = []
    for aid, c in not_owner_by_account.items():
        name, network = names[aid]
        if network == "vk":
            continue
        found = c["found"]
        if found < min_found:
            continue
        rate = c["not_owner"] / found
        if rate < min_rate:
            continue
        rows.append((rate, found, c["not_owner"], name, network, aid))
    rows.sort(reverse=True)
    print(f"FB/IG-каналы с долей чужого контента на стене >= {min_rate*100:.0f}% "
          f"(из проверок, где хоть что-то нашлось, минимум {min_found}; VK не в счёт — "
          f"там authored_by_owner=0 значит голый репост, не чужой контент):\n")
    for rate, found, not_owner, name, network, aid in rows:
        print(f"  {rate*100:5.1f}%  ({not_owner}/{found})  {name:<28} {network:<10} account_id={aid}")
    if not rows:
        print("  (никого не набралось при текущих порогах)")


def main():
    con = sqlite3.connect(DB)
    run_dates, breakdown = battle_run_dates(con, MAX_COUNT_PER_DAY)

    if not AS_JSON:
        excluded = [(d, c) for d, c in breakdown if d not in run_dates]
        print(f"боевых дат (<= {MAX_COUNT_PER_DAY} проверок/день): {len(run_dates)} "
              f"из {len(breakdown)} — {', '.join(sorted(run_dates))}")
        if excluded:
            print(f"исключены как разовые заливки: "
                  + ", ".join(f"{d}({c})" for d, c in sorted(excluded)))
        print()

    cells, not_owner_by_account, names = compute_cells(con, run_dates, MIN_IMPORTANCE)

    if SHOW_NOT_OWNER:
        report_not_owner(names, not_owner_by_account)
    elif AS_JSON:
        out = {f"{net}|{imp}|{lyr}": dict(c) for (net, imp, lyr), c in cells.items()}
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        report_cells(con, cells, MIN_SAMPLE)

    con.close()


if __name__ == "__main__":
    main()
