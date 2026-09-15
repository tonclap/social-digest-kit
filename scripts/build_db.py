#!/usr/bin/env python3
"""
Фаза 0 — сборка единой базы людей social.db из переписи.

Идемпотентно: повторный запуск подхватывает новых людей из census-файлов и
никого не задваивает. Строки переписи БЕЗ ссылки (IG-никнеймы, FB без url, VK
с пустой колонкой) сверяются по имени среди уже заведённых безссылочных
аккаунтов той же сети — раньше так умел только Facebook, а VK и Instagram
заводили такому человеку новую запись на КАЖДОМ прогоне.

Источники (внутри папки, переданной первым аргументом):
  raw/census/vk_census.csv, facebook_census.csv, instagram_census.csv  — перепись
  raw/census/cross_network_matches.csv                                 — пары VK<->FB
  raw/contacts_names.txt                                               — телефонная книжка
  archive/_people.yml                                                  — старый реестр, если есть

Колонки census-файлов: name + vk_url / facebook_url / instagram_url.
Ни одного census-файла не нашлось — скрипт останавливается с кодом 1, а не
создаёт молча пустую базу.

Не входят по решению 01.08.2026: vk_idols.csv, facebook_followed.csv (медиа-контент).

Запуск: python3 build_db.py <папка с raw/census/> [social.db]
"""
import csv, os, re, sqlite3, sys, unicodedata
from datetime import datetime, timezone
from pathlib import Path

USAGE = "Запуск: python3 build_db.py <папка с raw/census/> [social.db]"
if len(sys.argv) < 2:
    sys.exit(USAGE)
SRC = sys.argv[1]
DB = sys.argv[2] if len(sys.argv) > 2 else "social.db"
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

# Схема берётся из schema.sql, своей копии CREATE TABLE здесь больше нет.
# Копия расходилась с живой базой на 18 колонок (в том числе accounts.network_id
# и observations.authored_by_owner), и собранная по ней база роняла due_today.py
# и export_vk_daily.py на OperationalError: no such column.
SCHEMA_SQL = Path(__file__).resolve().parent.parent / "schema.sql"

# Раньше здесь был CREATE VIEW person_activity — убран 04.08.2026 как мёртвый
# код: ни один потребитель (due_today.py, main.py) на самом деле его не
# использовал, каждый заново писал тот же JOIN сам. Сам view также удалён
# из живой базы тем же числом.

# ---------- нормализация имён ----------
def norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return " ".join(s.split())

def name_key(s):
    """Порядок слов не важен: 'Иван Петров' == 'Петров Иван'."""
    return " ".join(sorted(norm(s).split()))

# ---------- чтение источников ----------
def read_census(path, url_col):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            nm = (r.get("name") or "").strip()
            if not nm:
                continue
            rows.append((nm, (r.get(url_col) or "").strip() or None))
    return rows

def read_contacts(path):
    keys = set()
    if not os.path.exists(path):
        return keys
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k = name_key(line)
            if len(k.split()) >= 2:          # только полные имена, одиночные слова шумят
                keys.add(k)
    return keys

def read_people_yml(path):
    import yaml
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("people", []) or []

