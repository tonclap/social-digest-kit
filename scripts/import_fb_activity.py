#!/usr/bin/env python3
"""
Загружает результат порции fb_activity_*.json в social.db (таблица observations,
source='fb_page'). После этого export_fb_batch.py сам выдаст следующую порцию —
уже обойдённые он не предлагает.

Запуск: python3 import_fb_activity.py <fb_activity_*.json> [social.db]
"""
import json, sqlite3, sys
from datetime import date, datetime, timezone

SRC = sys.argv[1]
DB = sys.argv[2] if len(sys.argv) > 2 else "social.db"
TODAY = date.today().isoformat()

data = json.load(open(SRC, encoding="utf-8"))
con = sqlite3.connect(DB)
cur = con.cursor()

ins = skip = dated = errs = 0
for p in data["people"]:
    aid = p["account_id"]
    if cur.execute("""SELECT 1 FROM observations WHERE account_id=? AND checked_at=?
                      AND source='fb_page'""", (aid, TODAY)).fetchone():
        skip += 1
        continue
    if p.get("error"):
        errs += 1
        continue                          # любая ошибка (checkpoint/timeout/iframe и т.п.) —
                                           # не считаем проверкой, профиль должен попасть в
                                           # следующую порцию, а не выпасть из очереди навсегда
    ts = p.get("last_post_ts")
    post_date = (datetime.fromtimestamp(ts, timezone.utc).date().isoformat() if ts else None)
    cur.execute("""INSERT INTO observations(account_id, checked_at, found_post, post_date,
                                            post_url, summary, source)
                   VALUES (?,?,?,?,?,?, 'fb_page')""",
                (aid, TODAY, 1 if ts else 0, post_date, p["url"], None))
    ins += 1
    dated += 1 if ts else 0

con.commit()
print(f"порция от {data.get('collected_at','?')[:16]}: {len(data['people'])} профилей за "
      f"{data.get('seconds','?')} с")
print(f"  записано наблюдений: {ins} (дата найдена у {dated})")
print(f"  повторов пропущено:  {skip}")
print(f"  с ошибкой:           {errs}")
left = cur.execute("""SELECT COUNT(*) FROM accounts a WHERE a.network='facebook'
                      AND a.url IS NOT NULL AND COALESCE(a.is_dead,0)=0
                      AND NOT EXISTS (SELECT 1 FROM observations o
                                      WHERE o.account_id=a.id AND o.source='fb_page')"""
                   ).fetchone()[0]
print(f"  осталось обойти:     {left}")
con.close()
