#!/usr/bin/env python3
"""
Убирает дублирующиеся аккаунты внутри одного человека.

После слияния дублей (import_vk_sweep) у человека остаются две VK-записи — «красивая»
ссылка из переписи и числовая из API. Это один и тот же профиль: если не убрать, ежедневный
прогон будет ходить на него дважды.

Оставляем ту запись, что подтверждена реальным списком друзей (in_friends=1) и имеет
network_id; наблюдения переносим на неё.

Запуск: python3 dedupe_accounts.py [social.db] [--apply]
"""
import sqlite3, sys

APPLY = "--apply" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("--")]
DB = args[0] if args else "social.db"

con = sqlite3.connect(DB)
cur = con.cursor()

dups = cur.execute("""
    SELECT person_id, network, COUNT(*) FROM accounts
    GROUP BY person_id, network HAVING COUNT(*) > 1
""").fetchall()


def collapse_same_day(cur, account_ids, apply):
    """Две записи одного канала за один день с одним источником — это не две
    проверки, а след переноса: проверки за один день у дублирующихся аккаунтов
    после UPDATE ложатся на один account_id. Гейт полноты считает находки по
    строкам observations, поэтому такая пара удваивает находку в отчёте.
    Оставляем самую содержательную (нашли пост → есть ссылка → есть текст →
    меньший id), остальные убираем.

    Считаем по ВСЕМ сливаемым аккаунтам сразу, а не по одному оставшемуся:
    в пробном прогоне переноса ещё не было, и группировка по keep показала бы
    ноль — то есть dry-run врал бы о том, что сделает --apply.
    """
    placeholders = ",".join("?" * len(account_ids))
    rows = cur.execute(f"""SELECT id, checked_at, source, found_post, post_url, summary
                           FROM observations WHERE account_id IN ({placeholders})""",
                       tuple(account_ids)).fetchall()
    groups = {}
    for oid, checked_at, source, found, url, summary in rows:
        groups.setdefault((checked_at, source), []).append(
            (oid, found or 0, 1 if url else 0, 1 if summary else 0))
    extra = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: (-r[1], -r[2], -r[3], r[0]))
        for oid, *_ in group[1:]:
            if apply:
                cur.execute("DELETE FROM observations WHERE id=?", (oid,))
            extra += 1
    return extra


removed = collapsed = 0
merged_groups = []
for pid, net, _ in dups:
    accs = cur.execute("""SELECT id, url, COALESCE(in_friends,0), network_id,
                                 (SELECT COUNT(*) FROM observations o WHERE o.account_id=a.id)
                          FROM accounts a WHERE person_id=? AND network=?""",
                       (pid, net)).fetchall()
    # лучший: в друзьях → есть числовой id → больше наблюдений → меньший id
    accs.sort(key=lambda r: (-r[2], -(1 if r[3] else 0), -r[4], r[0]))
    keep = accs[0][0]
    merged_groups.append([a[0] for a in accs])
    for aid, url, _f, _n, _o in accs[1:]:
        if APPLY:
            cur.execute("UPDATE observations SET account_id=? WHERE account_id=?", (keep, aid))
            cur.execute("DELETE FROM accounts WHERE id=?", (aid,))
        removed += 1

for group in merged_groups:
    collapsed += collapse_same_day(cur, group, APPLY)

if APPLY:
    con.commit()
print(f"людей с дублями аккаунтов: {len(dups)}")
print(f"  лишних записей убрано: {removed}")
if collapsed:
    print(f"  наблюдений-двойников за один день схлопнуто: {collapsed}")
for net, n in cur.execute("SELECT network, COUNT(*) FROM accounts GROUP BY 1"):
    print(f"  {net}: {n}")
if not APPLY:
    print("\n(пробный прогон — ничего не записано, добавь --apply)")
con.close()