# ---------- сборка ----------
def main():
    census_paths = {
        "vk": (f"{SRC}/raw/census/vk_census.csv", "vk_url"),
        "facebook": (f"{SRC}/raw/census/facebook_census.csv", "facebook_url"),
        "instagram": (f"{SRC}/raw/census/instagram_census.csv", "instagram_url"),
    }
    if not any(os.path.exists(p) for p, _ in census_paths.values()):
        sys.exit(f"в {SRC} не нашлось ни одного census-файла:\n" +
                 "".join(f"  {p}\n" for p, _ in census_paths.values()) +
                 "без переписи база будет пустой — собери хотя бы один файл "
                 "(name + <сеть>_url) и запусти снова.\n" + USAGE)

    fresh = not os.path.exists(DB)
    con = sqlite3.connect(DB)
    if not con.execute("""SELECT name FROM sqlite_master
                          WHERE type='table' AND name='people'""").fetchone():
        con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    cur = con.cursor()

    vk = read_census(*census_paths["vk"])
    fb = read_census(*census_paths["facebook"])
    ig = read_census(*census_paths["instagram"])
    contacts = read_contacts(f"{SRC}/raw/contacts_names.txt")

    # --- 1. слияние VK<->FB по готовым парам ---
    # ключ VK-аккаунта = url (надёжно), ключ FB = имя (ссылок почти нет)
    vk_url_to_fbname = {}
    mp = f"{SRC}/raw/census/cross_network_matches.csv"
    if os.path.exists(mp):
        with open(mp, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                u = (r.get("vk_url") or "").strip()
                fbn = (r.get("person_facebook") or "").strip()
                if u and fbn:
                    vk_url_to_fbname[u] = name_key(fbn)

    # FB-имена, встречающиеся более одного раза — по ним сливать нельзя (неоднозначно)
    fb_name_counts = {}
    for nm, _ in fb:
        fb_name_counts[name_key(nm)] = fb_name_counts.get(name_key(nm), 0) + 1
    ambiguous_fb = {k for k, c in fb_name_counts.items() if c > 1}

    # --- 2. что уже есть в базе (идемпотентность) ---
    existing = {}   # (network, url) -> (account_id, person_id)
    for aid, pid, net, url in cur.execute(
            "SELECT id, person_id, network, url FROM accounts WHERE url IS NOT NULL"):
        existing[(net, url)] = (aid, pid)
    # аккаунты без ссылки сверяются по имени — единственный ключ, который у них
    # есть. Ключ общий для всех сетей: до 15.09.2026 так умел только Facebook,
    # из-за чего строка переписи без ссылки в VK/IG заводила нового человека на
    # каждом прогоне (идемпотентность держалась только на URL).
    existing_by_name = {}      # (network, name_key) -> person_id
    for pid, net, nm in cur.execute(
            "SELECT person_id, network, name_raw FROM accounts WHERE url IS NULL"):
        existing_by_name.setdefault((net, name_key(nm)), pid)

    stats = dict(people_new=0, acc_new=0, acc_skip=0, merged=0, obs=0)

    def new_person(display, in_contacts=None, note=None):
        cur.execute("""INSERT INTO people(display_name, in_contacts, note, created_at, updated_at)
                       VALUES (?,?,?,?,?)""", (display, in_contacts, note, NOW, NOW))
        stats["people_new"] += 1
        return cur.lastrowid

    def add_account(person_id, network, url, name_raw, source="census"):
        if url and (network, url) in existing:
            stats["acc_skip"] += 1
            return existing[(network, url)][0]
        cur.execute("""INSERT INTO accounts(person_id, network, url, handle, name_raw, source, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (person_id, network, url, (url or "").rstrip("/").split("/")[-1] or None,
                     name_raw, source, NOW))
        aid = cur.lastrowid
        if url:
            existing[(network, url)] = (aid, person_id)
        stats["acc_new"] += 1
        return aid

    def contacts_flag(nm):
        return "yes" if name_key(nm) in contacts else "no"

    # --- 3. VK: базовый слой, у всех есть URL ---
    vk_person_by_url = {}
    for nm, url in vk:
        nk = name_key(nm)
        if url and ("vk", url) in existing:
            pid = existing[("vk", url)][1]
        elif not url and ("vk", nk) in existing_by_name:
            pid = existing_by_name[("vk", nk)]
        else:
            pid = new_person(nm, contacts_flag(nm))
            add_account(pid, "vk", url, nm)
            if not url:
                existing_by_name.setdefault(("vk", nk), pid)
        if url:
            vk_person_by_url[url] = pid

    # --- 4. FB: подклеиваем к VK-человеку по парам, иначе новый человек ---
    fb_person_by_namekey = {}
    for nm, url in fb:
        nk = name_key(nm)
        pid = None
        if url and ("facebook", url) in existing:
            pid = existing[("facebook", url)][1]
        elif ("facebook", nk) in existing_by_name:
            pid = existing_by_name[("facebook", nk)]
        else:
            # ищем VK-человека, с которым этот FB сопоставлен
            if nk not in ambiguous_fb:
                for vurl, fbnk in vk_url_to_fbname.items():
                    if fbnk == nk and vurl in vk_person_by_url:
                        pid = vk_person_by_url[vurl]
                        stats["merged"] += 1
                        break
            if pid is None:
                pid = new_person(nm, contacts_flag(nm))
            add_account(pid, "facebook", url, nm)
            if url is None:
                existing_by_name.setdefault(("facebook", nk), pid)
        fb_person_by_namekey.setdefault(nk, pid)

    # --- 5. IG: никнеймы, сливать по имени нельзя — всегда отдельный человек ---
    for nm, url in ig:
        nk = name_key(nm)
        if url and ("instagram", url) in existing:
            continue
        if not url and ("instagram", nk) in existing_by_name:
            continue
        pid = new_person(nm, contacts_flag(nm))
        add_account(pid, "instagram", url, nm)
        if not url:
            existing_by_name.setdefault(("instagram", nk), pid)

    # --- 6. старый _people.yml: заметки и реальные наблюдения от 31.07 ---
    for p in read_people_yml(f"{SRC}/archive/_people.yml"):
        nets = p.get("networks") or {}
        pid = None
        acc_ids = []
        for net, url in nets.items():
            url = (url or "").strip()
            if (net, url) in existing:
                aid, pid_found = existing[(net, url)]
                pid = pid or pid_found
                acc_ids.append((aid, net))
        if pid is None:                      # человека нет в переписи — заводим
            pid = new_person(p.get("name", "?"), p.get("in_contacts"))
            for net, url in nets.items():
                acc_ids.append((add_account(pid, net, (url or "").strip(),
                                            p.get("name", "?"), source="people_yml"), net))
        # заметка и in_contacts из старого реестра точнее (проверялись руками)
        if p.get("note") or p.get("in_contacts"):
            cur.execute("""UPDATE people SET note=COALESCE(?, note),
                                             in_contacts=COALESCE(?, in_contacts),
                                             updated_at=? WHERE id=?""",
                        (p.get("note"), p.get("in_contacts"), NOW, pid))
        # наблюдения: last_checked (проверяли) + last_seen (нашли пост)
        lc = p.get("last_checked")
        ls = p.get("last_seen") or {}
        if lc and acc_ids:
            for aid, net in acc_ids:
                already = cur.execute("""SELECT 1 FROM observations
                                         WHERE account_id=? AND checked_at=? AND source='import'""",
                                      (aid, str(lc))).fetchone()
                if already:
                    continue
                found = 1 if ls.get("date") else 0
                cur.execute("""INSERT INTO observations(account_id, checked_at, found_post,
                                                        post_date, post_url, summary, source)
                               VALUES (?,?,?,?,?,?, 'import')""",
                            (aid, str(lc), found,
                             str(ls.get("date")) if found else None, None,
                             ls.get("summary") if found else None))
                stats["obs"] += 1

    con.commit()

    # --- отчёт ---
    q = lambda s: cur.execute(s).fetchone()[0]
    print(f"{'создана' if fresh else 'обновлена'}: {DB}")
    print(f"  люди:            {q('SELECT COUNT(*) FROM people')}  (+{stats['people_new']})")
    print(f"  аккаунты:        {q('SELECT COUNT(*) FROM accounts')}  (+{stats['acc_new']}, пропущено дублей {stats['acc_skip']})")
    for net in ("vk", "facebook", "instagram"):
        tot = q(f"SELECT COUNT(*) FROM accounts WHERE network='{net}'")
        nourl = q(f"SELECT COUNT(*) FROM accounts WHERE network='{net}' AND url IS NULL")
        print(f"     {net:<10} {tot:>5}  без ссылки {nourl}")
    print(f"  слито VK+FB:     {stats['merged']} человек в двух сетях")
    print(f"  в контактах:     {q(chr(39).join(['SELECT COUNT(*) FROM people WHERE in_contacts=', 'yes', '']))}")
    print(f"  наблюдений:      {q('SELECT COUNT(*) FROM observations')}  (+{stats['obs']} из _people.yml)")
    print(f"  размечено важностью: {q('SELECT COUNT(*) FROM people WHERE importance IS NOT NULL')}")
    multi = q("""SELECT COUNT(*) FROM (SELECT person_id FROM accounts
                 GROUP BY person_id HAVING COUNT(DISTINCT network) > 1)""")
    print(f"  людей в 2+ сетях: {multi}")
    con.close()

if __name__ == "__main__":
    main()
