"""אליאסים TG/RID: טעינת CSV, עריכות ידניות, join, /api/aliases."""
import json


def test_manual_alias_roundtrip(paths):
    import aliases
    ok, err = aliases.replace_manual({"tg": {"2451": "מוקד"}, "rid": {"3141592": "יחידה 1"}})
    assert ok
    assert aliases.tg_name(2451) == "מוקד"
    assert aliases.rid_name(3141592) == "יחידה 1"
    assert aliases.tg_name(9999) is None


def test_manual_validation_rejects_nonnumeric(paths):
    import aliases
    ok, err = aliases.replace_manual({"tg": {"abc": "x"}})
    assert not ok


def test_csv_import_radioid_format(paths):
    import aliases
    aliases.RID_CSV.write_text("RADIO_ID,CALLSIGN,NAME\n3141592,4X-ABC,Alice\n3140001,4X-XYZ,Bob\n")
    aliases.load()
    assert aliases.rid_name(3141592) == "4X-ABC"
    assert aliases.rid_name(3140001) == "4X-XYZ"


def test_csv_import_headerless(paths):
    import aliases
    aliases.TG_CSV.write_text("2451,Dispatch\n2452,Ops\n")
    aliases.load()
    assert aliases.tg_name(2451) == "Dispatch"


def test_manual_overrides_import(paths):
    import aliases
    aliases.RID_CSV.write_text("id,name\n5,FromCSV\n")
    aliases.load()
    assert aliases.rid_name(5) == "FromCSV"
    aliases.replace_manual({"rid": {"5": "FromManual"}})
    assert aliases.rid_name(5) == "FromManual"   # ידני גובר


def test_api_aliases_get_put(paths):
    app = paths
    c = app.app.test_client()
    r = c.put("/api/aliases", json={"tg": {"100": "TG100"}, "rid": {}})
    assert r.status_code == 200
    body = c.get("/api/aliases").get_json()
    assert body["aliases"]["tg"]["100"] == "TG100"


def test_api_aliases_bad_put(paths):
    app = paths
    r = app.app.test_client().put("/api/aliases", json={"tg": "not-an-object"})
    assert r.status_code == 400


# --- תור לא-מזוהים (worklist למתן שמות) --------------------------------------
def test_unknown_aliases_ranks_by_count_and_excludes_named(paths):
    """RID/TG שנצפו בתעבורה ואין להם שם => בתור, ממוין לפי count יורד. מה
    ש-aliasdb פותר (ידני/ייבוא) מוסלל החוצה — מתן שם מפיל מהתור."""
    import aliases
    app = paths
    aliases.replace_manual({"rid": {"500": "מוכר"}, "tg": {}})
    recs = [
        {"t": 10, "src": 100, "tg": 9, "tgt": None},   # RID 100 ×3
        {"t": 11, "src": 100, "tg": 9, "tgt": None},
        {"t": 12, "src": 100, "tg": 9, "tgt": None},
        {"t": 13, "src": 200, "tg": 9, "tgt": None},   # RID 200 ×1
        {"t": 14, "src": 500, "tg": 9, "tgt": None},   # RID 500 מוכר => לא בתור
    ]
    unk = app._unknown_aliases(recs)
    rids = [u for u in unk if u["kind"] == "rid"]
    ids = [u["id"] for u in rids]
    assert 500 not in ids                       # שם ידני => מוסלל
    assert ids[0] == 100 and rids[0]["count"] == 3   # הפעיל ביותר ראשון
    assert 200 in ids
    tg9 = next(u for u in unk if u["kind"] == "tg" and u["id"] == 9)
    assert tg9["rid_count"] == 3                 # 100/200/500 מובחנים
    assert 9 in rids[0]["tgs"]                    # הקשר: עם אילו TG דיבר


def test_unknown_aliases_includes_target_rid(paths):
    """גם RID-יעד (tgt, למשל שיחת יחיד) נכנס לתור — לא רק מקור."""
    app = paths
    unk = app._unknown_aliases([{"t": 1, "src": 10, "tgt": 20, "tg": None}])
    ids = {(u["kind"], u["id"]) for u in unk}
    assert ("rid", 10) in ids and ("rid", 20) in ids


def test_api_aliases_unknown(paths):
    app = paths
    c = app.app.test_client()
    b = app._day_bounds("2026-07-20")
    mid = (b[0] + b[1]) / 2
    app.DMR_LOG_PATH.write_text(
        json.dumps({"t": mid, "src": 777, "tg": 42, "call_type": "group"}) + "\n")
    body = c.get("/api/aliases/unknown?day=2026-07-20").get_json()
    assert body["ok"]
    ids = {(u["kind"], u["id"]) for u in body["unknown"]}
    assert ("rid", 777) in ids and ("tg", 42) in ids


def test_api_aliases_unknown_bad_day(paths):
    assert paths.app.test_client().get("/api/aliases/unknown?day=bad").status_code == 400
