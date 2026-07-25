"""מודיעין-מערכת (system_intel): אתרים, מפת-תפוסה, CDR שיחות-יחיד, סחיפת-CC.
כל הפונקציות טהורות (מקבלות ערכים ישירות) => נבדקות בלי חומרה/UDP."""


def test_record_site_accumulates_count_and_times(paths):
    import system_intel
    system_intel.record_site("s1", 2, t=100.0)
    system_intel.record_site("s1", 2, t=110.0)
    p = system_intel.export_for("s1")
    assert p["sites"]["2"] == {"first_seen": 100.0, "last_seen": 110.0, "count": 2}


def test_record_site_tracks_multiple_sites_separately(paths):
    import system_intel
    system_intel.record_site("s1", 1, t=10.0)
    system_intel.record_site("s1", 2, t=20.0)
    p = system_intel.export_for("s1")
    assert set(p["sites"]) == {"1", "2"}


def test_record_lsn_status_only_updates_last_change_on_real_change(paths):
    import system_intel
    system_intel.record_lsn_status("s1", {6: "idle"}, t=10.0)
    system_intel.record_lsn_status("s1", {6: "idle"}, t=20.0)   # same occupant
    p = system_intel.export_for("s1")
    assert p["lsn_directory"]["6"] == {"occupant": "idle", "last_seen": 20.0, "last_change": 10.0}
    system_intel.record_lsn_status("s1", {6: 64250}, t=30.0)    # real change
    p = system_intel.export_for("s1")
    assert p["lsn_directory"]["6"] == {"occupant": 64250, "last_seen": 30.0, "last_change": 30.0}


def test_record_lsn_status_multiple_channels_in_one_line(paths):
    import system_intel
    system_intel.record_lsn_status("s1", {5: 223, 6: 64250, 7: "idle", 8: "idle"}, t=1.0)
    p = system_intel.export_for("s1")
    assert set(p["lsn_directory"]) == {"5", "6", "7", "8"}
    assert p["lsn_directory"]["6"]["occupant"] == 64250


def test_record_private_call_ring_buffer_caps_at_max(paths):
    """הבאפר הפנימי (לא רק תצוגת ה-export המקוצרת ל-20) מוגבל ל-
    PRIVATE_CALLS_MAX -- לא לוג-אינסופי שגדל עד שוחק-דיסק."""
    import system_intel
    for i in range(system_intel.PRIVATE_CALLS_MAX + 10):
        system_intel.record_private_call("s1", src=100 + i, tgt=200, kind="csbk", t=float(i))
    assert len(system_intel._intel["s1"]["private_calls"]) == system_intel.PRIVATE_CALLS_MAX
    assert system_intel._intel["s1"]["private_calls"][-1]["src"] == 100 + system_intel.PRIVATE_CALLS_MAX + 9


def test_export_for_private_calls_newest_first_limited_to_20(paths):
    import system_intel
    for i in range(30):
        system_intel.record_private_call("s1", src=i, tgt=999, kind="data", t=float(i))
    p = system_intel.export_for("s1")
    assert len(p["private_calls"]) == 20
    assert p["private_calls"][0]["src"] == 29   # newest first
    assert p["private_calls"][-1]["src"] == 10


def test_record_private_call_requires_target(paths):
    import system_intel
    system_intel.record_private_call("s1", src=1, tgt=None, kind="csbk")
    assert system_intel.export_for("s1")["private_calls"] == []


def test_record_cc_detects_mismatch(paths):
    import system_intel
    system_intel.record_cc("s1", observed_cc=2, configured_cc=1, t=5.0)
    p = system_intel.export_for("s1")
    assert p["cc"] == {"observed": 2, "configured": 1, "mismatch": True, "last_seen": 5.0}


def test_record_cc_no_mismatch_when_equal(paths):
    import system_intel
    system_intel.record_cc("s1", observed_cc=1, configured_cc=1)
    assert system_intel.export_for("s1")["cc"]["mismatch"] is False


def test_record_cc_no_mismatch_flag_when_configured_unknown(paths):
    """אין color_code מוגדר (None) => לא ניתן לקבוע mismatch -- False, לא ניחוש."""
    import system_intel
    system_intel.record_cc("s1", observed_cc=3, configured_cc=None)
    assert system_intel.export_for("s1")["cc"]["mismatch"] is False


