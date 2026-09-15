#!/usr/bin/env python3
"""
Готовит страницу ежедневного сбора по VK: берёт из базы тех, кто сегодня к проверке,
и печёт tools/vk_daily.html с их id внутри. владелец вставляет токен, жмёт кнопку —
страница забирает свежие посты (до 10 на человека, не старше указанной даты) и
сохраняет vk_daily_ГГГГ-ММ-ДД.json.

Запуск: python3 export_vk_daily.py [social.db] [tools/vk_daily.html] [--days 7]
"""
import json, sqlite3, sys, time
from datetime import date, timedelta
from _cli import positionals, require_db   # см. _cli.py

args = positionals(sys.argv[1:], ("--days",))
DB = require_db(args[0] if args else "social.db")
OUT = args[1] if len(args) > 1 else "vk_daily.html"
DAYS = 7
for i, a in enumerate(sys.argv):
    if a == "--days" and i + 1 < len(sys.argv):
        DAYS = int(sys.argv[i + 1])

sys.path.insert(0, ".")
import due_today as D

SNIPPET = r"""
(async () => {
  const T = __TARGETS__, SINCE = __SINCE__, V = "5.199";
  const token = (prompt("Вставь токен VK (vk1.a...)") || "").trim();
  if (!token) { console.warn("без токена нечего делать"); return; }
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let n = 0;
  const jsonp = (method, params) => new Promise((res, rej) => {
    const cb = "__cb" + (n++) + "_" + T.length;
    const s = document.createElement("script");
    const fin = (f, a) => { delete window[cb]; s.remove(); f(a); };
    window[cb] = d => fin(res, d);
    s.onerror = () => fin(rej, new Error("сеть"));
    s.src = "https://api.vk.com/method/" + method + "?" +
            new URLSearchParams({ ...params, v: V, callback: cb });
    document.body.appendChild(s);
  });

  const out = [];
  for (let i = 0; i < T.length; i += 25) {
    const chunk = T.slice(i, i + 25);
    const code = "var ids=" + JSON.stringify(chunk.map(t => t.owner_id)) +
      ";var r=[];var i=0;while(i<ids.length){r.push(API.wall.get({owner_id:ids[i]," +
      "count:10,filter:\"owner\"}));i=i+1;}return r;";
    let res = null;
    try { const r = await jsonp("execute", { access_token: token, code });
          if (r.error) throw new Error(r.error.error_msg); res = r.response; }
    catch (e) { console.warn("пачка не прошла: " + e.message); }
    chunk.forEach((t, k) => {
      const w = res && res[k];
      const posts = ((w && w.items) || [])
        .filter(p => p.date >= SINCE)
        .map(p => ({
          date: new Date(p.date * 1000).toISOString().slice(0, 10),
          url: "https://vk.com/wall" + p.owner_id + "_" + p.id,
          text: (p.text || "").slice(0, 1500),
          repost: !!(p.copy_history && p.copy_history.length),
          repost_text: p.copy_history && p.copy_history[0]
                       ? (p.copy_history[0].text || "").slice(0, 600) : null,
          likes: p.likes ? p.likes.count : null,
          comments: p.comments ? p.comments.count : null
        }));
      out.push({ account_id: t.account_id, person_id: t.person_id, name: t.name,
                 url: t.url, readable: !!w, posts });
    });
    console.log(`${Math.min(i + 25, T.length)}/${T.length}`);
    await sleep(350);
  }

  const withPosts = out.filter(p => p.posts.length).length;
  const blob = new Blob([JSON.stringify({ collected_at: new Date().toISOString(),
    since_ts: SINCE, people: out }, null, 1)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "vk_daily_" + new Date().toISOString().slice(0, 10) + ".json";
  a.click();
  console.log("%cГотово: " + out.length + " человек, свежее есть у " + withPosts,
              "color:#15803d;font-weight:bold");
})();
"""

