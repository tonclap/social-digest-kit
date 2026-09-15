#!/usr/bin/env python3
"""
Разовая починка наблюдений, записанных до правок 15.09.2026. Правки в коде
чинят то, что пишется ДАЛЬШЕ; в уже накопленном журнале строки остаются
кривыми, и обе проверки повторов продолжают спотыкаться о них.

Две вещи, обе — только про source='fb_page'/'ig_page' (пачечный обход FB и
обход IG), ручной сбор через record_browser_observations.py не трогается:

1. **post_url = адрес профиля** (всегда, по умолчанию). Импортёры писали в
   «ссылку на пост» ссылку на страницу человека — одинаковую у всех его
   наблюдений. flag_repeat_posts.py и check_digest_completeness.py сравнивают
   post_url в первую очередь, поэтому КАЖДАЯ находка в этих сетях выглядела
   повтором прошлой проверки. Обнуляется только то, что дословно совпадает с
   accounts.url этого же аккаунта: настоящие ссылки на посты остаются.

2. **authored_by_owner у старых fb_page** (только с `--fb-authorship`).
   Пачечный обход снимает максимальный таймстемп со страницы регуляркой и не
   различает, чей это пост; с 15.09.2026 такие наблюдения пишутся с
   authored_by_owner=0. Старые записаны без поля, то есть NULL, а
   COALESCE(...,1)=1 в due_today.py читает NULL как «писал сам». Переписывать
   ли историю — решение владельца, поэтому отдельным флагом: после него часть
   FB-каналов уедет в более редкую проверку (слой станет считаться без этих
   находок).

Запуск:
    python3 fix_legacy_observations.py [social.db]                  # только показать
    python3 fix_legacy_observations.py [social.db] --apply
    python3 fix_legacy_observations.py [social.db] --apply --fb-authorship
"""
import sqlite3, sys

from _cli import positionals, require_db   # см. _cli.py

args = positionals(sys.argv[1:], ())
DB = require_db(args[0] if args else "social.db")
APPLY = "--apply" in sys.argv
FB_AUTHORSHIP = "--fb-authorship" in sys.argv

PROFILE_URL_SQL = """
    SELECT COUNT(*) FROM observations o
     WHERE o.source IN ('fb_page','ig_page')
       AND o.post_url IS NOT NULL
       AND o.post_url = (SELECT a.url FROM accounts a WHERE a.id = o.account_id)
"""
REAL_URL_SQL = """
    SELECT COUNT(*) FROM observations o
     WHERE o.source IN ('fb_page','ig_page')
       AND o.post_url IS NOT NULL
       AND o.post_url <> (SELECT a.url FROM accounts a WHERE a.id = o.account_id)
"""
FB_NULL_OWNER_SQL = """
    SELECT COUNT(*) FROM observations
     WHERE source='fb_page' AND found_post=1 AND authored_by_owner IS NULL
"""

con = sqlite3.connect(DB)
cur = con.cursor()
profile_urls = cur.execute(PROFILE_URL_SQL).fetchone()[0]
real_urls = cur.execute(REAL_URL_SQL).fetchone()[0]
fb_null_owner = cur.execute(FB_NULL_OWNER_SQL).fetchone()[0]

print(f"наблюдений FB/IG со ссылкой на ПРОФИЛЬ вместо поста: {profile_urls}")
print(f"  (ссылок, не совпадающих с профилем, — {real_urls}, их не трогаем)")
print(f"старых fb_page с authored_by_owner IS NULL: {fb_null_owner}"
      + ("" if FB_AUTHORSHIP else "  — переписать: --fb-authorship"))

if APPLY:
    cur.execute("""
        UPDATE observations SET post_url = NULL
         WHERE source IN ('fb_page','ig_page')
           AND post_url IS NOT NULL
           AND post_url = (SELECT a.url FROM accounts a WHERE a.id = observations.account_id)
    """)
    print(f"\nобнулено post_url: {cur.rowcount}")
    if FB_AUTHORSHIP:
        cur.execute("""UPDATE observations SET authored_by_owner = 0
                        WHERE source='fb_page' AND found_post=1 AND authored_by_owner IS NULL""")
        print(f"проставлено authored_by_owner=0: {cur.rowcount}")
    con.commit()
    print(f"integrity_check={cur.execute('PRAGMA integrity_check').fetchone()[0]}")
else:
    print("\n(пробный прогон — ничего не записано, добавь --apply)")
con.close()
