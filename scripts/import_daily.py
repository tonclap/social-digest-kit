#!/usr/bin/env python3
"""
Принимает vk_daily_*.json: пишет наблюдения в базу и печатает материал для дайджеста —
кто что написал, с текстами и ссылками, уже отсеяв то, что попадало в прошлые обзоры.

10.08.2026, второй заход: снимок от vk_daily.html УЖЕ несёт per-post флаг `repost`
(из VK API copy_history) и `repost_text` — это не использовалось при записи в
observations, из-за чего формальные VK-репосты без своих слов считались обычной
активностью владельца и держали канал в быстром слое проверки. Тот же баг
в тот же день нашёлся и в разовом импорте census — здесь он в ЖИВОМ
ежедневном сборе.

Теперь при нескольких постах в окне выбирается САМЫЙ СВЕЖИЙ СВОЙ (не bare-repost)
пост для post_date/summary; если в окне вообще нет ничего кроме bare-репостов —
берётся самый свежий из них, но наблюдение получает `authored_by_owner=0` (репост
это факт, что канал жив, но не сигнал, что владелец сам писал — не должен
«освежать» слой активности в due_today.py).

`bare_repost` = `repost:true` И собственный текст (`post.text`, то, что человек
дописал поверх репоста) пуст. Репост с добавленными своими словами (VK позволяет
комментировать при репосте) остаётся `authored_by_owner=1` — это тоже действие
владельца, не просто эхо чужого поста. Прямая договорённость с владельцем
10.08.2026: не путать «формальный репост без своих слов» с «поделился с
комментарием» — второе НЕ считается пустым сигналом.

20.08.2026 — КРИТИЧЕСКАЯ ПРАВКА: фильтр «свежести» раньше сравнивал первые 80
символов ТЕКСТА ПОСТА с первыми 80 символами `digest_items.summary` (готовым
пересказом из прошлых дайджестов, написанным прозой) — эти строки почти
никогда не совпадают, потому что пересказ не цитирует пост дословно. Из-за
этого «отсеивание уже пересказанного» не работало НИКОГДА (тихо), и один и
тот же пост попадал в material (а оттуда — в текст дайджеста) заново на
каждой проверке, пока человек не напишет что-то новое. Подтверждено на двух
дайджестах подряд: у четверых человек стоял тот же post_url, что и неделей
раньше, но абзац в «По людям» был написан заново оба раза, как будто это
свежая находка.
Также была мёртвая переменная `seen_urls`, которая вопреки названию тоже
тянула `summary`, а не `post_url`, и нигде не использовалась.

Правильное сравнение — по `post_url` ПРЕДЫДУЩЕГО найденного поста ТОГО ЖЕ
account_id (не по тексту дайджеста): если выбранный сегодня пост имеет тот же
url, что и последнее записанное наблюдение с found_post=1 по этому каналу —
это не новость, а тот же самый пост, что уже видели в прошлый раз.
ОГРАНИЧЕНИЕ: в observations сохраняется только ОДИН (выбранный) пост в день —
если в окне несколько постов и лишь часть из них уже фигурировала в прошлых
дайджестах по памяти (не как отдельная запись в observations), это сравнение
их не поймает. Финальная подстраховка — `flag_repeat_posts.py`, который
сверяет уже ЗАПИСАННЫЕ наблюдения между собой и должен запускаться отдельным
шагом после сбора всех сетей, перед текстом дайджеста (см. SKILL.md).

Запуск: python3 import_daily.py <vk_daily_*.json> [social.db] [--apply]
Без --apply наблюдения не пишутся, материал всё равно печатается (удобно для черновика).
"""
import json, sqlite3, sys
from datetime import date

APPLY = "--apply" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("--")]
SRC = args[0]
DB = args[1] if len(args) > 1 else "social.db"
TODAY = date.today().isoformat()

data = json.load(open(SRC, encoding="utf-8"))
con = sqlite3.connect(DB)
cur = con.cursor()

# post_url последнего ДО СЕГОДНЯ найденного поста по каждому каналу (account_id) —
# см. правку 20.08.2026 в шапке файла про то, почему раньше это сравнивалось
# неправильно (с текстом дайджеста вместо post_url).
last_known_url = {}
for account_id, url in cur.execute("""
    SELECT o.account_id, o.post_url
    FROM observations o
    WHERE o.found_post = 1 AND o.post_url IS NOT NULL
      AND o.checked_at = (
          SELECT MAX(o2.checked_at) FROM observations o2
          WHERE o2.account_id = o.account_id AND o2.found_post = 1
      )
"""):
    last_known_url[account_id] = url


def is_bare_repost(post):
    """VK-репост (copy_history непустой) БЕЗ добавленных своих слов —
    post['text'] пуст (это ровно то поле, куда VK пишет комментарий
    поверх репоста, если человек его добавил)."""
    if not post.get("repost"):
        return False
    return not (post.get("text") or "").strip()


def pick_post(posts):
    """Из всех постов в окне выбирает, что записать как post_date/summary:
    приоритет — самый свежий СВОЙ (не bare-repost) пост. Если таких нет —
    самый свежий bare-repost (канал жив, но это не его слова).
    Возвращает (post, is_own) либо (None, True), если постов вообще нет."""
    if not posts:
        return None, True
    genuine = [p for p in posts if not is_bare_repost(p)]
    if genuine:
        return max(genuine, key=lambda x: x["date"]), True
    return max(posts, key=lambda x: x["date"]), False


material, obs = [], 0
for p in data["people"]:
    posts = p.get("posts") or []
    chosen, is_own = pick_post(posts)
    prev_url = last_known_url.get(p["account_id"])
    # "свежее" = либо постов не было, либо выбранный сегодня отличается от
    # того, что уже было записано в прошлый раз по этому каналу.
    is_fresh = chosen is not None and (not prev_url or chosen.get("url") != prev_url)
    if APPLY:
        if chosen is None:
            summary, post_date, post_url, owner_flag = None, None, None, None
        else:
            post_date, post_url = chosen["date"], chosen["url"]
            if is_own:
                summary = (chosen.get("text") or "").strip()[:300] or None
                owner_flag = None
            else:
                # bare repost: своих слов нет — показываем содержание репоста
                # с тем же маркером, что и в census-импорте ([репост] ...)
                src_text = (chosen.get("repost_text") or "").strip()
                summary = ("[репост] " + src_text)[:300] if src_text else "[репост]"
                owner_flag = 0
        cur.execute("""INSERT INTO observations(account_id, checked_at, found_post, post_date,
                                                post_url, summary, source, authored_by_owner)
                       VALUES (?,?,?,?,?,?, 'api', ?)""",
                    (p["account_id"], TODAY, 1 if chosen else 0, post_date,
                     post_url, summary, owner_flag))
        obs += 1
    if is_fresh:
        material.append({**{k: p[k] for k in ("person_id", "name", "url")}, "posts": [chosen]})

if APPLY:
    con.commit()

meta = {r[0]: r[1:] for r in cur.execute(
    "SELECT id, importance, circle, note FROM people")}
for m in material:
    imp, circle, note = meta.get(m["person_id"], (None, None, None))
    m["importance"], m["circle"], m["note"] = imp, circle, note
material.sort(key=lambda m: (-(m["importance"] or 0), m["name"]))

print(json.dumps({"date": TODAY,
                  "checked": len(data["people"]),
                  "with_new": len(material),
                  "observations_written": obs,
                  "people": material}, ensure_ascii=False, indent=1))
con.close()