def test_export_for_unknown_system_returns_blank_profile_not_none(paths):
    import system_intel
    p = system_intel.export_for("never-seen")
    assert p == {"sites": {}, "lsn_directory": {}, "cc": None, "private_calls": [],
                 "lsn_freq": {}, "lsn_freq_seen": None,
                 "lsn_map": {}, "lsn_channelmap": []}


def test_record_ignores_none_system_id(paths):
    import system_intel
    system_intel.record_site(None, 1)
    system_intel.record_lsn_status(None, {1: "idle"})
    assert system_intel._intel == {}


def test_maybe_flush_debounces_rapid_writes(paths):
    """אחרי flush ראשון (שקובע _last_flush לזמן-אמת), עדכון-נוסף מיידי לא
    כותב שוב לדיסק (מונע שחיקת-SD מ-lsn_status התכוף) -- אבל הזיכרון-הפנימי
    כן מתעדכן מיד; רק force=True כותב את המצב הטרי בפועל."""
    import system_intel
    system_intel.record_site("s1", 1, t=1.0)
    system_intel.maybe_flush(force=True)             # קובע _last_flush = עכשיו
    assert system_intel.INTEL_PATH.exists()
    mtime_before = system_intel.INTEL_PATH.stat().st_mtime

    system_intel.record_site("s1", 2, t=2.0)          # מסמן dirty, אבל בתוך חלון ה-debounce
    assert system_intel.INTEL_PATH.stat().st_mtime == mtime_before   # לא נכתב מחדש
    assert "2" in system_intel.export_for("s1")["sites"]              # אבל הזיכרון עדכני

    system_intel.maybe_flush(force=True)
    assert system_intel.INTEL_PATH.stat().st_mtime >= mtime_before


def test_load_roundtrips_after_forced_flush(paths):
    import system_intel
    system_intel.record_site("s1", 2, t=1.0)
    system_intel.maybe_flush(force=True)
    system_intel._intel.clear()
    system_intel.load()
    assert system_intel.export_for("s1")["sites"]["2"]["count"] == 1


def test_load_handles_missing_file_silently(paths):
    import system_intel
    system_intel.load()
    assert system_intel._intel == {}


def test_load_handles_corrupt_file_silently(paths):
    import system_intel
    system_intel.INTEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    system_intel.INTEL_PATH.write_text("not json{{{")
    system_intel.load()
    assert system_intel._intel == {}


# --- ★ מיפוי LSN↔תדר (v0.12.0) -----------------------------------------------
# הלוגיקה הטהורה: הצבעות → הכרעה → channelmap. בלי UDP/Flask/חומרה.
def test_derive_lsn_map_needs_min_votes():
    """תצפית בודדת אינה מפה — מתחת ל-LSN_FREQ_MIN_VOTES לא מכריעים בכלל."""
    import system_intel
    assert system_intel.derive_lsn_map({"5": {"164106250": 2}}) == {}


def test_derive_lsn_map_decides_and_infers_pair():
    """3 הצבעות עקביות ל-LSN 5 => הכרעה (source=rest), וה-LSN השותף (6)
    מוסק לאותו תדר (source=pair) — ב-Cap+ זוג LSN חולק ערוץ פיזי אחד."""
    import system_intel
    m = system_intel.derive_lsn_map({"5": {"164106250": 4}})
    assert m[5]["freq_hz"] == 164_106_250
    assert m[5]["source"] == "rest" and m[5]["confidence"] == 1.0
    assert m[5]["physical_channel"] == 3        # LSN 5,6 => ערוץ פיזי 3
    assert m[6]["freq_hz"] == 164_106_250
    assert m[6]["source"] == "pair" and m[6]["votes"] == 0
    assert m[6]["physical_channel"] == 3


def test_derive_lsn_map_rejects_when_no_dominance():
    """הצבעות מפוצלות ~50/50 => אין רוב ברור, לא מכריעים (לא בוחרים 'הכי גדול')."""
    import system_intel
    m = system_intel.derive_lsn_map({"3": {"164300000": 5, "164325000": 4}})
    assert m == {}


