#!/usr/bin/env python3
"""
Регрессии на дефекты, найденные при разборе 15.09.2026.

Каждый тест — сценарий, который до правки вёл себя неправильно, и для каждого
рядом стоит отрицательный контроль: проверка, которая ловит нарушение, обязана
молчать, когда нарушения нет. Иначе «тест зелёный» не значит ничего.

Зависимостей нет: stdlib + sqlite3, база каждый раз собирается из schema.sql во
временной папке. Скрипты запускаются как подпроцессы — ровно так, как их
запускает SKILL.md, а не импортом.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SCHEMA = (ROOT / "schema.sql").read_text(encoding="utf-8")
TODAY = date.today().isoformat()


def run(script, *args):
    """Прогон скрипта из scripts/ — возвращает (код возврата, stdout+stderr)."""
    p = subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)],
                       capture_output=True, text=True, cwd=ROOT)
    return p.returncode, p.stdout + p.stderr


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "social.db")
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.commit()
        con.close()
        self.addCleanup(self.tmp.cleanup)

    def con(self):
        return sqlite3.connect(self.db)

    def person(self, name, importance=4, circle="друзья"):
        with self.con() as con:
            cur = con.execute(
                "INSERT INTO people(display_name, importance, circle, created_at, updated_at) "
                "VALUES (?,?,?,date('now'),date('now'))", (name, importance, circle))
            return cur.lastrowid

    def account(self, pid, network, url, network_id=None):
        with self.con() as con:
            cur = con.execute(
                "INSERT INTO accounts(person_id, network, url, name_raw, created_at, network_id) "
                "VALUES (?,?,?,'x',date('now'),?)", (pid, network, url, network_id))
            return cur.lastrowid

    def observation(self, aid, checked_at, found, post_date=None, post_url=None,
                    summary=None, source="api", owner=1):
        with self.con() as con:
            con.execute(
                "INSERT INTO observations(account_id, checked_at, found_post, post_date, post_url,"
                " summary, source, authored_by_owner) VALUES (?,?,?,?,?,?,?,?)",
                (aid, checked_at, found, post_date, post_url, summary, source, owner))

    def write_json(self, name, obj):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        return path


class RepeatDetection(Base):
    """FB/IG-импортёры писали в observations.post_url адрес ПРОФИЛЯ — один и тот
    же у всех наблюдений канала. Обе проверки повторов сравнивают post_url в
    первую очередь, поэтому объявляли повтором любую находку в этих сетях."""

    TS = 1789300000                       # момент «последнего поста» на странице профиля
    TS_DATE = datetime.fromtimestamp(TS, timezone.utc).date().isoformat()

    def fb_batch(self, ts):
        pid = self.person("Тест Фейсбучный")
        aid = self.account(pid, "facebook", "https://facebook.com/test-fb-1")
        return pid, aid, self.write_json("fb.json", {
            "collected_at": TODAY, "seconds": 1,
            "people": [{"account_id": aid, "url": "https://facebook.com/test-fb-1",
                        "ok": True, "last_post_ts": ts, "error": None}]})

    def test_new_post_is_not_a_repeat(self):
        pid, aid, src = self.fb_batch(self.TS)
        # прошлое наблюдение — как его писал старый импортёр: в post_url лежит
        # адрес профиля, а не поста (именно такие строки и лежат в живой базе)
        self.observation(aid, "2026-09-01", 1, post_date="2026-08-28",
                         post_url="https://facebook.com/test-fb-1", source="fb_page")
        self.assertEqual(run("import_fb_activity.py", src, self.db)[0], 0)
        code, out = run("flag_repeat_posts.py", self.db, "--date", TODAY)
        self.assertEqual(code, 0, out)
        self.assertIn("повтор предыдущей проверки того же канала: 0", out)

    def test_unchanged_post_is_still_a_repeat(self):
        """Отрицательный контроль: без него первый тест проходил бы и на
        проверке, которая просто перестала что-либо находить."""
        pid, aid, src = self.fb_batch(self.TS)
        self.observation(aid, "2026-09-01", 1, post_date=self.TS_DATE,
                         post_url="https://facebook.com/test-fb-1", source="fb_page")
        run("import_fb_activity.py", src, self.db)
        code, out = run("flag_repeat_posts.py", self.db, "--date", TODAY)
        self.assertEqual(code, 1, out)
        self.assertIn("повтор предыдущей проверки того же канала: 1", out)


class CompletenessGate(Base):
    """Гейт полноты сверял только person_id: находка во второй сети человека
    пряталась за пересказом из первой."""

    def setUp(self):
        super().setUp()
        self.pid = self.person("Двусеточный")
        for net, url in (("vk", "https://vk.com/id555"), ("instagram", "https://instagram.com/x555")):
            aid = self.account(self.pid, net, url)
            self.observation(aid, TODAY, 1, post_date=TODAY, post_url=url + "/post",
                             summary=f"пост в {net}")

    def test_finding_in_second_network_is_reported(self):
        items = self.write_json("items.json", [
            {"person_id": self.pid, "networks": "vk", "category": "po_lyudyam", "summary": "пересказ"}])
        code, out = run("check_digest_completeness.py", self.db, items, "--date", TODAY)
        self.assertEqual(code, 1, out)
        self.assertIn("instagram", out)

    def test_both_networks_covered_passes(self):
        items = self.write_json("items.json", [
            {"person_id": self.pid, "networks": "vk", "category": "po_lyudyam", "summary": "пересказ"},
            {"person_id": self.pid, "networks": "ig", "category": "ne_voshlo", "summary": "репост"}])
        code, out = run("check_digest_completeness.py", self.db, items, "--date", TODAY)
        self.assertEqual(code, 0, out)


class DailyImport(Base):
    """Фильтр «уже пересказывали» сравнивал находку в том числе с наблюдением,
    записанным этим же скриптом минуту назад: повторный прогон за день выдавал
    пустой material — «сегодня никто ничего не написал»."""

    def daily(self, post_url, post_date):
        pid = self.person("Пишущий", importance=5)
        aid = self.account(pid, "vk", "https://vk.com/id888")
        return aid, self.write_json("vk_daily.json", {"people": [
            {"account_id": aid, "person_id": pid, "name": "Пишущий", "url": "https://vk.com/id888",
             "posts": [{"date": post_date, "url": post_url, "text": "текст", "repost": False}]}]})

    def test_second_run_same_day_keeps_material(self):
        aid, src = self.daily("https://vk.com/wall888_9", TODAY)
        self.observation(aid, "2026-09-10", 1, post_date="2026-09-09",
                         post_url="https://vk.com/wall888_8")
        first = json.loads(run("import_daily.py", src, self.db, "--apply")[1])
        second = json.loads(run("import_daily.py", src, self.db, "--apply")[1])
        self.assertEqual(first["with_new"], 1)
        self.assertEqual(second["with_new"], 1)
        self.assertEqual(second["observations_written"], 0)     # и не задваиваем запись
        with self.con() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM observations "
                                         "WHERE checked_at=?", (TODAY,)).fetchone()[0], 1)

    def test_unchanged_post_still_filtered(self):
        """Отрицательный контроль: пост, не изменившийся с прошлой проверки,
        по-прежнему не считается свежим материалом."""
        aid, src = self.daily("https://vk.com/wall888_8", "2026-09-09")
        self.observation(aid, "2026-09-10", 1, post_date="2026-09-09",
                         post_url="https://vk.com/wall888_8")
        out = json.loads(run("import_daily.py", src, self.db, "--apply")[1])
        self.assertEqual(out["with_new"], 0)


class VkSweep(Base):
    def test_bare_repost_is_not_own_activity(self):
        pid = self.person("Репостер")
        self.account(pid, "vk", "https://vk.com/id777")
        src = self.write_json("sweep.json", {"generated_at": TODAY, "people": [
            {"id": 777, "url": "https://vk.com/id777", "name": "Репостер", "readable": True,
             "last_post_date": "2026-09-14", "last_post_url": "https://vk.com/wall777_1",
             "last_post_text": "", "is_repost": True}]})
        self.assertEqual(run("import_vk_sweep.py", src, self.db)[0], 0)
        with self.con() as con:
            found, owner, summary = con.execute(
                "SELECT found_post, authored_by_owner, summary FROM observations").fetchone()
        self.assertEqual((found, owner), (1, 0))
        self.assertEqual(summary, "[репост]")

    def test_repost_with_own_words_stays_own_activity(self):
        """Отрицательный контроль: репост с добавленным своим текстом — это
        действие владельца канала, ему authored_by_owner=0 ставить нельзя."""
        pid = self.person("Комментатор")
        self.account(pid, "vk", "https://vk.com/id778")
        src = self.write_json("sweep.json", {"generated_at": TODAY, "people": [
            {"id": 778, "url": "https://vk.com/id778", "name": "Комментатор", "readable": True,
             "last_post_date": "2026-09-14", "last_post_url": "https://vk.com/wall778_1",
             "last_post_text": "мои слова поверх репоста", "is_repost": True}]})
        run("import_vk_sweep.py", src, self.db)
        with self.con() as con:
            owner, summary = con.execute(
                "SELECT authored_by_owner, summary FROM observations").fetchone()
        self.assertIsNone(owner)                     # NULL = COALESCE(...,1) = сам человек
        self.assertTrue(summary.startswith("[репост] мои слова"))

    def test_merge_keeps_owner_labeling(self):
        """Слияние дублей (домен + числовой id) не должно терять важность и круг:
        они ставятся руками и восстановить их неоткуда."""
        plain = self.person("Тест Дубль (перепись)", importance=None, circle=None)
        self.account(plain, "vk", "https://vk.com/id42")
        labeled = self.person("Тест Дубль (размеченный)", importance=5, circle="семья")
        self.account(labeled, "vk", "https://vk.com/test_vk_alias")
        src = self.write_json("sweep.json", {"generated_at": TODAY, "people": [
            {"id": 42, "url": "https://vk.com/test_vk_alias", "name": "Тест Дубль (размеченный)", "readable": True,
             "last_post_date": "2026-09-13", "last_post_url": "https://vk.com/wall42_7",
             "last_post_text": "привет", "is_repost": False}]})
        run("import_vk_sweep.py", src, self.db)
        with self.con() as con:
            rows = con.execute("SELECT importance, circle FROM people").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], (5, "семья"))


class VkExport(Base):
    def test_two_vk_accounts_of_one_person_both_exported(self):
        """У человека законно бывает два VK-профиля (старый и новый). Пока
        словарь целей строился по person_id, в страницу попадал только один из
        них — дважды, а второй не собирался никогда."""
        pid = self.person("Двойной профиль", importance=5)
        self.account(pid, "vk", "https://vk.com/id111", network_id="111")
        self.account(pid, "vk", "https://vk.com/id222", network_id="222")
        out_html = os.path.join(self.tmp.name, "vk_daily.html")
        code, out = run("export_vk_daily.py", self.db, out_html)
        self.assertEqual(code, 0, out)
        page = Path(out_html).read_text(encoding="utf-8")
        self.assertIn("111", page)
        self.assertIn("222", page)


class Recalibration(Base):
    """enforce_monotonic() поджимал соседние слои только для печати: в
    schedule_config.json уезжало исходное предложение, и конфиг оставался ровно
    в том виде, который докстринг обещает исключить — dormant проверяется чаще,
    чем rare."""

    CONFIG = SCRIPTS / "schedule_config.json"

    def setUp(self):
        super().setUp()
        # рабочий конфиг лежит рядом со скриптом и его же правит --apply:
        # возвращаем содержимое как было, чем бы тест ни кончился
        original = self.CONFIG.read_text(encoding="utf-8") if self.CONFIG.exists() else None
        self.addCleanup(lambda: self.CONFIG.write_text(original, encoding="utf-8")
                        if original is not None else self.CONFIG.unlink(missing_ok=True))
        cfg = json.loads(original or "{}")
        cfg.setdefault("vk", {}).setdefault("3", {})["dormant"] = 21   # заведомо меньше будущего rare
        self.CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1, sort_keys=True),
                               encoding="utf-8")

        pid = self.person("Калибр", importance=3)
        aid = self.account(pid, "vk", "https://vk.com/id99")
        # слой rare (последний свой пост давно) и два десятка пустых проверок:
        # доля находок 0% → интервал предлагается удвоить, 14 → 28
        self.observation(aid, "2026-05-01", 1, post_date="2026-05-01", post_url="u1", summary="p")
        for i in range(20):
            self.observation(aid, f"2026-08-{i + 1:02d}", 0)

    def test_monotonicity_is_written_not_just_printed(self):
        code, out = run("recalibrate_schedule.py", self.db, "--min-sample", "10", "--apply")
        self.assertEqual(code, 0, out)
        cfg = json.loads(self.CONFIG.read_text(encoding="utf-8"))["vk"]["3"]
        self.assertEqual(cfg["rare"], 28)
        self.assertGreaterEqual(cfg["dormant"], cfg["rare"],
                                "dormant проверяется чаще rare — ровно то, что скрипт обещает не допускать")

    def test_second_run_on_same_data_changes_nothing(self):
        """Отрицательный контроль: пересчёт идемпотентен — без новых наблюдений
        второй прогон не должен растягивать интервалы дальше."""
        run("recalibrate_schedule.py", self.db, "--min-sample", "10", "--apply")
        after_first = self.CONFIG.read_text(encoding="utf-8")
        code, out = run("recalibrate_schedule.py", self.db, "--min-sample", "10", "--apply")
        self.assertIn("менять нечего", out)
        self.assertEqual(self.CONFIG.read_text(encoding="utf-8"), after_first)


class Cli(Base):
    def test_flag_value_is_not_taken_for_db_path(self):
        """`due_today.py --network vk` (без пути к базе) раньше понимал «vk» как
        имя файла базы: sqlite3 молча заводил пустой файл и скрипт отвечал
        «к проверке 0 человек» — уверенно и неверно."""
        ghost = ROOT / "vk"
        self.addCleanup(lambda: ghost.exists() and ghost.unlink())
        code, out = run("due_today.py", "--network", "vk")
        self.assertFalse(ghost.exists(), "создан файл-призрак с именем значения флага")
        self.assertIn("базы нет", out)

    def test_flag_value_after_db_path_still_works(self):
        """Отрицательный контроль: обычный вызов с путём и флагом не сломан."""
        code, out = run("due_today.py", self.db, "--network", "vk")
        self.assertEqual(code, 0, out)
        self.assertIn("(сеть vk)", out)

    def test_missing_db_is_an_error_not_an_empty_one(self):
        code, out = run("due_today.py", os.path.join(self.tmp.name, "нет-такой.db"))
        self.assertEqual(code, 1)
        self.assertIn("базы нет", out)


if __name__ == "__main__":
    unittest.main()
