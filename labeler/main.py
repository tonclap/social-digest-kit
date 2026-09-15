#!/usr/bin/env python3
"""
Веб-очередь разметки поверх social.db.

Две очереди:
  /people  — «Разметка людей»: все, у кого нет важности или круга (важность
             почти всегда уже стоит дефолтом от количества сетей, в которых
             человек найден, — так что практически это очередь на круг).
             Тир Живые/С сигналом/Остальные считается от реальной важности
             и слоя активности (последний обход всех соцсетей уже прошёл,
             данные не заглушки). Сохранение — один клик по кнопке-цифре,
             без перезагрузки страницы (AJAX), строка сразу пропадает из очереди.
  /digest  — уже собранные пункты дайджеста (digest_items), отметить
             релевантно/нет — колонки relevance/relevance_note/reviewed_at
             добавляются в digest_items при первом запуске (nullable, аддитивно).

Превью страницы человека — НЕ iframe чужого сайта: vk/fb/instagram блокируют
встраивание через X-Frame-Options/CSP frame-ancestors (проверено на facebook.com —
`content-security-policy: frame-ancestors 'self'`), внешний iframe был бы пустым.
Вместо этого превью разворачивает то, что уже собрано в observations — полный
текст последнего поста и историю прошлых постов, без перехода на другую страницу.

Прототип: один файл, без ORM/миграций, пишет прямо в рабочую social.db.
Путь к базе — переменная окружения SOCIAL_DB_PATH, по умолчанию ./social.db
(запускать из корня проекта).
"""
import json
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

from flask import Flask, request, redirect, url_for, render_template_string, jsonify

# Пороги давности берём из scripts/due_today.py, а не держим вторую копию:
# раньше здесь было своё 30/90/365, там своё 30/180/365, и связывал их только
# комментарий «если меняешь одно, вспомни про второе».
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from due_today import layer as schedule_layer   # noqa: E402

DB_PATH = os.environ.get(
    "SOCIAL_DB_PATH",
    "social.db",
)

CIRCLES = ["семья", "друзья", "коллеги", "обучались вместе", "троичане", "активисты", "ученики", "знакомые", "Подписки"]
CONTACT_OPTIONS = [("yes", "да"), ("no", "нет"), ("maybe", "может")]
IMPORTANCE_VALUES = [5, 4, 3, 2, 1, 0]

app = Flask(__name__)


def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


BIO_MIN_IMPORTANCE = 3  # кому нужна сводка «кто это» — ниже этого порога биосправка не нужна