PAGE = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>VK — сбор за день (%(n)d чел.)</title><style>
 :root{color-scheme:light dark}
 body{font:15px/1.6 -apple-system,"Segoe UI",Roboto,sans-serif;max-width:760px;margin:40px auto;
      padding:0 20px;color:#1a1a1a;background:#fff}
 @media (prefers-color-scheme:dark){body{color:#e8e8e8;background:#16181c}
   pre{background:#22252b!important;border-color:#3a3f47!important}}
 h1{font-size:20px;margin:0 0 4px}.sub{color:#6b7280;margin:0 0 20px}
 ol{padding-left:22px}li{margin:8px 0}
 pre{background:#f6f7f9;border:1px solid #e5e7eb;border-radius:8px;padding:14px;
     font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;overflow:auto;max-height:240px}
 button{padding:10px 20px;font-size:15px;font-weight:600;background:#2563eb;color:#fff;
        border:0;border-radius:8px;cursor:pointer}
 code{font:13px ui-monospace,Menlo,Consolas,monospace}
</style></head><body>
<h1>VK — сбор за день</h1>
<p class="sub">Сегодня к проверке %(n)d человек. Посты не старше %(days)d дней. Минута работы.</p>
<ol>
 <li>Свежий токен (сутки живёт):
   <a href="https://oauth.vk.com/authorize?client_id=54702780&display=page&redirect_uri=https://oauth.vk.com/blank.html&scope=friends,wall&response_type=token&v=5.199">получить</a>
   — скопировать из адресной строки после <code>#access_token=</code> до <code>&amp;</code>.
   <br><span class="sub">Права: <code>friends,wall</code> — читать друзей и стены.
   Токен выпускается от имени чужого приложения (<code>client_id</code> в ссылке —
   публичный id для implicit-flow, не автора этого кода). Не устраивает — завести
   своё standalone-приложение VK и подставить его <code>client_id</code> здесь же,
   в <code>PAGE</code> внутри <code>export_vk_daily.py</code>.</span></li>
 <li>Открыть консоль (<b>F12</b> → Console), при необходимости напечатать <code>allow pasting</code>.</li>
 <li>Кнопка ниже → вставить в консоль → Enter → вставить токен в появившееся окно.</li>
 <li>Готовый <code>vk_daily_ГГГГ-ММ-ДД.json</code> положить в папку проекта.</li>
</ol>
<button id="copy">Скопировать скрипт</button>
<pre id="src"></pre>
<script>
const S = %(snippet)s;
document.getElementById("src").textContent = S;
document.getElementById("copy").onclick = async () => {
  await navigator.clipboard.writeText(S);
  const b = document.getElementById("copy");
  b.textContent = "Скопировано \\u2713";
  setTimeout(() => b.textContent = "Скопировать скрипт", 2500);
};
</script></body></html>
"""


def main():
    today = date.today()
    # NB: не strftime("%s") — это POSIX-расширение, на Windows оно падает
    # с ValueError: Invalid format string. time.mktime переносим.
    since = int(time.mktime((today - timedelta(days=DAYS)).timetuple()))

    con = sqlite3.connect(DB)
    cur = con.cursor()
    # ключ — account_id, а НЕ person_id: у человека законно бывает два VK-аккаунта
    # (старый заброшенный профиль и новый, оба заведены переписью). Пока ключом был
    # person_id, в словаре оставался только последний из них — и обе due-записи
    # человека уезжали в страницу с id и url ОДНОГО и того же аккаунта: второй не
    # собирался никогда, его last_checked не двигался, и он висел в due на каждом
    # прогоне, а первый получал по два одинаковых наблюдения за день.
    accounts = {}
    for aid, url, nid in cur.execute("""SELECT id, url, network_id FROM accounts
                                        WHERE network='vk' AND url IS NOT NULL
                                          AND COALESCE(is_dead,0)=0"""):
        accounts[aid] = (url, nid)

    # due_today.iter_due() — общая логика "кому пора" (важность×активность,
    # per-network last_checked, важность 1 исключена, важность 2 раз в месяц).
    # Раньше здесь была своя копия этой логики поверх D.SQL — расползалась и
    # могла разъехаться с due_today.py; теперь дергаем сразу его функцию.
    targets = []
    for r in D.iter_due(con, network="vk"):
        aid = r["account_id"]
        if aid not in accounts:
            continue
        url, nid = accounts[aid]
        owner = nid or url.rstrip("/").split("/")[-1].replace("id", "")
        if not str(owner).isdigit():
            continue                      # без числового id execute не сработает
        targets.append(dict(account_id=aid, person_id=r["person_id"], name=r["name"],
                            url=url, owner_id=int(owner)))
    con.close()

    snippet = (SNIPPET.replace("__TARGETS__", json.dumps(targets, ensure_ascii=False))
                      .replace("__SINCE__", str(since)).strip())
    open(OUT, "w", encoding="utf-8").write(
        PAGE % dict(n=len(targets), days=DAYS, snippet=json.dumps(snippet)))
    print(f"{OUT}: {len(targets)} человек к сбору по VK")
    for t in targets[:8]:
        print("   ", t["name"])


if __name__ == "__main__":
    main()
