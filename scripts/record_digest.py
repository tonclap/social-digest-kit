#!/usr/bin/env python3
"""
Записывает состав готового дайджеста в базу — чтобы завтра не пересказать то же самое.

На вход JSON: [{"person_id":1,"networks":"vk","category":"povod","summary":"…",
                "post_url":"https://vk.com/wall...","post_date":"2026-08-19"}, …]
Категории: povod | tema | po_lyudyam | ne_voshlo
`post_url`/`post_date` — необязательные (могут отсутствовать, например для
категории `tema`, где находка не привязана к одному посту, или когда FB/IG
не дали достать ссылку на конкретный пост) — но если они есть у находки,
передавать их: это то, чего раньше не хватало для честной сверки «уже
показывали владельцу» (20.08.2026 — дедуп «уже пересказывалось» сравнивался
с текстом дайджеста и не работал; post_url/post_date в digest_items — задел
на будущее, чтобы при
желании можно было сверяться с историей дайджестов напрямую, а не только с
последним наблюдением канала, как делает flag_repeat_posts.py сейчас).

Запуск: python3 record_digest.py <items.json> [social.db] [--date YYYY-MM-DD]
"""
import json, sqlite3, sys
from datetime import date

args = [a for a in sys.argv[1:] if not a.startswith("--")]
SRC = args[0]
DB = args[1] if len(args) > 1 else "social.db"
DAY = date.today().isoformat()
for i, a in enumerate(sys.argv):
    if a == "--date" and i + 1 < len(sys.argv):
        DAY = sys.argv[i + 1]

items = json.load(open(SRC, encoding="utf-8"))
con = sqlite3.connect(DB)
cur = con.cursor()
ok = bad = 0
for it in items:
    pid = it.get("person_id")
    if not cur.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone():
        print(f"  нет человека id={pid}, пропускаю")
        bad += 1
        continue
    cur.execute("""INSERT INTO digest_items(digest_date, person_id, networks, category,
                                            summary, post_url, post_date)
                   VALUES (?,?,?,?,?,?,?)""",
                (DAY, pid, it.get("networks"), it.get("category"), it.get("summary"),
                 it.get("post_url"), it.get("post_date")))
    ok += 1
con.commit()
print(f"дайджест {DAY}: записано {ok} строк" + (f", пропущено {bad}" if bad else ""))
with_url = sum(1 for it in items if it.get("post_url"))
print(f"  из них с post_url: {with_url}/{ok}" if ok else "")
for cat, n in cur.execute("""SELECT category, COUNT(*) FROM digest_items
                             WHERE digest_date=? GROUP BY 1""", (DAY,)):
    print(f"  {cat}: {n}")
con.close()
