"""Tests for library filter + sort additions (slopsmith #129/#69/#128/#22).

Each filter axis is exercised independently and combined. Sort cases
cover the new year sort and the rewritten tuning sort (now
musical-distance-based instead of alphabetical).

Tests stub `MetadataDB` directly via `meta_db.put()`, bypassing the
PSARC/sloppak scanner — same approach as test_settings_api.py.
"""

import importlib
import json
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def server_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    sys.modules.pop("server", None)
    mod = importlib.import_module("server")
    yield mod
    conn = getattr(getattr(mod, "meta_db", None), "conn", None)
    if conn is not None:
        conn.close()


@pytest.fixture()
def client(server_mod):
    c = TestClient(server_mod.app)
    try:
        yield c
    finally:
        c.close()


def _put(server_mod, *, filename, title, artist, year="", arrangements=None,
         has_lyrics=False, format="psarc", stem_ids=None, tuning_name="E Standard",
         tuning_sort_key=0, mtime=1.0, size=1):
    server_mod.meta_db.put(filename, mtime, size, {
        "title": title, "artist": artist, "album": f"{artist} - LP",
        "year": year, "duration": 200.0,
        "tuning": tuning_name,
        "arrangements": arrangements or [],
        "has_lyrics": has_lyrics,
        "format": format,
        "stem_count": len(stem_ids) if stem_ids else 0,
        "stem_ids": stem_ids if stem_ids is not None else [],
        "tuning_name": tuning_name,
        "tuning_sort_key": tuning_sort_key,
    })


@pytest.fixture()
def seeded(server_mod):
    """Populate 6 deterministic rows covering the matrix of axes."""
    _put(server_mod, filename="a.psarc", title="A song", artist="A Band",
         year="2010", has_lyrics=True, format="psarc",
         arrangements=[{"index": 0, "name": "Lead", "notes": 100},
                       {"index": 1, "name": "Rhythm", "notes": 80}],
         tuning_name="E Standard", tuning_sort_key=0)
    _put(server_mod, filename="b.psarc", title="B song", artist="B Band",
         year="2005", has_lyrics=False, format="psarc",
         arrangements=[{"index": 0, "name": "Bass", "notes": 60}],
         tuning_name="Drop D", tuning_sort_key=-2)
    _put(server_mod, filename="c.sloppak", title="C song", artist="C Band",
         year="2020", has_lyrics=True, format="sloppak",
         arrangements=[{"index": 0, "name": "Combo", "notes": 200}],
         stem_ids=["drums", "bass", "vocals", "piano", "other"],
         tuning_name="E Standard", tuning_sort_key=0)
    _put(server_mod, filename="d.sloppak", title="D song", artist="D Band",
         year="2018", has_lyrics=False, format="sloppak",
         arrangements=[{"index": 0, "name": "Lead", "notes": 90}],
         stem_ids=["drums", "vocals"],
         tuning_name="Eb Standard", tuning_sort_key=-6)
    # Legacy row: stem_ids deliberately set to NULL via raw SQL to
    # simulate a row that predates the slopsmith#129 migration.
    server_mod.meta_db.conn.execute(
        "INSERT INTO songs (filename, mtime, size, title, artist, album, year, duration, "
        "tuning, arrangements, has_lyrics, format, stem_count, stem_ids, tuning_name, tuning_sort_key) "
        "VALUES (?, 1.0, 1, ?, ?, ?, '', 200.0, ?, ?, 0, 'sloppak', 1, NULL, ?, ?)",
        ("e.sloppak", "E song", "E Band", "E Band - LP", "Drop D",
         json.dumps([{"index": 0, "name": "Lead", "notes": 50}]),
         "Drop D", -2),
    )
    server_mod.meta_db.conn.commit()
    _put(server_mod, filename="f.psarc", title="F song", artist="F Band",
         year="2015", has_lyrics=True, format="psarc",
         arrangements=[{"index": 0, "name": "Lead", "notes": 110},
                       {"index": 1, "name": "Bass", "notes": 70}],
         tuning_name="Eb Standard", tuning_sort_key=-6)


