#!/usr/bin/env python3
"""
Помечает "не рабочие" каналы — сети, где у человека последний найденный пост
старше 365 дней (тот же порог, что и layer()=="archived" в due_today.py).
По прямой просьбе владельца ("пометь везде не рабочий канал") после того, как
в дайджесте VK 3-5 он на глаз заметил два заброшенных канала (VK с последним
постом 2011 года и давно молчащий Instagram) — 06.08.2026, следом за
добавлением слоя archived.

Пишет ДВА разных места, с разным смыслом:

1. **`accounts.dormant_channel`** (новое поле, INTEGER DEFAULT 0) — машиночитаемый
   флаг per-account, ПОЛНЫЙ РЕСИНК при каждом запуске: ставит 1 всем текущим
   archived-каналам и СБРАСЫВАЕТ в 0 те, что раньше были помечены, но перестали
   быть archived (например, у человека наконец нашёлся свежий пост). Это НЕ
   влияет на расписание due_today.py — оно само каждый раз заново считает layer()
   и не читает этот флаг; колонка — только для того, чтобы можно было быстро
   отфильтровать "неактивные каналы" запросом, не гоняя channel_health.py.

2. **`people.note`** — короткий человекочитаемый тег в конце заметки, вида
   "[VK неактивен, посл. пост 2011-03-27]". Существующий текст note НИКОГДА не
   стирается (как и в mark_deceased.py) — скрипт снимает только СВОИ СОБСТВЕННЫЕ
   теги этого формата (по регулярке) и переписывает их заново по актуальному
   состоянию при каждом запуске, остальной текст note остаётся как был. Поэтому
   повторный запуск не плодит дубли и не оставляет протухший тег, если канал
   снова ожил.

Только полностью механическая правка — порог "365 дней без поста" не требует
разбора вручную (в отличие, например, от telegram_scan.py, где каждая ссылка
нуждается в человеческом суждении). Поэтому, в отличие от mark_deceased.py,
--apply не требует списка id — применяется сразу ко всем, кто подходит по
--min-importance (по умолчанию 2, как и весь мониторинг в due_today.py).

Запуск:
    python3 mark_dormant_channels.py [social.db] [--min-importance N] [--apply]
    (без --apply — dry-run, только печатает, что будет сделано)
"""
import re, sqlite3, sys
from datetime import date

sys.path.insert(0, ".")
import due_today as D

args = [a for a in sys.argv[1:] if not a.startswith("--")]
DB = args[0] if args else "social.db"
APPLY = "--apply" in sys.argv
MIN_IMPORTANCE = 2
for i, a in enumerate(sys.argv):
    if a == "--min-importance" and i + 1 < len(sys.argv):
        MIN_IMPORTANCE = int(sys.argv[i + 1])

NET_LABEL = {"vk": "VK", "facebook": "Facebook", "instagram": "Instagram"}
TAG_RE = re.compile(r"\s*\[(?:VK|Facebook|Instagram) неактивен, посл\. пост [\d-]+\]")


def ensure_column(con):
    try:
        con.execute("ALTER TABLE accounts ADD COLUMN dormant_channel INTEGER DEFAULT 0")
        con.commit()
        print("accounts.dormant_channel добавлена (первый запуск)")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e):
            raise


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    today = date.today()

    ensure_column(con)

    people = {}
    for pid, name, imp, circle, note, _ in cur.execute(D.PEOPLE_SQL):
        if imp < MIN_IMPORTANCE:
            continue
        people[pid] = dict(name=name, note=note)

    current_flags = dict(cur.execute("SELECT id, COALESCE(dormant_channel,0) FROM accounts"))

    # currently-archived аккаунты по каждому подходящему человеку, для пересборки
    # тегов в note — и заодно для ресинка dormant_channel
    archived_by_person = {}
    to_mark, to_clear = [], []
    for aid, pid, net, url, last_checked, last_post in cur.execute(D.ACCOUNTS_SQL):
        if pid not in people:
            continue
        is_archived = D.layer(last_post, today) == "archived"
        was = current_flags.get(aid, 0)
        if is_archived:
            archived_by_person.setdefault(pid, []).append((net, last_post))
            if not was:
                to_mark.append((aid, pid, net, last_post))
        elif was:
            to_clear.append((aid, pid, net, last_post))

    print(f"важность >= {MIN_IMPORTANCE}: accounts.dormant_channel — "
          f"{len(to_mark)} пометить, {len(to_clear)} снять")
    for aid, pid, net, last_post in to_mark[:15]:
        print(f"  + [{aid}] {people[pid]['name']} {net} посл.пост {last_post}")
    if len(to_mark) > 15:
        print(f"  ... и ещё {len(to_mark) - 15}")
    for aid, pid, net, last_post in to_clear:
        print(f"  - [{aid}] {people[pid]['name']} {net} больше не archived (посл.пост {last_post})")

    # пересобираем note-теги для всех людей, у кого есть хоть один archived-аккаунт
    # СЕЙЧАС, плюс для тех, у кого раньше были теги, но сейчас архивных каналов
    # не осталось (нужно снять тег) — вторые вычисляются через TAG_RE на самом note.
    note_updates = []
    touched_people = set(archived_by_person) | {
        pid for pid, info in people.items() if info["note"] and TAG_RE.search(info["note"])
    }
    for pid in touched_people:
        info = people[pid]
        old_note = info["note"] or ""
        base = TAG_RE.sub("", old_note).rstrip()
        nets = sorted(archived_by_person.get(pid, []), key=lambda x: x[0])
        tags = "".join(f" [{NET_LABEL[net]} неактивен, посл. пост {lp}]" for net, lp in nets)
        new_note = (base + tags).strip() or None
        if new_note != (old_note or None):
            note_updates.append((pid, info["name"], old_note, new_note))

    print(f"\npeople.note: {len(note_updates)} человек — обновить тег(и)")
    for pid, name, old, new in note_updates[:15]:
        print(f"  {pid} {name!r}:")
        print(f"      было: {old!r}")
        print(f"      станет: {new!r}")
    if len(note_updates) > 15:
        print(f"  ... и ещё {len(note_updates) - 15}")

    if APPLY:
        for aid, *_ in to_mark:
            con.execute("UPDATE accounts SET dormant_channel=1 WHERE id=?", (aid,))
        for aid, *_ in to_clear:
            con.execute("UPDATE accounts SET dormant_channel=0 WHERE id=?", (aid,))
        for pid, name, old, new in note_updates:
            con.execute("UPDATE people SET note=? WHERE id=?", (new, pid))
        con.commit()
        ok = cur.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"\nприменено. integrity_check={ok}")
    else:
        print("\n(пробный прогон — ничего не записано, добавь --apply)")
    con.close()


if __name__ == "__main__":
    main()
