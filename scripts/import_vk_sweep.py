#!/usr/bin/env python3
"""
Загрузка результата vk_sweep.html в social.db.

Что делает:
  1. сверяет каждого человека из выгрузки с аккаунтами в базе — по числовой ссылке
     (vk.com/idNNN) и по «красивой» (vk.com/domain) одновременно;
  2. если обе формы ссылки нашлись у РАЗНЫХ людей — это тот самый дубль, который
     прошлые сессии не решились разводить: сливает их в одного;
  3. пишет наблюдение (проверяли / нашли пост / дата / ссылка) в observations;
  4. проставляет is_dead удалённым и забаненным;
  5. заводит новых друзей, которых нет в переписи.

Запуск: python3 import_vk_sweep.py <sweep.json> [social.db]
"""
import json, re, sqlite3, sys
from datetime import date

SWEEP = sys.argv[1] if len(sys.argv) > 1 else "vk_sweep.json"
DB = sys.argv[2] if len(sys.argv) > 2 else "social.db"
TODAY = date.today().isoformat()


def key(url):
    """vk.com/idNNN и https://vk.com/IdNNN/ — один и тот же ключ."""
    if not url:
        return None
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^(m\.|www\.)", "", u)
    u = u.rstrip("/")
    u = u.replace("vk.ru/", "vk.com/")
    return u or None


def main():
    data = json.load(open(SWEEP, encoding="utf-8"))
    people = data.get("people") or []
    con = sqlite3.connect(DB)
    cur = con.cursor()

    # network_id — числовой id VK, добавляем один раз
    cols = {r[1] for r in cur.execute("PRAGMA table_info(accounts)")}
    if "network_id" not in cols:
        cur.execute("ALTER TABLE accounts ADD COLUMN network_id TEXT")

    # индекс существующих VK-аккаунтов по нормализованной ссылке
    idx = {}
    for aid, pid, url in cur.execute(
            "SELECT id, person_id, url FROM accounts WHERE network='vk' AND url IS NOT NULL"):
        k = key(url)
        if k:
            idx.setdefault(k, (aid, pid))

    st = dict(obs=0, merged=0, new=0, matched=0, dead=0, unreadable=0)
    merge_log, new_log = [], []

    def merge(keep_pid, drop_pid):
        if keep_pid == drop_pid:
            return
        cur.execute("UPDATE accounts SET person_id=? WHERE person_id=?", (keep_pid, drop_pid))
        cur.execute("UPDATE digest_items SET person_id=? WHERE person_id=?", (keep_pid, drop_pid))
        # заметку и in_contacts не теряем
        row = cur.execute("SELECT note, in_contacts FROM people WHERE id=?", (drop_pid,)).fetchone()
        if row:
            cur.execute("""UPDATE people SET note=COALESCE(note, ?),
                                             in_contacts=COALESCE(in_contacts, ?)
                           WHERE id=?""", (row[0], row[1], keep_pid))
        cur.execute("DELETE FROM people WHERE id=?", (drop_pid,))
        st["merged"] += 1

    for p in people:
        vid = str(p.get("id"))
        url = p.get("url") or f"https://vk.com/id{vid}"
        k_url = key(url)
        k_num = key(f"vk.com/id{vid}")

        hit_url = idx.get(k_url)
        hit_num = idx.get(k_num)

        if hit_url and hit_num and hit_url[1] != hit_num[1]:
            # один человек записан дважды — под доменом и под числовым id
            keep, drop = sorted([hit_url[1], hit_num[1]])
            names = [r[0] for r in cur.execute(
                "SELECT display_name FROM people WHERE id IN (?,?)", (keep, drop))]
            merge(keep, drop)
            merge_log.append(f"{' / '.join(names)} → {p['name']}")
            hit = (hit_url[0], keep)
        else:
            hit = hit_url or hit_num

        if hit:
            aid, pid = hit
            st["matched"] += 1
            cur.execute("UPDATE accounts SET network_id=?, url=? WHERE id=?", (vid, url, aid))
        else:
            cur.execute("""INSERT INTO people(display_name, created_at, updated_at)
                           VALUES (?,?,?)""", (p["name"], TODAY, TODAY))
            pid = cur.lastrowid
            cur.execute("""INSERT INTO accounts(person_id, network, url, handle, name_raw,
                                                source, network_id, created_at)
                           VALUES (?,'vk',?,?,?,'vk_api',?,?)""",
                        (pid, url, (url.rstrip('/').split('/')[-1]), p["name"], vid, TODAY))
            aid = cur.lastrowid
            idx[k_url] = idx[k_num] = (aid, pid)
            st["new"] += 1
            new_log.append(p["name"])

        if p.get("deactivated"):
            cur.execute("UPDATE accounts SET is_dead=1 WHERE id=?", (aid,))
            st["dead"] += 1
            continue
        if not p.get("readable"):
            st["unreadable"] += 1

        found = 1 if p.get("last_post_date") else 0
        summary = (p.get("last_post_text") or "").strip().replace("\n", " ")[:300] or None
        if found and p.get("is_repost") and summary:
            summary = "[репост] " + summary
        exists = cur.execute("""SELECT 1 FROM observations
                                WHERE account_id=? AND checked_at=? AND source='api'""",
                             (aid, TODAY)).fetchone()
        if not exists:
            cur.execute("""INSERT INTO observations(account_id, checked_at, found_post,
                                                    post_date, post_url, summary, source)
                           VALUES (?,?,?,?,?,?, 'api')""",
                        (aid, TODAY, found, p.get("last_post_date"), p.get("last_post_url"), summary))
            st["obs"] += 1

    con.commit()

    # --- сводка ---
    q = lambda s, *a: cur.execute(s, a).fetchone()[0]
    print(f"выгрузка от {data.get('generated_at','?')[:10]}: {len(people)} человек\n")
    print(f"  сопоставлено с базой:  {st['matched']}")
    print(f"  заведено новых:        {st['new']}" + (f"  ({', '.join(new_log[:5])}…)" if new_log else ""))
    print(f"  слито дублей:          {st['merged']}")
    for m in merge_log[:12]:
        print(f"      {m}")
    print(f"  наблюдений записано:   {st['obs']}")
    print(f"  удалённых/забаненных:  {st['dead']}")
    print(f"  стена закрыта:         {st['unreadable']}")

    print("\nАктивность VK по свежей выгрузке:")
    for label, cond in [
        ("писали за 30 дней",  "julianday('now') - julianday(post_date) <= 30"),
        ("за 31-90 дней",      "julianday('now') - julianday(post_date) BETWEEN 31 AND 90"),
        ("за 91-365 дней",     "julianday('now') - julianday(post_date) BETWEEN 91 AND 365"),
        ("больше года назад",  "julianday('now') - julianday(post_date) > 365"),
    ]:
        n = q(f"""SELECT COUNT(DISTINCT a.person_id) FROM observations o
                  JOIN accounts a ON a.id=o.account_id
                  WHERE o.source='api' AND o.found_post=1 AND {cond}""")
        print(f"  {label:<22} {n}")
    silent = q("""SELECT COUNT(DISTINCT a.person_id) FROM observations o
                  JOIN accounts a ON a.id=o.account_id
                  WHERE o.source='api' AND o.found_post=0""")
    print(f"  {'ни одного поста':<22} {silent}")
    print(f"\n  всего людей в базе: {q('SELECT COUNT(*) FROM people')}")
    con.close()


if __name__ == "__main__":
    main()
