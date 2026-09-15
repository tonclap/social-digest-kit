#!/usr/bin/env python3
"""
Кого проверяем сегодня.

Частота проверки = важность (ставит владелец) × активность (считается из наблюдений).
С 07.08.2026 расписание считается ПО КАЖДОЙ СЕТИ ОТДЕЛЬНО: свежая проверка VK у
человека больше не «освежает» дату его FB/IG — у каждого аккаунта свой last_checked,
а не общий на человека. Раньше last_checked брался как MAX(checked_at) по всем сетям
человека разом — из-за этого частые VK-проверки важных людей маскировали то, что их
FB/IG никто давно не смотрел.

10.08.2026 — две правки за один заход, обе про то же самое: расписание не должно
принимать «нашли что-то на странице» за «человек сам пишет».

1) **`observations.authored_by_owner`** (новая колонка, NULL/1 = автор — сам человек,
   0 — найденное на странице ему не принадлежит: поздравления с ДР от других на
   стене, гостевой пост друга, тег в чужом посте, воспоминание родственника и т.п.).
   Обнаружено по прямому замечанию владельца: раньше `found_post=1` ставился даже
   когда единственное найденное — чужие поздравления на стене, и это «освежало»
   `last_post`/слой активности так, будто человек сам недавно писал, хотя это чистый
   шум чужих действий. Реальный кейс 10.08.2026 — 23 наблюдения за один прогон
   (18 «только поздравления с ДР от других» + 5 гостевых/чужих постов), см.
   `record_browser_observations.py` про то, как выставлять это поле при сборе.
   На VK это не нужно — `export_vk_daily.py` и так тянет `wall.get(filter="owner")`,
   чужие посты со стены туда physически не попадают.
   Во ВСЕХ местах, где считается `last_post` (что и есть сигнал активности для
   `layer()`), теперь фильтр `found_post=1 AND COALESCE(authored_by_owner,1)=1` —
   было просто `found_post=1`.

2) **`empty_streak`** — по прямой просьбе владельца «придумать гибкую методику
   понижения частоты для тех, кто совсем не пишет». Раньше даже слой `archived`
   (пост старше года) имел ФИКСИРОВАННЫЙ интервал по важности навсегда — канал,
   молчащий 13 месяцев, и канал, молчащий 5 лет, проверялись одинаково часто.
   Хуже того: канал, где вообще НИ РАЗУ не находили пост (`last_post IS NULL`),
   был обречён вечно сидеть в `dormant` (14–90 дней) — для него `layer()` в принципе
   не может доехать до `archived`, потому что там не от чего отсчитывать возраст.
   Теперь считается `empty_streak` — сколько ПОДРЯД последних наблюдений (считая
   от самого свежего) не показали активности самого владельца (см. `authored_by_owner`
   выше — поздравления от других тоже НЕ прерывают streak, это тоже «пусто» с точки
   зрения активности человека). Streak растягивает интервал сверх обычного значения
   из DAYS: см. `BACKOFF` и `ABS_MAX_INTERVAL_DAYS` ниже. Как только находится
   собственный пост — streak обнуляется, и канал возвращается к обычному темпу через
   `layer()` уже на следующий прогон.

Решение владельца от 07.08.2026 (уточнено 07.08.2026, второй заход):
  - важность 1 в мониторинг и дайджест не входит вовсе (неинтересно на этом этапе);
  - важность 2 — раз в 30 дней (active/rare), dormant — раз в 90 дней;
  - важность 3-4-5 — в фокусе: active-интервалы ослаблены (реже, чем было в
    самой первой версии расписания) — 3: 7д, 4: 3д, 5: 2д; 5-rare — 5д.

06.08.2026: слой активности («active»/«rare»/«dormant») до этой правки считался
по last_post ЛЮБОЙ сети человека разом (PEOPLE_SQL брал MAX(post_date) по всем
accounts персоны) — при этом сам интервал проверки уже был per-network. Из-за
этого возникал разрыв: если человек активен, скажем, в Facebook, а в VK молчит
15 лет, его VK всё равно проверялся с «active»-частотой (для важности 5 — раз в
2 дня), потому что «активность» смотрела не в ту сеть. Обнаружено на примере
census-разбора VK 01.08.2026 — у двух человек последний пост VK датировался
2011 и 2025-10, и дайджест 06.08 предложил проверить, не лежит ли их основной
канал вообще в другой сети.

Исправлено: layer() теперь считается по last_post ТОЙ ЖЕ сети, что и
проверяемый аккаунт (ACCOUNTS_SQL сам берёт per-account last_post). Плюс
добавлен новый слой «archived» — сеть, где последний найденный пост старше
365 дней: у канала, который годами не обновлялся, даже прежний «dormant»
(14–90 дней в зависимости от важности) был слишком частым интервалом.
«archived» разведён по важности так же, как остальные слои — см. DAYS ниже.
Не путать с «постов вообще не нашли» — это по-прежнему просто «dormant»
(одного отсутствия находки мало, чтобы понижать частоту сильнее; см.
channel_health.py — он показывает archived/dormant-каналы без привязки к
тому, пора ли их сегодня трогать, для ручного разбора).

20.08.2026 — по прямому запросу владельца «низкий процент настоящих находок,
прогоны обходят аккаунты вхолостую» посчитали факт (`calibration_report.py`):
за 5 боевых прогонов (07,10,13,19,20.08) VK дал 15.2% настоящих новых находок
(78.9% проверок — пусто), FB 42.8%, IG 71.2%. Внутри VK разброс ещё резче: слой
active — 44-57% (расписание там работает нормально), а rare+dormant+archived
вместе — 189 из 237 VK-проверок, из них находок 7.4% (archived отдельно: 94
проверки, 0 находок). Причина — DAYS ниже одна на все три сети, подобрана на
глаз 06-10.08.2026, ни разу не сверялась с тем, что реально находится, хотя
сети ведут себя по-разному (VK эта же выборка постит куда реже FB/IG). Теперь
DAYS — это дефолт; `schedule_config.json` (если лежит рядом со скриптом)
переопределяет интервалы per-network, per-важность, per-слой — см.
`load_days_by_network()` ниже и `recalibrate_schedule.py`, который считает
предлагаемые значения по факту находок и пишет туда diff на просмотр (не молча
перезаписывает). Если файла нет — поведение идентично version до 20.08.2026.

Запуск:
    python3 due_today.py [social.db] [--json] [--network vk|facebook|instagram] [--limit N]
"""
import hashlib, json, os, sqlite3, sys
from datetime import date
from _cli import positionals, require_db   # см. _cli.py

