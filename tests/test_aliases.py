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
