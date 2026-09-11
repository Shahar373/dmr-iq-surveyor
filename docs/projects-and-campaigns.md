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
absent; it belongs to the hardware profile named by `defaults.hardware`, and
that profile is what a round's stops are declared against.

In both files an unknown key is an error rather than a silent no-op. A
misspelled default is a default that would not apply, and failing is the only
way to say so.

## Commands

| command | does | touches the database |
|---|---|---|
| `project validate <manifest>` | parse and check a project or campaign manifest | no |
| `project show --project P` | resolved manifest, database, claim, campaigns found | read-only |
| `project init --create` | manifest + a new database + its claim; reports unless `--write` | yes — the only creating path |
| `project init --adopt` | take on an existing database; reports unless `--write` | read-only, then one row |
| `project campaign new --project P --campaign-id C` | write a campaign manifest | no |

A project is named or pathed. `--project p25` is looked up as
`config/projects/p25/project.yaml`, then `projects/p25/project.yaml`; a path to
a manifest or to its directory works too. It is the same two-step shape as
`--band` and `--site`.

### Creating a new project

```bash
dmr-surveyor project init --create \
  --project-id p25_central_il \
  --label "P25 central Israel" \
  --database /var/lib/dmr-field/inventory/dmr_inventory.sqlite3 \
  --manifest /etc/dmr-field/projects/p25/project.yaml
```

Like adoption, that reports and stops: what would be created, what it would
be claimed as, and what is at the manifest target. Nothing is written without
`--write`.

`--create` is the only command in the codebase permitted to bring a project
database into existence, and it refuses to touch a path that already holds
one — that is adoption's job.

#### Failure modes, and what each leaves behind

With `--write`, in this order: refuse an existing database; compare the
manifest target; create the database and claim it; write the manifest.

| what fails | what is left | how to finish |
|---|---|---|
| the database path already exists | nothing written | use `--adopt`, or choose another path |
| the manifest at the target differs | nothing written — **no database is created** | move the manifest aside, or edit it by hand |
| the claim cannot be written | the just-created database is **removed** | fix the cause and re-run |
| the manifest cannot be written | the just-created database is **removed** | fix the cause and re-run |
| the process is killed between the two | database claimed, manifest missing | **re-run the same command**: it completes by writing the manifest |

The last two rows are the same accident handled two ways, because only one of
them can be. A rollback needs the process to still be alive; a kill leaves
whatever was on disk. So creation both rolls back what it can and recognises
what it cannot: a database carrying *exactly* this project's claim is treated
as the half-done creation it is, and the re-run finishes it. A database
claimed by another project, or claimed by nobody, is still refused — those are
adoption's business.

Rolling back means deleting a database, which is only ever safe because of
what is known at that moment: the path did not exist when the command started,
and the file holds nothing but the claim this command just wrote. Anything
that existed beforehand is never touched.

A repeat run is idempotent. An identical claim is left exactly as it was,
timestamp included, and an identical manifest is a no-op.

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

That reports and stops. It prints the database's size, the result of SQLite's
own integrity check, whether the schema is recognised, its row counts, any
claim already on it, what is at the manifest target, and the exact manifest and
claim row it *would* write. Nothing is written without `--write`.

The file is identified by reading its first sixteen bytes, not by connecting to
it — connecting is what manufactures a database on a path that has none.

**A valid SQLite file is not a reason to claim it.** A browser cache, a package
index and a phone backup are all valid SQLite. Before anything is written, and
through a connection that structurally cannot write, adoption runs
`PRAGMA quick_check` and looks for a minimum schema signature: the five Phase 5
tables (`runs`, `attempts`, `events`, `sessions`, `channels`) and the columns
that make them this project's rather than something else's. A foreign or
corrupt file is refused and left byte-for-byte as it was found.

The signature is the *oldest* schema layer on purpose. A database written
before Phase 6A has only those tables and is still this project's database —
the later ones arrive by additive migration once it is opened normally.
Requiring today's eighteen tables would refuse exactly the databases adoption
exists for.

A manifest already at the target that differs from what would be written is
reported and refused **in the dry run**, not only under `--write`: a run that
could never have succeeded must not read as one that is merely waiting.

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

