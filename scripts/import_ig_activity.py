#!/usr/bin/env python3
"""
Загружает результат порции ig_activity_*.json в social.db (таблица observations,
source='ig_page'). Формат входного JSON — как у людей из fb_activity (людей
собирает Claude вручную через Claude in Chrome, см. export_ig_batch.py):
{"collected_at": "...", "people": [{"account_id":.., "url":.., "ok":bool,
"last_post_ts":int|null, "error":str|null,
"bio_link":str|null, "bio_header_name":str|null}, ...]}

bio_link/bio_header_name — только для целей, у которых export_ig_batch.py
проставил need_bio:true (IG-заглушки). Если поля присутствуют в записи (даже
пустой строкой — значит посмотрели и не нашли ничего), пишем в accounts и
проставляем bio_captured_at, чтобы профиль больше не попадал в очередь на bio.
Если полей нет вовсе в записи — это обычный не-заглушка аккаунт, bio не трогаем.

Запуск: python3 import_ig_activity.py <ig_activity_*.json> [social.db]
"""
import json, sqlite3, sys
from datetime import date, datetime, timezone

SRC = sys.argv[1]
DB = sys.argv[2] if len(sys.argv) > 2 else "social.db"
TODAY = date.today().isoformat()

data = json.load(open(SRC, encoding="utf-8"))
con = sqlite3.connect(DB)
cur = con.cursor()

ins = skip = dated = errs = dead = bios = 0
for p in data["people"]:
    aid = p["account_id"]

    # bio можно записать независимо от того, есть ли новое наблюдение о посте
    # сегодня (профиль мог уже быть проверен на пост раньше) — поэтому эта
    # ветка не привязана к "continue" ниже.
    if "bio_link" in p or "bio_header_name" in p:
        cur.execute(
            """UPDATE accounts SET bio_link=?, bio_header_name=?, bio_captured_at=?
               WHERE id=?""",
            (p.get("bio_link") or None, p.get("bio_header_name") or None, TODAY, aid),
        )
        bios += 1

    if cur.execute("""SELECT 1 FROM observations WHERE account_id=? AND checked_at=?
                      AND source='ig_page'""", (aid, TODAY)).fetchone():
        skip += 1
        continue
    if p.get("error"):
        errs += 1
        if p["error"] == "not_found":
            # страница не существует (удалён/переименован аккаунт) — постоянное
            # состояние, а не сбой метода. Помечаем is_dead, чтобы профиль не
            # всплывал в export_ig_batch.py каждый раз заново.
            cur.execute("UPDATE accounts SET is_dead=1 WHERE id=?", (aid,))
            dead += 1
        continue                          # прочие ошибки (private/timeout и т.п.) — не
                                           # считаем проверкой, профиль остаётся в очереди
    ts = p.get("last_post_ts")
    post_date = (datetime.fromtimestamp(ts, timezone.utc).date().isoformat() if ts else None)
    # post_url — NULL, а не адрес профиля: см. тот же комментарий в
    # import_fb_activity.py. Профильная ссылка в поле «ссылка на пост» превращала
    # любую находку в «тот же пост, что и в прошлый раз» для обеих проверок повторов.
    cur.execute("""INSERT INTO observations(account_id, checked_at, found_post, post_date,
                                            post_url, summary, source)
                   VALUES (?,?,?,?,?,?, 'ig_page')""",
                (aid, TODAY, 1 if ts else 0, post_date, None, None))
    ins += 1
    dated += 1 if ts else 0

con.commit()
print(f"порция от {data.get('collected_at','?')[:16]}: {len(data['people'])} профилей")
print(f"  записано наблюдений: {ins} (дата найдена у {dated})")
print(f"  bio записано:        {bios}")
print(f"  повторов пропущено:  {skip}")
print(f"  с ошибкой:           {errs} (из них помечено is_dead: {dead})")
left = cur.execute("""SELECT COUNT(*) FROM accounts a WHERE a.network='instagram'
                      AND a.url IS NOT NULL AND COALESCE(a.is_dead,0)=0
                      AND NOT EXISTS (SELECT 1 FROM observations o
                                      WHERE o.account_id=a.id AND o.source='ig_page')"""
                   ).fetchone()[0]
left_bio = cur.execute("""SELECT COUNT(*) FROM accounts a JOIN people p ON p.id=a.person_id
                      WHERE a.network='instagram' AND COALESCE(a.is_dead,0)=0
                      AND NOT EXISTS (SELECT 1 FROM accounts a2 WHERE a2.person_id=p.id AND a2.network!='instagram')
                      AND a.bio_captured_at IS NULL"""
                   ).fetchone()[0]
print(f"  осталось обойти (пост):        {left}")
print(f"  осталось обойти (bio заглушек): {left_bio}")
con.close()
