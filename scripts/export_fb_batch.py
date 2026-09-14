#!/usr/bin/env python3
"""
Готовит очередную порцию профилей Facebook для сбора активности.

02.08.2026: способ сбора переделан. Раньше страница просто делала fetch() на профиль —
это перестало работать: Facebook отдаёт на голый fetch пустой каркас приложения без
данных (проверено — 0 сигналов у трёх заведомо активных профилей). Реальные данные
появляются только при настоящем рендере страницы. Оказалось, что скрытый <iframe>
всё-таки рендерит полную страницу (комментарий в старой версии этого файла про запрет
iframe был ошибочным или устаревшим) — так и собираем теперь.

Известная дыра: профили вида profile.php?id=... не грузятся в iframe (похоже, Facebook
их так защищает — систематическая ошибка вида "Cannot read properties of null (reading
location)"). Такие профили из выборки исключены, пока не появится отдельный способ
их обходить (скорее всего — настоящая навигация вкладки вместо iframe, штука медленнее
и заметнее, поэтому для них нужен отдельный, более редкий прогон).

Страницу нельзя запросить из контейнера (нужна залогиненная сессия) — поэтому единственный
рабочий способ collection — скрипт в консоли браузера владельца (или тот же скрипт,
запущенный через Claude in Chrome в его настоящей вкладке).

Порядок обхода — сначала те, у кого больше шансов оказаться интересными:
есть в телефонной книжке / найдены в двух сетях / уже размечены важностью.

07.08.2026: до этой правки скрипт умел только "первый проход" — брал аккаунты,
которые НИ РАЗУ не проверялись, и молчал (0 целей), как только этот список
кончался. Реального механизма периодической ПЕРЕПРОВЕРКИ по расписанию не было —
due_today.py существовал, но этот скрипт его не читал. Теперь порция — это
сначала "новые" (как раньше, приоритет), а когда они кончаются — добор из
due_today.iter_due() (те, кому по важности/активности пора освежить FB).
Заодно: важность 1 в выборку не попадает вовсе, важность 2 — не чаще раза
в месяц (см. due_today.py).

Запуск: python3 export_fb_batch.py [social.db] [размер порции] [файл.html]
"""
import json, sqlite3, sys

DB = sys.argv[1] if len(sys.argv) > 1 else "social.db"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 150
OUT = sys.argv[3] if len(sys.argv) > 3 else "fb_batch.html"

sys.path.insert(0, ".")
import due_today as D

NOT_PROFILE_PHP = "a.url NOT LIKE '%profile.php%'"

# "новые" — аккаунты, которые ещё ни разу не проверялись (первый проход).
# Условие важности здесь то же самое, что в due_today (>=2 — важность 1 не
# мониторится вовсе), иначе бэклог тянул бы и неважных людей.
BACKLOG_SQL = f"""
SELECT a.id, a.url, p.display_name
FROM accounts a
JOIN people p ON p.id = a.person_id
WHERE a.network='facebook' AND a.url IS NOT NULL AND COALESCE(a.is_dead,0)=0
  AND {NOT_PROFILE_PHP}
  AND COALESCE(p.importance,0) >= 2
  AND NOT EXISTS (SELECT 1 FROM observations o
                  WHERE o.account_id = a.id AND o.source='fb_page')
ORDER BY COALESCE(p.importance, -1) DESC,
         (p.in_contacts='yes') DESC,
         (SELECT COUNT(DISTINCT network) FROM accounts a2 WHERE a2.person_id=p.id) DESC,
         p.display_name
LIMIT ?
"""

BACKLOG_COUNT_SQL = f"""
SELECT COUNT(*) FROM accounts a WHERE a.network='facebook'
  AND a.url IS NOT NULL AND COALESCE(a.is_dead,0)=0
  AND {NOT_PROFILE_PHP}
  AND COALESCE((SELECT p.importance FROM people p WHERE p.id=a.person_id),0) >= 2
  AND NOT EXISTS (SELECT 1 FROM observations o
                  WHERE o.account_id=a.id AND o.source='fb_page')
"""