def _get(client, **kw):
    return client.get("/api/library", params=kw).json()


# ── Arrangements axis ───────────────────────────────────────────────────────

def test_arrangement_has_lead(client, seeded):
    data = _get(client, arrangements_has="Lead")
    files = {s["filename"] for s in data["songs"]}
    # Rows with Lead: a, d, e, f. Combo (c) does NOT match strict-name "Lead".
    assert files == {"a.psarc", "d.sloppak", "e.sloppak", "f.psarc"}


def test_arrangement_has_or_within_axis(client, seeded):
    data = _get(client, arrangements_has="Lead,Bass")
    files = {s["filename"] for s in data["songs"]}
    # Lead OR Bass: a, b, d, e, f.
    assert files == {"a.psarc", "b.psarc", "d.sloppak", "e.sloppak", "f.psarc"}


def test_arrangement_lacks_bass(client, seeded):
    data = _get(client, arrangements_lacks="Bass")
    files = {s["filename"] for s in data["songs"]}
    # b.psarc and f.psarc both have Bass, exclude them.
    assert "b.psarc" not in files
    assert "f.psarc" not in files
    assert "a.psarc" in files


# ── Arrangements axis — smart naming mode ───────────────────────────────────

@pytest.fixture()
def seeded_smart(server_mod):
    """Rows that exercise the smart-mode filter branches in _build_where."""
    # Row with explicit smart_name="Alt. Lead" — must match arrangements_has=Lead
    # in smart mode (LIKE 'Alt. Lead%').
    _put(server_mod, filename="alt.psarc", title="Alt", artist="Alt Band",
         arrangements=[{"index": 0, "name": "Lead", "notes": 100,
                        "smart_name": "Alt. Lead"}])
    # Row with explicit smart_name="Bonus Rhythm".
    _put(server_mod, filename="bonus.psarc", title="Bon", artist="Bon Band",
         arrangements=[{"index": 0, "name": "Rhythm", "notes": 50,
                        "smart_name": "Bonus Rhythm"}])
    # Legacy-cached row WITHOUT smart_name key (json_type IS NULL):
    # name="Combo" → must match arrangements_has=Lead via name fallback.
    _put(server_mod, filename="combo-old.psarc", title="ComboOld", artist="X",
         arrangements=[{"index": 0, "name": "Combo", "notes": 80}])
    # Legacy-cached row WITHOUT smart_name where name="Bass 2" (load_song
    # synthesises this for real_bass_22 when manifest data is missing):
    # must match arrangements_has=Bass via the extras fallback.
    _put(server_mod, filename="bass2-old.psarc", title="Bass2Old", artist="Z",
         arrangements=[{"index": 0, "name": "Bass 2", "notes": 70}])
    # Scanned ambiguous row with explicit smart_name=None (json_type='null'):
    # name="Combo" must NOT match Lead in smart mode (suppress name-fallback).
    _put(server_mod, filename="combo-ambig.psarc", title="ComboAmb", artist="Y",
         arrangements=[{"index": 0, "name": "Combo", "notes": 90,
                        "smart_name": None}])


def test_smart_mode_matches_alt_lead(client, seeded_smart):
    data = _get(client, arrangements_has="Lead", naming_mode="smart")
    files = {s["filename"] for s in data["songs"]}
    assert "alt.psarc" in files          # smart_name="Alt. Lead"
    assert "combo-old.psarc" in files    # name-fallback (key absent)
    assert "combo-ambig.psarc" not in files  # explicit null suppresses fallback
    assert "bonus.psarc" not in files    # Bonus Rhythm not Lead


