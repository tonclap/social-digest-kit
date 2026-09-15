#!/usr/bin/env python3
"""
Загружает результат порции fb_activity_*.json в social.db (таблица observations,
source='fb_page'). После этого export_fb_batch.py сам выдаст следующую порцию —
уже обойдённые он не предлагает.

15.09.2026: в observations.post_url писался адрес ПРОФИЛЯ (`p["url"]`) — он
одинаков у всех наблюдений канала, а flag_repeat_posts.py и
check_digest_completeness.py сравнивают именно post_url в первую очередь. Из-за
этого КАЖДАЯ находка в FB (и в IG, там была та же строка) объявлялась повтором
прошлой проверки, даже когда дата поста изменилась. Теперь пишется NULL.

Наблюдения, записанные до этой правки, чинятся разово — по равенству адресу
профиля, чтобы не тронуть настоящие ссылки на посты из ручного обхода:

    UPDATE observations SET post_url = NULL
     WHERE source IN ('fb_page','ig_page')
       AND post_url = (SELECT a.url FROM accounts a WHERE a.id = observations.account_id);

15.09.2026, вторая правка: **этот метод сбора не знает, кто автор.** Скрипт в
export_fb_batch.py снимает со страницы профиля максимальный `creation_time` /
`publish_time` регуляркой по всему HTML — в него одинаково попадают и посты
человека, и чужие поздравления с ДР на стене, и гостевые записи, и теги. Ровно
то, ради чего 10.08.2026 заводилось поле `authored_by_owner`; по вашему же
`calibration_report.py` чужой контент — 36.9% находок в FB. Раньше наблюдение
писалось без этого поля, а `COALESCE(authored_by_owner,1)=1` в due_today.py
читает NULL как «писал сам»: пачечный обход «освежал» слой активности канала
чужими действиями.

Теперь пачечный обход пишет `authored_by_owner=0` — «нашли на странице, но что
это его слова, метод не подтверждает». Следствие, которое надо понимать:
канал, по которому есть ТОЛЬКО пачечные наблюдения, для расписания выглядит
молчащим и проверяется реже (слой dormant + backoff по empty_streak). Тон
задаёт ручной обход через `record_browser_observations.py` — он автора
различает, это и есть основной путь по SKILL.md, а пачка закрывает бэклог.
Вернуть прежнее поведение — `--assume-owner`.

Запуск: python3 import_fb_activity.py <fb_activity_*.json> [social.db]
                                      [--assume-owner]
"""
import json, sqlite3, sys
from datetime import date, datetime, timezone
from _cli import positionals, require_db   # см. _cli.py

args = positionals(sys.argv[1:], ())
ASSUME_OWNER = "--assume-owner" in sys.argv
SRC = args[0]
DB = require_db(args[1] if len(args) > 1 else "social.db")
TODAY = date.today().isoformat()

# NULL = «сам человек» (как читает COALESCE в due_today.py), 0 = «автор не
# подтверждён». По умолчанию 0: см. правку от 15.09.2026 в шапке файла.
OWNER_FLAG = None if ASSUME_OWNER else 0
UNKNOWN_AUTHOR_SUMMARY = ("дата снята со страницы профиля пачечным обходом; "
                          "автор поста этим методом не определяется")

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
    # post_url остаётся NULL: этот метод сбора ссылку на КОНКРЕТНЫЙ пост не достаёт,
    # он снимает только максимальный creation_time со страницы профиля. Раньше сюда
    # писался p["url"] — адрес самого ПРОФИЛЯ, одинаковый у всех наблюдений канала:
    # flag_repeat_posts.py и check_digest_completeness.py сравнивают post_url в первую
    # очередь и поэтому объявляли повтором КАЖДУЮ FB-находку, даже когда дата поста
    # изменилась. С NULL сравнение падает на post_date — ровно то, что обе проверки
    # и описывают у себя в докстрингах для сетей без ссылки на пост.
    cur.execute("""INSERT INTO observations(account_id, checked_at, found_post, post_date,
                                            post_url, summary, source, authored_by_owner)
                   VALUES (?,?,?,?,?,?, 'fb_page', ?)""",
                (aid, TODAY, 1 if ts else 0, post_date, None,
                 (UNKNOWN_AUTHOR_SUMMARY if ts and not ASSUME_OWNER else None),
                 OWNER_FLAG if ts else None))
    ins += 1
    dated += 1 if ts else 0

con.commit()
print(f"порция от {data.get('collected_at','?')[:16]}: {len(data['people'])} профилей за "
      f"{data.get('seconds','?')} с")
print(f"  записано наблюдений: {ins} (дата найдена у {dated})")
print("  авторство: " + ("считаем находку собственным постом (--assume-owner)"
                         if ASSUME_OWNER else
                         "authored_by_owner=0 — пачечный обход автора не различает, "
                         "слой активности такие находки не освежают"))
print(f"  повторов пропущено:  {skip}")
print(f"  с ошибкой:           {errs}")
left = cur.execute("""SELECT COUNT(*) FROM accounts a WHERE a.network='facebook'
                      AND a.url IS NOT NULL AND COALESCE(a.is_dead,0)=0
                      AND NOT EXISTS (SELECT 1 FROM observations o
                                      WHERE o.account_id=a.id AND o.source='fb_page')"""
                   ).fetchone()[0]
print(f"  осталось обойти:     {left}")
con.close()
