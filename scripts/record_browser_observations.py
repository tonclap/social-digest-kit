#!/usr/bin/env python3
"""
Единая точка записи для наблюдений, собранных ВРУЧНУЮ через Claude in Chrome
(Facebook и Instagram, шаг 3 в SKILL.md) — вместо разового SQL по ходу сессии.

Раньше ручной обход писал в observations как придётся: иногда без summary
(пустая строка терялась при отказе доступа — profile недоступен/ограничен, и
причина оставалась только в тексте дайджеста, не в базе), почти всегда без
post_url (ссылка на конкретный пост нигде не сохранялась — в отличие от VK и
автоматического FB-сборщика, которые её пишут). Найдено и разобрано
06.08.2026 при анализе дайджеста.

Правила, которые этот скрипт заставляет соблюдать:
  - summary ОБЯЗАТЕЛЕН всегда, даже если found_post=0 (страница не открылась,
    доступ ограничен, стена пуста — коротко, но не NULL). Скрипт откажется
    писать запись без summary.
  - access_blocked=1 — отдельно от found_post, когда причина отсутствия поста
    в том, что профиль вообще не открылся / доступ ограничен (не спутать с
    «открылся нормально, но постов нет»). due_today.py на это пока не смотрит
    (человек всё равно попадёт в следующую проверку по расписанию), это чисто
    для истории/статистики — сколько раз подряд профиль был недоступен, стоит
    ли переводить в is_dead=1.
  - post_url — если во время обхода получилось достать ссылку на конкретный
    пост (например через read_page(filter="interactive") — искать ссылку на
    таймстемп поста, вид /posts/... или /permalink.php?...) — передать её.
    Не обязателен (для FB через ручной обход часто просто не достать без
    лишних кликов, которые рискуют капчей) — но если он есть, не терять.
  - authored_by_owner (добавлено 10.08.2026) — 0, если found_post=1, но найденное
    на странице НЕ написано самим человеком: поздравления с ДР от других на его
    стене, гостевой пост друга, тег в чужом посте, чьё-то воспоминание на его
    странице. По умолчанию (поле не передано) считается 1 — сам человек. Это
    важно для due_today.py: расписание проверок считает «человек активен» только
    по authored_by_owner=1 — иначе чужие поздравления на стене «освежают» канал
    так, будто владелец сам недавно писал, хотя это чистый шум чужих действий.
    На VK эта проблема не возникает (export_vk_daily.py тянет только
    wall.get(filter="owner")), это касается именно ручного обхода FB/IG.

Вход — JSON-список записей вида:
  [{"account_id": 123, "found_post": 1, "post_date": "2026-08-01",
    "post_url": "https://...", "summary": "..."},
   {"account_id": 456, "found_post": 0, "access_blocked": 1,
    "summary": "НЕ СОБРАНО: доступ к профилю ограничен или профиль недоступен"},
   {"account_id": 789, "found_post": 1, "post_date": "2026-08-01", "authored_by_owner": 0,
    "summary": "только поздравления с ДР от друзей — НЕ ВОШЛО"}]

Запуск:
    python3 record_browser_observations.py items.json social.db [--source browser]
"""
import json, sqlite3, sys
from datetime import date
from _cli import positionals, require_db   # см. _cli.py

args = positionals(sys.argv[1:], ("--source",))
ITEMS, DB = args[0], require_db(args[1] if len(args) > 1 else "social.db")
SOURCE = "browser"
for i, a in enumerate(sys.argv):
    if a == "--source" and i + 1 < len(sys.argv):
        SOURCE = sys.argv[i + 1]
TODAY = date.today().isoformat()

items = json.load(open(ITEMS, encoding="utf-8"))

bad = [it for it in items if not (it.get("summary") or "").strip()]
if bad:
    print(f"ОТКАЗ: {len(bad)} записей без summary — заполнить перед записью:")
    for it in bad:
        print(f"  account_id={it.get('account_id')}")
    sys.exit(1)

con = sqlite3.connect(DB)
cur = con.cursor()
n_written = n_blocked = n_with_url = 0
for it in items:
    aid = it["account_id"]
    if not cur.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone():
        print(f"  нет аккаунта id={aid}, пропускаю")
        continue
    found = 1 if it.get("found_post") else 0
    blocked = 1 if it.get("access_blocked") else 0
    # authored_by_owner: явный 0 — «найденное не от самого человека» (поздравления
    # от других, гостевой пост и т.п.); не передано — считаем 1 (сам человек).
    owner = 0 if it.get("authored_by_owner") == 0 else 1
    cur.execute("""INSERT INTO observations(account_id, checked_at, found_post, post_date,
                                            post_url, summary, source, access_blocked,
                                            authored_by_owner)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (aid, TODAY, found, it.get("post_date"), it.get("post_url"),
                 it["summary"], SOURCE, blocked, owner))
    n_written += 1
    n_blocked += blocked
    n_with_url += 1 if it.get("post_url") else 0

con.commit()
print(f"записано наблюдений: {n_written}")
print(f"  access_blocked=1: {n_blocked}")
print(f"  с post_url:       {n_with_url} из {n_written}"
      + ("" if n_with_url == n_written else " (для остальных ссылку на пост достать не удалось)"))
con.close()
