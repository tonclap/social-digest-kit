#!/usr/bin/env python3
"""
Сливает IG-плейсхолдер-человека (person создан только из handle, без vk/fb)
в реального человека, когда bio-ссылка на IG однозначно указывает на его
уже известный vk/fb аккаунт.

Использование: merge_person.py <db> <placeholder_person_id> <real_person_id> [--apply]
Переносит все accounts и observations placeholder -> real, затем удаляет
placeholder person (если после переноса у него не осталось своих записей).
"""
import sqlite3, sys

APPLY = "--apply" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("--")]
DB, placeholder_id, real_id = args[0], int(args[1]), int(args[2])

con = sqlite3.connect(DB)
cur = con.cursor()

ph = cur.execute("SELECT id, display_name FROM people WHERE id=?", (placeholder_id,)).fetchone()
real = cur.execute("SELECT id, display_name FROM people WHERE id=?", (real_id,)).fetchone()
if not ph or not real:
    print("person not found:", placeholder_id, real_id); sys.exit(1)

accs = cur.execute("SELECT id, network, url FROM accounts WHERE person_id=?", (placeholder_id,)).fetchall()
print(f"merging person {placeholder_id} ({ph[1]!r}) -> {real_id} ({real[1]!r})")
print(f"  moving accounts: {accs}")

if APPLY:
    cur.execute("UPDATE accounts SET person_id=? WHERE person_id=?", (real_id, placeholder_id))
    left = cur.execute("SELECT COUNT(*) FROM accounts WHERE person_id=?", (placeholder_id,)).fetchone()[0]
    if left == 0:
        cur.execute("DELETE FROM people WHERE id=?", (placeholder_id,))
        print(f"  placeholder person {placeholder_id} removed (no accounts left)")
    else:
        print(f"  WARNING: placeholder still has {left} accounts, not removed")
    con.commit()
else:
    print("  (пробный прогон — ничего не записано, добавь --apply)")
con.close()