def test_smart_mode_matches_bass_2_via_fallback(client, seeded_smart):
    # Legacy cached row with name="Bass 2" must match arrangements_has=Bass
    # in smart mode via the NULL-smart_name name-fallback extras.
    data = _get(client, arrangements_has="Bass", naming_mode="smart")
    files = {s["filename"] for s in data["songs"]}
    assert "bass2-old.psarc" in files


def test_smart_mode_combo_normalized_to_lead(client, seeded_smart):
    # arrangements_has=Combo in smart mode must behave identically to Lead —
    # the server normalizes the alias before building the SQL.
    lead = _get(client, arrangements_has="Lead", naming_mode="smart")
    combo = _get(client, arrangements_has="Combo", naming_mode="smart")
    assert {s["filename"] for s in lead["songs"]} == {s["filename"] for s in combo["songs"]}


def test_smart_mode_lacks_lead_excludes_alt_lead(client, seeded_smart):
    # arrangements_lacks=Lead must exclude rows whose smart_name is any Lead
    # variant (Lead / Alt. Lead / Bonus Lead) AND ambiguous rows whose
    # smart_name is explicitly null (we don't know if they have Lead).
    data = _get(client, arrangements_lacks="Lead", naming_mode="smart")
    files = {s["filename"] for s in data["songs"]}
    assert "alt.psarc" not in files
    assert "combo-old.psarc" not in files
    assert "combo-ambig.psarc" not in files  # ambiguous — don't claim it lacks Lead
    assert "bonus.psarc" in files  # has Bonus Rhythm, no Lead variant


# ── Lyrics axis ─────────────────────────────────────────────────────────────

def test_has_lyrics_require(client, seeded):
    data = _get(client, has_lyrics="1")
    files = {s["filename"] for s in data["songs"]}
    assert files == {"a.psarc", "c.sloppak", "f.psarc"}


def test_has_lyrics_exclude(client, seeded):
    data = _get(client, has_lyrics="0")
    files = {s["filename"] for s in data["songs"]}
    assert files == {"b.psarc", "d.sloppak", "e.sloppak"}


# ── Stems axis ──────────────────────────────────────────────────────────────

def test_stems_has_piano(client, seeded):
    data = _get(client, stems_has="piano")
    # Only c.sloppak has piano.
    assert {s["filename"] for s in data["songs"]} == {"c.sloppak"}


def test_stems_has_or_within_axis(client, seeded):
    data = _get(client, stems_has="drums,piano")
    # drums OR piano: c (all stems) and d (drums + vocals).
    assert {s["filename"] for s in data["songs"]} == {"c.sloppak", "d.sloppak"}


def test_stems_has_excludes_psarcs_and_legacy_null(client, seeded):
    """PSARCs have empty stem_ids; legacy row has NULL. Both are
    excluded by stems_has — there's no proof the stem is present."""
    data = _get(client, stems_has="drums")
    files = {s["filename"] for s in data["songs"]}
    assert files == {"c.sloppak", "d.sloppak"}
    # PSARC rows missing.
    assert "a.psarc" not in files
    # Legacy NULL row missing.
    assert "e.sloppak" not in files


def test_stems_lacks_other(client, seeded):
    data = _get(client, stems_lacks="other")
    files = {s["filename"] for s in data["songs"]}
    # c.sloppak has "other" — must be excluded.
    assert "c.sloppak" not in files
    # Everything else lacks it (PSARCs have empty stem_ids; legacy NULL
    # also lacks it because json_each yields nothing).
    assert "a.psarc" in files


# ── Tuning axis ─────────────────────────────────────────────────────────────

def test_tunings_or_within_axis(client, seeded):
    data = _get(client, tunings="E Standard,Drop D")
    files = {s["filename"] for s in data["songs"]}
    assert files == {"a.psarc", "b.psarc", "c.sloppak", "e.sloppak"}


def test_tunings_eb_standard_only(client, seeded):
    data = _get(client, tunings="Eb Standard")
    assert {s["filename"] for s in data["songs"]} == {"d.sloppak", "f.psarc"}