0. a binding exists at all — it is held for the length of the work it was made
   for and dropped in a `finally`, never left behind on a failure;
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

The filename is checked unconditionally, including when it is not a valid
slug, and it is checked *exactly*: `2026-09_Day1.yaml` is refused even though
it normalises to the same slug as `campaign_id: 2026-09_day1`. `resolve_campaign`
builds the literal, already-lowercased path and reads whatever is at it, and a
filesystem comparison is case-sensitive on the deployment target -- Linux,
including the Pi -- so a file that only agrees after normalising is a file
that can be *loaded* by path and can never be *found* by name. A campaign is
found by its filename, so `Day One.yaml` is not a cosmetic problem either: it
is a file that can be read by path and never by name, a campaign that exists
when listed and is missing when asked for.

**The binding lasts exactly as long as the serving.** It is made before the
verifying open and dropped on every way out — a profile that will not resolve,
a token file with the wrong mode, TLS that cannot be configured, an exception,
or the server returning normally. That matters because the binding is
process-wide and cannot be replaced once set: one left behind would judge
whatever the process did next against a project it was no longer serving, and
would make a second `web serve` in the same process impossible.

### Precedence

Highest first: **explicit flag → campaign manifest → project manifest → the
CLI's own default.**

"Explicit" is Click's parameter source, not the value. `--band` has a non-None
default, so its value alone cannot say whether you chose it, and the difference
decides whether a manifest is overridden or a conflict is refused.

| key | behaviour |
|---|---|
| `band` | a contradicting explicit flag is **refused**, naming both values and both origins. Compared by resolved content, not by spelling, so a name and a path to the same profile agree — and so does a copy of it |
| `database` | an explicit flag is allowed, but the alternative must exist and carry the same project's claim for the same analyzer |
| `output`, `recordings`, `tls-dir`, `token-file` | the flag wins, silently |
| `capture` settings | a campaign pins them; an explicit flag wins |
| `campaign` | selects a campaign manifest; it does not contradict one |

Band is the one refusal because levels recorded under different bands are not
comparable: a band silently swapped underneath a campaign does not produce a
slightly different campaign, it produces measurements that cannot be compared
with the ones already in the database. Everything else changes where bytes
land, not what they mean.

## A campaign is an analysis boundary

`campaign_id` is not only a label on a run. Pass `--campaign` to `geo
measurements`, `geo solve`, `geo sites`, `geo history`, `geo plan`, `geo
export` or `scripts/campaign_digest.py`, and **every derived value** is
computed from that round's runs alone:

- the reference gain and the noise-floor median — the two numbers that decide
  whether levels are comparable at all;
- the common-mode offsets, which fall out of the solve's own residuals;
- the measurements the solver reads, joined through `survey_runs`;
- the solutions, the next-stop plan, the site overview's counts, and the
  GeoJSON, KML and GPX exports.

Two rules hold everywhere:

**No campaign means the whole database.** An unscoped command runs the same
SQL it always did. That is what keeps every existing invocation and every
stored report true.

**A campaign never includes the unassigned.** A run written before campaigns
existed carries `campaign_id IS NULL`. It was not taken under the round being
asked about — nobody declared that it was — so it is excluded rather than
swept in. A run you name explicitly that is outside the campaign is *refused*,
not silently dropped: a rebuild that skipped a stop you asked for would report
success over work it never did.

`geo_solutions` and `geo_plans` carry the campaign their solve was scoped to.
A solve run without `--campaign` stores `NULL` there and is never offered as
one campaign's conclusion — it read every run in the file. Those columns are
not backfilled from older batches for the same reason: labelling a solve
afterwards would claim a boundary that was never applied.

Reference imports stay whole-database on purpose. A corrected snapshot
invalidates measurements for every round, and scoping that rebuild would leave
the others quietly stale.

`survey compare` reports `campaign_differs` and names both rounds, but never
blocks: comparing two rounds is the point of running a second one. What it
tells you is that a level difference may be the rounds rather than the RF,
since each round establishes its own reference gain and noise floor.

## The hardware profile

`config/hardware/*.yaml` says what the receiver **is**, and what it is meant
to be set to. A campaign names one:

```yaml
defaults:
  hardware: field_rsp1a     # or an absolute path
```

It is separate from a site profile because the two answer different questions
on different clocks. A site profile is *where* — one place, its antenna, its
coordinates — and there is one per stop. A hardware profile is *what with*,
changed once for every stop at every site when a round swaps a radio or
settles on a gain.

Splitting them fixes a real ambiguity. `sites` is one mutable row per profile
that every run rewrites, so gain recorded there describes the profile as it
stands *now*, not as it stood for the run being read. A hardware profile is a
file, snapshotted into each run's own provenance when the run is recorded, and
unable to rewrite history afterwards.

**Nothing in it is ever `applied`.** However precise the file is, it declares
what the operator intends; only the radio's own read-back may claim to be what
the receiver was actually set to.

### Precedence, both ways

*Reading* what a run was recorded at:

**applied → requested → declared → the legacy `sites` row**

The last tier is offered only to a run whose own provenance is empty — a row
written before runs carried their own declaration — and is labelled apart
(`declared, from the site row`), so a number resting on the mutable row is
never mistaken for one the run recorded. `geo measurements` reports how many
runs rested on each tier.

*Requesting* a capture:

**command line → hardware profile → site profile → built-in fallback**

The hardware profile outranks the site profile because gain belongs to the
radio, not to the place; the site profile still answers whatever the hardware
profile leaves unset, so naming one never takes information away from a run.
An explicit `--if-gain-reduction` or `--lna-state` still wins — the operator is
at the radio — but says so when it contradicts the profile, because a round
whose stops were not all taken at one gain is exactly what the drift check
hunts for afterwards. The startup banner always names the origin of each half.

`--hardware` on `survey run` and `survey capture` names a profile directly for
an offline or single-stop analysis.

## Capture campaign, view scope, and browsing safely

A campaign answered two different questions with one value, and that was a
bug. It said *where new evidence is written*, and it also said *what the
operator is looking at*. Setting `FIELD_CAMPAIGN` on the Pi therefore did two
things, and only one of them had been asked for: 51 rounds recorded before
campaigns existed vanished from the field app behind an empty map and the
words "No stops recorded yet."

**Nothing had been deleted.** `campaign_id = 'g4'` is never true for a row
holding `NULL`, so those runs were filtered out of one screen and nowhere
else -- an unscoped `geo sites`, `geo plan`, `geo export` or
`scripts/campaign_digest.py` read them the whole time. Every `DELETE` in the
tree is keyed by run id, batch or site id; no statement anywhere assigns or
clears a `campaign_id`; the connect path only adds columns.

So the two questions now have two answers:

| | what it is | where it lives |
|---|---|---|
| **capture campaign** | the one and only place new evidence is written | `FieldSettings.capture_campaign_id`, from `--campaign` |
| **view scope** | which rounds are on screen right now | the `?scope=` query parameter, per request |

### The four views

| scope | reads | writable |
|---|---|---|
| `current` *(default)* | the capture campaign -- or the whole database, when none is named | yes |
| `legacy` | `campaign_id IS NULL` | no |
| `all` | every round, grouped and labelled by campaign | no |
| `campaign:<id>` | one other named round | no |

`legacy` is spelled out rather than expressed as an absent value, because
`None` was already spoken for: in `CampaignScope` it means *the whole
database*. A third concept needed a third name, not a second meaning for a
value that already had one. `CampaignScope` therefore carries an
`unassigned_only` flag alongside `campaign_id`, and its predicate is
`IS NULL` -- never `= NULL`, which is never true for any row and would report
an empty database rather than the rows it was asked for.

Naming the campaign you are already recording into (`campaign:<the capture
campaign>`) *is* the current view, not a read-only copy of it. Spelling out
your own round must not take Record away from you.

### What a read-only view means

Everything but `current` is read-only. Not because browsing is dangerous, but
because the alternative is an operator who excludes a stop, or taps Record,
while looking at a screen full of last month's work and believing it is this
morning's.

Refused from a historical view, with a 409 that names the campaign new work
actually goes to: capture, analyse, solve, drive start, live solve, purge,
and stop exclude / include / delete. Not refused: marking a position, live
position fixes, a pull-over request, a device rescan and job cancel. None of
those writes evidence or carries a campaign, and refusing the middle three
would break a drive that is already running because the operator glanced at
history.

