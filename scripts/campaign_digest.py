#!/usr/bin/env python3
"""Print a short, pasteable summary of everything a campaign has measured.

Written to be READ, not parsed: after a drive, one command produces a page
of text that says what was collected, what it was worth, and what was
thrown away and why. Small enough to paste into a conversation, which is
the whole point -- the GeoJSON export is the machine-readable artefact and
is far too large to discuss.

    python scripts/campaign_digest.py [--database PATH]

Reads only. It never solves, never re-materialises measurements, and never
writes to the database, so it is safe to run at any moment -- including
while a drive is still running.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from dmr_iq_surveyor.geo.model import haversine_m
from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.survey.pipeline import DEFAULT_DATABASE_PATH


def _rows(connection: sqlite3.Connection, sql: str, *args) -> list[sqlite3.Row]:
    return list(connection.execute(sql, args).fetchall())


def _settings(row: sqlite3.Row) -> dict:
    try:
        return json.loads(row["settings_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def stops(connection: sqlite3.Connection) -> None:
    runs = _rows(
        connection,
        "SELECT r.survey_run_id, r.capture_start_utc, r.coverage_status, r.gps_latitude, "
        "       r.gps_longitude, r.analyzed_seconds, r.segment_count, r.settings_json, "
        "       r.sample_rate_hz, r.center_frequency_hz, s.gain "
        "FROM survey_runs r LEFT JOIN sites s ON s.site_id = r.site_id "
        "ORDER BY COALESCE(r.capture_start_utc, r.imported_at)",
    )
    live = [row for row in runs if _settings(row).get("mode") == "live"]
    fixed = [row for row in runs if _settings(row).get("mode") != "live"]

    print("== WHAT WAS COLLECTED " + "=" * 47)
    print(f"  stops total          {len(runs)}   ({len(fixed)} recorded, {len(live)} drive bins)")
    if runs:
        first = runs[0]["capture_start_utc"] or "?"
        last = runs[-1]["capture_start_utc"] or "?"
        print(f"  first / last         {first[:16]}  ->  {last[:16]}")

    # Gain discipline: levels measured at different gain are not comparable,
    # and that is the assumption the whole method rests on. Only the IF gain
    # reduction is stored per stop -- the LNA state is applied to the radio
    # but never written to the database, so a campaign that changed LNA state
    # between stops would not be caught here. Said plainly rather than
    # implied, because a check that silently covers half the setting is worse
    # than no check.
    gains = Counter(row["gain"] for row in runs if row["gain"] is not None)
    if len(gains) > 1:
        print("  !! IF GAIN VARIES ACROSS STOPS -- levels are not comparable:")
        for gain, count in gains.most_common():
            print(f"       IFGR {gain} dB: {count} stop(s)")
    elif gains:
        gain, _count = gains.most_common(1)[0]
        print(f"  gain (all stops)     IFGR {gain} dB   (LNA state is not recorded per stop)")

    rates = Counter(row["sample_rate_hz"] for row in runs)
    print(
        "  sample rates         "
        + ", ".join(f"{rate / 1e6:g} MS/s x{count}" for rate, count in rates.most_common())
    )
    partial = [row for row in runs if row["coverage_status"] != "complete"]
    if partial:
        print(f"  partial coverage     {len(partial)} stop(s) did not cover the whole band")

    if live:
        spans = Counter(round(_settings(row).get("bin_size_m") or 0) for row in live)
        spreads = [_settings(row).get("position_spread_m") or 0.0 for row in live]
        sessions = Counter(_settings(row).get("session_id") for row in live)
        lats = [row["gps_latitude"] for row in live if row["gps_latitude"] is not None]
        lons = [row["gps_longitude"] for row in live if row["gps_longitude"] is not None]
        print()
        print(f"  drives               {len(sessions)} session(s): "
              + ", ".join(f"{name} ({count} bins)" for name, count in sessions.most_common()))
        print("  measurement spans    "
              + ", ".join(f"{span} m x{count}" for span, count in sorted(spans.items())))
        if spreads:
            print(f"  position spread      median {sorted(spreads)[len(spreads) // 2]:.0f} m "
                  f"(max {max(spreads):.0f} m)")
        if len(lats) > 1:
            extent_n = haversine_m(min(lats), lons[0], max(lats), lons[0]) / 1000.0
            extent_e = haversine_m(lats[0], min(lons), lats[0], max(lons)) / 1000.0
            print(f"  area driven          {extent_n:.1f} km N-S x {extent_e:.1f} km E-W")
        windows = sum(row["segment_count"] or 0 for row in live)
        print(f"  windows measured     {windows} (~{windows / 60:.0f} minutes of listening)")


def measurements(connection: sqlite3.Connection) -> None:
    rows = _rows(
        connection,
        "SELECT usability, attribution, detected, COUNT(*) AS n FROM geo_measurements "
        "GROUP BY usability, attribution, detected",
    )
    print()
    print("== WHAT COUNTED AS EVIDENCE " + "=" * 41)
    if not rows:
        print("  none -- run `dmr-surveyor geo measurements` first")
        return
    usable_det = sum(r["n"] for r in rows if r["usability"] == "usable" and r["detected"])
    usable_non = sum(r["n"] for r in rows if r["usability"] == "usable" and not r["detected"])
    print(f"  usable               {usable_det} detection(s), {usable_non} non-detection(s)")
    dropped = Counter()
    for row in rows:
        if row["usability"] != "usable":
            dropped[row["usability"]] += row["n"]
    for reason, count in dropped.most_common():
        print(f"  {reason:20s} {count} measurement(s) set aside")

    excluded = _rows(connection, "SELECT survey_run_id, reason, scope FROM geo_run_exclusions")
    for row in excluded:
        scope = "" if row["scope"] == "all" else f" [{row['scope']} only]"
        print(f"  excluded stop        {row['survey_run_id']}: {row['reason']}{scope}")


def solutions(connection: sqlite3.Connection) -> None:
    latest = connection.execute(
        "SELECT solve_batch_id FROM geo_solutions ORDER BY solved_at DESC LIMIT 1"
    ).fetchone()
    print()
    print("== WHAT THE SOLVER CONCLUDED " + "=" * 40)
    if latest is None:
        print("  nothing solved yet -- run `dmr-surveyor geo solve`")
        return
    rows = _rows(
        connection,
        "SELECT s.*, p.site_key FROM geo_solutions s "
        "JOIN p25_sites p ON p.p25_site_id = s.p25_site_id "
        "WHERE s.solve_batch_id = ? ORDER BY s.status, p.site_key",
        latest["solve_batch_id"],
    )
    solved = [r for r in rows if r["status"] == "ok"]
    # The resolution and other settings a batch was solved at can differ from
    # what an operator most recently typed: this is the LATEST stored batch,
    # which may be an automatic solve the field app ran after a drive (at
    # its own, coarser default) rather than a `geo solve` invocation that was
    # interrupted before it could write anything -- SQLite writes nothing
    # until the whole batch finishes, so a cancelled solve leaves no trace at
    # all, and this always shows the last one that actually completed.
    batch_settings = json.loads(rows[0]["settings_json"] or "{}") if rows else {}
    resolution = batch_settings.get("resolution_m")
    print(f"  batch {latest['solve_batch_id']}"
          + (f"  (resolution {resolution:g} m)" if resolution else "")
          + f"   {len(solved)} of {len(rows)} site(s) produced a bounded region")
    for row in rows:
        if not (row["detection_count"] or row["non_detection_count"]):
            continue
        print()
        print(f"  {row['site_key']}  [{row['status']}]"
              + (f"  {row['status_reason']}" if row["status_reason"] else ""))
        print(f"      evidence      {row['detection_count']} detection(s), "
              f"{row['non_detection_count']} non-detection(s), "
              f"{row['excluded_count']} set aside")
        if row["mode_latitude"] is not None:
            print(f"      most likely   {row['mode_latitude']:.5f}, {row['mode_longitude']:.5f}"
                  "   (a search area's peak, NOT a transmitter coordinate)")
        if row["area_km2_90"] is not None:
            print(f"      region        50%: {row['area_km2_50']:.2f} km2   "
                  f"90%: {row['area_km2_90']:.2f} km2")
        bits = []
        if row["path_loss_exponent"] is not None:
            bits.append(f"n={row['path_loss_exponent']:.2f}")
        if row["reference_level_db"] is not None:
            bits.append(f"P0={row['reference_level_db']:.1f} dB")
        try:
            fading = json.loads(row["diagnostics_json"] or "{}").get("shadow_fading", {})
            if fading.get("best_sigma_db") is not None:
                bits.append(f"sigma={fading['best_sigma_db']:.0f} dB")
        except (TypeError, json.JSONDecodeError):
            pass
        if row["residual_rms_db"] is not None:
            bits.append(f"residual RMS={row['residual_rms_db']:.1f} dB")
        if row["azimuth_span_deg"] is not None:
            bits.append(f"seen across {row['azimuth_span_deg']:.0f} deg of bearing")
        if bits:
            print("      fit           " + ", ".join(bits))
        warnings = json.loads(row["warnings_json"] or "[]")
        for warning in warnings:
            print(f"      ! {warning}")


def plan(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT status, reason, plan_json FROM geo_plans ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    print()
    print("== WHERE TO GO NEXT " + "=" * 49)
    if row is None:
        print("  no plan yet -- it is written by `geo solve`")
        return
    print(f"  {row['status']}: {row['reason']}")
    # Rank is the position in the list, which is how the map numbers them --
    # the stored entries carry no rank of their own.
    stops_ = json.loads(row["plan_json"] or "{}").get("top_stops", [])
    for rank, entry in enumerate(stops_[:5], start=1):
        helps = ", ".join(h["site_key"] for h in entry.get("helps_most", []))
        print(f"    {rank}. {entry['latitude']:.5f}, {entry['longitude']:.5f}"
              f"   value {entry['value']:.2f}   helps: {helps or '-'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    arguments = parser.parse_args()
    if not Path(arguments.database).expanduser().is_file():
        raise SystemExit(f"no database at {arguments.database}")
    connection = connect_geo_database(arguments.database)
    try:
        print(f"campaign digest for {arguments.database}")
        print()
        stops(connection)
        measurements(connection)
        solutions(connection)
        plan(connection)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
