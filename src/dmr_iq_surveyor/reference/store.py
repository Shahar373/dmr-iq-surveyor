"""Persist an external P25 site snapshot into the shared SQLite database.

Additive only: `connect_reference_database()` opens the existing survey
database first (which itself opens the DMR inventory database first) and
then creates the reference tables if they are absent. No pre-existing table
is altered, and `rf_frequencies` in particular stays protocol-neutral -- the
site-to-frequency relation lives in `p25_site_channels` here, never as a
column on the frequency catalog.

Idempotency matches the rest of the project: re-importing a `snapshot_id`
replaces exactly that snapshot's rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dmr_iq_surveyor.reference.p25_sites import (
    CHANNEL_EVIDENCE_SNAPSHOT,
    CHANNEL_ROLE_PRIMARY_CONTROL,
    P25SiteSnapshot,
)
from dmr_iq_surveyor.survey.store import connect_survey_database

REFERENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS reference_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT,
    imported_at TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS p25_systems (
    p25_system_id INTEGER PRIMARY KEY AUTOINCREMENT,
    wacn_hex TEXT NOT NULL,
    system_id_hex TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    UNIQUE(wacn_hex, system_id_hex)
);
CREATE TABLE IF NOT EXISTS p25_sites (
    p25_site_id INTEGER PRIMARY KEY AUTOINCREMENT,
    p25_system_id INTEGER NOT NULL REFERENCES p25_systems(p25_system_id),
    rfss INTEGER NOT NULL,
    site INTEGER NOT NULL,
    site_key TEXT NOT NULL UNIQUE,
    observation_status TEXT NOT NULL,
    nac_hex TEXT,
    notes TEXT NOT NULL DEFAULT '',
    snapshot_id TEXT REFERENCES reference_snapshots(snapshot_id),
    UNIQUE(p25_system_id, rfss, site)
);
CREATE TABLE IF NOT EXISTS p25_site_channels (
    p25_site_channel_id INTEGER PRIMARY KEY AUTOINCREMENT,
    p25_site_id INTEGER NOT NULL REFERENCES p25_sites(p25_site_id) ON DELETE CASCADE,
    frequency_hz REAL NOT NULL,
    role TEXT NOT NULL,
    evidence TEXT NOT NULL,
    snapshot_id TEXT REFERENCES reference_snapshots(snapshot_id),
    UNIQUE(p25_site_id, frequency_hz, role)
);
CREATE INDEX IF NOT EXISTS idx_p25_site_channels_frequency
    ON p25_site_channels(frequency_hz);
"""


def connect_reference_database(path: str | Path) -> sqlite3.Connection:
    connection = connect_survey_database(path)
    connection.executescript(REFERENCE_SCHEMA)
    connection.commit()
    return connection


def _find_or_create_system(
    connection: sqlite3.Connection, wacn_hex: str, system_id_hex: str
) -> int:
    row = connection.execute(
        "SELECT p25_system_id FROM p25_systems WHERE wacn_hex = ? AND system_id_hex = ?",
        (wacn_hex, system_id_hex),
    ).fetchone()
    if row is not None:
        return int(row["p25_system_id"])
    cursor = connection.execute(
        "INSERT INTO p25_systems(wacn_hex, system_id_hex) VALUES (?, ?)",
        (wacn_hex, system_id_hex),
    )
    return int(cursor.lastrowid)