The view check is the **outer** one, and it is the stricter of the two. The
campaign narrowing that already guarded exclude and delete does not cover the
case that matters most here: under `all`, the stop being looked at may well be
in the capture campaign, so the narrowing lets it through -- correctly, it is
in the campaign -- and the row is destroyed. Widening what may be *seen* must
never widen what may be *changed*.

A solve scoped to the unassigned runs refuses to store itself.
`geo_solutions.campaign_id IS NULL` already means "this solve read the whole
file", so such a solve has no honest value to stamp: `NULL` would claim a
breadth it never had, and any id would claim a campaign nobody declared. The
refusal happens before the grid search, not after.

### What the operator sees

A permanent status bar names the project, the campaign being recorded into,
the receiver profile when one is named, and which rounds are on screen. The
selector offers Current, Legacy when there are unassigned runs, each other
campaign that actually holds something, and All -- built from a census of
`survey_runs`, so a campaign that holds nothing is not offered. Choosing one
refreshes the map, the tables and the summaries **together**: a map still
showing one scope's measurements under another scope's stop list is the
confusion this exists to remove.

A historical view says so in a banner that follows the operator across tabs
and names where new captures actually go, and every mutating control goes
with it -- Record, Free disk, Resolve, the position controls, the whole Drive
row, and the per-stop Set aside and Delete buttons.

The view lives in a plain variable in the page: not `localStorage`, not the
URL, nothing the server remembers. **A reload is back on the campaign being
recorded.** A request that names no scope at all is `current`, which is what
keeps every client that predates the parameter -- and every hand-typed URL --
behaving exactly as it did.

An empty view says which it is. "No stops recorded yet" is only the truth when
the file is empty; when it is not, the page counts what is filed elsewhere and
names where, because saying "no data" over 51 rounds of work is what started
this.

## Where this is going

Done:

1. **PR1** -- run provenance and campaign tagging.
2. **PR2** -- project and campaign manifests, `project_meta`, the database guard.
3. **PR3** -- campaign-scoped analysis, hardware profiles, ops integration.
4. **PR3 acceptance on a real Pi** -- reboot, a full capture, provenance,
   isolation, and no token leak.
5. **PR4** -- capture campaign separated from view scope; legacy analyses
   readable again; Current / Legacy / All selector; no write or delete through
   a historical view. *This section.*

Planned, in order, and none of it started here:

6. **PR5 -- campaign lifecycle and ops hardening.** `fieldctl campaign
   list/current/new/use/close`; an atomic `field.env` edit that checks for a
   running job, restarts and can roll back; the `field.env.local` parity fix;
   the receiver serial completed in observed hardware identity; the PR3 Pi
   acceptance written up formally.
7. **PR6 -- historical campaign curation.** An explicit assignment command
   with a dry run, selecting by run id or by time range. No automatic
   backfill, ever. Derived analysis is **recomputed**, never given a blind
   label -- a run moved into a campaign changes that campaign's reference
   gain and noise floor, so its conclusions have to be drawn again.
8. **PR7 -- rich analysis UI.** A campaign dashboard, full provenance per run,
   campaign comparison, and detection / geometry / solution-confidence
   measures.
9. **Field validation.** A new campaign, not the acceptance one: 6-10 stops on
   live, continuous P25, checking detection, the solver, and accuracy against
   ground truth.
10. **Later only.** An analyzer abstraction for P25, VOR, ATIS, DMR and other
    signal types. Not before the above.

## What this does not do

- It does not move, rewrite or reinterpret any existing row.
- It does not assign a historical run to a campaign, and there is no backfill.
  That is PR6's subject, and it is deliberately not solved by a migration.
- It does not change the estimator. Campaign scoping decides *which* evidence
  is read; the mathematics that reads it is untouched.
- It does not add an analyzer. There is still exactly one, and site attribution
  is still by frequency alone.
- A view does not change what a campaign *is*. `survey_runs.campaign_id` keeps
  the meaning it has always had; the view is a question asked of it, never an
  edit to it.