# ── Combined cross-axis (AND) ───────────────────────────────────────────────

def test_combined_axes(client, seeded):
    data = _get(client, arrangements_has="Lead", has_lyrics="1", tunings="E Standard")
    # Lead AND lyrics AND E Standard:
    # a (Lead, lyrics, E Std) ✓
    # f (Lead, lyrics, Eb Std) ✗ (wrong tuning)
    # c is Combo not Lead
    assert {s["filename"] for s in data["songs"]} == {"a.psarc"}


# ── Whitelist sanitization (defense-in-depth) ───────────────────────────────

def test_whitelist_rejects_unknown_arrangement(client, seeded):
    """Unknown arrangement names are dropped silently (whitelist), so a
    bogus value is treated as 'no filter' rather than reaching SQL."""
    full = _get(client)
    bogus = _get(client, arrangements_has="DROP TABLE songs")
    # Same row count as no-filter — whitelist stripped the unknown name.
    assert bogus["total"] == full["total"]


# ── Year sort (slopsmith#128) ───────────────────────────────────────────────

def test_year_sort_desc_newest_first(client, seeded):
    data = _get(client, sort="year-desc")
    files = [s["filename"] for s in data["songs"]]
    # Years: c=2020, d=2018, f=2015, a=2010, b=2005, e=''.
    # Empty year goes to the bottom for both directions.
    assert files == ["c.sloppak", "d.sloppak", "f.psarc", "a.psarc", "b.psarc", "e.sloppak"]


def test_year_sort_asc_oldest_first(client, seeded):
    data = _get(client, sort="year")
    files = [s["filename"] for s in data["songs"]]
    # Empty year still bottom — only the dated rows reverse.
    assert files == ["b.psarc", "a.psarc", "f.psarc", "d.sloppak", "c.sloppak", "e.sloppak"]


def test_tuning_sort_down_tuned_before_up_tuned_at_same_distance(client, server_mod):
    """Within an ABS(tuning_sort_key) tier, the down-tuned variant
    must come before the up-tuned one so the order matches Rocksmith's
    grouping (Eb Standard before F Standard at distance 6, etc.).
    Earlier code used signed-key DESC for the tiebreaker, which put
    +6 before -6 — the opposite of intent. Regression for Copilot
    finding on PR #134."""
    _put(server_mod, filename="up.psarc", title="Up", artist="A",
         tuning_name="F Standard", tuning_sort_key=6,
         arrangements=[{"index": 0, "name": "Lead", "notes": 1}])
    _put(server_mod, filename="down.psarc", title="Down", artist="B",
         tuning_name="Eb Standard", tuning_sort_key=-6,
         arrangements=[{"index": 0, "name": "Lead", "notes": 1}])
    _put(server_mod, filename="std.psarc", title="Std", artist="C",
         tuning_name="E Standard", tuning_sort_key=0,
         arrangements=[{"index": 0, "name": "Lead", "notes": 1}])

    # /api/library?sort=tuning
    data = _get(client, sort="tuning")
    seen = []
    for s in data["songs"]:
        tn = s.get("tuning_name")
        if tn and tn not in seen:
            seen.append(tn)
    assert seen == ["E Standard", "Eb Standard", "F Standard"]

    # /api/library/tuning-names — same intended order.
    names = [t["name"] for t in client.get("/api/library/tuning-names").json()["tunings"]]
    assert names == ["E Standard", "Eb Standard", "F Standard"]


