#!/usr/bin/env python3
"""
Отмечает человека как умершего — единая точка входа вместо ручного SQL.

Ставит:
  - accounts.is_dead = 1 на ВСЕХ аккаунтах человека (это уже само по себе
    исключает его из due_today.py / iter_due() — см. ACCOUNTS_SQL там же);
  - people.is_deceased = 1 — отдельный от is_dead маркер. is_dead исторически
    используется и для других причин исключения из мониторинга (аккаунт удалён,
    недоступен, "не открывается" и т.п. — таких сейчас в базе большинство),
    поэтому по одному only is_dead нельзя понять, кто из отмеченных людей
    реально умер, а кто просто больше не мониторится. is_deceased ставится
    ТОЛЬКО через этот скрипт и означает именно смерть (подтверждённую или
    предполагаемую по сильному сигналу вроде мемориального статуса FB) — см.
    note конкретного человека для формулировки уверенности.
  - people.note — если note уже пуст, записывает переданный текст. Если note
    уже что-то содержит, НЕ перезаписывает (чтобы не затереть вручную
    вписанный текст) — печатает предупреждение, правь note руками.

Не трогает importance и circle — это поля владельца.

Запуск (без --apply — только показывает, что будет сделано):
    python3 mark_deceased.py social.db <person_id> "<note, напр. 'Умер, подтверждено владельцем напрямую 06.08.2026.'>" [--apply]
"""
import sqlite3, sys
from datetime import date
from _cli import positionals, require_db   # см. _cli.py

APPLY = "--apply" in sys.argv
args = positionals(sys.argv[1:], ())
if len(args) < 3:
    print(__doc__)
    sys.exit(1)
DB, person_id, note_text = require_db(args[0]), int(args[1]), args[2]

con = sqlite3.connect(DB)
cur = con.cursor()

person = cur.execute(
    "SELECT id, display_name, is_deceased, note FROM people WHERE id=?", (person_id,)
).fetchone()
if not person:
    print(f"person {person_id} not found"); sys.exit(1)
pid, name, already, existing_note = person

accs = cur.execute(
    "SELECT id, network, is_dead FROM accounts WHERE person_id=?", (pid,)
).fetchall()

print(f"person {pid} ({name!r}) — уже is_deceased={already}")
print(f"  аккаунты: {[(a[1], 'is_dead=' + str(a[2])) for a in accs]}")
if existing_note:
    print(f"  note уже не пуст, оставляю как есть: {existing_note!r}")
else:
    print(f"  note будет: {note_text!r}")

if APPLY:
    cur.execute("UPDATE accounts SET is_dead=1 WHERE person_id=?", (pid,))
    cur.execute("UPDATE people SET is_deceased=1 WHERE id=?", (pid,))
    if not existing_note:
        cur.execute("UPDATE people SET note=? WHERE id=?", (note_text, pid))
    con.commit()
    ok = cur.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"  applied. integrity_check={ok}")
else:
    print("  (пробный прогон — ничего не записано, добавь --apply)")
con.close()
