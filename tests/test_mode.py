"""מעברי מצב DMR: _enter_dmr, /api/mode, נפילה-ל-off, /api/state, /api/health.
SDR/systemd ממוקפים דרך fixture sysctl."""
import json


def _client(app):
    return app.app.test_client()


def _seed_systems(app, systems):
    app.SYSTEMS_PATH.write_text(json.dumps(systems))


def test_enter_dmr_success(paths, sysctl, no_sleep):
    app = paths
    system = {"id": "s1", "name": "Test", "control": 461.0375, "color_code": 1,
              "channelmap": [{"lcn": 1, "freq": 461.0375}]}
    err, detail = app._enter_dmr(system)
    assert err is None
    assert ("restart", app.DMR_SERVICE) in sysctl.calls
    # env + channelmap נכתבו
    assert "DSD_CONTROL_FREQ=461037500" in app.DMR_ENV_PATH.read_text()
    assert "461037500" in app.CHANNELMAP_PATH.read_text()


def test_enter_dmr_caches_active_system_for_intel(paths, sysctl, no_sleep):
    """_active_system_id/_active_color_code נקבעים ב-_enter_dmr => ה-listener
    יודע לשייך system-radar (lsn_status/site_info/וכו') בלי load_state() בכל
    אירוע UDP (ר' §5 system_intel)."""
    app = paths
    system = {"id": "s1", "name": "Test", "control": 461.0375, "color_code": 3,
              "channelmap": [{"lcn": 1, "freq": 461.0375}]}
    err, detail = app._enter_dmr(system)
    assert err is None
    assert app._active_system_id == "s1" and app._active_color_code == 3


def test_enter_dmr_clears_channel_status(paths, sysctl, no_sleep):
    """restart אמיתי = כל מפענחי ה-multi עולים מ-0 (Phase 7 partial-restart) --
    סטטוס-ערוצים (down/restarting) מריצה קודמת לא רלוונטי יותר."""
    app = paths
    with app._channel_status_lock:
        app._channel_status[3] = {"status": "down", "restart_count": 3, "t": 1.0}
    system = {"id": "s1", "name": "Test", "control": 461.0375, "color_code": 1,
              "channelmap": [{"lcn": 1, "freq": 461.0375}]}
    err, detail = app._enter_dmr(system)
    assert err is None
    with app._channel_status_lock:
        assert app._channel_status == {}


def test_enter_dmr_crash_returns_error(paths, sysctl, no_sleep, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "_is_active", lambda svc: False)   # השירות "קרס" מיד
    system = {"id": "s1", "name": "T", "control": 461.0, "color_code": 1, "channelmap": []}
    err, detail = app._enter_dmr(system)
    assert err is not None