args = positionals(sys.argv[1:], ("--network", "--limit"))
DB = args[0] if args else "social.db"
if __name__ == "__main__":   # модуль импортируют (export_vk_daily.py / channel_health.py и др.) — там нужен только код, не база
    DB = require_db(DB)
AS_JSON = "--json" in sys.argv
NET = None
LIMIT = None
for i, a in enumerate(sys.argv):
    if a == "--network" and i + 1 < len(sys.argv):
        NET = sys.argv[i + 1]
    if a == "--limit" and i + 1 < len(sys.argv):
        LIMIT = int(sys.argv[i + 1])

# интервал проверки в днях: важность × слой активности КАНАЛА (человек×сеть).
# Важности 1 в таблице нет намеренно — такие люди не мониторятся вовсе.
# "archived" — канал, чей последний найденный пост старше 365 дней (см. layer()
# ниже и комментарий от 06.08.2026 выше про то, зачем понадобился этот слой).
# Это ДЕФОЛТ на случай, если schedule_config.json нет или сеть/ячейка в нём не
# упомянута — см. load_days_by_network() и комментарий от 20.08.2026 в шапке
# файла. Сами по себе эти числа больше не источник истины по всем трём сетям.
DAYS = {
    5: {"active": 2,  "rare": 5,  "dormant": 14, "archived": 60},
    4: {"active": 3,  "rare": 7,  "dormant": 30, "archived": 120},
    3: {"active": 7,  "rare": 14, "dormant": 30, "archived": 180},
    2: {"active": 30, "rare": 30, "dormant": 90, "archived": 365},
}

# Файл ищем РЯДОМ СО СКРИПТОМ (та же папка scripts/, целиком стейджится вместе),
# не рядом с БД — БД и scripts исторически стейджатся в разные временные пути.
SCHEDULE_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schedule_config.json")


