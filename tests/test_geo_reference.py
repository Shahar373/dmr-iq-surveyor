"""Reference registry: parsing an external P25 site snapshot and storing it."""

from __future__ import annotations

from pathlib import Path

import pytest

from dmr_iq_surveyor.reference.p25_sites import (
    ReferenceError,
    load_p25_site_csv,
    parse_p25_site_csv,
)
from dmr_iq_surveyor.reference.store import (
    connect_reference_database,
    import_snapshot,
    list_sites,
    list_snapshots,
)

HEADER = "wacn_hex,system_id_hex,rfss,site,observation_status,primary_cc_mhz,nac_hex,notes\n"
SAMPLE = HEADER + (
    "BEE00,37D,1,30,DIRECT,867.762500,37B,\n"
    "BEE00,37D,1,50,NEIGHBOR_ONLY,867.912500,,reuse\n"
    "BEE00,37D,1,82,NEIGHBOR_ONLY,867.912500,,reuse\n"
    "BEE00,37D,1,81,NEIGHBOR_ONLY,,,frequency never confirmed\n"
    "\n"
)


def test_parses_frequencies_to_exact_hertz() -> None:
    snapshot = parse_p25_site_csv(SAMPLE)
    frequencies = {
        record.site: record.control_frequency_hz for record in snapshot.records
    }
    assert frequencies[30] == 867_762_500.0
    # Exactly representable, not 867912499.9999999 -- downstream joins match
    # on this value.
    assert frequencies[50] == 867_912_500.0


def test_missing_frequency_is_none_not_zero_and_is_warned_about() -> None:
    snapshot = parse_p25_site_csv(SAMPLE)
    site81 = next(record for record in snapshot.records if record.site == 81)
    assert site81.control_frequency_hz is None
    assert any("BEE00:37D:1:81" in warning for warning in snapshot.warnings)


def test_reused_frequency_is_detected_and_warned_about() -> None:
    snapshot = parse_p25_site_csv(SAMPLE)
    reused = snapshot.reused_frequencies_hz()
    assert list(reused) == [867_912_500.0]
    assert reused[867_912_500.0] == ["BEE00:37D:1:50", "BEE00:37D:1:82"]
    assert any("cannot be attributed to one site" in w for w in snapshot.warnings)


def test_trailing_blank_line_is_not_an_error() -> None:
    assert len(parse_p25_site_csv(SAMPLE).records) == 4


def test_utf8_bom_is_stripped() -> None:
    assert len(parse_p25_site_csv("﻿" + SAMPLE).records) == 4


def test_missing_required_column_is_rejected() -> None:
    with pytest.raises(ReferenceError, match="missing required columns"):
        parse_p25_site_csv("wacn_hex,system_id_hex\nBEE00,37D\n")


def test_duplicate_site_row_is_rejected() -> None:
    duplicated = HEADER + (
        "BEE00,37D,1,30,DIRECT,867.762500,37B,\n" "BEE00,37D,1,30,DIRECT,867.762500,37B,\n"
    )
    with pytest.raises(ReferenceError, match="duplicate site"):
        parse_p25_site_csv(duplicated)


def test_non_numeric_identifier_is_rejected() -> None:
    with pytest.raises(ReferenceError, match="must be an integer"):
        parse_p25_site_csv(HEADER + "BEE00,37D,one,30,DIRECT,867.7625,37B,\n")


def test_unparseable_frequency_is_rejected() -> None:
    with pytest.raises(ReferenceError, match="primary_cc_mhz"):
        parse_p25_site_csv(HEADER + "BEE00,37D,1,30,DIRECT,not-a-number,37B,\n")


def test_empty_snapshot_is_rejected() -> None:
    with pytest.raises(ReferenceError, match="no usable site rows"):
        parse_p25_site_csv(HEADER)


def test_load_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "sites.csv"
    path.write_text(SAMPLE, encoding="utf-8")
    assert len(load_p25_site_csv(path).records) == 4


def test_import_is_idempotent_and_reports_sharing(tmp_path: Path) -> None:
    connection = connect_reference_database(tmp_path / "db.sqlite3")
    snapshot = parse_p25_site_csv(SAMPLE)
    first = import_snapshot(
        connection, snapshot_id="snap", snapshot=snapshot, source_path="sites.csv"
    )
    assert first["sites_created"] == 4
    assert first["channels_imported"] == 3
    assert first["sites_without_frequency"] == 1

    second = import_snapshot(
        connection, snapshot_id="snap", snapshot=snapshot, source_path="sites.csv"
    )
    assert second["sites_created"] == 0
    assert second["sites_updated"] == 4
    assert len(list_snapshots(connection)) == 1

    sites = {site["site_key"]: site for site in list_sites(connection)}
    assert len(sites) == 4
    assert sites["BEE00:37D:1:50"]["channels"][0]["sharing_site_count"] == 2
    assert sites["BEE00:37D:1:30"]["channels"][0]["sharing_site_count"] == 1
    assert sites["BEE00:37D:1:81"]["channels"] == []
    connection.close()


def test_reimport_without_a_frequency_removes_the_stale_channel(tmp_path: Path) -> None:
    connection = connect_reference_database(tmp_path / "db.sqlite3")
    import_snapshot(
        connection,
        snapshot_id="snap",
        snapshot=parse_p25_site_csv(HEADER + "BEE00,37D,1,30,DIRECT,867.762500,37B,\n"),
        source_path="a.csv",
    )
    import_snapshot(
        connection,
        snapshot_id="snap",
        snapshot=parse_p25_site_csv(HEADER + "BEE00,37D,1,30,NEIGHBOR_ONLY,,,retracted\n"),
        source_path="a.csv",
    )
    sites = list_sites(connection)
    assert sites[0]["channels"] == []
    assert sites[0]["observation_status"] == "NEIGHBOR_ONLY"
    connection.close()