def import_snapshot(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    snapshot: P25SiteSnapshot,
    source_path: str,
    source_sha256: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Store one reference snapshot, replacing any prior import under the
    same `snapshot_id`.

    Sites are keyed by identity, not by row order, so re-importing a
    corrected snapshot updates a site in place and keeps every
    `geo_measurements` row that already points at it. Channels contributed
    by *this* snapshot are replaced wholesale, so a frequency removed from
    the source is removed here too rather than lingering forever.
    """
    connection.execute("DELETE FROM p25_site_channels WHERE snapshot_id = ?", (snapshot_id,))
    # Upserted rather than deleted and re-inserted: p25_sites rows from a
    # previous import of this snapshot still reference it, and this
    # database has foreign keys enforced, so deleting the parent row would
    # fail. Upserting also keeps every geo_measurements row that already
    # points at those sites.
    connection.execute(
        """
        INSERT INTO reference_snapshots(
            snapshot_id, source_kind, source_path, source_sha256,
            imported_at, row_count, warnings_json, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_id) DO UPDATE SET
            source_kind = excluded.source_kind,
            source_path = excluded.source_path,
            source_sha256 = excluded.source_sha256,
            imported_at = excluded.imported_at,
            row_count = excluded.row_count,
            warnings_json = excluded.warnings_json,
            notes = excluded.notes
        """,
        (
            snapshot_id,
            snapshot.source_kind,
            source_path,
            source_sha256,
            datetime.now(UTC).isoformat(),
            len(snapshot.records),
            json.dumps(snapshot.warnings),
            notes,
        ),
    )

    sites_created = 0
    sites_updated = 0
    channels = 0
    for record in snapshot.records:
        system_id = _find_or_create_system(connection, record.wacn_hex, record.system_id_hex)
        existing = connection.execute(
            "SELECT p25_site_id FROM p25_sites WHERE site_key = ?", (record.site_key,)
        ).fetchone()
        if existing is None:
            cursor = connection.execute(
                """
                INSERT INTO p25_sites(
                    p25_system_id, rfss, site, site_key, observation_status,
                    nac_hex, notes, snapshot_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    system_id,
                    record.rfss,
                    record.site,
                    record.site_key,
                    record.observation_status,
                    record.nac_hex,
                    record.notes,
                    snapshot_id,
                ),
            )
            p25_site_id = int(cursor.lastrowid)
            sites_created += 1
        else:
            p25_site_id = int(existing["p25_site_id"])
            connection.execute(
                """
                UPDATE p25_sites SET
                    observation_status = ?, nac_hex = ?, notes = ?, snapshot_id = ?
                WHERE p25_site_id = ?
                """,
                (
                    record.observation_status,
                    record.nac_hex,
                    record.notes,
                    snapshot_id,
                    p25_site_id,
                ),
            )
            sites_updated += 1

        if record.control_frequency_hz is not None:
            connection.execute(
                """
                INSERT OR REPLACE INTO p25_site_channels(
                    p25_site_id, frequency_hz, role, evidence, snapshot_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    p25_site_id,
                    record.control_frequency_hz,
                    CHANNEL_ROLE_PRIMARY_CONTROL,
                    CHANNEL_EVIDENCE_SNAPSHOT,
                    snapshot_id,
                ),
            )
            channels += 1

    # A site present in an EARLIER import of this same snapshot_id but absent
    # from this one is stale: `snapshot_id` on `p25_sites` is only ever set by
    # whichever import last touched that row, so a row still carrying this
    # id that this loop did not just process was dropped by the source, not
    # merely left unchanged. Left in place, it lingers forever -- reported
    # `frequency_unknown` alongside sites that never had a channel at all,
    # and clutters `geo sites` and every future solve with rows that no
    # longer belong.
    #
    # It is removed only when nothing has actually measured against it. A
    # site with real field evidence is never deleted here, even if the
    # operator's snapshot stopped listing it -- that decision needs a human,
    # so it is reported instead.
    touched_site_keys = {record.site_key for record in snapshot.records}
    protected_site_ids = _site_ids_with_measurements(connection)
    removed_sites: list[str] = []
    retained_with_data: list[str] = []
    for row in connection.execute(
        "SELECT p25_site_id, site_key FROM p25_sites WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchall():
        if row["site_key"] in touched_site_keys:
            continue
        if int(row["p25_site_id"]) in protected_site_ids:
            retained_with_data.append(row["site_key"])
            continue
        connection.execute("DELETE FROM p25_sites WHERE p25_site_id = ?", (row["p25_site_id"],))
        removed_sites.append(row["site_key"])

    warnings = list(snapshot.warnings)
    if removed_sites:
        warnings.append(
            f"{len(removed_sites)} site(s) no longer in this snapshot were removed from the "
            f"registry (had no measurements): {', '.join(sorted(removed_sites))}"
        )
    if retained_with_data:
        warnings.append(
            f"{len(retained_with_data)} site(s) no longer in this snapshot were KEPT because "
            f"they already have measurements: {', '.join(sorted(retained_with_data))}"
        )

    connection.commit()
    return {
        "snapshot_id": snapshot_id,
        "sites_created": sites_created,
        "sites_updated": sites_updated,
        "channels_imported": channels,
        "sites_without_frequency": len(snapshot.records) - channels,
        "sites_removed": sorted(removed_sites),
        "sites_retained_with_data": sorted(retained_with_data),
        "warnings": warnings,
    }


def _site_ids_with_measurements(connection: sqlite3.Connection) -> set[int]:
    """Site ids already attached to a geo_measurements row, if that table
    exists.

    `reference/store.py` must not import `geo/store.py` -- geo depends on
    reference, not the reverse -- so this checks defensively instead of
    importing its schema. A connection that has never gone through
    `geo.store.connect_geo_database()` has no such table, which correctly
    means nothing has measured against any site yet.
    """
    try:
        rows = connection.execute("SELECT DISTINCT p25_site_id FROM geo_measurements")
    except sqlite3.OperationalError:
        return set()
    return {int(row[0]) for row in rows}


def list_sites(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every registry site with its channels and how many other sites share
    each of those channels.

    `sharing_site_count` is computed here rather than at every call site
    because it is what decides whether a measurement on that frequency can
    be attributed to one site at all.
    """
    rows = connection.execute(
        """
        SELECT
            s.p25_site_id, s.rfss, s.site, s.site_key, s.observation_status,
            s.nac_hex, s.notes, y.wacn_hex, y.system_id_hex
        FROM p25_sites s
        JOIN p25_systems y ON y.p25_system_id = s.p25_system_id
        ORDER BY y.wacn_hex, y.system_id_hex, s.rfss, s.site
        """
    ).fetchall()
    sites = [dict(row) for row in rows]
    channel_rows = connection.execute(
        """
        SELECT
            c.p25_site_id, c.frequency_hz, c.role, c.evidence,
            (
                SELECT COUNT(DISTINCT o.p25_site_id)
                FROM p25_site_channels o
                WHERE o.frequency_hz = c.frequency_hz AND o.role = c.role
            ) AS sharing_site_count
        FROM p25_site_channels c
        ORDER BY c.frequency_hz
        """
    ).fetchall()
    by_site: dict[int, list[dict[str, Any]]] = {}
    for row in channel_rows:
        by_site.setdefault(int(row["p25_site_id"]), []).append(dict(row))
    for site in sites:
        site["channels"] = by_site.get(int(site["p25_site_id"]), [])
    return sites


def list_snapshots(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM reference_snapshots ORDER BY imported_at ASC"
        )
    ]


__all__ = [
    "REFERENCE_SCHEMA",
    "connect_reference_database",
    "import_snapshot",
    "list_sites",
    "list_snapshots",
]