def load_days_by_network(path=SCHEDULE_CONFIG_PATH, default=DAYS):
    """DAYS_BY_NETWORK = {network: {importance: {layer: days}}}. Стартуем с трёх
    копий дефолтной DAYS (одна на сеть), затем накладываем schedule_config.json,
    если он есть — поячеечно (сеть/важность/слой, которых в файле нет, остаются
    дефолтными). Формат schedule_config.json специально совпадает с DAYS
    (importance как строка-ключ JSON — JSON не умеет int-ключи), чтобы его можно
    было писать вручную, а не только через recalibrate_schedule.py.
    Битый/нечитаемый файл — не падаем, откатываемся к дефолту на все сети и
    печатаем предупреждение в stderr (лучше проверить чуть реже по старой
    таблице, чем упасть посреди прогона)."""
    result = {net: {imp: dict(layers) for imp, layers in default.items()}
              for net in ("vk", "facebook", "instagram")}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                override = json.load(f)
            for net, by_imp in override.items():
                result.setdefault(net, {imp: dict(layers) for imp, layers in default.items()})
                for imp_s, by_layer in by_imp.items():
                    imp = int(imp_s)
                    result[net].setdefault(imp, {})
                    for lyr, days in by_layer.items():
                        result[net][imp][lyr] = days
        except Exception as e:
            print(f"schedule_config.json не прочитан ({path}: {e}) — "
                  f"использую единый DAYS для всех сетей", file=sys.stderr)
    return result


DAYS_BY_NETWORK = load_days_by_network()

# людей и их важность/круг/заметку берём одним запросом; last_post_any_network —
# СПРАВОЧНЫЙ (по любой сети сразу, «человек вообще где-то ещё пишет») — только
# для отображения и channel_health.py, а не для расчёта слоя конкретного канала
# (с 06.08.2026 слой считается per-network, см. ACCOUNTS_SQL / iter_due ниже).
# С 10.08.2026 — только СОБСТВЕННАЯ активность (authored_by_owner), см. комментарий
# в шапке файла: поздравления от других на стене не считаются «человек написал».
PEOPLE_SQL = """
SELECT p.id, p.display_name, p.importance, p.circle, p.note,
       (SELECT MAX(o.post_date) FROM observations o JOIN accounts a ON a.id=o.account_id
         WHERE a.person_id=p.id AND o.found_post=1 AND COALESCE(o.authored_by_owner,1)=1)
                                                                   AS last_post_any_network
FROM people p
WHERE p.importance IS NOT NULL AND p.importance > 1
"""

# last_checked И last_post — оба per account (per сеть), не по человеку целиком.
# last_checked — когда в последний раз смотрели именно этот канал (ЛЮБОЕ наблюдение,
# в т.ч. authored_by_owner=0 — сам факт проверки не обнуляется тем, что найденное
# оказалось не его словами).
# last_post — когда там в последний раз нашёлся СОБСТВЕННЫЙ пост владельца (это и
# есть сигнал для слоя активности ИМЕННО этого канала) — authored_by_owner=0
# (поздравления от других, гостевые посты, теги в чужих постах) не в счёт.
ACCOUNTS_SQL = """
SELECT a.id, a.person_id, a.network, a.url,
       (SELECT MAX(o.checked_at) FROM observations o WHERE o.account_id = a.id) AS last_checked,
       (SELECT MAX(o.post_date) FROM observations o
         WHERE o.account_id = a.id AND o.found_post=1 AND COALESCE(o.authored_by_owner,1)=1)
                                                                   AS last_post
FROM accounts a
WHERE a.url IS NOT NULL AND COALESCE(a.is_dead,0)=0
"""

# Растяжка интервала сверх DAYS по мере накопления empty_streak (см. compute_empty_streaks
# ниже) — по мере того как повторные проверки подтверждают «человек тут правда не пишет»,
# а не просто «давно не было повода». Потолок общий для всех важностей — даже важность 5
# не проверяется чаще раза в ABS_MAX_INTERVAL_DAYS, если 20+ проверок подряд ничего не дали.
BACKOFF = [(20, 3.0), (10, 2.0), (5, 1.5)]  # (порог streak, множитель), первое совпадение сверху
ABS_MAX_INTERVAL_DAYS = 730
# streak, начиная с которого стоит не просто растягивать автоматически, а показать
# владельцу как кандидата на явное решение (понизить важность / пометить канал нерабочим)
REVIEW_STREAK_THRESHOLD = 20

