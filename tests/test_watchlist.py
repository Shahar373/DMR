"""מעקב RID/TG להתראה מקומית: load/match/validate/replace + /api/watchlist."""
import json


def test_match_finds_tg(paths):
    import watchlist
    watchlist.replace({"tg": [101], "rid": []})
    assert watchlist.match(tg=101, src=None, tgt=None) == {"kind": "tg", "id": 101}
    assert watchlist.match(tg=999, src=None, tgt=None) is None


def test_match_finds_source_rid(paths):
    import watchlist
    watchlist.replace({"tg": [], "rid": [2050123]})
    assert watchlist.match(tg=None, src=2050123, tgt=None) == {"kind": "rid", "id": 2050123}


def test_match_finds_target_rid(paths):
    import watchlist
    watchlist.replace({"tg": [], "rid": [2052888]})
    assert watchlist.match(tg=None, src=None, tgt=2052888) == {"kind": "rid", "id": 2052888}


def test_match_priority_tg_over_rid(paths):
    """סדר-עדיפות: tg קודם ל-src/tgt (תג יחיד, לא רשימה) — גם אם שניהם במעקב."""
    import watchlist
    watchlist.replace({"tg": [101], "rid": [999]})
    assert watchlist.match(tg=101, src=999, tgt=None) == {"kind": "tg", "id": 101}


def test_match_none_when_empty(paths):
    import watchlist
    assert watchlist.match(tg=1, src=2, tgt=3) is None


def test_replace_validation_rejects_bad_shape(paths):
    import watchlist
    ok, err = watchlist.replace({"tg": "not-a-list"})
    assert not ok and err
    ok, err = watchlist.replace({"tg": ["abc"]})
    assert not ok and err


def test_replace_persists_across_reload(paths):
    import watchlist
    ok, err = watchlist.replace({"tg": [101, 205], "rid": [7]})
    assert ok and err is None
    watchlist._tg.clear(); watchlist._rid.clear()
    watchlist.load()
    assert watchlist.export_all() == {"tg": [101, 205], "rid": [7]}


def test_api_watchlist_get_put(paths):
    app = paths
    c = app.app.test_client()
    r = c.put("/api/watchlist", json={"tg": [101], "rid": [2050123]})
    assert r.status_code == 200
    body = c.get("/api/watchlist").get_json()
    assert body["watchlist"] == {"tg": [101], "rid": [2050123]}


def test_api_watchlist_bad_put(paths):
    r = paths.app.test_client().put("/api/watchlist", json={"tg": "nope"})
    assert r.status_code == 400