def test_a_site_dropped_from_a_reimported_snapshot_is_removed_if_unmeasured(
    tmp_path: Path,
) -> None:
    """This is what an operator hits after accidentally importing example
    data under a real snapshot id and then re-importing the real file: the
    example sites must not linger in the registry forever."""
    connection = connect_reference_database(tmp_path / "db.sqlite3")
    import_snapshot(
        connection,
        snapshot_id="p25_sites_v1",
        snapshot=parse_p25_site_csv(
            "wacn_hex,system_id_hex,rfss,site,observation_status,primary_cc_mhz,nac_hex,notes\n"
            "ABCDE,123,1,1,DIRECT,866.012500,4A1,example\n"
        ),
        source_path="example.csv",
    )
    assert {site["site_key"] for site in list_sites(connection)} == {"ABCDE:123:1:1"}

    summary = import_snapshot(
        connection, snapshot_id="p25_sites_v1", snapshot=parse_p25_site_csv(SAMPLE), source_path="real.csv"
    )
    assert summary["sites_removed"] == ["ABCDE:123:1:1"]
    assert any("were removed from the registry" in warning for warning in summary["warnings"])

    site_keys = {site["site_key"] for site in list_sites(connection)}
    assert "ABCDE:123:1:1" not in site_keys
    assert site_keys == {"BEE00:37D:1:30", "BEE00:37D:1:50", "BEE00:37D:1:82", "BEE00:37D:1:81"}
    connection.close()


def test_a_site_dropped_from_a_reimported_snapshot_is_kept_if_it_has_measurements(
    tmp_path: Path,
) -> None:
    """A site the operator actually measured against must never be silently
    deleted just because a later snapshot stopped listing it."""
    database = tmp_path / "db.sqlite3"
    connection = connect_reference_database(database)
    import_snapshot(
        connection,
        snapshot_id="v1",
        snapshot=parse_p25_site_csv(
            "wacn_hex,system_id_hex,rfss,site,observation_status,primary_cc_mhz,nac_hex,notes\n"
            "ABCDE,123,1,9,DIRECT,866.012500,4A1,\n"
        ),
        source_path="a.csv",
    )
    site_id = connection.execute(
        "SELECT p25_site_id FROM p25_sites WHERE site_key = 'ABCDE:123:1:9'"
    ).fetchone()["p25_site_id"]
    # Simulate a real field measurement having been attached to this site,
    # without depending on geo/store.py (reference/ must not import it).
    connection.execute(
        "CREATE TABLE geo_measurements (p25_site_id INTEGER)"
    )
    connection.execute("INSERT INTO geo_measurements(p25_site_id) VALUES (?)", (site_id,))
    connection.commit()

    summary = import_snapshot(
        connection,
        snapshot_id="v1",
        snapshot=parse_p25_site_csv(
            "wacn_hex,system_id_hex,rfss,site,observation_status,primary_cc_mhz,nac_hex,notes\n"
            "ABCDE,123,1,10,DIRECT,867.012500,4A1,\n"
        ),
        source_path="b.csv",
    )
    assert summary["sites_removed"] == []
    assert summary["sites_retained_with_data"] == ["ABCDE:123:1:9"]
    assert any("KEPT because" in warning for warning in summary["warnings"])
    assert "ABCDE:123:1:9" in {site["site_key"] for site in list_sites(connection)}
    connection.close()


def test_reimporting_under_a_different_snapshot_id_never_removes_anything(
    tmp_path: Path,
) -> None:
    """Only a re-import under the SAME snapshot id can drop a site -- a
    second, independent snapshot must never delete the first one's sites."""
    connection = connect_reference_database(tmp_path / "db.sqlite3")
    import_snapshot(
        connection,
        snapshot_id="snap_a",
        snapshot=parse_p25_site_csv(HEADER + "BEE00,37D,1,30,DIRECT,867.762500,37B,\n"),
        source_path="a.csv",
    )
    summary = import_snapshot(
        connection,
        snapshot_id="snap_b",
        snapshot=parse_p25_site_csv(HEADER + "BEE00,37D,1,33,DIRECT,866.712500,377,\n"),
        source_path="b.csv",
    )
    assert summary["sites_removed"] == []
    assert {site["site_key"] for site in list_sites(connection)} == {
        "BEE00:37D:1:30",
        "BEE00:37D:1:33",
    }
    connection.close()


def test_existing_survey_database_extends_cleanly(tmp_path: Path) -> None:
    from dmr_iq_surveyor.survey.store import connect_survey_database

    path = tmp_path / "db.sqlite3"
    survey = connect_survey_database(path)
    survey.execute("INSERT INTO rf_frequencies(nominal_frequency_hz) VALUES (866000000)")
    survey.commit()
    survey.close()

    connection = connect_reference_database(path)
    assert connection.execute("SELECT COUNT(*) FROM rf_frequencies").fetchone()[0] == 1
    columns = {row[1] for row in connection.execute("PRAGMA table_info(rf_frequencies)")}
    # The frequency catalog stays protocol-neutral: the site relation lives
    # in p25_site_channels, never as a column here.
    assert not columns & {"protocol", "system", "site", "role", "p25_site_id"}
    connection.close()