# 20.08.2026 — по прямому запросу владельца: прогноз due-списка на 20-27.08.2026
# (due_dynamics.html) показал пилу — 127/131 due 20-21.08, провал до 7-9
# 22-23.08, скачок до 88 26.08. Причина не в самих интервалах, а в том, что
# много каналов ОДНОГО и того же интервала (например, FB active — 5д) были
# проверены пачкой в один и тот же день (большие батчи типа "весь VK due" или
# разбор очереди FB/IG важности 3) — при одинаковом every вся пачка синхронно
# "воскресает" в один день. JITTER_FRACTION размазывает батч по календарю:
# у каждого канала свой стабильный множитель ±доля от every, зависящий ТОЛЬКО
# от account_id (не хранится в БД, пересчитывается на лету каждый прогон — тот
# же принцип, что и layer()/compute_empty_streaks() — см. комментарий в шапке
# файла). Средний интервал не меняется, меняется только ФАЗА конкретного
# канала внутри цикла — частота проверки, откалиброванная per network×
# importance×layer, остаётся той же.
# 0.30 подобрано эмпирически прогоном simulate-скрипта на прогнозе 20-27.08:
# при меньших значениях (0.15-0.20) скачок 26.08 сглаживался недостаточно
# (короткие FB/IG-интервалы 5-7д дают всего ±1-1.5д разброса), при 0.30 пик
# размазывается на несколько дней и дневная нагрузка выравнивается заметно
# лучше без ощутимой потери частоты проверки важных каналов.
JITTER_FRACTION = 0.30


def jitter_multiplier(account_id, fraction=JITTER_FRACTION):
    """Детерминированный псевдослучайный множитель в [1-fraction, 1+fraction],
    завязанный ТОЛЬКО на account_id — один и тот же канал каждый прогон
    получает один и тот же множитель (иначе due-статус скакал бы туда-сюда
    день ото дня без всякой связи с реальными проверками). md5 — не для
    криптографии, просто удобный детерминированный источник псевдослучайности
    без сторонних зависимостей."""
    h = hashlib.md5(str(account_id).encode()).hexdigest()
    frac01 = int(h[:8], 16) / 0xFFFFFFFF
    return (1 - fraction) + 2 * fraction * frac01


def backoff_multiplier(streak):
    for threshold, mult in BACKOFF:
        if streak >= threshold:
            return mult
    return 1.0


def compute_empty_streaks(con):
    """Для каждого account_id — сколько ПОДРЯД последних наблюдений (от самого
    свежего назад) НЕ показали собственной активности владельца: found_post=0,
    ИЛИ found_post=1, но authored_by_owner=0 (поздравления от других и т.п. —
    см. комментарий в шапке файла). Streak обрывается первым же найденным
    СОБСТВЕННЫМ постом. Считается заново каждый прогон (та же философия, что и
    layer() — не хранить, не заводить отдельный ресинк, который может разъехаться)."""
    cur = con.cursor()
    rows = cur.execute("""
        SELECT account_id, found_post, COALESCE(authored_by_owner,1)
        FROM observations
        ORDER BY account_id, checked_at DESC, id DESC
    """).fetchall()
    streaks = {}
    current, streak, active_seen = None, 0, False
    for account_id, found_post, owner in rows:
        if account_id != current:
            if current is not None:
                streaks[current] = streak
            current, streak, active_seen = account_id, 0, False
        if active_seen:
            continue
        if found_post == 1 and owner == 1:
            active_seen = True
        else:
            streak += 1
    if current is not None:
        streaks[current] = streak
    return streaks


def layer(last_post, today):
    """Слой давности КОНКРЕТНОГО КАНАЛА (человек×сеть) для расписания проверок:
    active <=30д, rare <=180д, dormant <=365д, archived >365д (пост в этой сети
    не находили вовсе — тоже dormant, не archived: одного отсутствия находки
    мало, чтобы понижать частоту сильнее; см. комментарий от 06.08.2026 в шапке
    файла). Не путать с compute_layer() в labeler/main.py — та же
    идея, но другие пороги (30/90/365, без archived) и другая цель (бейдж в
    очереди разметки, не расписание). Если меняешь пороги здесь ради дайджеста,
    проверь, не нужно ли поправить и тот файл — они не связаны кодом, только
    по смыслу."""
    if not last_post:
        return "dormant"
    y, m, d = map(int, last_post.split("-"))
    days = (today - date(y, m, d)).days
    if days <= 30:
        return "active"
    if days <= 180:
        return "rare"
    if days <= 365:
        return "dormant"
    return "archived"


