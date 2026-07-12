#!/usr/bin/env python3
# ============================================================================
#  DMR  -  ייצוא פיד השיחות (CSV/JSON)
# ----------------------------------------------------------------------------
#  מנגנון הייצוא של AIR-AM (_export_response) — CSV עם BOM ל-Excel (עברית ב-
#  category נכונה) או JSON מסודר. עמודות לפי cols; שדות מחושבים: time_iso,
#  timestamp, alg_name (מתוך enc), text (ניקוי newlines).
# ============================================================================
import csv
import io
import json
import time


def _cell(rec, col):
    """ערך תא לעמודה נתונה (כולל שדות מחושבים)."""
    t = rec.get("t")
    if col == "time_iso":
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
    if col == "timestamp":
        return t
    if col == "text":
        return (rec.get("text") or "").replace("\r", " ").replace("\n", " ")
    if col == "alg_name":
        enc = rec.get("enc") or {}
        return enc.get("alg_name") if isinstance(enc, dict) else None
    return rec.get(col)


def export_response(app, request, recs, cols, basename):
    """בונה תגובת ייצוא (CSV עם BOM / JSON) מרשומות מנורמלות."""
    fmt = (request.args.get("format") or "csv").lower()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if fmt == "json":
        resp = app.response_class(json.dumps(recs, ensure_ascii=False, indent=1),
                                  mimetype="application/json")
        fname = f"{basename}-{stamp}.json"
    else:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in recs:
            w.writerow([_cell(r, c) for c in cols])
        # BOM => Excel מזהה UTF-8 ומציג עברית נכון
        resp = app.response_class("﻿" + buf.getvalue(),
                                  mimetype="text/csv; charset=utf-8")
        fname = f"{basename}-{stamp}.csv"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    resp.headers["Cache-Control"] = "no-store"
    return resp
