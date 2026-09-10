# Projects and campaigns

A **project** is one subject studied with one analyzer, held in one SQLite
database: "P25 sites in central Israel" is a project. A **campaign** is one
collection round inside it: "day 1, coastal road". PR1 gave every run a
`campaign_id` and a record of what the receiver was set to. This adds the layer
above: a database that says which project it is, files that declare a project
and its campaigns, and a guard that stops a project-aware command from opening
anything else.

Nothing here changes an existing command. Without `--project` every invocation
behaves exactly as it did, including its database path and its output layout.

## Why a database needs an identity

Opening a path in this codebase does not read a database, it makes one:

```
$ dmr-surveyor geo history --database /tmp/typo.sqlite3
```

...returns a 180 KB, 18-table, fully committed database on a path that did not
exist a moment earlier. An existing *empty* file is opened and silently
schema'd the same way. So "the file is there" was never evidence that it was
the right database, and a typo never failed — it produced an empty history and
an operator wondering where the campaign went.

A claim fixes that, but only for paths that opt in. See
[the guard](#the-guard) below.

## The two files

Both live under a project root. `config/projects/example/` is a worked example;
copy the directory and edit it.

```
config/projects/<name>/
├── project.yaml
└── campaigns/
    ├── 2026-09_day1.yaml
    └── 2026-09_day2.yaml
```

### `project.yaml`

```yaml
schema_version: 1
project_id: p25_central_il          # ^[a-z0-9][a-z0-9._-]{0,63}$, immutable
label: "P25 central Israel"
analyzer: p25_site_geolocation      # the only analyzer that exists
database: /var/lib/dmr-field/inventory/dmr_inventory.sqlite3
defaults:                           # every key optional
  band: central_800_narrow
  site: /etc/dmr-field/sites/field.yaml
  output: /var/lib/dmr-field
```

`database` may be absolute or relative to the manifest. `project_id` is what
the database is claimed to; changing it after adoption means the manifest and
the database no longer agree, and every open is refused until they do.

`analyzer` accepts only `p25_site_geolocation`. VOR, ATIS and DMR are named in
the roadmap and are not written; a manifest asking for one is refused rather
than quietly read as P25, because a project pointed at the wrong reader is a
project whose results mean something other than they say.

### `campaigns/<campaign_id>.yaml`

```yaml
schema_version: 1
campaign_id: 2026-09_day1           # must equal the filename stem
project_id: p25_central_il          # must equal the project's
label: "Day 1, coastal road"
defaults:
  band: central_800_narrow
  site: /etc/dmr-field/sites/field.yaml
  capture:
    center_frequency_hz: 867406250
    sample_rate_hz: 5000000
    duration_seconds: 90
```

`campaign_id` uses `normalise_campaign_id` — the same validator `--campaign`
has used since PR1, not a second dialect. `defaults.capture` accepts exactly
the three keys above, each a positive number: they are what has to stay
identical across stops for their levels to be comparable. Gain is deliberately
absent; it belongs to the hardware profile, which is not written yet, so the
site profile remains its only declaration source.

In both files an unknown key is an error rather than a silent no-op. A
misspelled default is a default that would not apply, and failing is the only
way to say so.

## Commands

| command | does | touches the database |
|---|---|---|
| `project validate <manifest>` | parse and check a project or campaign manifest | no |
| `project show --project P` | resolved manifest, database, claim, campaigns found | read-only |
| `project init --create` | manifest + a new database + its claim | yes — the only creating path |
| `project init --adopt` | take on an existing database; reports unless `--write` | read-only, then one row |
| `project campaign new --project P --campaign-id C` | write a campaign manifest | no |

A project is named or pathed. `--project p25` is looked up as
`config/projects/p25/project.yaml`, then `projects/p25/project.yaml`; a path to
a manifest or to its directory works too. It is the same two-step shape as
`--band` and `--site`.

### Adopting the existing database

The P25 database in the field has already been validated. Adoption takes it on
in place: no data is moved, no row is rewritten, and nothing is created.

```bash
dmr-surveyor project init --adopt \
  --project-id p25_central_il \
  --label "P25 central Israel" \
  --database /var/lib/dmr-field/inventory/dmr_inventory.sqlite3 \
  --manifest /etc/dmr-field/projects/p25/project.yaml
```

That reports and stops. It prints the database's size, its row counts, any
claim already on it, and the exact manifest and claim row it *would* write.
Nothing is written without `--write`.

The file is identified by reading its first sixteen bytes, not by connecting to
it — connecting is what manufactures a database on a path that has none.

With `--write`, in this order:

1. the manifest is rendered and re-parsed **in memory**, so an invalid manifest
   is never the reason a database was touched;
2. an existing manifest at the target that differs is refused; an identical one
   is a no-op;
3. the claim goes in inside `BEGIN IMMEDIATE … COMMIT` — the whole typed row or
   nothing;
4. the manifest is written to a temporary sibling and `os.replace`d onto the
   target, leaving no `.tmp` behind.

Fail-closed and idempotent. If the process dies between 3 and 4 the database is
claimed and the manifest is missing; re-running writes only the manifest,
because a claim identical to the stored one is a no-op. A claim naming a
different project or a different analyzer is refused, with both values shown.

**What adoption means, precisely.** It assigns the whole database — every
table, every historical run — to the project. It assigns no run to a campaign:
`campaign_id` stays `NULL` on existing rows, and there is no backfill.

## The guard

`project_meta` holds one typed row: `project_id`, `analyzer`,
`manifest_schema_version`, `claimed_at`, `claimed_by_version`. A `CHECK
(id = 1)` makes a second row impossible and `NOT NULL` on every column makes a
partial one impossible, so there is no half-claimed state to reason about.

A project-aware entry point binds the process to one project and one database
before any work. `inventory/store.py` holds the only `sqlite3.connect` in the
codebase, so the check there covers every open, direct or indirect —
`/api/state`, `run_survey`, `materialise_measurements`, `solve_all_sites`, a
live drive. In order, before anything is written:

1. the path must equal the bound path — a stray `--database` inside the process
   is refused;
2. the file must exist, be non-empty, and begin with `SQLite format 3\0`,
   checked without connecting, so **no file and no directory is created**;
3. the claim is read, and both `project_id` and `analyzer` must match.

An unclaimed database is a refusal, not an invitation: it is adopted
deliberately or not at all.

With no project bound, `connect_database` behaves byte-for-byte as it always
has, creating on a fresh path. That is what keeps every existing invocation
working, and it is why the create-on-open behaviour above is still reachable
for unprojected paths.

## Serving a project

```bash
dmr-surveyor web serve --project p25 --campaign 2026-09_day1 --host 0.0.0.0
```

The manifest is resolved first, because it may supply the band, site, output
root and database everything after it consumes. Then the process binds and
opens the database **once, at startup, with a message you can read** — so a
missing, empty, non-SQLite, unclaimed or foreign database fails there, before
anything is served and before anyone has driven anywhere. Without `--project`
nothing binds and nothing is opened at startup, exactly as before.

`--campaign` alongside `--project` selects `campaigns/<id>.yaml`, which must
exist and must agree with the project id and with its own filename. Without it,
project defaults apply and stops recorded stay unassigned — which is what every
run recorded before campaigns existed is.

### Precedence

Highest first: **explicit flag → campaign manifest → project manifest → the
CLI's own default.**

"Explicit" is Click's parameter source, not the value. `--band` has a non-None
default, so its value alone cannot say whether you chose it, and the difference
decides whether a manifest is overridden or a conflict is refused.

| key | behaviour |
|---|---|
| `band` | a contradicting explicit flag is **refused**, naming both values and both origins |
| `database` | an explicit flag is allowed, but the alternative must exist and carry the same project's claim for the same analyzer |
| `output`, `recordings`, `tls-dir`, `token-file` | the flag wins, silently |
| `capture` settings | a campaign pins them; an explicit flag wins |
| `campaign` | selects a campaign manifest; it does not contradict one |

Band is the one refusal because levels recorded under different bands are not
comparable: a band silently swapped underneath a campaign does not produce a
slightly different campaign, it produces measurements that cannot be compared
with the ones already in the database. Everything else changes where bytes
land, not what they mean.

## What this does not do

- It does not move, rewrite or reinterpret any existing row.
- It does not assign a historical run to a campaign.
- It does not add a hardware profile — `config/hardware/*.yaml` is the next
  step, and until then `SiteProfile` remains the only declaration source for
  gain and LNA state.
- It does not add an analyzer. There is still exactly one, and site attribution
  is still by frequency alone.
