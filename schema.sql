-- social-digest-kit: схема рабочей базы (snapshot 13.09.2026)
-- ЕДИНСТВЕННЫЙ источник схемы: build_db.py на пустой базе исполняет этот файл,
-- своей копии CREATE TABLE у него больше нет. Поля, которых нет в самой первой
-- версии схемы (01.08.2026), прирастали ALTER TABLE по ходу работы и дописаны
-- здесь в конец соответствующих CREATE TABLE — отсюда их слитная запись:
--   people      + ig_review_status, bio_summary, bio_updated_at, is_deceased,
--                 telegram_url
--   accounts    + network_id, in_friends, bio_link, bio_header_name,
--                 bio_captured_at, dormant_channel
--   observations+ access_blocked, authored_by_owner
--   digest_items+ relevance, relevance_note, reviewed_at, post_url, post_date
-- Таблицы contexts/person_context заводит labeler/main.py при первом запуске;
-- здесь они есть, чтобы схема снималась и восстанавливалась целиком.
-- empty_streak полем НЕ является: это вычисляемая величина, см.
-- compute_empty_streaks() в scripts/due_today.py.

CREATE TABLE accounts (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES people(id),
    network    TEXT NOT NULL,               -- vk/facebook/instagram
    url        TEXT,                        -- NULL для FB без ссылки
    handle     TEXT,
    name_raw   TEXT NOT NULL,               -- как записано в переписи
    source     TEXT,                        -- census/people_yml
    is_dead    INTEGER DEFAULT 0,
    created_at TEXT NOT NULL, network_id TEXT, in_friends INTEGER, bio_link TEXT, bio_header_name TEXT, bio_captured_at TEXT, dormant_channel INTEGER DEFAULT 0,
    UNIQUE(network, url)
);

CREATE TABLE contexts (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT, created_at TEXT NOT NULL
    );

CREATE TABLE digest_items (
    id          INTEGER PRIMARY KEY,
    digest_date TEXT NOT NULL,
    person_id   INTEGER NOT NULL REFERENCES people(id),
    networks    TEXT,
    category    TEXT,                       -- povod/tema/po_lyudyam/ne_voshlo
    summary     TEXT
, relevance TEXT, relevance_note TEXT, reviewed_at TEXT, post_url TEXT, post_date TEXT);

CREATE TABLE observations (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    checked_at TEXT NOT NULL,
    found_post INTEGER NOT NULL,            -- 0/1
    post_date  TEXT,
    post_url   TEXT,
    summary    TEXT,
    source     TEXT                         -- api/browser/import
, access_blocked INTEGER DEFAULT 0, authored_by_owner INTEGER);

CREATE TABLE people (
    id           INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    importance   INTEGER,                   -- 0-5, ставит владелец; NULL = не размечен
    circle       TEXT,                      -- свободный текст; реальный словарь — CIRCLES в labeler/main.py
    in_contacts  TEXT,                      -- yes/no/maybe
    note         TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
, ig_review_status TEXT, bio_summary TEXT, bio_updated_at TEXT, is_deceased INTEGER DEFAULT 0, telegram_url TEXT);

CREATE TABLE person_context (
        id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES people(id),
        context_id INTEGER NOT NULL REFERENCES contexts(id), role TEXT, note TEXT,
        created_at TEXT NOT NULL, UNIQUE(person_id, context_id)
    );

CREATE INDEX idx_acc_person ON accounts(person_id);

CREATE INDEX idx_dig_person ON digest_items(person_id);

CREATE INDEX idx_obs_acc    ON observations(account_id, checked_at);

