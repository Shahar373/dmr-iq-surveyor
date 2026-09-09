# `p25_sites` snapshot format

Input for `dmr-surveyor geo import-sites`. Copy `config/p25_sites.example.csv`, fill in your own
snapshot, and keep the filled-in copy out of the repository — `config/p25_sites.csv` is gitignored,
because an operational site list is not example data.

| Column | Meaning |
|---|---|
| `wacn_hex`, `system_id_hex` | the P25 system this site belongs to |
| `rfss`, `site` | the site's identity inside that system |
| `observation_status` | `DIRECT` or `NEIGHBOR_ONLY`, as reported by whatever produced the snapshot. Provenance carried through; it says nothing about *this* project's recordings. |
| `primary_cc_mhz` | control-channel frequency in MHz, **or empty if unknown** |
| `nac_hex` | optional |
| `notes` | free text, carried into reports |

Three properties of this format are load-bearing, and the importer preserves rather than tidies them:

- **An empty `primary_cc_mhz` is not zero and not a reason to drop the row.** The site stays in the
  registry and every report gives it the status `frequency_unknown`: known to exist, impossible to
  measure until its control channel is found.
- **A frequency listed for more than one site stays on all of them.** Every measurement on it is then
  `ambiguous_reuse` and is excluded from geolocation, because the level measured there is a mixture
  of both transmitters. The importer prints which frequencies these are.
- **`nac_hex` is not a site identifier.** One NAC is routinely shared by many sites in a system, so
  it is stored as context and never used to attribute a measurement.

Re-importing under the same `--snapshot-id` replaces that snapshot's rows in place, so correcting a
frequency and re-importing updates the site without losing any measurement already attached to it.
