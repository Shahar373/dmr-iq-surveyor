from __future__ import annotations

import json
from pathlib import Path

from dmr_iq_surveyor.inventory.standalone import import_standalone_log


def test_standalone_log_import_builds_inventory_and_preserves_metadata(
    tmp_path: Path,
) -> None:
    log = tmp_path / "dsd.log"
    lines = [
        (
            f"20:00:0{index} Sync: +DMR [slot1] slot2 "
            "| Color Code=08 | CSBK"
        )
        for index in range(6)
    ]
    lines.append("TG: 1234 SRC: 5678")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output = tmp_path / "standalone"
    result = import_standalone_log(
        log,
        output,
        frequency_hz=164_537_500.0,
        run_id="field-001",
        recording_id="site-a",
        capture_metadata={
            "latitude": 32.0,
            "longitude": 34.9,
            "antenna": "VHF vertical",
            "gain_db": 28,
        },
    )
    assert result["status"] in {
        "dmr_confirmed_clean",
        "dmr_confirmed_degraded",
    }
    attempts = json.loads(
        (output / "inventory" / "attempts.json").read_text(
            encoding="utf-8"
        )
    )
    assert attempts[0]["capture_metadata"]["antenna"] == "VHF vertical"
    assert attempts[0]["capture_metadata"]["latitude"] == 32.0
    channels = json.loads(
        (output / "inventory" / "channels.json").read_text(
            encoding="utf-8"
        )
    )
    assert channels[0]["frequency_hz"] == 164_537_500.0
    assert channels[0]["dominant_color_code"] == 8
    assert channels[0]["talkgroup_ids"] == [1234]
    assert channels[0]["radio_ids"] == [5678]
