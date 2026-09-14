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

moved = removed = 0
for pid, net, _ in dups:
    accs = cur.execute("""SELECT id, url, COALESCE(in_friends,0), network_id,
                                 (SELECT COUNT(*) FROM observations o WHERE o.account_id=a.id)
                          FROM accounts a WHERE person_id=? AND network=?""",
                       (pid, net)).fetchall()
    # лучший: в друзьях → есть числовой id → больше наблюдений → меньший id
    accs.sort(key=lambda r: (-r[2], -(1 if r[3] else 0), -r[4], r[0]))
    keep = accs[0][0]
    for aid, url, _f, _n, _o in accs[1:]:
        if APPLY:
            cur.execute("UPDATE observations SET account_id=? WHERE account_id=?", (keep, aid))
            cur.execute("DELETE FROM accounts WHERE id=?", (aid,))
        moved += 1
        removed += 1

if APPLY:
    con.commit()
print(f"людей с дублями аккаунтов: {len(dups)}")
print(f"  лишних записей убрано: {removed}")
for net, n in cur.execute("SELECT network, COUNT(*) FROM accounts GROUP BY 1"):
    print(f"  {net}: {n}")
if not APPLY:
    print("\n(пробный прогон — ничего не записано, добавь --apply)")
con.close()