def test_tuning_sort_pushes_empty_tuning_name_to_bottom(client, server_mod):
    """Pre-rescan rows have empty `tuning_name` and `tuning_sort_key=0`.
    Without a leading `(tuning_name='') ASC` term, ABS(0) collides with
    E Standard's 0 so unscanned rows would float to the top of the
    tuning sort. Regression for Copilot finding on PR #134."""
    _put(server_mod, filename="real.psarc", title="Real", artist="A",
         tuning_name="E Standard", tuning_sort_key=0,
         arrangements=[{"index": 0, "name": "Lead", "notes": 1}])
    _put(server_mod, filename="legacy.psarc", title="Legacy", artist="B",
         tuning_name="", tuning_sort_key=0,
         arrangements=[{"index": 0, "name": "Lead", "notes": 1}])
    data = _get(client, sort="tuning")
    files = [s["filename"] for s in data["songs"]]
    assert files == ["real.psarc", "legacy.psarc"]


def test_tuning_sort_pushes_null_tuning_name_to_bottom(client, server_mod):
    """Defense in depth for the same intent as the empty-string case: a
    row with NULL `tuning_name` / NULL `tuning_sort_key` (which can
    arise from raw SQL inserts that bypass `put()`, future code that
    writes None, or edge-case migration paths) must also fall to the
    bottom. Without COALESCE in the ORDER BY, `(tuning_name = '')`
    evaluates to NULL for those rows and NULLs sort *ahead of* 0 in
    SQLite's ASC ordering — the legacy row would float above E
    Standard. Regression for Copilot finding on PR #134."""
    _put(server_mod, filename="real.psarc", title="Real", artist="A",
         tuning_name="E Standard", tuning_sort_key=0,
         arrangements=[{"index": 0, "name": "Lead", "notes": 1}])
    # Direct INSERT with NULL tuning_name AND NULL tuning_sort_key —
    # mimics what a pre-migration row would look like if SQLite hadn't
    # backfilled the literal-constant defaults on ADD COLUMN.
    server_mod.meta_db.conn.execute(
        "INSERT INTO songs (filename, mtime, size, title, artist, album, year, "
        "duration, tuning, arrangements, has_lyrics, format, stem_count, stem_ids, "
        "tuning_name, tuning_sort_key) "
        "VALUES (?, 1.0, 1, ?, ?, ?, '', 200.0, '', ?, 0, 'psarc', 0, '[]', NULL, NULL)",
        ("legacy.psarc", "Legacy", "Z", "Z - LP", json.dumps([])),
    )
    server_mod.meta_db.conn.commit()

    data = _get(client, sort="tuning")
    files = [s["filename"] for s in data["songs"]]
    assert files == ["real.psarc", "legacy.psarc"]

    # /api/library/tuning-names should also exclude the NULL row from
    # the picker entirely (users can't usefully filter by an unknown
    # tuning).
    names = [t["name"] for t in client.get("/api/library/tuning-names").json()["tunings"]]
    assert names == ["E Standard"]


def test_query_stats_artist_count_is_case_insensitive(client, server_mod):
    """`total_artists` previously used `COUNT(DISTINCT artist)` (case-
    sensitive) while `query_artists` and the per-letter counts used
    NOCASE — leading to mismatched totals when the same artist was
    indexed under different casings. Regression for Copilot finding
    on PR #134."""
    _put(server_mod, filename="x.psarc", title="X", artist="The Beatles",
         arrangements=[{"index": 0, "name": "Lead", "notes": 1}])
    _put(server_mod, filename="y.psarc", title="Y", artist="the beatles",
         arrangements=[{"index": 0, "name": "Lead", "notes": 1}])
    stats = client.get("/api/library/stats").json()
    assert stats["total_artists"] == 1
    # Letter-bar count agrees.
    assert stats["letters"].get("T") == 1


def test_query_stats_groups_non_ascii_artist_letters_under_hash(client, server_mod):
    _put(server_mod, filename="angstrom.psarc", title="Angstrom", artist="Ångström")

    stats = client.get("/api/library/stats").json()

    assert stats["letters"] == {"#": 1}


