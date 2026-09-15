#!/usr/bin/env python3
"""
Здоровье каналов: для каждой пары (человек, сеть) — на каком слое активности
она сейчас находится (active/rare/dormant/archived), НЕЗАВИСИМО от того, пора
ли её сегодня проверять по due_today.py. iter_due() показывает только то, что
нужно проверить прямо сейчас; этот скрипт — снимок всей картины, чтобы увидеть
каналы, скатившиеся в "archived" (последний найденный пост там старше года), и
решить по каждому: это тупиковая сеть для человека (условно VK — не его канал)
или просто давно не было повода писать.

Добавлен 06.08.2026 вместе с per-network слоем активности в due_today.py — до
этого такие каналы (VK, заброшенный с 2011 года; Instagram, замолчавший год
назад) обнаруживались только на глаз при чтении дайджеста, если повезёт.

Ничего не меняет в базе — только печатает. Решение по каждому каналу
(отметить в people.note, что сеть не рабочая для человека; завести новый
аккаунт в другой сети; оставить как есть) — за владельцем, как и с дублями
(см. README.md).

10.08.2026 — добавлен `empty_streak` (см. due_today.py, комментарий в шапке файла
про `compute_empty_streaks`/`authored_by_owner`): сколько подряд последних
наблюдений НЕ показали собственной активности владельца — поздравления от других
на стене в это не считаются. `--min-streak N` — показать только каналы, где
streak дотянул до N (due_today.py сам предлагает `--min-streak 20`, когда
автоматическая растяжка интервала уже на потолке — дальше понижать частоту
можно только явным решением: снизить важность или пометить канал нерабочим).

Запуск:
    python3 channel_health.py [social.db] [--min-importance N] [--layer archived|dormant|rare|active]
                               [--min-streak N]
    (по умолчанию --min-importance 2 — как и в due_today.py, важность 1 не в счёт)
"""
import sqlite3, sys
from datetime import date

sys.path.insert(0, ".")
import due_today as D
from _cli import positionals, require_db   # см. _cli.py

args = positionals(sys.argv[1:], ("--min-importance", "--layer", "--min-streak"))
DB = require_db(args[0] if args else "social.db")
MIN_IMPORTANCE = 2
LAYER_FILTER = None
MIN_STREAK = None
for i, a in enumerate(sys.argv):
    if a == "--min-importance" and i + 1 < len(sys.argv):
        MIN_IMPORTANCE = int(sys.argv[i + 1])
    if a == "--layer" and i + 1 < len(sys.argv):
        LAYER_FILTER = sys.argv[i + 1]
    if a == "--min-streak" and i + 1 < len(sys.argv):
        MIN_STREAK = int(sys.argv[i + 1])

LAYER_ORDER = {"archived": 0, "dormant": 1, "rare": 2, "active": 3}


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    today = date.today()
    streaks = D.compute_empty_streaks(con)

    people = {}
    for pid, name, imp, circle, note, _ in cur.execute(D.PEOPLE_SQL):
        if imp < MIN_IMPORTANCE:
            continue
        people[pid] = dict(name=name, importance=imp, circle=circle, note=note)

    rows = []
    for aid, pid, net, url, last_checked, last_post in cur.execute(D.ACCOUNTS_SQL):
        info = people.get(pid)
        if not info:
            continue
        lyr = D.layer(last_post, today)
        if LAYER_FILTER and lyr != LAYER_FILTER:
            continue
        streak = streaks.get(aid, 0)
        if MIN_STREAK is not None and streak < MIN_STREAK:
            continue
        rows.append(dict(account_id=aid, person_id=pid, name=info["name"],
                          importance=info["importance"], circle=info["circle"],
                          note=info["note"], network=net, layer=lyr,
                          last_post=last_post, last_checked=last_checked, url=url,
                          empty_streak=streak))
    con.close()

    sort_key = (lambda r: (-r["empty_streak"], -r["importance"], r["name"])) if MIN_STREAK is not None \
        else (lambda r: (LAYER_ORDER.get(r["layer"], 9), -r["importance"], r["name"]))
    rows.sort(key=sort_key)

    label = f" (слой={LAYER_FILTER})" if LAYER_FILTER else ""
    label += f" (streak>={MIN_STREAK})" if MIN_STREAK is not None else ""
    print(f"{len(rows)} каналов{label}, важность >= {MIN_IMPORTANCE}\n")
    for r in rows:
        seen = r["last_post"] or "ни разу"
        note = f"  — {r['note']}" if r["note"] and (LAYER_FILTER or MIN_STREAK is not None) else ""
        streak_s = f" streak={r['empty_streak']}" if r["empty_streak"] else ""
        # circle у человека бывает не проставлен (очередь разметки спрашивает
        # важность и круг по отдельности) — формат-спека на None падает TypeError
        # и роняет весь список, а не одну строку.
        print(f"  [{r['importance']}] {r['name']:<28} {(r['circle'] or '—'):<16} "
              f"{r['network']:<10} {r['layer']:<8} посл. пост {seen}{streak_s}{note}")

    if not LAYER_FILTER and MIN_STREAK is None:
        archived = sum(1 for r in rows if r["layer"] == "archived")
        print(f"\nиз них archived: {archived} — "
              f"python3 channel_health.py {DB} --layer archived, чтобы увидеть только их")


if __name__ == "__main__":
    main()
