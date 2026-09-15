#!/usr/bin/env python3
"""
Второй заход разбора 15.09.2026 — находки, которые в первом заходе были только
названы («что НЕ трогал» в PR), и починены после прямого решения владельца.

Те же правила, что в test_regressions.py: база из schema.sql во временной
папке, скрипты — подпроцессами, у каждой проверки есть отрицательный контроль.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import subprocess
import sys
import types
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_regressions import Base, ROOT, SCRIPTS, run, TODAY   # noqa: E402


class FacebookAuthorship(Base):
    """Пачечный обход FB снимает максимальный таймстемп со страницы регуляркой и
    не знает, чей это пост: чужое поздравление на стене выглядело как «человек
    сам написал» и освежало слой активности канала."""

    def batch(self, aid):
        return self.write_json("fb.json", {"collected_at": TODAY, "seconds": 1, "people": [
            {"account_id": aid, "url": "https://facebook.com/test-fb-1", "ok": True,
             "last_post_ts": 1789300000, "error": None}]})

    def setUp(self):
        super().setUp()
        self.pid = self.person("Тест Фейсбучный", importance=5)
        self.aid = self.account(self.pid, "facebook", "https://facebook.com/test-fb-1")

    def test_batch_find_is_not_own_activity(self):
        code, out = run("import_fb_activity.py", self.batch(self.aid), self.db)
        self.assertEqual(code, 0, out)
        with self.con() as con:
            found, owner, summary = con.execute(
                "SELECT found_post, authored_by_owner, summary FROM observations").fetchone()
        self.assertEqual((found, owner), (1, 0))
        self.assertIn("автор", summary)          # провенанс виден там, где его читают глазами
        # и расписание такую находку за собственный пост не принимает
        _, due = run("channel_health.py", self.db)
        self.assertIn("посл. пост ни разу", due)

    def test_assume_owner_keeps_old_behaviour(self):
        """Отрицательный контроль: прежнее поведение никуда не делось, оно под
        флагом — и тогда находка снова считается собственным постом."""
        code, out = run("import_fb_activity.py", self.batch(self.aid), self.db, "--assume-owner")
        self.assertEqual(code, 0, out)
        with self.con() as con:
            owner, = con.execute("SELECT authored_by_owner FROM observations").fetchone()
        self.assertIsNone(owner)
        _, health = run("channel_health.py", self.db)
        self.assertNotIn("посл. пост ни разу", health)


class BuildDbIdempotency(Base):
    """Идемпотентность держалась на URL: строка переписи без ссылки заводила
    нового человека на каждом прогоне (для FB это уже было учтено, для VK и IG —
    нет)."""

    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.tmp.name, "src")
        os.makedirs(os.path.join(self.src, "raw", "census"))
        census = os.path.join(self.src, "raw", "census")
        with open(os.path.join(census, "instagram_census.csv"), "w", encoding="utf-8") as f:
            f.write("name,instagram_url\nБез Ссылки,\nС Ссылкой,https://instagram.com/x\n")
        with open(os.path.join(census, "vk_census.csv"), "w", encoding="utf-8") as f:
            f.write("name,vk_url\nВК Безссылочный,\n")

    def people_count(self):
        with self.con() as con:
            return con.execute("SELECT COUNT(*) FROM people").fetchone()[0]

    def test_second_build_does_not_duplicate_url_less_rows(self):
        self.assertEqual(run("build_db.py", self.src, self.db)[0], 0)
        first = self.people_count()
        self.assertEqual(run("build_db.py", self.src, self.db)[0], 0)
        self.assertEqual(self.people_count(), first)
        self.assertEqual(first, 3)

    def test_new_row_still_added(self):
        """Отрицательный контроль: идемпотентность не должна превратиться в
        «второй прогон вообще ничего не заводит»."""
        run("build_db.py", self.src, self.db)
        first = self.people_count()
        with open(os.path.join(self.src, "raw", "census", "instagram_census.csv"),
                  "a", encoding="utf-8") as f:
            f.write("Новый Человек,\n")
        run("build_db.py", self.src, self.db)
        self.assertEqual(self.people_count(), first + 1)


class FbLinksOwner(Base):
    """Свой профиль был зашит в код константой: у постороннего, кто соберёт
    это у себя, собственная страница приезжала в базу как друг."""

    def harvest(self):
        return self.write_json("fb_friends.json", {"people": [
            {"name": "Свой Профиль", "facebook_url": "https://www.facebook.com/myslug"},
            {"name": "Чужой Паблик", "facebook_url": "https://www.facebook.com/somepage"},
            {"name": "Реальный Друг", "facebook_url": "https://www.facebook.com/friend"}]})

    def test_owner_and_pages_are_excluded_by_flag(self):
        code, out = run("import_fb_links.py", self.harvest(), self.db, "--apply",
                        "--owner", "myslug", "--page", "somepage")
        self.assertEqual(code, 0, out)
        with self.con() as con:
            names = {r[0] for r in con.execute("SELECT display_name FROM people")}
        self.assertEqual(names, {"Реальный Друг"})

    def test_without_owner_flag_it_says_so(self):
        """Отрицательный контроль: без флага поведение прежнее, но молча этого
        не происходит — скрипт предупреждает."""
        code, out = run("import_fb_links.py", self.harvest(), self.db, "--apply")
        self.assertIn("--owner не задан", out)
        with self.con() as con:
            names = {r[0] for r in con.execute("SELECT display_name FROM people")}
        self.assertIn("Свой Профиль", names)


class SweepSummary(Base):
    """«Активность VK по свежей выгрузке» считалась запросом по всем
    наблюдениям source='api' за всю историю базы — туда же пишет import_daily.py."""

    def test_summary_counts_only_this_sweep(self):
        pid = self.person("Из выгрузки")
        aid = self.account(pid, "vk", "https://vk.com/id42")
        old = self.person("Посторонний")
        oaid = self.account(old, "vk", "https://vk.com/id43")
        # чужая свежая запись тем же источником (так пишет ежедневный
        # import_daily.py) — в сводку ЭТОЙ выгрузки попадать не должна
        self.observation(oaid, TODAY, 1, post_date=TODAY, source="api")
        src = self.write_json("sweep.json", {"generated_at": TODAY, "people": [
            {"id": 42, "url": "https://vk.com/id42", "name": "Из выгрузки", "readable": True,
             "last_post_date": TODAY, "last_post_url": "u", "last_post_text": "текст",
             "is_repost": False}]})
        code, out = run("import_vk_sweep.py", src, self.db)
        self.assertEqual(code, 0, out)
        tail = out.split("Активность VK по свежей выгрузке:")[1]
        self.assertIn("писали за 30 дней      1", tail)

    def test_silent_and_unreadable_are_separated(self):
        """Отрицательный контроль: пустая стена и недоступная стена — разные
        строки сводки, иначе сломанный сбор читается как «никто не пишет»."""
        pid = self.person("Молчун")
        self.account(pid, "vk", "https://vk.com/id44")
        pid2 = self.person("Закрытый")
        self.account(pid2, "vk", "https://vk.com/id45")
        src = self.write_json("sweep.json", {"generated_at": TODAY, "people": [
            {"id": 44, "url": "https://vk.com/id44", "name": "Молчун", "readable": True,
             "last_post_date": None},
            {"id": 45, "url": "https://vk.com/id45", "name": "Закрытый", "readable": False,
             "last_post_date": None}]})
        out = run("import_vk_sweep.py", src, self.db)[1]
        tail = out.split("Активность VK по свежей выгрузке:")[1]
        self.assertIn("ни одного поста        1", tail)
        self.assertIn("стена недоступна       1", tail)


class BatchLeftovers(Base):
    """«Осталось обойти» считало и те аккаунты, которые только что уехали в эту
    же порцию."""

    def setUp(self):
        super().setUp()
        for i in range(3):
            pid = self.person(f"Человек {i}", importance=4)
            self.account(pid, "facebook", f"https://facebook.com/p{i}")
            self.account(pid, "instagram", f"https://instagram.com/p{i}")

    def test_fb_leftovers_exclude_current_batch(self):
        out = run("export_fb_batch.py", self.db, 2, os.path.join(self.tmp.name, "b.html"))[1]
        self.assertIn("2 профилей в порции", out)
        self.assertIn("новых непройденных 1", out)

    def test_ig_leftovers_exclude_current_batch(self):
        out = run("export_ig_batch.py", self.db, 2, os.path.join(self.tmp.name, "b.json"))[1]
        self.assertIn("2 профилей в порции", out)
        self.assertIn("без поста 1", out)


class DedupeAccounts(Base):
    """После переноса наблюдений на оставшийся аккаунт две проверки одного дня
    ложились на один канал — гейт полноты считал находку дважды."""

    def setUp(self):
        super().setUp()
        self.pid = self.person("Дубль")
        self.keep = self.account(self.pid, "vk", "https://vk.com/id7", network_id="7")
        self.drop = self.account(self.pid, "vk", "https://vk.com/domain")
        with self.con() as con:
            con.execute("UPDATE accounts SET in_friends=1 WHERE id=?", (self.keep,))

    def test_same_day_observations_are_collapsed(self):
        self.observation(self.keep, TODAY, 1, post_date=TODAY, post_url="https://vk.com/wall7_1")
        self.observation(self.drop, TODAY, 0)
        dry = run("dedupe_accounts.py", self.db)[1]
        self.assertIn("двойников за один день схлопнуто: 1", dry)   # пробный прогон не врёт
        with self.con() as con:                                     # и ничего не трогает
            self.assertEqual(con.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2)
        code, out = run("dedupe_accounts.py", self.db, "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("двойников за один день схлопнуто: 1", out)
        with self.con() as con:
            rows = con.execute("SELECT found_post, post_url FROM observations").fetchall()
        self.assertEqual(rows, [(1, "https://vk.com/wall7_1")])   # оставлена содержательная

    def test_different_days_are_kept(self):
        """Отрицательный контроль: две проверки в РАЗНЫЕ дни — это две проверки,
        схлопывать их нельзя."""
        self.observation(self.keep, TODAY, 1, post_date=TODAY, post_url="u1")
        self.observation(self.drop, "2026-09-01", 1, post_date="2026-09-01", post_url="u2")
        out = run("dedupe_accounts.py", self.db, "--apply")[1]
        self.assertNotIn("схлопнуто", out)
        with self.con() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2)


class TelegramScan(Base):
    def setUp(self):
        super().setUp()
        self.pid = self.person("Каналовладелец")

    def bio(self, text):
        with self.con() as con:
            con.execute("UPDATE people SET bio_summary=? WHERE id=?", (text, self.pid))

    def test_foreign_domain_is_not_a_telegram_link(self):
        self.bio("пишет в блоге на bot.me/somechannel, телеграма нет")
        out = run("telegram_scan.py", self.db)[1]
        self.assertIn("кандидатов: 0", out)

    def test_real_link_is_still_found(self):
        """Отрицательный контроль: настоящая ссылка по-прежнему находится."""
        self.bio("мой канал https://t.me/somechannel")
        out = run("telegram_scan.py", self.db)[1]
        self.assertIn("кандидатов: 1", out)
        self.assertIn("t.me/somechannel", out)


class LegacyRepair(Base):
    """Правки в коде чинят то, что пишется дальше; в накопленном журнале строки
    остаются кривыми — для них отдельный разовый скрипт."""

    def setUp(self):
        super().setUp()
        pid = self.person("С историей")
        self.aid = self.account(pid, "facebook", "https://facebook.com/test-fb-1")
        self.observation(self.aid, "2026-09-01", 1, post_date="2026-08-28",
                         post_url="https://facebook.com/test-fb-1", source="fb_page", owner=None)
        self.observation(self.aid, "2026-09-05", 1, post_date="2026-09-04",
                         post_url="https://facebook.com/test-fb-1/posts/123", source="browser")

    def urls(self):
        with self.con() as con:
            return [r[0] for r in con.execute(
                "SELECT post_url FROM observations ORDER BY checked_at")]

    def test_profile_urls_are_cleared_and_real_ones_kept(self):
        code, out = run("fix_legacy_observations.py", self.db, "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.urls(), [None, "https://facebook.com/test-fb-1/posts/123"])

    def test_dry_run_changes_nothing(self):
        """Отрицательный контроль: без --apply скрипт только считает."""
        before = self.urls()
        out = run("fix_legacy_observations.py", self.db)[1]
        self.assertIn("пробный прогон", out)
        self.assertEqual(self.urls(), before)

    def test_fb_authorship_is_opt_in(self):
        run("fix_legacy_observations.py", self.db, "--apply")
        with self.con() as con:
            self.assertIsNone(con.execute(
                "SELECT authored_by_owner FROM observations WHERE source='fb_page'").fetchone()[0])
        run("fix_legacy_observations.py", self.db, "--apply", "--fb-authorship")
        with self.con() as con:
            self.assertEqual(con.execute(
                "SELECT authored_by_owner FROM observations WHERE source='fb_page'").fetchone()[0], 0)


class LabelerLayer(unittest.TestCase):
    """Бейдж очереди разметки и слой расписания держали каждый свою копию
    порогов (30/90/365 против 30/180/365), связанные только комментарием.
    Flask на машине может быть не установлен — подставляем заглушку, чтобы
    проверить сам модуль, а не наличие зависимости."""

    def setUp(self):
        if "flask" not in sys.modules:
            flask = types.ModuleType("flask")

            class App:
                def __init__(self, *a, **kw):
                    self.debug = False

                def route(self, *a, **kw):
                    return lambda fn: fn

                def run(self, *a, **kw):
                    raise AssertionError("сервер в тестах не поднимаем")

            flask.Flask = App
            for name in ("request", "redirect", "url_for", "render_template_string", "jsonify"):
                setattr(flask, name, lambda *a, **kw: None)
            sys.modules["flask"] = flask
            self.addCleanup(sys.modules.pop, "flask", None)
        sys.path.insert(0, str(ROOT / "labeler"))
        self.addCleanup(lambda: sys.path.remove(str(ROOT / "labeler")))
        sys.modules.pop("main", None)
        self.addCleanup(sys.modules.pop, "main", None)

    def test_badge_follows_schedule_thresholds(self):
        import main
        sys.path.insert(0, str(SCRIPTS))
        from due_today import layer
        today = date(2026, 9, 15)
        for d in ("2026-09-10", "2026-08-01", "2026-03-01", "2024-01-01"):
            badge = main.compute_layer(d, 1, today)
            self.assertEqual(badge, main.LAYER_BADGE[layer(d, today)])
        # граница «полгода/год» — та же, что у rare/dormant в расписании
        self.assertEqual(main.compute_layer("2026-03-19", 1, today), "полгода")   # 180 дней
        self.assertEqual(main.compute_layer("2026-03-18", 1, today), "год")       # 181

    def test_no_post_cases_are_preserved(self):
        """Отрицательный контроль: «молчит» и «не проверялся» — по-прежнему
        разные ответы, их слой расписания не различает."""
        import main
        today = date(2026, 9, 15)
        self.assertEqual(main.compute_layer(None, 3, today), "молчит")
        self.assertEqual(main.compute_layer(None, 0, today), "не проверялся")
        self.assertEqual(main.compute_layer("кривая-дата", 0, today), "не проверялся")


if __name__ == "__main__":
    unittest.main()
