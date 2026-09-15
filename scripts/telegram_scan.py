#!/usr/bin/env python3
"""
Ищет упоминания Telegram-каналов в уже собранных данных (bio_summary,
observations.summary) и печатает кандидатов на запись в people.telegram_url.

НЕ пишет автоматически без разбора — telegram-ссылка в тексте поста часто
принадлежит не самому человеку, а тому, кого он цитирует/репостит/рекламирует.
Разметка "это правда его канал" — вручную, по контексту (see примеры контекста
в выводе): explicit "мой канал", "у меня есть канал", хэндл совпадает с именем,
профильная организация пишет "наш канал" — да; ссылка внутри рекламного поста,
цитаты, репоста чужого контента, списка ресурсов — нет.

Запуск:
    python3 telegram_scan.py social.db                     # только показать кандидатов
    python3 telegram_scan.py social.db --apply 110,66,455   # записать для этих person_id
                                                              # (берёт первую найденную
                                                              # ссылку для каждого, не
                                                              # трогает уже заполненный
                                                              # telegram_url)
"""
import re, sqlite3, sys
from _cli import positionals, require_db   # см. _cli.py

args = positionals(sys.argv[1:], ("--apply",))
DB = require_db(args[0] if args else "social.db")
APPLY = None
for i, a in enumerate(sys.argv):
    if a == "--apply" and i + 1 < len(sys.argv):
        APPLY = {int(x) for x in sys.argv[i + 1].split(",")}

# (?<![\w.-]) — граница слева: без неё «bot.me/x» и «client.me/x» внутри любого
# другого домена читались как телеграм-ссылка (совпадало хвостовое «t.me/x»).
URL_RE = re.compile(r'(?<![\w.-])(?:https?://)?(?:www\.)?t(?:elegram)?\.me/([a-zA-Z0-9_]{3,})', re.I)

con = sqlite3.connect(DB)
cur = con.cursor()
found = {}  # person_id -> [(source, link, context)]

for pid, name, bio in cur.execute("SELECT id, display_name, bio_summary FROM people WHERE bio_summary IS NOT NULL"):
    for m in URL_RE.finditer(bio):
        found.setdefault(pid, []).append(("bio", m.group(0), bio[max(0, m.start() - 40):m.end() + 10]))

for pid, name, summary in cur.execute("""
        SELECT p.id, p.display_name, o.summary FROM observations o
        JOIN accounts a ON a.id = o.account_id JOIN people p ON p.id = a.person_id
        WHERE o.summary IS NOT NULL"""):
    for m in URL_RE.finditer(summary):
        found.setdefault(pid, []).append(("observation", m.group(0), summary[max(0, m.start() - 40):m.end() + 10]))

names = dict(cur.execute("SELECT id, display_name FROM people"))
already = {pid for pid, url in cur.execute("SELECT id, telegram_url FROM people") if url}

if not APPLY:
    print(f"кандидатов: {len(found)} человек (уже заполнено у {len(already)})")
    for pid, hits in found.items():
        mark = " [уже заполнено]" if pid in already else ""
        print(f"  {pid} {names.get(pid)!r}{mark}")
        for src, link, ctx in hits:
            print(f"      {src}: {link}  | ...{ctx}...")
    print("\nНичего не записано. Разбери вручную и вызови с --apply id1,id2,...")
else:
    n = 0
    for pid in APPLY:
        if pid not in found:
            print(f"  {pid}: нет кандидатов, пропуск")
            continue
        link = found[pid][0][1]
        if not link.startswith("http"):
            link = "https://" + link
        cur.execute("UPDATE people SET telegram_url=? WHERE id=? AND telegram_url IS NULL", (link, pid))
        print(f"  {pid} {names.get(pid)!r} -> {link}")
        n += 1
    con.commit()
    print(f"записано: {n}")

con.close()
