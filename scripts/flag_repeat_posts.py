#!/usr/bin/env python3
"""
Флагует находки за дату дайджеста, которые на самом деле — тот же самый пост,
что уже был найден на ПРЕДЫДУЩЕЙ проверке этого же канала (тот же post_url,
либо для аккаунтов без url — тот же post_date). due_today.py решает, пора ли
перепроверить канал, ИСХОДЯ ИЗ ДАТЫ последней проверки — но сам факт
"проверка due" не означает "на странице появилось что-то новое": если человек
не писал с прошлого захода, найдётся тот же самый пост, что и был. Компилятор
дайджеста (человек или Claude) должен исключать такие находки из «По людям»/
«Поводы»/«Темы дня» и относить их в «Не вошло» с пометкой «тот же пост, без
изменений с последней проверки» — но это делалось по памяти составителя и
поэтому терялось между прогонами (подтверждено на двух дайджестах подряд:
у пятерых человек в обеих проверках оказался один и тот же post_url).

20.08.2026, вторая правка: сравнение по FB/IG (где post_url часто не достаётся,
см. SKILL.md) раньше требовало ЕЩЁ И совпадения `summary` дословно — но summary
это пересказ, который Claude in Chrome пишет заново своими словами при каждом
обходе, он почти никогда не совпадает буквально, даже когда пост тот же самый.
Из-за этого пропустило ДВА реальных повтора на одном дайджесте: у обоих
пост уже был пересказан в более раннем дайджесте, post_date совпадал ровно,
но пересказ был написан другими словами — и сравнение текста промолчало.
Нашёл не автопроверкой, а потому что владелец сам заметил при
чтении. Теперь для каналов без post_url сравнение идёт ТОЛЬКО по post_date —
это менее точно (в редкой ситуации двух разных постов в один день даст
ложное срабатывание), но заметно надёжнее, чем сравнение текста, которое не
срабатывало почти никогда.

Запуск (после сбора наблюдений, ДО того как писать текст дайджеста):
    python3 flag_repeat_posts.py social.db [--date YYYY-MM-DD]

Печатает находки за дату, у которых предыдущая проверка ТОГО ЖЕ канала дала
тот же самый пост — их не пересказывать в «По людям» заново, а перенести в
«Не вошло» (или прямо переиспользовать готовую фразу из вывода). Находки без
post_url помечены как менее точное совпадение (по одной дате) — стоит бегло
свериться с текстом, что это правда тот же пост, а не два разных в один день.
Пустой вывод и код возврата 0 — повторов нет, можно писать дайджест как обычно.
"""
import sqlite3, sys
from datetime import date
from _cli import positionals, require_db   # см. _cli.py

args = positionals(sys.argv[1:], ("--date",))
DB = require_db(args[0] if args else "social.db")
DAY = date.today().isoformat()
for i, a in enumerate(sys.argv):
    if a == "--date" and i + 1 < len(sys.argv):
        DAY = sys.argv[i + 1]

con = sqlite3.connect(DB)
cur = con.cursor()

today_rows = cur.execute("""
    SELECT o.id, o.account_id, o.post_date, o.post_url, o.summary,
           p.id, p.display_name, p.importance, p.circle, a.network
    FROM observations o
    JOIN accounts a ON a.id = o.account_id
    JOIN people p ON p.id = a.person_id
    WHERE date(o.checked_at) = ? AND o.found_post = 1
""", (DAY,)).fetchall()

repeats = []
for oid, acc_id, post_date, post_url, summary, pid, name, imp, circle, net in today_rows:
    prev = cur.execute("""
        SELECT post_date, post_url, summary, checked_at FROM observations
        WHERE account_id = ? AND id < ? AND found_post = 1
        ORDER BY id DESC LIMIT 1
    """, (acc_id, oid)).fetchone()
    if not prev:
        continue
    prev_date, prev_url, prev_summary, prev_checked = prev
    same, exact = False, False
    if post_url and prev_url:
        same = exact = post_url == prev_url
    elif post_date and prev_date:
        # нет url (напр. FB через Claude in Chrome без ссылки) — сверяем ТОЛЬКО
        # дату: сравнение с текстом summary пропускало реальные повторы, см.
        # правку 20.08.2026 в шапке файла.
        same = post_date == prev_date
        exact = same and (summary or "") == (prev_summary or "")
    if same:
        repeats.append(dict(name=name, importance=imp, circle=circle, network=net,
                             post_date=post_date, post_url=post_url,
                             prev_checked=prev_checked, exact=exact))

print(f"находок с found_post=1 за {DAY}: {len(today_rows)}, из них повтор "
      f"предыдущей проверки того же канала: {len(repeats)}")
for r in sorted(repeats, key=lambda r: (-r["importance"], r["name"])):
    where = r["post_url"] or f"пост от {r['post_date']}"
    confidence = "" if r["exact"] else " (совпадение только по дате, без url и без точного текста — свериться глазами)"
    print(f"  [{r['importance']}] {r['name']} ({r['circle']}, {r['network']}): "
          f"{where} — тот же, что и на проверке {r['prev_checked']}{confidence}. "
          f"НЕ пересказывать в «По людям» — в «Не вошло»: "
          f"«тот же пост, что и в прошлый раз, ничего нового».")

con.close()
sys.exit(1 if repeats else 0)