SNIPPET = r"""
(async () => {
  const TARGETS = __TARGETS__;
  const out = [], t0 = Date.now();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const stamp = () => new Date().toLocaleTimeString();
  let stop = false;
  window.__fbSweepStop = () => { stop = true; console.log('останавливаюсь после текущего…'); };

  function loadIframe(url, timeoutMs) {
    return new Promise((resolve) => {
      const f = document.createElement('iframe');
      f.style.cssText = 'position:fixed;left:-9999px;top:-9999px;width:800px;height:600px;';
      let done = false;
      const finish = (r) => { if (!done) { done = true; f.remove(); resolve(r); } };
      f.onload = () => {
        try {
          const doc = f.contentDocument;
          finish({ ok: true, finalUrl: doc.location ? doc.location.href : '', html: doc.documentElement.outerHTML });
        } catch (e) { finish({ ok: false, error: 'access: ' + String(e).slice(0,200) }); }
      };
      f.onerror = () => finish({ ok: false, error: 'onerror' });
      setTimeout(() => finish({ ok: false, error: 'timeout' }), timeoutMs || 15000);
      document.body.appendChild(f);
      f.src = url;
    });
  }

  console.log('%cСбор начат: ' + TARGETS.length + ' профилей, ~' +
    Math.round(TARGETS.length * 7 / 60) + ' мин. Прервать: __fbSweepStop()',
    'color:#2563eb;font-weight:bold');

  for (let i = 0; i < TARGETS.length && !stop; i++) {
    const t = TARGETS[i];
    let rec = { account_id: t.id, url: t.url, ok: false, last_post_ts: null, error: null };
    try {
      const res = await loadIframe(t.url, 15000);
      if (!res.ok) {
        rec.error = res.error || 'load_failed';
      } else if (/\/(checkpoint|login)\//.test(res.finalUrl)) {
        console.warn('Facebook просит подтверждение входа — прерываю сбор');
        rec.error = 'checkpoint';
        out.push(rec);
        break;
      } else {
        const html = res.html;
        const now = Math.floor(Date.now() / 1000);
        const times = [];
        for (const m of html.matchAll(/"(?:creation_time|publish_time)"\s*:\s*(\d{9,10})/g)) {
          const v = +m[1];
          if (v > 1000000000 && v <= now) times.push(v);
        }
        rec.ok = true;
        rec.last_post_ts = times.length ? Math.max(...times) : null;
        rec.signals = times.length;
      }
    } catch (e) {
      rec.error = String(e).slice(0, 120);
    }
    out.push(rec);
    if ((i + 1) % 10 === 0 || i === TARGETS.length - 1) {
      const done = out.filter(x => x.last_post_ts).length;
      console.log(`[${stamp()}] ${i + 1}/${TARGETS.length}, дат найдено ${done}`);
    }
    await sleep(4200 + Math.random() * 2600);
  }

  const blob = new Blob([JSON.stringify({
    collected_at: new Date().toISOString(),
    seconds: Math.round((Date.now() - t0) / 1000),
    people: out
  }, null, 1)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'fb_activity_' + new Date().toISOString().slice(0, 10) + '.json';
  a.click();
  console.log('%cГотово: ' + out.length + ' профилей, файл сохранён', 'color:#15803d;font-weight:bold');
})();
"""

PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Facebook — порция %(n)d профилей</title>
<style>
 :root{color-scheme:light dark}
 body{font:15px/1.6 -apple-system,"Segoe UI",Roboto,sans-serif;max-width:780px;margin:40px auto;
      padding:0 20px;color:#1a1a1a;background:#fff}
 @media (prefers-color-scheme:dark){body{color:#e8e8e8;background:#16181c}
   pre,.note{background:#22252b!important;border-color:#3a3f47!important}}
 h1{font-size:20px;margin:0 0 4px}.sub{color:#6b7280;margin:0 0 22px}
 ol{padding-left:22px}li{margin:10px 0}
 pre{background:#f6f7f9;border:1px solid #e5e7eb;border-radius:8px;padding:14px;
     font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;overflow:auto;max-height:260px}
 button{padding:10px 20px;font-size:15px;font-weight:600;background:#2563eb;color:#fff;
        border:0;border-radius:8px;cursor:pointer}
 .note{background:#fffbe6;border:1px solid #f5e6a8;border-radius:8px;padding:12px 16px;
       font-size:14px;margin:22px 0}
 code{font:13px ui-monospace,Menlo,Consolas,monospace}
</style></head><body>
<h1>Facebook — сбор активности, порция %(n)d профилей</h1>
<p class="sub">Осталось необойдённых (без profile.php?id=): %(left)d. Займёт примерно %(mins)d минут — можно свернуть вкладку и заниматься своим,
но не закрывать её.</p>
<ol>
  <li>Открой любую страницу <b>facebook.com</b> (подойдёт лента).</li>
  <li><b>F12</b> → <b>Console</b>. Если ругается на вставку — напечатай <code>allow pasting</code>, Enter.</li>
  <li>Кнопка ниже, вставить в консоль, Enter.</li>
  <li>По окончании браузер сохранит <code>fb_activity_ГГГГ-ММ-ДД.json</code> — положи его в папку проекта.</li>
</ol>
<button id="copy">Скопировать скрипт</button>
<div class="note">
  Скрипт открывает страницы профилей по одной в скрытом iframe с паузой около пяти секунд —
  это темп человека, листающего ленту, а не робота. Ничего не публикует и не пишет. Если
  Facebook попросит подтвердить вход, сбор сам остановится и сохранит то, что успел.
  Прервать вручную: <code>__fbSweepStop()</code> в консоли.
</div>
<pre id="src"></pre>
<script>
const SNIPPET = %(snippet)s;
document.getElementById("src").textContent = SNIPPET;
document.getElementById("copy").onclick = async () => {
  await navigator.clipboard.writeText(SNIPPET);
  const b = document.getElementById("copy");
  b.textContent = "Скопировано \\u2713";
  setTimeout(() => b.textContent = "Скопировать скрипт", 2500);
};
</script></body></html>
"""


def main():
    con = sqlite3.connect(DB)

    # 1) сначала бэклог — новые аккаунты, ещё ни разу не проверенные
    backlog_rows = con.execute(BACKLOG_SQL, (N,)).fetchall()
    backlog_ids = {r[0] for r in backlog_rows}
    remaining = max(0, N - len(backlog_rows))

    # 2) остаток порции — перепроверка по due_today (кому пора освежить FB)
    recheck_rows = []
    if remaining:
        due = [r for r in D.iter_due(con, network="facebook")
               if "profile.php" not in (r["url"] or "") and r["account_id"] not in backlog_ids]
        due.sort(key=lambda r: (-r["importance"], -(r["waited_days"] or 0), r["name"]))
        for r in due[:remaining]:
            recheck_rows.append((r["account_id"], r["url"], r["name"]))

    rows = list(backlog_rows) + recheck_rows
    left_backlog = con.execute(BACKLOG_COUNT_SQL).fetchone()[0]
    left_recheck = sum(1 for r in D.iter_due(con, network="facebook")
                        if "profile.php" not in (r["url"] or ""))
    con.close()

    targets = [{"id": r[0], "url": r[1]} for r in rows]
    snippet = SNIPPET.replace("__TARGETS__", json.dumps(targets, ensure_ascii=False)).strip()
    html = PAGE % dict(n=len(targets), left=left_backlog + left_recheck,
                       mins=max(1, round(len(targets) * 7 / 60)),
                       snippet=json.dumps(snippet))
    open(OUT, "w", encoding="utf-8").write(html)
    print(f"{OUT}: {len(targets)} профилей в порции "
          f"({len(backlog_rows)} новых, {len(recheck_rows)} на перепроверку)")
    print(f"осталось: новых непройденных {left_backlog}, на перепроверку по графику {left_recheck}")
    print("первые:", ", ".join(r[2] for r in rows[:5]))


if __name__ == "__main__":
    main()
