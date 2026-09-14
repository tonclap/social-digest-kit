#!/usr/bin/env python3
"""
Готовит очередную порцию профилей Instagram для сбора активности.

02.08.2026: метод сбора Instagram — НЕ такой, как у Facebook. Скрытый iframe
у Instagram не работает вовсе (X-Frame-Options/CSP блокирует доступ к
contentDocument — проверено, "Cannot read properties of null (reading
'location')" сразу на первом же профиле). Значит, паста-скрипт в консоль,
который сам бегает по профилям в фоне, здесь невозможен: чтобы получить дату
последнего поста, нужна НАСТОЯЩАЯ навигация вкладки (открыть профиль → найти
ссылку на первый СВОЙ пост в сетке → открыть сам пост → вытащить "taken_at" из
HTML), а каждая настоящая навигация убивает JS-контекст предыдущей страницы.
Поэтому сбор IG ведёт сам Claude через Claude in Chrome, вручную по одному
профилю (навигация + чтение), а не бэкграунд-скрипт как для FB.

Этот скрипт просто печатает / сохраняет в JSON очередную порцию целей —
дальше Claude обходит их вживую в браузере.

ВАЖНО: сторис (Истории) никогда не открывать — их просмотр виден автору.
Обычные посты/рилсы смотреть безопасно (в отличие от сторис, не оставляют
файла "просмотрено" для автора).

Известная дыра метода: у Instagram есть до 3 «закреплённых» постов, которые
могут быть не самыми свежими, но лежат в сетке первыми — теоретически
last_post_ts может быть не совсем точным для аккаунтов с закрепом. Это
приемлемо для грубого сигнала активности, но стоит иметь в виду.

03.08.2026: добавлен сбор bio (внешняя ссылка в био + имя из шапки профиля)
для IG-заглушек (person, у которого из аккаунтов — только instagram). Цель —
дедупликация: bio-ссылка на vk/fb или совпадение реального имени с уже
известным человеком позволяют слить заглушку без отдельного захода вручную
(слияние — scripts/merge_person.py). Каждая цель в порции размечена флагом
"need_bio": true — значит, при посещении профиля нужно ЗАОДНО прочитать био
(верхний блок профиля: ссылка "Website"/внешняя ссылка + отображаемое имя в
шапке, НЕ описание) и положить их в поля "bio_link"/"bio_header_name" в
выходном JSON, даже если found_post/last_post_ts не изменились. Для целей без
этого флага (обычный, не-заглушка аккаунт) bio читать не нужно — экономим
время обхода.

Порядок очереди: сначала обычные ещё не проверенные на пост аккаунты
(как раньше), затем — уже проверенные на пост IG-заглушки, у которых ещё нет
bio (bio_captured_at IS NULL), и только когда оба этих бэклога исчерпаны —
перепроверка по расписанию (due_today.iter_due) у аккаунтов, которым давно
не смотрели пост. Так регулярный обход сам, без отдельного захода, закрывает
и пост-активность, и bio-дедупликацию, и (07.08.2026) не останавливается
навсегда после первого прохода.

07.08.2026: раньше скрипт умел только "первый проход" (пока есть ни разу не
проверенные/без bio аккаунты) и не знал про due_today.py вовсе — после того
как бэклог исчерпывался, порция была бы всегда пустой, и обход IG тихо бы
остановился. Плюс: важность 1 в выборку теперь не попадает, важность 2 —
не чаще раза в месяц (см. due_today.py).

Запуск: python3 export_ig_batch.py [social.db] [размер порции] [файл.json]
"""
import json, sqlite3, sys

DB = sys.argv[1] if len(sys.argv) > 1 else "social.db"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 10
OUT = sys.argv[3] if len(sys.argv) > 3 else "ig_batch.json"

sys.path.insert(0, ".")
import due_today as D

MIN_IMPORTANCE = "COALESCE(p.importance,0) >= 2"