def ensure_schema(con):
    """Аддитивная миграция: nullable-колонки, ничего не стирает."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(digest_items)")}
    for name, ddl in (
        ("relevance", "ALTER TABLE digest_items ADD COLUMN relevance TEXT"),
        ("relevance_note", "ALTER TABLE digest_items ADD COLUMN relevance_note TEXT"),
        ("reviewed_at", "ALTER TABLE digest_items ADD COLUMN reviewed_at TEXT"),
    ):
        if name not in cols:
            con.execute(ddl)
    pcols = {r[1] for r in con.execute("PRAGMA table_info(people)")}
    for name, ddl in (
        ("bio_summary", "ALTER TABLE people ADD COLUMN bio_summary TEXT"),
        ("bio_updated_at", "ALTER TABLE people ADD COLUMN bio_updated_at TEXT"),
    ):
        if name not in pcols:
            con.execute(ddl)
    # Связи между людьми — не пары «дружат», а общие проекты и сообщества
    # (один лагерь, одна конференция, один кружок), вскрывшиеся при сборе
    # сводок — решение владельца 04.08.2026 хранить это как контексты, не как
    # рёбра графа человек-человек: иначе на одну группу из 5 человек пришлось
    # бы создавать 10 попарных записей вместо 5 членств в одном контексте.
    con.execute("""
        CREATE TABLE IF NOT EXISTS contexts (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            description TEXT,
            created_at  TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS person_context (
            id         INTEGER PRIMARY KEY,
            person_id  INTEGER NOT NULL REFERENCES people(id),
            context_id INTEGER NOT NULL REFERENCES contexts(id),
            role       TEXT,
            note       TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(person_id, context_id)
        )
    """)
    con.commit()


PEOPLE_QUERY_TMPL = """
SELECT p.id, p.display_name, p.importance, p.circle, p.in_contacts, p.note,
       MAX(CASE WHEN a.network='vk'        THEN a.url END) AS vk,
       MAX(CASE WHEN a.network='facebook'  THEN a.url END) AS fb,
       MAX(CASE WHEN a.network='instagram' THEN a.url END) AS ig,
       COUNT(DISTINCT a.network) AS nets,
       (SELECT MAX(o.post_date) FROM observations o
          JOIN accounts a2 ON a2.id=o.account_id
         WHERE a2.person_id=p.id AND o.found_post=1) AS last_post,
       (SELECT COUNT(*) FROM observations o
          JOIN accounts a2 ON a2.id=o.account_id
         WHERE a2.person_id=p.id) AS checks,
       (SELECT o.summary FROM observations o
          JOIN accounts a2 ON a2.id=o.account_id
         WHERE a2.person_id=p.id AND o.found_post=1
         ORDER BY o.post_date DESC LIMIT 1) AS summary
FROM people p LEFT JOIN accounts a ON a.person_id=p.id
WHERE {where}
GROUP BY p.id
"""

PEOPLE_WHERE = "p.importance IS NULL OR p.circle IS NULL"


# Человеческие подписи к слоям из scripts/due_today.py: active/rare/dormant/
# archived — это 30/180/365 дней и «старше года».
LAYER_BADGE = {"active": "30 дней", "rare": "полгода", "dormant": "год",
               "archived": "больше года"}


def compute_layer(last_post, checks, today):
    """Бейдж давности для очереди разметки. Пороги — те же, что у расписания
    проверок (layer() в scripts/due_today.py), отличается только подпись:
    здесь она про «когда человек последний раз писал», там про то, как часто
    его канал трогать. Своей копии порогов тут больше нет — из-за неё «3 месяца»
    в очереди и «rare» в расписании означали разное."""
    if not last_post:
        return "молчит" if checks else "не проверялся"
    try:
        return LAYER_BADGE[schedule_layer(last_post, today)]
    except Exception:          # дата в базе не разбирается — не роняем очередь
        return "молчит" if checks else "не проверялся"


def load_people_queue(con):
    """Тир Живые/С сигналом/Остальные — теперь на реальных данных: важность
    почти всегда уже проставлена дефолтом от количества сетей, а
    слой активности посчитан по завершённому первому обходу всех соцсетей,
    не по заглушкам. Раньше тут была отдельная модель-подсказка для ещё не
    известной важности — она больше не нужна, важность уже есть у всех, кроме
    единичных случаев ручного сброса."""
    today = date.today()
    rows = []
    for r in con.execute(PEOPLE_QUERY_TMPL.format(where=PEOPLE_WHERE)):
        layer = compute_layer(r["last_post"], r["checks"], today)
        importance = r["importance"] if r["importance"] is not None else 0

        score = importance * 10
        score += 3 if r["in_contacts"] == "yes" else 0
        score += {"30 дней": 4, "полгода": 3, "год": 1}.get(layer, 0)

        rows.append(dict(
            id=r["id"], display_name=r["display_name"],
            importance=r["importance"], circle=r["circle"],
            in_contacts=r["in_contacts"] or "", note=r["note"] or "",
            vk=r["vk"], fb=r["fb"], ig=r["ig"], nets=r["nets"] or 0,
            last_post=r["last_post"] or "", layer=layer,
            summary=(r["summary"] or "")[:200], score=score,
        ))

    # Тиры разводятся по id, а не по `r not in live`: там сравнение шло словарями,
    # то есть на каждой строке очереди пересравнивались все поля всех предыдущих —
    # на двух с половиной тысячах человек это единственное место, которое заметно
    # тормозило страницу.
    live = [r for r in rows if r["layer"] in ("30 дней", "полгода")]
    live_ids = {r["id"] for r in live}
    rest = [r for r in rows if r["id"] not in live_ids]
    # r["in_contacts"] хранит 'yes'/'no'/'maybe'/'' — 'no' truthy как строка,
    # поэтому сравнивать явно с 'yes', а не проверять истинность самой строки.
    signal = [r for r in rest if r["in_contacts"] == "yes" or (r["importance"] or 0) >= 2]
    signal_ids = {r["id"] for r in signal}
    other = [r for r in rest if r["id"] not in signal_ids]
    for lst in (live, signal, other):
        lst.sort(key=lambda r: (-r["score"], r["last_post"] == "", r["display_name"]))

    for r in live:
        r["tier"] = "1. Живые"
    for r in signal:
        r["tier"] = "2. С сигналом"
    for r in other:
        r["tier"] = "3. Остальные"
    return live + signal + other


BASE = """
<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>{{ title }}</title>
<style>
 body{font-family:system-ui,Arial,sans-serif;margin:0;padding:1.5rem;background:#fafafa;color:#222}
 h1{font-size:1.3rem;margin:0 0 .3rem}
 nav a{margin-right:1rem}
 table{border-collapse:collapse;width:100%;font-size:.85rem;background:#fff}
 th,td{border:1px solid #ddd;padding:.35rem .5rem;text-align:left;vertical-align:top}
 th{background:#1F3864;color:#fff;position:sticky;top:0}
 tr:nth-child(even){background:#f5f5f5}
 select,input[type=text]{font-size:.85rem;padding:.15rem}
 button{cursor:pointer}
 .tier{font-weight:bold;background:#FFF2CC}
 .summary{max-width:320px;font-size:.8rem;color:#555}
 .muted{color:#888}
 .counts{margin:.5rem 0 1rem;color:#444}
 .flash{background:#e6f4ea;border:1px solid #b7dfc0;padding:.4rem .7rem;margin-bottom:.8rem;display:inline-block}
 .pills{display:flex;gap:.2rem}
 .pill{width:1.7rem;height:1.7rem;border:1px solid #999;border-radius:4px;background:#fff;
       font-size:.8rem;display:flex;align-items:center;justify-content:center;padding:0}
 .pill:hover{background:#e8eefc}
 .pill.current{background:#1F3864;color:#fff;border-color:#1F3864}
 .pill.saving{opacity:.4;pointer-events:none}
 tr.saved{opacity:.25;transition:opacity .4s}
 .preview-toggle{background:none;border:none;color:#0563C1;text-decoration:underline;padding:0;font-size:.8rem}
 .preview-box{margin-top:.4rem;padding:.4rem;background:#f0f4fa;border-radius:4px;max-width:420px;display:none}
 .preview-box ul{margin:.2rem 0;padding-left:1.1rem}
 .preview-box li{margin-bottom:.3rem}
</style></head><body>
<h1>labeler/main.py</h1>
<nav><a href="{{ url_for('index') }}">Главная</a>
<a href="{{ url_for('labeled_people') }}">Полностью размечены</a>
<a href="{{ url_for('people_profiles') }}">Сводки по людям</a>
<a href="{{ url_for('contexts_list') }}">Связи (контексты)</a>
<a href="{{ url_for('digest_queue') }}">Дайджест — релевантность</a></nav>
<hr>
{{ body|safe }}
</body></html>
"""


@app.route("/")
def index():
    con = get_db()
    ensure_schema(con)
    total = con.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    no_imp = con.execute("SELECT COUNT(*) FROM people WHERE importance IS NULL").fetchone()[0]
    no_cir = con.execute("SELECT COUNT(*) FROM people WHERE circle IS NULL").fetchone()[0]
    di_total = con.execute("SELECT COUNT(*) FROM digest_items").fetchone()[0]
    di_unrev = con.execute("SELECT COUNT(*) FROM digest_items WHERE relevance IS NULL").fetchone()[0]
    con.close()
    body = render_template_string("""
    <p class="counts">
      База: <b>{{ path }}</b><br>
      Людей всего: {{ total }} · без важности: {{ no_imp }} · без круга: {{ no_cir }}<br>
      Пунктов дайджеста: {{ di_total }} · без оценки релевантности: {{ di_unrev }}
    </p>
    <p><a href="{{ url_for('digest_queue') }}">→ Оценивать релевантность постов</a></p>
    """, path=DB_PATH, total=total, no_imp=no_imp, no_cir=no_cir, di_total=di_total, di_unrev=di_unrev)
    return render_template_string(BASE, title="labeler/main.py", body=body)


@app.route("/people")
def people_queue():
    con = get_db()
    ensure_schema(con)
    limit = int(request.args.get("limit", 50))
    queue = load_people_queue(con)
    con.close()
    shown = queue[:limit]
    body = render_template_string("""
    <p class="counts">Показано {{ shown|length }} из {{ total }} без важности или круга
    (осталось <span id="remaining">{{ total }}</span>).
    {% if total > shown|length %}<a href="?limit={{ limit + 50 }}">показать ещё 50</a>{% endif %}</p>
    <table>
    <tr>
      <th>Тир</th><th>Имя</th><th>Слой</th><th>Что писал</th><th>Важность</th><th>Круг</th>
    </tr>
    {% for r in shown %}
    <tr id="row-{{ r.id }}" data-pid="{{ r.id }}"
        data-importance="{{ r.importance if r.importance is not none else '' }}"
        data-circle="{{ r.circle or '' }}">
      <td class="tier">{{ r.tier }}</td>
      <td>{{ r.display_name }}
        {% if r.vk %}<br><a href="{{ r.vk }}" target="_blank">vk</a>{% endif %}
        {% if r.fb %}<br><a href="{{ r.fb }}" target="_blank">fb</a>{% endif %}
        {% if r.ig %}<br><a href="{{ r.ig }}" target="_blank">ig</a>{% endif %}
      </td>
      <td>{{ r.layer }}{% if r.last_post %}<br><span class="muted">{{ r.last_post }}</span>{% endif %}</td>
      <td class="summary">{{ r.summary }}
        <br><button type="button" class="preview-toggle" onclick="togglePreview({{ r.id }})">превью</button>
        <div class="preview-box" id="preview-{{ r.id }}"></div>
      </td>
      <td><div class="pills">
        {% for v in importance_values %}
        <button type="button" class="pill {{ 'current' if r.importance == v else '' }}"
                onclick="savePerson({{ r.id }}, 'importance', '{{ v }}', this)">{{ v }}</button>
        {% endfor %}
      </div></td>
      <td><div class="pills">
        {% for c in circles %}
        <button type="button" class="pill {{ 'current' if r.circle == c else '' }}" title="{{ c }}"
                onclick="savePerson({{ r.id }}, 'circle', '{{ c }}', this)">{{ c[:2] }}</button>
        {% endfor %}
      </div></td>
    </tr>
    {% endfor %}
    </table>
    <script>
    function savePerson(pid, field, value, btn) {
      var row = document.getElementById('row-' + pid);
      var group = btn.parentElement;
      var pills = group.querySelectorAll('.pill');
      pills.forEach(function(p) { p.classList.add('saving'); });
      var body = field + '=' + encodeURIComponent(value);
      fetch('/people/' + pid + '/update', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'fetch'},
        body: body
      }).then(function(r) {
        if (!r.ok) throw new Error('save failed');
        return r.json();
      }).then(function() {
        pills.forEach(function(p) { p.classList.remove('saving', 'current'); });
        btn.classList.add('current');
        row.dataset[field] = value;
        // Убрать строку из очереди только когда заполнены ОБА поля —
        // очередь общая (важность + круг), одного сохранения недостаточно.
        if (row.dataset.importance !== '' && row.dataset.circle !== '') {
          row.classList.add('saved');
          setTimeout(function() {
            row.remove();
            var el = document.getElementById('remaining');
            if (el) el.textContent = Math.max(0, parseInt(el.textContent, 10) - 1);
          }, 350);
        }
      }).catch(function(e) {
        pills.forEach(function(p) { p.classList.remove('saving'); });
        alert('Не сохранилось: ' + e);
      });
    }
    function togglePreview(pid) {
      var box = document.getElementById('preview-' + pid);
      if (box.dataset.loaded) {
        box.style.display = box.style.display === 'none' ? 'block' : 'none';
        return;
      }
      box.textContent = 'Загрузка…';
      box.style.display = 'block';
      fetch('/people/' + pid + '/preview').then(function(r) { return r.text(); }).then(function(html) {
        box.innerHTML = html;
        box.dataset.loaded = '1';
      });
    }
    </script>
    """, shown=shown, total=len(queue), limit=limit, circles=CIRCLES, importance_values=IMPORTANCE_VALUES)
    return render_template_string(BASE, title="Разметка людей", body=body)


@app.route("/people/<int:pid>/update", methods=["POST"])
def people_update(pid):
    imp = request.form.get("importance", "")
    cir = request.form.get("circle", "")
    contacts = request.form.get("in_contacts", "")
    con = get_db()
    fields, values = [], []
    if imp != "":
        fields.append("importance=?")
        values.append(int(imp))
    if cir != "":
        fields.append("circle=?")
        values.append(cir)
    if contacts != "":
        fields.append("in_contacts=?")
        values.append(contacts)
    if fields:
        fields.append("updated_at=?")
        values.append(datetime.now().isoformat(timespec="seconds"))
        values.append(pid)
        con.execute(f"UPDATE people SET {', '.join(fields)} WHERE id=?", values)
        con.commit()
    con.close()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(request.referrer or url_for("people_queue"))


@app.route("/people/<int:pid>/preview")
def people_preview(pid):
    con = get_db()
    person = con.execute(
        "SELECT display_name, note, bio_summary, bio_updated_at FROM people WHERE id=?", (pid,)
    ).fetchone()
    if not person:
        con.close()
        return "человек не найден", 404
    ig_bio = con.execute("""
        SELECT bio_header_name, bio_link FROM accounts
        WHERE person_id = ? AND network='instagram'
          AND (bio_header_name IS NOT NULL OR bio_link IS NOT NULL)
        ORDER BY bio_captured_at DESC LIMIT 1
    """, (pid,)).fetchone()
    obs = con.execute("""
        SELECT a.network, o.post_date, o.summary, o.post_url
        FROM observations o JOIN accounts a ON a.id = o.account_id
        WHERE a.person_id = ? AND o.found_post = 1
        ORDER BY o.post_date DESC LIMIT 15
    """, (pid,)).fetchall()
    related = con.execute("""
        SELECT c.name AS context_name, pc.role, p2.id AS person_id, p2.display_name
        FROM person_context pc
        JOIN contexts c ON c.id = pc.context_id
        JOIN person_context pc2 ON pc2.context_id = pc.context_id AND pc2.person_id != pc.person_id
        JOIN people p2 ON p2.id = pc2.person_id
        WHERE pc.person_id = ?
        ORDER BY c.name, p2.display_name COLLATE NOCASE
    """, (pid,)).fetchall()
    con.close()
    return render_template_string("""
    {% if bio_summary %}<p><b>Сводка</b> ({{ bio_updated_at or '?' }}): {{ bio_summary }}</p>{% endif %}
    {% if related %}
    <p class="muted"><b>Пересечения:</b>
    {% for r in related %}{{ r.context_name }} — {{ r.display_name }}{% if not loop.last %}; {% endif %}{% endfor %}
    </p>
    {% endif %}
    {% if note %}<p><i>{{ note }}</i></p>{% endif %}
    {% if ig_bio and (ig_bio.bio_header_name or ig_bio.bio_link) %}
    <p class="muted">IG bio:
      {% if ig_bio.bio_header_name %}{{ ig_bio.bio_header_name }}{% endif %}
      {% if ig_bio.bio_link %} — <a href="{{ ig_bio.bio_link }}" target="_blank">{{ ig_bio.bio_link }}</a>{% endif %}
    </p>
    {% endif %}
    {% if obs %}
    <ul>
    {% for o in obs %}
      <li><b>{{ o.post_date }}</b> [{{ o.network }}] {{ o.summary or '(без текста)' }}
      {% if o.post_url %} — <a href="{{ o.post_url }}" target="_blank">пост</a>{% endif %}</li>
    {% endfor %}
    </ul>
    {% else %}<p class="muted">Постов не найдено (страница чужой соцсети сюда не встраивается —
    vk/facebook/instagram блокируют iframe; открыть можно по ссылке рядом с именем).</p>{% endif %}
    """, note=person["note"], bio_summary=person["bio_summary"], bio_updated_at=person["bio_updated_at"],
       ig_bio=ig_bio, obs=obs, related=related)


@app.route("/people/labeled")
def labeled_people():
    """Read-only список тех, у кого важность И круг уже оба проставлены —
    независимо от IG, просто чтобы видеть, кто уже полностью размечен.
    Фильтр по важности/кругу через GET-параметры, каждый сам себя отправляет."""
    imp_filter = request.args.get("importance", "")
    circle_filter = request.args.get("circle", "")
    where = ["p.importance IS NOT NULL", "p.circle IS NOT NULL"]
    params = []
    if imp_filter != "":
        where.append("p.importance = ?")
        params.append(int(imp_filter))
    if circle_filter != "":
        where.append("p.circle = ?")
        params.append(circle_filter)
    con = get_db()
    rows = con.execute(f"""
        SELECT p.id, p.display_name, p.importance, p.circle,
               MAX(CASE WHEN a.network='vk'        THEN a.url END) AS vk,
               MAX(CASE WHEN a.network='facebook'  THEN a.url END) AS fb,
               MAX(CASE WHEN a.network='instagram' THEN a.url END) AS ig,
               (SELECT o.summary FROM observations o JOIN accounts a2 ON a2.id=o.account_id
                  WHERE a2.person_id=p.id AND o.found_post=1
                  ORDER BY o.post_date DESC LIMIT 1) AS summary
        FROM people p LEFT JOIN accounts a ON a.person_id = p.id
        WHERE {' AND '.join(where)}
        GROUP BY p.id
        ORDER BY p.importance DESC, p.display_name COLLATE NOCASE
    """, params).fetchall()
    con.close()
    body = render_template_string("""
    <p class="counts">
      <form method="get" style="display:inline">
        Важность:
        <select name="importance" onchange="this.form.submit()">
          <option value="">все</option>
          {% for v in importance_values %}
          <option value="{{ v }}" {{ 'selected' if imp_filter == v|string else '' }}>{{ v }}</option>
          {% endfor %}
        </select>
        Круг:
        <select name="circle" onchange="this.form.submit()">
          <option value="">все</option>
          {% for c in circles %}
          <option value="{{ c }}" {{ 'selected' if circle_filter == c else '' }}>{{ c }}</option>
          {% endfor %}
        </select>
      </form>
      — {{ rows|length }} человек{% if imp_filter or circle_filter %} под фильтром{% else %} полностью размечены (важность + круг){% endif %}.
    </p>
    <table>
    <tr><th>Имя</th><th>Важность</th><th>Круг</th><th>Сети</th><th>Что писал</th></tr>
    {% for r in rows %}
    <tr id="lrow-{{ r.id }}">
      <td>{{ r.display_name }}</td>
      <td><div class="pills">
        {% for v in importance_values %}
        <button type="button" class="pill {{ 'current' if r.importance == v else '' }}"
                onclick="saveLabeled({{ r.id }}, 'importance', '{{ v }}', this)">{{ v }}</button>
        {% endfor %}
      </div></td>
      <td><div class="pills">
        {% for c in circles %}
        <button type="button" class="pill {{ 'current' if r.circle == c else '' }}" title="{{ c }}"
                onclick="saveLabeled({{ r.id }}, 'circle', '{{ c }}', this)">{{ c[:2] }}</button>
        {% endfor %}
      </div></td>
      <td>
        {% if r.vk %}<a href="{{ r.vk }}" target="_blank">vk</a>{% endif %}
        {% if r.fb %} <a href="{{ r.fb }}" target="_blank">fb</a>{% endif %}
        {% if r.ig %} <a href="{{ r.ig }}" target="_blank">ig</a>{% endif %}
      </td>
      <td class="summary">{{ (r.summary or '')[:200] }}
        <br><button type="button" class="preview-toggle" onclick="togglePreview({{ r.id }})">превью</button>
        <div class="preview-box" id="preview-{{ r.id }}"></div>
      </td>
    </tr>
    {% endfor %}
    </table>
    <script>
    function saveLabeled(pid, field, value, btn) {
      var group = btn.parentElement;
      var pills = group.querySelectorAll('.pill');
      pills.forEach(function(p) { p.classList.add('saving'); });
      fetch('/people/' + pid + '/update', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'fetch'},
        body: field + '=' + encodeURIComponent(value)
      }).then(function(r) {
        if (!r.ok) throw new Error('save failed');
        return r.json();
      }).then(function() {
        pills.forEach(function(p) { p.classList.remove('saving', 'current'); });
        btn.classList.add('current');
      }).catch(function(e) {
        pills.forEach(function(p) { p.classList.remove('saving'); });
        alert('Не сохранилось: ' + e);
      });
    }
    function togglePreview(pid) {
      var box = document.getElementById('preview-' + pid);
      if (box.dataset.loaded) {
        box.style.display = box.style.display === 'none' ? 'block' : 'none';
        return;
      }
      box.textContent = 'Загрузка…';
      box.style.display = 'block';
      fetch('/people/' + pid + '/preview').then(function(r) { return r.text(); }).then(function(html) {
        box.innerHTML = html;
        box.dataset.loaded = '1';
      });
    }
    </script>
    """, rows=rows, imp_filter=imp_filter, circle_filter=circle_filter,
       circles=CIRCLES, importance_values=IMPORTANCE_VALUES)
    return render_template_string(BASE, title="Полностью размечены", body=body)


@app.route("/people/profiles")
def people_profiles():
    """Короткие сводки «кто это и что о нём известно» — для важности >=
    BIO_MIN_IMPORTANCE. Заполняются НЕ автоматически — по запросу агенту
    (поиск + то, что уже известно из наблюдений), либо руками прямо здесь.
    Эта страница — витрина + бэклог (кому ещё сводка не написана)."""
    con = get_db()
    ensure_schema(con)
    only_missing = request.args.get("all") != "1"
    where = "p.importance >= ?"
    params = [BIO_MIN_IMPORTANCE]
    if only_missing:
        where += " AND (p.bio_summary IS NULL OR p.bio_summary = '')"
    rows = con.execute(f"""
        SELECT p.id, p.display_name, p.importance, p.circle, p.bio_summary, p.bio_updated_at
        FROM people p
        WHERE {where}
        ORDER BY p.importance DESC, p.display_name COLLATE NOCASE
    """, params).fetchall()
    total_eligible = con.execute(
        "SELECT COUNT(*) FROM people WHERE importance >= ?", (BIO_MIN_IMPORTANCE,)
    ).fetchone()[0]
    with_bio = con.execute(
        "SELECT COUNT(*) FROM people WHERE importance >= ? AND bio_summary IS NOT NULL AND bio_summary != ''",
        (BIO_MIN_IMPORTANCE,),
    ).fetchone()[0]
    con.close()
    body = render_template_string("""
    <p class="counts">
      Сводки положены важности {{ min_imp }}+ — таких {{ total_eligible }} человек,
      у {{ with_bio }} уже есть сводка, у {{ total_eligible - with_bio }} ещё нет.<br>
      {% if only_missing %}Показаны только без сводки. <a href="?all=1">показать всех</a>
      {% else %}Показаны все. <a href="?">только без сводки</a>{% endif %}
    </p>
    <table>
    <tr><th>Имя</th><th>Важность</th><th>Круг</th><th>Сводка</th></tr>
    {% for r in rows %}
    <tr>
      <td>{{ r.display_name }}</td>
      <td>{{ r.importance }}</td>
      <td>{{ r.circle or '' }}</td>
      <td>
        <form method="post" action="{{ url_for('people_bio_update', pid=r.id) }}">
        <textarea name="bio_summary" rows="3" style="width:100%;font:inherit"
          placeholder="Кто это, что известно (можно с источниками) — заполняется по запросу в чате или вручную">{{ r.bio_summary or '' }}</textarea>
        <button type="submit">Сохранить</button>
        {% if r.bio_updated_at %}<span class="muted"> · обновлено {{ r.bio_updated_at }}</span>{% endif %}
        </form>
      </td>
    </tr>
    {% endfor %}
    </table>
    """, rows=rows, only_missing=only_missing, total_eligible=total_eligible,
       with_bio=with_bio, min_imp=BIO_MIN_IMPORTANCE)
    return render_template_string(BASE, title="Сводки по людям", body=body)


@app.route("/people/<int:pid>/bio/update", methods=["POST"])
def people_bio_update(pid):
    text = request.form.get("bio_summary", "").strip()
    con = get_db()
    ensure_schema(con)
    con.execute(
        "UPDATE people SET bio_summary=?, bio_updated_at=? WHERE id=?",
        (text or None, datetime.now().isoformat(timespec="seconds") if text else None, pid),
    )
    con.commit()
    con.close()
    return redirect(request.referrer or url_for("people_profiles"))


@app.route("/contexts")
def contexts_list():
    """Общие проекты/сообщества, вскрывшиеся при сборе сводок (не пары
    «дружат» — общие контексты: Гринкемп, «Будущее сегодня» и т.п.), решение
    хранить так — 04.08.2026. Каждый контекст = сколько угодно людей с ролью."""
    con = get_db()
    ensure_schema(con)
    ctxs = con.execute("SELECT id, name, description FROM contexts ORDER BY name COLLATE NOCASE").fetchall()
    members = {}
    for pc in con.execute("""
        SELECT pc.context_id, p.id AS person_id, p.display_name, pc.role, pc.note
        FROM person_context pc JOIN people p ON p.id = pc.person_id
        ORDER BY pc.role, p.display_name COLLATE NOCASE
    """).fetchall():
        members.setdefault(pc["context_id"], []).append(pc)
    all_people = con.execute(
        "SELECT id, display_name FROM people ORDER BY display_name COLLATE NOCASE"
    ).fetchall()
    con.close()
    # 2369+ человек — обычный <select> раздувает страницу до полусотни КБ и
    # непригоден для поиска глазами; вместо этого один общий JS-массив (грузится
    # один раз) + текстовый фильтр на клиенте перед каждой формой добавления.
    people_json = json.dumps([{"id": p["id"], "name": p["display_name"]} for p in all_people], ensure_ascii=False)
    body = render_template_string("""
    <p class="counts">{{ ctxs|length }} контекстов.</p>

    <form method="post" action="{{ url_for('context_create') }}" style="margin-bottom:1rem">
      <input type="text" name="name" placeholder="Название контекста (напр. Гринкемп)" required>
      <input type="text" name="description" placeholder="Описание" style="width:40%">
      <button type="submit">Создать</button>
    </form>

    {% for c in ctxs %}
    <div style="background:#fff;border:1px solid #ddd;border-radius:4px;padding:.6rem 1rem;margin-bottom:.8rem">
      <b>{{ c.name }}</b>{% if c.description %} — <span class="muted">{{ c.description }}</span>{% endif %}
      <ul>
        {% for m in members.get(c.id, []) %}
        <li>{{ m.display_name }}{% if m.role %} — {{ m.role }}{% endif %}
            {% if m.note %} <span class="muted">({{ m.note }})</span>{% endif %}</li>
        {% endfor %}
      </ul>
      <form method="post" action="{{ url_for('context_add_person', cid=c.id) }}"
            style="font-size:.85rem;position:relative" onsubmit="return prepSubmit(this)">
        <input type="text" class="person-search" placeholder="начните вводить имя…" autocomplete="off"
               style="max-width:220px" oninput="filterPeople(this)">
        <input type="hidden" name="person_id" class="person-id">
        <div class="person-suggest" style="display:none;position:absolute;top:1.6rem;left:0;z-index:5;
             background:#fff;border:1px solid #ccc;max-height:200px;overflow:auto;width:220px"></div>
        <input type="text" name="role" placeholder="роль (напр. основатель)">
        <input type="text" name="note" placeholder="заметка">
        <button type="submit">Добавить</button>
      </form>
    </div>
    {% endfor %}

    <script>
    var PEOPLE = {{ people_json|safe }};
    function filterPeople(input) {
      var box = input.parentElement.querySelector('.person-suggest');
      var hidden = input.parentElement.querySelector('.person-id');
      hidden.value = '';
      var q = input.value.trim().toLowerCase();
      if (q.length < 2) { box.style.display = 'none'; return; }
      var matches = PEOPLE.filter(function(p) { return p.name.toLowerCase().indexOf(q) !== -1; }).slice(0, 20);
      box.innerHTML = matches.map(function(p) {
        return '<div style="padding:.2rem .4rem;cursor:pointer" data-id="' + p.id + '" data-name="' +
               p.name.replace(/"/g, '&quot;') + '">' + p.name + '</div>';
      }).join('');
      box.style.display = matches.length ? 'block' : 'none';
      box.querySelectorAll('div').forEach(function(el) {
        el.onclick = function() {
          input.value = el.dataset.name;
          hidden.value = el.dataset.id;
          box.style.display = 'none';
        };
      });
    }
    function prepSubmit(form) {
      if (!form.querySelector('.person-id').value) { alert('Выберите человека из списка'); return false; }
      return true;
    }
    </script>
    """, ctxs=ctxs, members=members, people_json=people_json)
    return render_template_string(BASE, title="Связи (контексты)", body=body)


@app.route("/contexts/new", methods=["POST"])
def context_create():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if name:
        con = get_db()
        ensure_schema(con)
        con.execute(
            "INSERT OR IGNORE INTO contexts (name, description, created_at) VALUES (?, ?, ?)",
            (name, description or None, datetime.now().isoformat(timespec="seconds")),
        )
        con.commit()
        con.close()
    return redirect(url_for("contexts_list"))


@app.route("/contexts/<int:cid>/add_person", methods=["POST"])
def context_add_person(cid):
    person_id = request.form.get("person_id", "")
    role = request.form.get("role", "").strip()
    note = request.form.get("note", "").strip()
    if person_id:
        con = get_db()
        ensure_schema(con)
        con.execute(
            "INSERT OR IGNORE INTO person_context (person_id, context_id, role, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(person_id), cid, role or None, note or None, datetime.now().isoformat(timespec="seconds")),
        )
        con.commit()
        con.close()
    return redirect(url_for("contexts_list"))


@app.route("/people/new")
def new_people():
    """Список IG-заглушек, подтверждённых как реальные новые люди (не дубль
    кого-то уже известного) во время ручного разбора 03.08.2026. Чисто для
    просмотра глазами — вдруг найдётся ещё кто-то знакомый, кого тогда не
    удалось сопоставить (bio не совпало ни с кем текстово). Слияние отсюда не
    делается — если найдёте совпадение, дайте id/имя, сольём вручную."""
    con = get_db()
    rows = con.execute("""
        SELECT p.id, p.display_name, a.handle, a.url, a.bio_header_name, a.bio_link
        FROM people p JOIN accounts a ON a.person_id = p.id AND a.network = 'instagram'
        WHERE p.ig_review_status = 'new_person'
        ORDER BY p.display_name COLLATE NOCASE
    """).fetchall()
    con.close()
    body = render_template_string("""
    <p class="counts">{{ rows|length }} IG-аккаунтов подтверждены как новые люди (не дубли).</p>
    <table>
    <tr><th>Имя / хендл</th><th>Bio</th></tr>
    {% for r in rows %}
    <tr>
      <td>{{ r.display_name }}<br><a href="{{ r.url }}" target="_blank">@{{ r.handle }}</a></td>
      <td>
        {% if r.bio_header_name %}{{ r.bio_header_name }}<br>{% endif %}
        {% if r.bio_link %}<a class="biolink" href="{{ r.bio_link }}" target="_blank">{{ r.bio_link }}</a>{% endif %}
      </td>
    </tr>
    {% endfor %}
    </table>
    <style>.biolink{font-size:.75rem;word-break:break-all}</style>
    """, rows=rows)
    return render_template_string(BASE, title="IG — подтверждённые новые люди", body=body)


@app.route("/digest")
def digest_queue():
    con = get_db()
    ensure_schema(con)
    only_unreviewed = request.args.get("all") != "1"
    q = """
    SELECT d.id, d.digest_date, d.networks, d.category, d.summary,
           d.relevance, d.relevance_note, p.display_name
    FROM digest_items d JOIN people p ON p.id = d.person_id
    """
    if only_unreviewed:
        q += " WHERE d.relevance IS NULL"
    q += " ORDER BY d.digest_date DESC, d.id DESC"
    items = con.execute(q).fetchall()
    con.close()
    body = render_template_string("""
    <p class="counts">{{ items|length }} пунктов {{ 'без оценки' if only_unreviewed else 'всего' }}.
    {% if only_unreviewed %}<a href="?all=1">показать все, включая оценённые</a>
    {% else %}<a href="?">только без оценки</a>{% endif %}</p>
    <table>
    <tr><th>Дата</th><th>Человек</th><th>Категория</th><th>Текст</th><th>Оценка</th></tr>
    {% for it in items %}
    <tr>
      <td>{{ it.digest_date }}</td>
      <td>{{ it.display_name }}</td>
      <td>{{ it.category or '' }}</td>
      <td class="summary">{{ it.summary }}</td>
      <td>
        <form method="post" action="{{ url_for('digest_update', item_id=it.id) }}">
        <select name="relevance">
          <option value="">—</option>
          <option value="relevant" {{ 'selected' if it.relevance=='relevant' else '' }}>релевантно</option>
          <option value="not_relevant" {{ 'selected' if it.relevance=='not_relevant' else '' }}>не по делу</option>
        </select>
        <input type="text" name="relevance_note" placeholder="заметка" value="{{ it.relevance_note or '' }}">
        <button type="submit">Сохранить</button>
        </form>
      </td>
    </tr>
    {% endfor %}
    </table>
    """, items=items, only_unreviewed=only_unreviewed)
    return render_template_string(BASE, title="Дайджест — релевантность", body=body)


@app.route("/digest/<int:item_id>/update", methods=["POST"])
def digest_update(item_id):
    relevance = request.form.get("relevance", "")
    note = request.form.get("relevance_note", "")
    con = get_db()
    ensure_schema(con)
    con.execute(
        "UPDATE digest_items SET relevance=?, relevance_note=?, reviewed_at=? WHERE id=?",
        (relevance or None, note or None, datetime.now().isoformat(timespec="seconds"), item_id),
    )
    con.commit()
    con.close()
    return redirect(request.referrer or url_for("digest_queue"))


if __name__ == "__main__":
    # use_reloader=False: со включённым автоперезапуском werkzeug следит за ВСЕМИ
    # .py-файлами, гружеными в процесс, включая посторонние проекты в соседних
    # каталогах — правка любого чужого файла рестартовала сервис и рвала запросы
    # прямо посреди живой сессии разметки (так потеряно ~130 из ~180 меток).
    # Правки main.py теперь требуют ручного перезапуска — это осознанный компромисс.
    # debug=False: со включённым debug любая необработанная ошибка (например
    # /people?limit=abc) отдаёт интерактивную консоль werkzeug — выполнение
    # произвольного кода в процессе, у которого открыта на запись база с
    # персональными данными полутора тысяч живых людей. Трассировки при этом
    # никуда не деваются, они по-прежнему печатаются в терминал, где запущен
    # сервер. Порт слушается только на 127.0.0.1 (умолчание Flask) — не менять.
    app.run(debug=False, port=5057, use_reloader=False)
