#!/usr/bin/env python3
"""
Проставляет ссылки на профили Facebook в social.db по выгрузке fb_friends.json
(собрана tools/fb_friends_harvest.html).

Логика: у 1424 из 1464 FB-аккаунтов в базе нет URL, зато есть имя. Сверяем имена
и дописываем ссылку. Неоднозначные имена (несколько человек с одинаковым) не трогаем —
их разберёт человек на этапе разметки.

Свой профиль и односторонние паблики раньше были зашиты в код константами —
у любого, кто соберёт это у себя, собственная страница приезжала в базу как
друг, а чужой паблик не отсекался. Теперь это аргументы: без --owner скрипт
прямо говорит, что свой профиль не исключён.

Запуск: python3 import_fb_links.py <fb_friends.json> [social.db] [--apply]
                                   [--owner <слаг своего профиля>]
                                   [--page <слаг паблика>] ...
Без --apply только показывает, что будет сделано.
"""
import json, re, sqlite3, sys, unicodedata
from _cli import positionals, require_db   # см. _cli.py

args = positionals(sys.argv[1:], ("--owner", "--page"))
APPLY = "--apply" in sys.argv
SRC = args[0] if args else "fb_friends.json"
DB = require_db(args[1] if len(args) > 1 else "social.db")

# служебные ссылки интерфейса, не люди
JUNK_PATH = {
    "professional_dashboard", "friends", "photos", "videos", "groups", "watch",
    "marketplace", "events", "pages", "settings", "bookmarks", "notifications",
    "messages", "stories", "reels", "gaming", "help", "policies", "privacy",
}
JUNK_NAME = {"панель", "редактировать", "все", "друзья", "ещё", "еще", "профиль",
             "посмотреть профиль", "создать", "меню"}
# собственный профиль (--owner) и односторонние паблики (--page, можно несколько)
OWNER = None
PAGES = set()
for i, a in enumerate(sys.argv):
    if a == "--owner" and i + 1 < len(sys.argv):
        OWNER = sys.argv[i + 1].rstrip("/").split("/")[-1]
    if a == "--page" and i + 1 < len(sys.argv):
        PAGES.add(sys.argv[i + 1].rstrip("/").split("/")[-1])


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower().replace("ё", "е")
    return " ".join(re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE).split())


def nkey(s):
    return " ".join(sorted(norm(s).split()))


def is_person(e):
    u = e["facebook_url"]
    slug = u.rstrip("/").split("/")[-1].split("?")[0]
    if slug in JUNK_PATH or slug == OWNER or slug in PAGES:
        return False
    if norm(e["name"]) in JUNK_NAME:
        return False
    return len(norm(e["name"]).split()) >= 2


def main():
    if not OWNER:
        print("ВНИМАНИЕ: --owner не задан — ваш собственный профиль, если он есть "
              "в выгрузке, попадёт в базу как человек из круга.")
    data = json.load(open(SRC, encoding="utf-8"))
    raw = data["people"]
    people = [e for e in raw if is_person(e)]
    print(f"в выгрузке {len(raw)}, похожих на людей {len(people)}, "
          f"отброшено служебных {len(raw) - len(people)}")

    # индекс выгрузки по имени
    by_name = {}
    for e in people:
        by_name.setdefault(nkey(e["name"]), []).append(e)

    con = sqlite3.connect(DB)
    cur = con.cursor()

    rows = cur.execute("""SELECT id, person_id, name_raw, url FROM accounts
                          WHERE network='facebook'""").fetchall()
    db_by_name = {}
    for aid, pid, nm, url in rows:
        db_by_name.setdefault(nkey(nm), []).append((aid, pid, nm, url))

    filled = amb_h = amb_db = already = 0
    matched_keys = set()
    for k, accs in db_by_name.items():
        hits = by_name.get(k)
        if not hits:
            continue
        matched_keys.add(k)
        if len(hits) > 1:
            amb_h += 1
            continue
        if len(accs) > 1:
            amb_db += 1
            continue
        aid, pid, nm, url = accs[0]
        if url:
            already += 1
            continue
        if APPLY:
            cur.execute("UPDATE accounts SET url=?, handle=?, source='fb_harvest' WHERE id=?",
                        (hits[0]["facebook_url"],
                         hits[0]["facebook_url"].rstrip("/").split("/")[-1], aid))
        filled += 1

    new = [e for k, v in by_name.items() if k not in db_by_name for e in v]
    if APPLY:
        for e in new:
            cur.execute("""INSERT INTO people(display_name, created_at, updated_at)
                           VALUES (?, date('now'), date('now'))""", (e["name"],))
            pid = cur.lastrowid
            cur.execute("""INSERT INTO accounts(person_id, network, url, handle, name_raw,
                                                source, created_at)
                           VALUES (?, 'facebook', ?, ?, ?, 'fb_harvest', date('now'))""",
                        (pid, e["facebook_url"],
                         e["facebook_url"].rstrip("/").split("/")[-1], e["name"]))
        con.commit()

    print(f"  ссылка проставлена:        {filled}")
    print(f"  уже была:                  {already}")
    print(f"  имя неоднозначно в выгрузке: {amb_h}")
    print(f"  имя неоднозначно в базе:     {amb_db}")
    print(f"  нет в базе (новые друзья):   {len(new)}")
    for e in new[:8]:
        print(f"      {e['name']}")
    left = cur.execute("""SELECT COUNT(*) FROM accounts
                          WHERE network='facebook' AND url IS NULL""").fetchone()[0]
    print(f"  осталось без ссылки:       {left}")
    if not APPLY:
        print("\n(пробный прогон — ничего не записано, добавь --apply)")
    con.close()


if __name__ == "__main__":
    main()