def test_query_stats_ignores_null_letter_counts(server_mod):
    """Legacy/corrupt rows can surface as NULL-ish letter aggregate
    rows on some SQLite builds. The stats endpoint should ignore those
    buckets instead of crashing while building the # group."""
    class Result:
        def __init__(self, one=None, rows=None):
            self.one = one
            self.rows = rows or []

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.rows

    class FakeConn:
        def execute(self, sql, params=()):
            if "GROUP BY letter" in sql:
                return Result(rows=[(None, None), ("#", None), ("T", 1)])
            if "COUNT(DISTINCT artist COLLATE NOCASE)" in sql:
                return Result(one=(1,))
            if "COUNT(*)" in sql:
                return Result(one=(1,))
            raise AssertionError(sql)

        def close(self):
            pass

    server_mod.meta_db.conn.close()
    server_mod.meta_db.conn = FakeConn()

    stats = server_mod.meta_db.query_stats()

    assert stats == {"total_songs": 1, "total_artists": 1, "letters": {"T": 1}}


def test_compound_sort_with_legacy_dir_desc_doesnt_error(client, seeded):
    """Regression for Copilot finding on PR #134: `sort=year&dir=desc`
    used to produce invalid SQL (`CAST(year AS INTEGER) ASC DESC`)
    because the global dir-append toggle didn't notice that the
    compound year sort already encoded direction. Now the append is
    suppressed when the sort clause already contains ASC or DESC."""
    r = client.get("/api/library", params={"sort": "year", "dir": "desc"})
    assert r.status_code == 200
    # Order matches plain `sort=year` (legacy dir is ignored on
    # already-directional clauses). The point is no 500 from invalid SQL.
    files = [s["filename"] for s in r.json()["songs"]]
    assert files == ["b.psarc", "a.psarc", "f.psarc", "d.sloppak", "c.sloppak", "e.sloppak"]


# ── Tuning sort by pitch distance (slopsmith#22) ────────────────────────────

def test_tuning_sort_by_pitch_distance(client, seeded):
    """Tuning sort previously alphabetized (Drop C, Drop D, E Standard).
    Now it's musical-distance from E Standard via ABS(sort_key) ASC,
    so E Standard (|0|) leads, then Drop D (|-2|), then Eb Standard
    (|-6|). See slopsmith#22."""
    data = _get(client, sort="tuning")
    # Group by tuning name, preserving order; assert the first
    # appearance of each tuning matches the expected musical-distance
    # ordering. (Within a tuning group, songs sort by row order.)
    seen_order = []
    for s in data["songs"]:
        tn = s.get("tuning_name")
        if tn and tn not in seen_order:
            seen_order.append(tn)
    assert seen_order == ["E Standard", "Drop D", "Eb Standard"]


# ── /api/library/tuning-names endpoint ──────────────────────────────────────

def test_tuning_names_endpoint(client, seeded):
    data = client.get("/api/library/tuning-names").json()
    names = [t["name"] for t in data["tunings"]]
    # ABS(sort_key) ascending puts E Standard first, then Drop D
    # (|-2|), then Eb Standard (|-6|).
    assert names == ["E Standard", "Drop D", "Eb Standard"]
    counts = {t["name"]: t["count"] for t in data["tunings"]}
    assert counts["E Standard"] == 2
    assert counts["Drop D"] == 2
    assert counts["Eb Standard"] == 2


# ── Stats endpoint mirrors filtered totals ──────────────────────────────────

def test_stats_reflects_filters(client, seeded):
    full = client.get("/api/library/stats").json()
    filtered = client.get("/api/library/stats", params={"has_lyrics": "1"}).json()
    assert full["total_songs"] == 6
    assert filtered["total_songs"] == 3
    assert filtered["total_artists"] == 3


# ── Empty values are no-ops ─────────────────────────────────────────────────

def test_empty_values_are_no_ops(client, seeded):
    full = _get(client)
    same = _get(client, arrangements_has="", arrangements_lacks=",,",
                stems_has="", tunings="", has_lyrics="")
    assert full["total"] == same["total"]
