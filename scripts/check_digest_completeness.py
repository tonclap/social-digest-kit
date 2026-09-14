#!/usr/bin/env python3
"""
Проверка полноты дайджеста — обязательный шаг ПЕРЕД тем, как отправить готовый
дайджест владельцу и записать его через record_digest.py.

Две независимые проверки за дату дайджеста:

1) ПОЛНОТА. Каждая находка (observations.found_post=1) обязана быть учтена
   хоть где-то — либо в «По людям»/«Поводы»/«Темы дня», либо явно объяснена
   в «Не вошло». Сверяет находки за дату с items.json (тем же файлом, что
   потом идёт в record_digest.py) и печатает те, что не покрыты НИ ОДНОЙ
   строкой. Именно так был найден реальный пропуск: содержательный пост
   человека с важностью 4 не попал ни в текст дайджеста, ни в «Не вошло».

2) ПОВТОРЫ (добавлено 20.08.2026). Среди находок, которые пересказаны как
   СВЕЖИЙ контент (`category` `po_lyudyam` или `tema` — НЕ `ne_voshlo` и НЕ
   `povod`) — есть ли такие, у которых пост не изменился с предыдущей
   проверки того же канала (тот же `post_url`, а если его нет — тот же
   `post_date`; см. `flag_repeat_posts.py`, та же логика). `povod` намеренно
   не проверяется — повод («просьба о помощи», «день рождения на этой
   неделе») законно может повторяться, пока не решится, это не заявка на
   «новое», в отличие от po_lyudyam/tema. Раньше это была ОТДЕЛЬНАЯ
   необязательная проверка (шаг 3.5), и на дайджесте 19.08.2026 она была
   пропущена/не работала — 11 из 15 находок оказались повторами из более
   ранних дайджестов, пересказанными как новые. Слита в этот
   скрипт, чтобы повтор ловился ТЕМ ЖЕ обязательным шагом 5, который и так
   нельзя пропускать (в отличие от отдельного шага 3.5, который можно забыть
   запустить или не узнать о нём, если SKILL.md не пересохранён).

Запуск (после того как items.json уже составлен, но ДО record_digest.py и ДО
отправки файла владельцу):
    python3 check_digest_completeness.py social.db items.json [--date YYYY-MM-DD]

Пустой вывод по обеим проверкам и код возврата 0 — дайджест полный и без
пропущенных повторов, можно отправлять. Непустой вывод и код 1 — дайджест
дописать (пропуски — в «По людям» или «Не вошло»; повторы — перенести из
«По людям»/«Поводов» в «Не вошло» с пометкой «тот же пост, что и на проверке
ГГГГ-ММ-ДД»), пересобрать items.json и прогнать снова.
"""
import json, sqlite3, sys
from datetime import date

args = [a for a in sys.argv[1:] if not a.startswith("--")]
if len(args) < 2:
    print(__doc__)
    sys.exit(2)
DB, ITEMS = args[0], args[1]
DAY = date.today().isoformat()
for i, a in enumerate(sys.argv):
    if a == "--date" and i + 1 < len(sys.argv):
        DAY = sys.argv[i + 1]

items = json.load(open(ITEMS, encoding="utf-8"))
covered = {it.get("person_id") for it in items if it.get("person_id") is not None}
# для проверки повторов нужно знать категорию и сети конкретно по person_id
by_person = {}
for it in items:
    pid = it.get("person_id")
    if pid is None:
        continue
    by_person.setdefault(pid, []).append(it)

con = sqlite3.connect(DB)
cur = con.cursor()

# --- 1) полнота ---
rows = cur.execute("""
    SELECT p.id, p.display_name, p.importance, p.circle, a.network, o.post_date, o.summary
    FROM observations o
    JOIN accounts a ON a.id = o.account_id
    JOIN people p ON p.id = a.person_id
    WHERE date(o.checked_at) = ? AND o.found_post = 1
    ORDER BY p.importance DESC, p.display_name
""", (DAY,)).fetchall()

missing = [r for r in rows if r[0] not in covered]

print(f"находок с found_post=1 за {DAY}: {len(rows)}, из них не покрыто items.json: {len(missing)}")
for pid, name, imp, circle, net, post_date, summary in missing:
    s = (summary or "(без текста)")
    s = s if len(s) <= 160 else s[:157] + "..."
    print(f"  [{imp}] {name} ({circle}, {net}, пост {post_date or '?'}): {s}")

# --- 2) повторы, пересказанные как новые (не ne_voshlo) ---
today_full = cur.execute("""
    SELECT o.id, o.account_id, o.post_date, o.post_url, o.summary,
           p.id, p.display_name, p.importance, p.circle, a.network
    FROM observations o
    JOIN accounts a ON a.id = o.account_id
    JOIN people p ON p.id = a.person_id
    WHERE date(o.checked_at) = ? AND o.found_post = 1
""", (DAY,)).fetchall()

miscategorized = []
for oid, acc_id, post_date, post_url, summary, pid, name, imp, circle, net in today_full:
    prev = cur.execute("""
        SELECT post_date, post_url FROM observations
        WHERE account_id = ? AND id < ? AND found_post = 1
        ORDER BY id DESC LIMIT 1
    """, (acc_id, oid)).fetchone()
    if not prev:
        continue
    prev_date, prev_url = prev
    same = False
    if post_url and prev_url:
        same = post_url == prev_url
    elif post_date and prev_date:
        same = post_date == prev_date  # см. докстринг: без url сверяем только дату
    if not same:
        continue
    # это повтор — проверяем, не пересказан ли он как контент (не ne_voshlo).
    # `povod` намеренно исключён: повод («просьба о помощи», «день рождения на
    # этой неделе») может законно повторяться, пока не решится или не пройдёт —
    # это не заявка на «новый контент», в отличие от po_lyudyam/tema, так что
    # его дублирование НЕ баг (иначе ловим ложные срабатывания вроде дня
    # рождения, который висит всю неделю, или незакрытой просьбы о помощи,
    # которая повторяется, пока её не решили).
    for it in by_person.get(pid, []):
        net_field = (it.get("networks") or "")
        cat = it.get("category") or ""
        if net in net_field and cat not in ("ne_voshlo", "povod"):
            miscategorized.append((imp, name, circle, net, post_url or post_date,
                                    cat, it.get("summary")))
            break

print(f"повторов среди пересказанного (не «не вошло»): {len(miscategorized)}")
for imp, name, circle, net, where, cat, summary in sorted(miscategorized, key=lambda r: (-r[0], r[1])):
    print(f"  [{imp}] {name} ({circle}, {net}): {where} — категория «{cat}», "
          f"пост не изменился с прошлой проверки. Перенести в «Не вошло»: "
          f"«{(summary or '')[:80]}» → «тот же пост, что и на прошлой проверке».")

con.close()
sys.exit(1 if (missing or miscategorized) else 0)