IS_STUB = """
  NOT EXISTS (SELECT 1 FROM accounts a2 WHERE a2.person_id=p.id AND a2.network!='instagram')
"""

# приоритет 0 = обычный обход (пост ещё не собран), приоритет 1 = заглушка,
# пост уже собран (или собирается сейчас), но bio ещё нет
BACKLOG_SQL = f"""
SELECT a.id, a.handle, a.url, p.display_name,
       CASE WHEN {IS_STUB} THEN 1 ELSE 0 END AS need_bio,
       CASE WHEN NOT EXISTS (SELECT 1 FROM observations o
                             WHERE o.account_id=a.id AND o.source='ig_page')
            THEN 0 ELSE 1 END AS prio
FROM accounts a
JOIN people p ON p.id = a.person_id
WHERE a.network='instagram' AND a.url IS NOT NULL AND COALESCE(a.is_dead,0)=0
  AND {MIN_IMPORTANCE}
  AND (
        NOT EXISTS (SELECT 1 FROM observations o
                    WHERE o.account_id = a.id AND o.source='ig_page')
        OR ({IS_STUB} AND a.bio_captured_at IS NULL)
      )
ORDER BY prio ASC,
         COALESCE(p.importance, -1) DESC,
         (p.in_contacts='yes') DESC,
         (SELECT COUNT(DISTINCT network) FROM accounts a2 WHERE a2.person_id=p.id) DESC,
         p.display_name
LIMIT ?
"""

COUNT_SQL = f"""
SELECT
  SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM observations o
                            WHERE o.account_id=a.id AND o.source='ig_page') THEN 1 ELSE 0 END),
  SUM(CASE WHEN ({IS_STUB}) AND a.bio_captured_at IS NULL
            AND EXISTS (SELECT 1 FROM observations o
                        WHERE o.account_id=a.id AND o.source='ig_page') THEN 1 ELSE 0 END)
FROM accounts a
JOIN people p ON p.id = a.person_id
WHERE a.network='instagram' AND a.url IS NOT NULL AND COALESCE(a.is_dead,0)=0
  AND {MIN_IMPORTANCE}
"""


def main():
    con = sqlite3.connect(DB)

    backlog_rows = con.execute(BACKLOG_SQL, (N,)).fetchall()
    backlog_ids = {r[0] for r in backlog_rows}
    remaining = max(0, N - len(backlog_rows))

    recheck_rows = []
    if remaining:
        due = [r for r in D.iter_due(con, network="instagram")
               if r["account_id"] not in backlog_ids]
        due.sort(key=lambda r: (-r["importance"], -(r["waited_days"] or 0), r["name"]))
        for r in due[:remaining]:
            # к моменту, когда аккаунт доходит до перепроверки, он либо не
            # заглушка, либо у заглушки bio уже собрано — need_bio=False
            handle = (r["url"] or "").rstrip("/").split("/")[-1] or None
            recheck_rows.append((r["account_id"], handle, r["url"], r["name"], 0))

    rows = list(backlog_rows) + recheck_rows
    left_post, left_bio_only = con.execute(COUNT_SQL).fetchone()
    left_recheck = sum(1 for _ in D.iter_due(con, network="instagram"))
    con.close()
    targets = [
        {"id": r[0], "handle": r[1], "url": r[2], "name": r[3], "need_bio": bool(r[4])}
        for r in rows
    ]
    json.dump({"targets": targets}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{OUT}: {len(targets)} профилей в порции "
          f"({len(backlog_rows)} бэклог, {len(recheck_rows)} перепроверка, "
          f"{sum(t['need_bio'] for t in targets)} с need_bio)")
    print(f"осталось: без поста {left_post or 0}, "
          f"пост есть но bio нет (заглушки) {left_bio_only or 0}, "
          f"на перепроверку по графику {left_recheck}")
    print("первые:", ", ".join(f"{r[3]} (@{r[1] or '?'}{'*' if r[4] else ''})" for r in rows[:5]))


if __name__ == "__main__":
    main()