def iter_due(con, network=None):
    """Генератор per-account (per-сеть) due-записей — общий источник правды
    для export_vk_daily.py / export_fb_batch.py / export_ig_batch.py, чтобы
    расписание не переизобреталось в каждом из них по-своему.

    Каждый dict: person_id, name, importance, circle, note, layer, every_days,
    last_post, network, url, account_id, last_checked, waited_days, empty_streak,
    backoff_applied (множитель > 1, если streak растянул интервал), jitter_applied
    (множитель сглаживания синхронных батчей, см. JITTER_FRACTION в шапке файла).
    layer/last_post — per-network (этого конкретного канала), с 06.08.2026.
    empty_streak/backoff — с 10.08.2026, jitter — с 20.08.2026, см. комментарии
    в шапке файла."""
    today = date.today()
    cur = con.cursor()
    streaks = compute_empty_streaks(con)
    people = {}
    for pid, name, imp, circle, note, last_post_any in cur.execute(PEOPLE_SQL):
        people[pid] = dict(name=name, importance=imp, circle=circle, note=note,
                            last_post_any_network=last_post_any)

    for aid, pid, net, url, last_checked, last_post in cur.execute(ACCOUNTS_SQL):
        info = people.get(pid)
        if not info:
            continue
        if network and net != network:
            continue
        acct_layer = layer(last_post, today)
        base_every = DAYS_BY_NETWORK.get(net, {}).get(info["importance"], {}).get(acct_layer)
        if base_every is None:
            continue
        streak = streaks.get(aid, 0)
        mult = backoff_multiplier(streak)
        jit = jitter_multiplier(aid)
        every = min(ABS_MAX_INTERVAL_DAYS, base_every * mult * jit)
        if last_checked:
            y, m, d = map(int, last_checked.split("-"))
            waited = (today - date(y, m, d)).days
        else:
            waited = 10 ** 4
        if waited < every:
            continue
        yield dict(person_id=pid, name=info["name"], importance=info["importance"],
                    circle=info["circle"], note=info["note"], layer=acct_layer,
                    every_days=every, last_post=last_post, network=net, url=url,
                    account_id=aid, last_checked=last_checked,
                    waited_days=None if waited > 9999 else waited,
                    empty_streak=streak, backoff_applied=mult if mult > 1 else None,
                    jitter_applied=round(jit, 2))


def main():
    con = sqlite3.connect(DB)
    rows = list(iter_due(con, network=NET))

    # группировка по человеку — только для читаемого вывода/JSON; сам due-статус
    # уже посчитан per-network внутри iter_due(). last_post/layer теперь разные
    # у разных сетей одного человека — поэтому группируем их в networks{}, а не
    # схлопываем в одно значение на человека (как было до 06.08.2026).
    by_person = {}
    for r in rows:
        p = by_person.get(r["person_id"])
        if p is None:
            p = dict(person_id=r["person_id"], name=r["name"], importance=r["importance"],
                      circle=r["circle"], note=r["note"], networks={})
            by_person[r["person_id"]] = p
        p["networks"][r["network"]] = dict(url=r["url"], layer=r["layer"],
                                            every_days=r["every_days"], last_post=r["last_post"],
                                            last_checked=r["last_checked"],
                                            waited_days=r["waited_days"],
                                            empty_streak=r["empty_streak"],
                                            backoff_applied=r["backoff_applied"],
                                            jitter_applied=r["jitter_applied"])

    due = list(by_person.values())
    due.sort(key=lambda r: (-r["importance"], r["name"]))
    if LIMIT:
        due = due[:LIMIT]

    if AS_JSON:
        print(json.dumps(due, ensure_ascii=False, indent=1))
    else:
        print(f"на {date.today().isoformat()} к проверке {len(due)} человек"
              + (f" (сеть {NET})" if NET else ""))
        for r in due:
            nets = ", ".join(
                f"{net}[{info['layer']}] посл.пост {info['last_post'] or 'никогда'}"
                + (f", streak={info['empty_streak']}×{info['backoff_applied']:g}"
                   if info["backoff_applied"] else "")
                for net, info in r["networks"].items()
            )
            print(f"  [{r['importance']}] {r['name']:<28} {nets}")

    # среди ВСЕХ каналов (не только due сегодня) — кандидаты на ручной разбор:
    # empty_streak дотянул до потолка автоматической растяжки, дальше понижать
    # частоту дальше некуда без явного решения владельца (importance / канал нерабочий).
    # Отдельный проход по compute_empty_streaks(), т.к. iter_due() уже отфильтровал
    # по waited>=every — а кандидатов на разбор нужно видеть независимо от того,
    # due ли канал именно сегодня.
    streaks = compute_empty_streaks(con)
    if streaks and not AS_JSON:
        review = [(aid, s) for aid, s in streaks.items() if s >= REVIEW_STREAK_THRESHOLD]
        if review:
            print(f"\n{len(review)} канал(ов) со streak >= {REVIEW_STREAK_THRESHOLD} "
                  f"(автоматическая растяжка уже на потолке {ABS_MAX_INTERVAL_DAYS}д) — "
                  f"python3 channel_health.py {DB} --min-streak {REVIEW_STREAK_THRESHOLD} "
                  f"для разбора")
    con.close()


if __name__ == "__main__":
    main()
