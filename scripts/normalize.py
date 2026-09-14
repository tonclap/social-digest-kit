#!/usr/bin/env python3
"""Приведение базы в порядок после первого импорта VK-выгрузки.

1. in_contacts: yaml-импорт превратил yes/no в 1/0 — чиним; новым людям считаем заново.
2. accounts.in_friends: помечаем VK-аккаунты, которых нет в реальном списке друзей
   (перепись их содержала, а friends.get — нет: отписки, устаревшие строки, дубли имён).
"""
import json, re, sqlite3, sys, unicodedata

DB = sys.argv[1] if len(sys.argv) > 1 else "social.db"
SWEEP = sys.argv[2] if len(sys.argv) > 2 else None
CONTACTS = sys.argv[3] if len(sys.argv) > 3 else None


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower().replace("ё", "е")
    return " ".join(re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE).split())


def key(url):
    if not url:
        return None
    u = re.sub(r"^https?://", "", url.strip().lower())
    return re.sub(r"^(m\.|www\.)", "", u).rstrip("/").replace("vk.ru/", "vk.com/")


con = sqlite3.connect(DB)
cur = con.cursor()

# 1. in_contacts
cur.execute("UPDATE people SET in_contacts='yes' WHERE in_contacts IN ('1','True','true')")
cur.execute("UPDATE people SET in_contacts='no'  WHERE in_contacts IN ('0','False','false')")
fixed = cur.rowcount
if CONTACTS:
    keys = set()
    for line in open(CONTACTS, encoding="utf-8", errors="replace"):
        k = " ".join(sorted(norm(line).split()))
        if len(k.split()) >= 2:
            keys.add(k)
    n = 0
    for pid, nm in cur.execute("SELECT id, display_name FROM people WHERE in_contacts IS NULL").fetchall():
        v = "yes" if " ".join(sorted(norm(nm).split())) in keys else "no"
        cur.execute("UPDATE people SET in_contacts=? WHERE id=?", (v, pid))
        n += 1
    print(f"in_contacts: нормализовано {fixed}, досчитано {n}")

# 2. кто реально в друзьях
cols = {r[1] for r in cur.execute("PRAGMA table_info(accounts)")}
if "in_friends" not in cols:
    cur.execute("ALTER TABLE accounts ADD COLUMN in_friends INTEGER")
if SWEEP:
    data = json.load(open(SWEEP, encoding="utf-8"))
    live = set()
    for p in data["people"]:
        live.add(key(p["url"]))
        live.add(key(f"vk.com/id{p['id']}"))
    yes = no = 0
    for aid, url in cur.execute("SELECT id, url FROM accounts WHERE network='vk'").fetchall():
        v = 1 if key(url) in live else 0
        cur.execute("UPDATE accounts SET in_friends=? WHERE id=?", (v, aid))
        yes, no = yes + v, no + (1 - v)
    print(f"VK-аккаунтов в реальном списке друзей: {yes}, вне его: {no}")

con.commit()
q = lambda s: cur.execute(s).fetchone()[0]
print("in_contacts=yes:", q("SELECT COUNT(*) FROM people WHERE in_contacts='yes'"))
print("всего людей:", q("SELECT COUNT(*) FROM people"))
con.close()