def test_api_mode_dmr(paths, sysctl, no_sleep):
    app = paths
    _seed_systems(app, [{"id": "s1", "name": "T", "control": 461.0375,
                         "color_code": 1, "channelmap": [{"lcn": 1, "freq": 461.0375}]}])
    r = _client(app).post("/api/mode", json={"mode": "dmr", "system": "s1"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    st = json.loads(app.STATE_PATH.read_text())
    assert st["app_mode"] == "dmr" and st["system"] == "s1"


def test_api_mode_off(paths, sysctl, no_sleep):
    app = paths
    app.save_state({"app_mode": "dmr", "system": "s1"})
    r = _client(app).post("/api/mode", json={"mode": "off"})
    assert r.status_code == 200
    assert ("stop", app.DMR_SERVICE) in sysctl.calls
    st = json.loads(app.STATE_PATH.read_text())
    assert st["app_mode"] == "off" and st["prev_mode"] == "dmr"


def test_api_mode_invalid(paths, sysctl):
    r = _client(paths).post("/api/mode", json={"mode": "banana"})
    assert r.status_code == 400


def test_api_mode_dmr_no_system(paths, sysctl, no_sleep):
    app = paths
    _seed_systems(app, [])   # אין מערכות => 400, לא crash
    r = _client(app).post("/api/mode", json={"mode": "dmr"})
    assert r.status_code == 400


def test_api_mode_fail_to_off(paths, sysctl, no_sleep, monkeypatch):
    """כשל כניסה => נפילה ל-off (500), לעולם לא נשאר תקוע."""
    app = paths
    _seed_systems(app, [{"id": "s1", "name": "T", "control": 461.0,
                         "color_code": 1, "channelmap": []}])
    monkeypatch.setattr(app, "_is_active", lambda svc: False)   # תמיד "נכשל לעלות"
    r = _client(app).post("/api/mode", json={"mode": "dmr", "system": "s1"})
    assert r.status_code == 500
    body = r.get_json()
    assert body["ok"] is False and body["app_mode"] == "off"
    st = json.loads(app.STATE_PATH.read_text())
    assert st["app_mode"] == "off"


def test_api_state_live_vs_saved(paths, sysctl, no_sleep, monkeypatch):
    app = paths
    app.save_state({"app_mode": "dmr", "system": "s1"})
    # שמור dmr אבל אף שירות לא רץ => mode_ok=False (תקלה מדווחת)
    monkeypatch.setattr(app, "_live_mode", lambda: None)
    r = _client(app).get("/api/state")
    body = r.get_json()
    assert body["app_mode"] == "dmr" and body["mode_ok"] is False


def test_api_state_off_is_ok(paths, sysctl, monkeypatch):
    app = paths
    app.save_state({"app_mode": "off"})
    monkeypatch.setattr(app, "_live_mode", lambda: None)
    body = _client(app).get("/api/state").get_json()
    assert body["app_mode"] == "off" and body["mode_ok"] is True


def test_api_health_dmr(paths, monkeypatch):
    app = paths
    app.save_state({"app_mode": "dmr", "system": "s1"})
    monkeypatch.setattr(app.subprocess, "run",
                        lambda *a, **k: __import__("types").SimpleNamespace(
                            stdout="active", returncode=0))
    body = _client(app).get("/api/health").get_json()
    assert body["app_mode"] == "dmr" and body["ok"] is True


def test_api_health_multi(paths, monkeypatch):
    """נתפס ב-visual-verification: dmr/multi חולקות systemd unit אחד, ו-
    api_health's גרסה-פרטית-לו של פתרון-המצב (`mode = "dmr" if dmr_active
    else saved`) בלעה כל multi בחזרה ל-"dmr" ברגע שהשירות פעיל — בדיוק אותו
    באג שכבר תוקן ב-_live_mode(), רק עותק שני שלא תוקן. חובה: /api/health
    ידווח app_mode="multi" (לא "dmr") + ok=True כשהשירות פעיל ו-multi שמור."""
    app = paths
    app.save_state({"app_mode": "multi", "system": "s1"})
    monkeypatch.setattr(app.subprocess, "run",
                        lambda *a, **k: __import__("types").SimpleNamespace(
                            stdout="active", returncode=0))
    body = _client(app).get("/api/health").get_json()
    assert body["app_mode"] == "multi" and body["ok"] is True


def test_systems_put_validation(paths):
    app = paths
    c = _client(app)
    # תקין
    good = [{"id": "s1", "name": "Sys", "control": 461.0, "color_code": 2,
             "channelmap": [{"lcn": 1, "freq": 461.0}]}]
    assert c.put("/api/systems", json=good).status_code == 200
    # color code מחוץ לטווח => 400
    bad = [{"id": "s1", "name": "Sys", "control": 461.0, "color_code": 99, "channelmap": []}]
    assert c.put("/api/systems", json=bad).status_code == 400


def test_render_dmr_env_hz_conversion(paths):
    app = paths
    system = {"id": "s1", "name": "T", "control": 461.0375, "color_code": 3, "channelmap": []}
    env = app.render_dmr_env(system)
    assert "DSD_CONTROL_FREQ=461037500" in env   # MHz → Hz
    assert "DSD_COLOR_CODE=3" in env


def test_render_dmr_env_keeps_bridge_keys(paths):
    """render_dmr_env דורס את כל dmr.env בכל מעבר מצב (_enter_dmr/_scan_enter_leg) —
    מפתחות הגשר (rsp_tcp/rsp_fm.py) חייבים להישאר בקובץ בכל מעבר, אחרת dsd_pty
    נופל בחזרה על ברירות-מחדל שאינן בהכרח מסונכרנות."""
    app = paths
    system = {"id": "s1", "name": "T", "control": 461.0375, "color_code": 3, "channelmap": []}
    env = app.render_dmr_env(system)
    for key in ("DSD_RTLTCP", "DSD_AUDIO_TCP", "DSD_RIGCTL", "DSD_IQ_RATE", "DSD_AUDIO_GAIN"):
        assert f"{key}=" in env, f"missing {key} in rendered dmr.env"


def test_render_channelmap(paths):
    app = paths
    txt = app.render_channelmap([{"lcn": 1, "freq": 461.0375}, {"lcn": 2, "freq": 461.0625}])
    assert "1,461037500" in txt and "2,461062500" in txt


def test_render_channelmap_always_has_header_row(paths):
    """★ באג אמיתי שתוקן ב-v0.12.0: csvChanImport של DSD-FME מדלג **תמיד** על
    השורה הראשונה (`if (row_count == 1) continue;` ב-src/dsd_import.c) — בלי
    שורת-כותרת הערוץ הראשון נזרק בשקט בכל הרצת-טראנקינג."""
    app = paths
    lines = app.render_channelmap([{"lcn": 1, "freq": 461.0375}]).splitlines()
    assert lines[0] == app.CHANNELMAP_HEADER
    assert lines[0].startswith("#")        # כדי ש-parse_channelmap_hz שלנו ידלג גם
    assert lines[1] == "1,461037500"       # הערוץ הראשון שורד את דילוג-השורה
    # גם מפה ריקה מקבלת כותרת (אחרת ‎-C על קובץ ריק מתנהג שונה)
    assert app.render_channelmap([]).splitlines() == [app.CHANNELMAP_HEADER]


def test_render_channelmap_lsn_pairs_expansion(paths):
    """מצב טראנקינג: מפת DSD-FME ל-Cap+ מאונדקסת ב-LSN, וכל תדר נושא שני
    LSN-ים (1+2 = ערוץ פיזי 1, 3+4 = השני...). ערוץ פיזי n => LSN 2n-1, 2n."""
    app = paths
    txt = app.render_channelmap([{"lcn": 1, "freq": 461.0375},
                                 {"lcn": 2, "freq": 461.0625}], lsn_pairs=True)
    assert txt.splitlines() == [app.CHANNELMAP_HEADER,
                                "1,461037500", "2,461037500",
                                "3,461062500", "4,461062500"]


def test_render_channelmap_multi_must_not_expand_pairs(paths):
    """ב-multi אותו קובץ הוא רשימת הערוצים הפיזיים לדמודולציה — הכפלה כאן
    הייתה מייצרת שני מדמודלטורים/מפענחים על אותו תדר."""
    app = paths
    cmap = [{"lcn": 1, "freq": 461.0375}, {"lcn": 2, "freq": 461.0625}]
    assert app.render_channelmap(cmap).splitlines()[1:] == ["1,461037500", "2,461062500"]


def test_channelmap_header_is_skipped_by_our_own_parsers(paths):
    """שני הצדדים של אותו קובץ: DSD-FME מדלג על שורה 1, ו-dsd_pty/rsp_fm
    (multi) חייבים לדלג עליה גם — אחרת ‎--multi-channelmap נופל על הכותרת."""
    app = paths
    import dsd_pty
    import rsp_fm
    app.write_channelmap([{"lcn": 1, "freq": 461.0375}, {"lcn": 2, "freq": 461.0625}])
    expected = [{"lcn": 1, "freq_hz": 461_037_500}, {"lcn": 2, "freq_hz": 461_062_500}]
    assert dsd_pty.parse_channelmap_hz(str(app.CHANNELMAP_PATH)) == expected
    assert rsp_fm.parse_channelmap_hz(str(app.CHANNELMAP_PATH)) == expected


# --- multi mode (Phase 2) ----------------------------------------------------
def _multi_system():
    return {"id": "s1", "name": "T", "control": 461.0375, "color_code": 1,
            "channelmap": [
                {"lcn": 1, "freq": 461.0375},
                {"lcn": 2, "freq": 461.0625},
                {"lcn": 3, "freq": 461.0875},
            ]}


def test_render_dmr_env_multi_adds_flags_and_keeps_base_keys():
    """multi=True מוסיף DSD_MULTI + DSD_MULTI_GUARD_HZ/MAX_RATE_HZ בלי לאבד
    אף מפתח-גשר בסיסי (render_dmr_env דורס את כל הקובץ בכל מעבר — §8)."""
    import app
    system = _multi_system()
    env = app.render_dmr_env(system, multi=True)
    assert "DSD_MULTI=1" in env
    assert f"DSD_MULTI_GUARD_HZ={app.MULTI_GUARD_HZ}" in env
    assert f"DSD_MULTI_MAX_RATE_HZ={app.MULTI_MAX_SPAN_HZ}" in env
    for key in ("DSD_RTLTCP", "DSD_AUDIO_TCP", "DSD_AUDIO_TCP_BASE", "DSD_RIGCTL",
                "DSD_IQ_RATE", "DSD_AUDIO_GAIN", "DSD_CHANNELMAP"):
        assert f"{key}=" in env, f"missing {key} in rendered multi dmr.env"
    # dmr/scan (multi=False, ברירת מחדל) לא כוללים בכלל את הדגלים האלה
    single = app.render_dmr_env(system)
    assert "DSD_MULTI" not in single


def test_validate_multi_feasible_requires_two_channels():
    import app
    ok, err = app._validate_multi_feasible(
        {"channelmap": [{"lcn": 1, "freq": 461.0}]})
    assert ok is False and "2" in err


def test_validate_multi_feasible_rejects_too_many_channels():
    import app
    cmap = [{"lcn": i, "freq": 461.0 + i * 0.0125} for i in range(1, app.MULTI_CHANNELS_MAX + 2)]
    ok, err = app._validate_multi_feasible({"channelmap": cmap})
    assert ok is False and str(app.MULTI_CHANNELS_MAX) in err


def test_validate_multi_feasible_rejects_span_too_wide():
    import app
    ok, err = app._validate_multi_feasible(
        {"channelmap": [{"lcn": 1, "freq": 461.0}, {"lcn": 2, "freq": 465.0}]})
    assert ok is False and "MHz" in err


def test_validate_multi_feasible_accepts_tight_channelmap():
    import app
    ok, err = app._validate_multi_feasible(_multi_system())
    assert ok is True and err is None


def test_validate_multi_feasible_rejects_duplicate_lcn():
    """Bug #4: MultiChannelBridge keys demodulators by LCN, so a duplicate LCN
    would silently drop a decoder and hang bring-up. Reject at multi entry with
    a clear error instead of an opaque hardware failure."""
    import app
    sys = {"id": "s1", "name": "T", "control": 461.0375, "color_code": 1,
           "channelmap": [{"lcn": 1, "freq": 461.0375},
                          {"lcn": 1, "freq": 461.0625}]}   # duplicate LCN 1
    ok, err = app._validate_multi_feasible(sys)
    assert ok is False and "LCN" in err


def test_validate_multi_feasible_rejects_near_duplicate_freqs():
    """Finding #2: two channels closer than the DMR 12.5kHz raster are a
    duplicate/typo -- they'd spawn two overlapping NFM demodulators (same
    offset) decoding one signal, producing duplicate events with no real
    separation. Reject at multi entry, not as a silent hardware mess."""
    import app
    sys = {"id": "s1", "name": "T", "control": 461.0375, "color_code": 1,
           "channelmap": [{"lcn": 1, "freq": 461.0375},
                          {"lcn": 2, "freq": 461.0400}]}   # 2.5kHz apart
    ok, err = app._validate_multi_feasible(sys)
    assert ok is False and "kHz" in err


def test_validate_multi_feasible_accepts_exact_raster_spacing():
    """A pair spaced exactly one DMR raster (12.5kHz) apart is legitimate and
    must pass -- the guard rejects only *tighter* than the raster."""
    import app
    sys = {"id": "s1", "name": "T", "control": 461.0375, "color_code": 1,
           "channelmap": [{"lcn": 1, "freq": 461.0375},
                          {"lcn": 2, "freq": 461.0500}]}   # exactly 12.5kHz
    ok, err = app._validate_multi_feasible(sys)
    assert ok is True and err is None


def test_api_mode_multi_rejects_duplicate_lcn(paths, sysctl, no_sleep):
    app = paths
    _seed_systems(app, [{"id": "s1", "name": "T", "control": 461.0375, "color_code": 1,
                         "channelmap": [{"lcn": 1, "freq": 461.0375},
                                        {"lcn": 1, "freq": 461.0625}]}])
    r = _client(app).post("/api/mode", json={"mode": "multi", "system": "s1"})
    assert r.status_code == 400
    assert ("restart", app.DMR_SERVICE) not in sysctl.calls   # never touched the SDR


def test_api_mode_multi_enters_and_renders_multi_env(paths, sysctl, no_sleep):
    app = paths
    _seed_systems(app, [_multi_system()])
    r = _client(app).post("/api/mode", json={"mode": "multi", "system": "s1"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert r.get_json()["app_mode"] == "multi"
    st = json.loads(app.STATE_PATH.read_text())
    assert st["app_mode"] == "multi" and st["system"] == "s1"
    assert "DSD_MULTI=1" in app.DMR_ENV_PATH.read_text()
    assert ("restart", app.DMR_SERVICE) in sysctl.calls


def test_api_mode_multi_rejects_single_channel_system(paths, sysctl, no_sleep):
    app = paths
    _seed_systems(app, [{"id": "s1", "name": "T", "control": 461.0, "color_code": 1,
                         "channelmap": [{"lcn": 1, "freq": 461.0}]}])
    r = _client(app).post("/api/mode", json={"mode": "multi", "system": "s1"})
    assert r.status_code == 400
    assert ("restart", app.DMR_SERVICE) not in sysctl.calls   # לא נגע ב-SDR בכלל


def test_api_mode_multi_rejects_span_too_wide(paths, sysctl, no_sleep):
    app = paths
    _seed_systems(app, [{"id": "s1", "name": "T", "control": 461.0, "color_code": 1,
                         "channelmap": [{"lcn": 1, "freq": 461.0}, {"lcn": 2, "freq": 465.0}]}])
    r = _client(app).post("/api/mode", json={"mode": "multi", "system": "s1"})
    assert r.status_code == 400
    assert "MHz" in r.get_json()["error"]


def test_api_mode_multi_no_system(paths, sysctl, no_sleep):
    app = paths
    _seed_systems(app, [])
    r = _client(app).post("/api/mode", json={"mode": "multi"})
    assert r.status_code == 400


def test_live_mode_distinguishes_multi_from_dmr(paths, sysctl, no_sleep):
    """dmr/multi חולקות את אותה יחידת systemd — _live_mode צריך להבדיל
    ביניהן לפי המצב השמור, לא רק systemctl is-active."""
    app = paths
    _seed_systems(app, [_multi_system()])
    _client(app).post("/api/mode", json={"mode": "multi", "system": "s1"})
    assert app._live_mode() == "multi"

    _seed_systems(app, [{"id": "s2", "name": "T2", "control": 461.0, "color_code": 1,
                         "channelmap": [{"lcn": 1, "freq": 461.0}]}])
    _client(app).post("/api/mode", json={"mode": "dmr", "system": "s2"})
    assert app._live_mode() == "dmr"




# --- ★ v0.14.0: טראנקינג הוא ברירת-מחדל רק למערכת רב-ערוצית -----------------

def test_single_channel_system_does_not_trunk_by_default(paths):
    """★ מערכת חד-ערוצית (16 מערכות סקר-השדה) היא תדר בודד — אין Rest-LSN
    לעקוב אחריו. עד v0.14.0 נכפה עליה `-T`, ו-DSD-FME נשלח לרדוף אחרי מפת-
    טראנקינג מדומה. עכשיו זה פשוט "כוונן ופענח"."""
    app = paths
    system = {"id": "one", "name": "ערוץ יחיד", "control": 164.325,
              "color_code": 6, "channelmap": [{"lcn": 1, "freq": 164.325}]}
    assert app._system_trunks(system) is False
    assert "DSD_TRUNK=0" in app.render_dmr_env(system)


def test_multi_channel_system_still_trunks_by_default(paths):
    app = paths
    system = {"id": "many", "name": "אשכול", "control": 164.10625, "color_code": 10,
              "channelmap": [{"lcn": 1, "freq": 164.10625}, {"lcn": 2, "freq": 164.3}]}
    assert app._system_trunks(system) is True
    assert "DSD_TRUNK=1" in app.render_dmr_env(system)


def test_explicit_trunk_field_overrides_the_default(paths):
    app = paths
    one = {"id": "a", "name": "כפוי", "control": 164.3, "color_code": 6,
           "channelmap": [{"lcn": 1, "freq": 164.3}], "trunk": "1"}
    many = {"id": "b", "name": "מבוטל", "control": 164.3, "color_code": 6,
            "channelmap": [{"lcn": 1, "freq": 164.3}, {"lcn": 2, "freq": 164.5}],
            "trunk": False}
    assert "DSD_TRUNK=1" in app.render_dmr_env(one)
    assert "DSD_TRUNK=0" in app.render_dmr_env(many)


def test_validate_systems_fills_in_trunk_flag(paths):
    app = paths
    ok, cleaned = app._validate_systems([
        {"id": "solo", "name": "יחיד", "control": 164.3, "color_code": 6,
         "channelmap": [{"lcn": 1, "freq": 164.3}]},
        {"id": "pair", "name": "זוג", "control": 164.3, "color_code": 6,
         "channelmap": [{"lcn": 1, "freq": 164.3}, {"lcn": 2, "freq": 164.5}]},
    ])
    assert ok
    assert cleaned[0]["trunk"] is False and cleaned[1]["trunk"] is True


def test_non_trunking_system_channelmap_is_not_lsn_doubled(paths, sysctl, no_sleep):
    """`lsn_pairs` קיים כדי להאכיל את מפת-הטראנקינג של DSD-FME. בלי טראנקינג
    הכפלת הערוץ היחיד לשתי שורות-LSN היא רק זיהום."""
    app = paths
    system = {"id": "solo", "name": "יחיד", "control": 164.325, "color_code": 6,
              "channelmap": [{"lcn": 1, "freq": 164.325}]}
    app._enter_dmr(system)
    rows = [r for r in app.CHANNELMAP_PATH.read_text().splitlines() if r.strip()]
    assert len(rows) == 2          # שורת-כותרת + ערוץ אחד (לא שניים)