def test_derive_lsn_map_flags_pair_conflict():
    """שני חצאי-זוג הוכרעו לתדרים שונים => הנחת-המבנה שבורה. מסמנים
    pair_conflict ולא 'מתקנים' בשקט, ו-lsn_map_to_channelmap משמיט אותם."""
    import system_intel
    m = system_intel.derive_lsn_map({"3": {"164300000": 9}, "4": {"164725000": 9}})
    assert m[3]["pair_conflict"] and m[4]["pair_conflict"]
    assert system_intel.lsn_map_to_channelmap(m) == []


def test_lsn_map_to_channelmap_builds_physical_channels():
    """המיפוי המוכרע → channelmap פיזי: lcn = מספר הערוץ הפיזי האמיתי
    (נגזר מה-LSN), התדר ב-MHz, ממוין. זוג LSN = שורה אחת."""
    import system_intel
    votes = {"1": {"164106250": 5}, "5": {"164537500": 5}}
    cmap = system_intel.lsn_map_to_channelmap(system_intel.derive_lsn_map(votes))
    assert cmap == [{"lcn": 1, "freq": 164.10625}, {"lcn": 3, "freq": 164.5375}]


def test_lsn_map_to_channelmap_output_passes_systems_validation(paths):
    """הפלט חייב לעבור את _validate_systems — זו הצורה שנכתבת ל-systems.json."""
    app = paths
    import system_intel
    cmap = system_intel.lsn_map_to_channelmap(
        system_intel.derive_lsn_map({"1": {"164106250": 5}, "3": {"164300000": 5}}))
    ok, cleaned = app._validate_systems(
        [{"id": "s1", "name": "T", "control": 164.10625, "color_code": 10,
          "channelmap": cmap}])
    assert ok and cleaned[0]["channelmap"] == cmap


def test_record_rest_channel_ignores_single_channel_mode(paths):
    """phys_freq_hz=None (חד-ערוצי — אין ground-truth) => אין הצבעה בכלל."""
    import system_intel
    system_intel.record_rest_channel("s1", 5, None, t=1.0)
    assert system_intel.export_for("s1")["lsn_freq"] == {}


def test_record_rest_channel_rejects_out_of_range(paths):
    import system_intel
    system_intel.record_rest_channel("s1", 0, 164_106_250, t=1.0)
    system_intel.record_rest_channel("s1", 999, 164_106_250, t=1.0)
    system_intel.record_rest_channel("s1", 5, 0, t=1.0)
    assert system_intel.export_for("s1")["lsn_freq"] == {}


def test_record_lsn_status_votes_only_for_the_rest_lsn(paths):
    """מתוך שורת lsn_status שלמה, רק ה-LSN שמסומן 'rest' מקבל הצבעה —
    התפוסים/הפנויים יושבים על תדרים אחרים שאיננו יודעים מהשורה הזו."""
    import system_intel
    channels = {5: "rest", 6: 3, 7: "idle", 8: "idle"}
    for _ in range(3):
        system_intel.record_lsn_status("s1", channels, t=1.0, phys_freq_hz=164_537_500)
    intel = system_intel.export_for("s1")
    assert intel["lsn_freq"] == {"5": {"164537500": 3}}
    assert intel["lsn_map"]["5"]["freq_hz"] == 164_537_500
    assert intel["lsn_map"]["6"]["source"] == "pair"


def test_record_lsn_status_without_phys_freq_records_no_votes(paths):
    """אותה שורה בחד-ערוצי: מפת-התפוסה נשמרת כרגיל, מיפוי-תדר לא נוצר."""
    import system_intel
    system_intel.record_lsn_status("s1", {5: "rest", 6: 3}, t=1.0)
    intel = system_intel.export_for("s1")
    assert intel["lsn_directory"]["5"]["occupant"] == "rest"
    assert intel["lsn_freq"] == {}


def test_profile_upgrades_schema_from_older_file(paths):
    """system_intel.json שנכתב ע"י v0.11.0 (בלי lsn_freq) — record_* לא קורס."""
    import json
    import system_intel
    system_intel.INTEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    system_intel.INTEL_PATH.write_text(json.dumps(
        {"s1": {"sites": {}, "lsn_directory": {}, "cc": None, "private_calls": []}}))
    system_intel.load()
    system_intel.record_rest_channel("s1", 5, 164_106_250, t=1.0)
    assert system_intel.export_for("s1")["lsn_freq"] == {"5": {"164106250": 1}}
