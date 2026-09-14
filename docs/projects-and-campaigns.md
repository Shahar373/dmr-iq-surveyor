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
`campaign_id` stays `NULL` on existing rows, and there is no backfill. Filing
those runs afterwards is a separate, deliberate act —
[`project campaign assign-runs`](#assigning-historical-runs-to-a-campaign) —
never something adoption does on the way past.

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

### What the radio says it is

A hardware profile is a declaration, however precise. Alongside it, a run
records what the device itself answered when it was opened -- its serial and
its label -- in `hardware_json.identity`, which holds observations and nothing
else. A serial typed as `--serial` selects which radio to open and is recorded
as that; a serial written into a profile stays in `declared`. Neither is
allowed to appear as something the radio reported.

The device is asked once, through the handle the capture is already streaming
from: no second open, no second enumeration, no probe subprocess, because the
SDRplay API hands the radio to one client at a time. Every question is
guarded on its own, so a driver that answers none of them simply records
nothing and the capture is unaffected. A blank answer is left out rather than
stored, since an empty serial would read as one the radio reported and nobody
can look up.

This is what lets a campaign say afterwards *which* RSP recorded which stop --
the question a spare radio swapped in mid-round makes urgent, and the one the
declaration cannot answer. `scripts/campaign_digest.py` prints the observed
receiver beside the declared one and says when more than one radio reported
itself across a round. No existing run is backfilled.

## Capture campaign, view scope, and browsing safely

A campaign answered two different questions with one value, and that was a
bug. It said *where new evidence is written*, and it also said *what the
operator is looking at*. Setting `FIELD_CAMPAIGN` on the Pi therefore did two
things, and only one of them had been asked for: 51 rounds recorded before
campaigns existed vanished from the field app behind an empty map and the
words "No stops recorded yet."

**Nothing had been deleted**, and the evidence said so before a line was
changed: 51 runs still carrying `campaign_id IS NULL`, 629 `rf_observations`,
960 `geo_measurements` and 26 `p25_sites` all still present, `PRAGMA
quick_check` returning `ok`, and a full pre-PR3 backup on the Pi that nothing
had needed to restore from. `campaign_id = 'g4'` is never true for a row
holding `NULL`, so those runs were filtered out of one screen and nowhere
else -- an unscoped `geo sites`, `geo plan`, `geo export` or
`scripts/campaign_digest.py` read them the whole time. Every `DELETE` in the
tree is keyed by run id, batch or site id; the connect path only adds columns;
and at the time nothing anywhere assigned or cleared a `campaign_id` at all.
One statement does now -- `project campaign assign-runs`, added by PR6 and
described below -- and it is the only one: it never runs without an operator
naming the runs, it refuses a run that already declares a campaign, and it
clears nothing.

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
actually goes to: capture, analyse, solve, drive start, live solve, a
pull-over hold, purge, marking a position, and stop exclude / include /
delete.

A **pull-over hold is a write**, not a pause: it routes through the same close
path a drive bin does and writes a `survey_runs` row, its observations and its
levels, under the campaign the drive is recording into. **Marking a position**
writes no database row, but it is the coordinate the next recording is filed
under, and recording a stop against the previous stop's coordinates is the one
mistake that silently corrupts a round.

Not refused: live position fixes, a device rescan, and job cancel. Those write
nothing and carry no campaign, and refusing them would break a drive already
under way because its operator glanced at history -- or take away the button
that stops it.

The view check is the **outer** one, and it is the stricter of the two. The
campaign narrowing that already guarded exclude and delete does not cover the
case that matters most here: under `all`, the stop being looked at may well be
in the capture campaign, so the narrowing lets it through -- correctly, it is
in the campaign -- and the row is destroyed. Widening what may be *seen* must
never widen what may be *changed*.

### A stored conclusion carries its own boundary, and says so

`geo_solutions.campaign_id IS NULL` and `geo_plans.campaign_id IS NULL` mean
*this solve read the whole database* -- not *this solve read the unassigned
runs*. The legacy view is the one place those two readings meet: the answers
it shows are the ones that were standing before campaigns existed, which is
exactly what a reader of the unassigned runs is asking for, and exactly what
must not be passed off as having been drawn from the stops beside it.

So the plan carries `campaign_id` and an `unscoped_solve` flag, the site
overview carries `solution_campaign_id`, and the page says it out loud: *"the
plan and regions below come from a solve that was run without a campaign, so
it read every round in the database at the time -- not only the stops listed
here."* That matters beyond the historical case, because `dmr-surveyor geo
solve` with no `--campaign` is a supported thing to run at any time, and from
then on the newest unscoped plan is one drawn across every round in the file.

The phrase is `Historical whole-database analysis`, and it is one phrase
everywhere it appears -- the plan, the site card, the map popup for a mode and
for a region -- rather than three near-misses an operator has to decide are the
same thing. `stored_analysis_label()` is the only place it is written.

`all` runs nothing either: no joint solve, no shared reference gain, no shared
noise floor. Every number on it is a row that was already in the file. But it
does have to *group* them, and grouping turned out to be the difference between
an overview and a lie. `latest_solutions` keeps one row per site -- the most
recently inserted -- which is the right answer for a file holding one round and
the wrong one for a file holding three: the newest round wins every site, and a
round whose solve found too little hides the rounds that found something. On
the acceptance fixture that is exactly what happened, and every transmitter
analysis on the overview disappeared behind a later campaign's
`insufficient_evidence`. So `all` reads `latest_solutions_by_campaign` instead
-- one stored row per (site, round) -- and each site lists every round that
solved it, labelled and apart.

`all` offers **no next-stop plan at all**, and says so. A plan is computed from
one round's evidence and only means anything inside it; handing over the newest
one would be precisely the shared aggregation an overview must not do.

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
row, and the per-stop Set aside and Delete buttons. The lock only ever takes a
control away and gives back only what it took, so leaving a historical view
cannot hand back a button that a running capture, or a browser that will not
give GPS over plain HTTP, had disabled for its own reasons. Arming "Tap map to
place" and then switching view disarms it, and the map's own handler refuses
as well -- it is the one path that writes without a button press.

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
   readable again, transmitter results included and labelled; Current / Legacy
   / All selector; no write or delete through a historical view. *The section
   above.* Accepted on the Pi:
   [`docs/validation/pr4-capture-campaign-and-view-scope.md`](validation/pr4-capture-campaign-and-view-scope.md).
6. **PR5 -- campaign lifecycle and ops hardening.** `fieldctl campaign
   list/current/new/use/close`; an atomic `field.env.local` edit that checks
   for a running job, restarts and can roll back; the `field.env.local`
   parity fix; the receiver serial and label completed in observed hardware
   identity; the PR4 Pi acceptance written up formally. *The two sections
   below.* **The implementation and the follow-up fix to the switch's
   recovery are both merged, and the Pi now runs `b6ff094`.**

   The follow-up was the part an external run against stateful stubs found,
   and every one of it was about the *service* rather than the file: a
   rollback that restored the configuration while the process kept running
   the new one, an interrupt after the stop that left the deployment not
   recording, a success check that compared the campaign but not the
   project, and two commands -- `close` and a `use` that is a no-op --
   deciding from the configuration file about a service that may have read a
   different one hours ago.

   **The lifecycle half of the Pi acceptance passed.** On the updated
   checkout and the updated installed copy of `fieldctl` -- they are two
   files, and the installed one is what runs -- the operator declared a
   campaign, switched the deployment to it, closed the previous round,
   confirmed the configuration and the API agreed on both project and
   campaign, and rebooted. The deployment came back recording into
   `2026-09-11_g4_geolocation_validation`, which is the campaign in force
   now and **still holds no runs**: it is a declared, open, empty round.

   **The RF half did not run, and is deferred rather than passed.** None of
   what passed above is evidence about the radio, and none of it should be
   read as acceptance of one: no capture was recorded into the new
   campaign, the settings the running service is actually set to were not
   read back off a live capture, and the positive-detection step was not
   attempted. The
   reason is not the software -- it is that there is currently no access to
   an external reception point where an 868 MHz signal can be expected, and
   a capture taken where nothing is transmitting proves nothing about either
   the receiver or the solver. See the two items below.

In progress:

7. **PR6 -- historical campaign curation.** An explicit assignment command
   with a dry run, selecting by run id or by an explicit time range. No
   automatic backfill, ever, and no campaign inferred from a filename, a
   site, a date or a position. Derived analysis is **recomputed**, never
   given a blind label -- a run moved into a campaign changes that
   campaign's reference gain and noise floor, so its conclusions have to be
   drawn again. *The section below.*

Before the field work can happen, and deliberately not part of PR6:

8. **Field geolocation validation.** Still ahead of PR7 in priority, and
   still the thing that matters most: everything above this line is
   bookkeeping around measurements; none of it shows that the measurements
   locate anything. Two steps, in order: first prove that a stop yields
   usable positive measurements at all -- a real detection on live,
   continuous P25, at a known gain, with a level the solver can read as
   distance -- and only then a campaign of 6-10 stops with the geometry the
   planner asks for, checked against a transmitter whose location is known.
   Until the first step passes, the second is a day of driving that cannot
   fail informatively.

   **Blocked on access, not on code.** The first step needs a reception
   point, outside, where an 868 MHz signal can actually be expected, and
   there is none available at the moment. It is deferred until there is.

9. **The map's tiles return 403.** Observed on the Pi. OpenStreetMap tile
   requests from the field app are being refused, so the operator gets the
   markers over an empty background. It is a **separate blocker, and it
   comes before the field test**: driving 6-10 stops from a page that cannot
   draw a map is worse than not driving them. It is deliberately not fixed
   as part of PR6 -- a tile source is not campaign curation, and folding an
   unrelated network fix into a database-membership change would make both
   harder to review.

Planned, in order, and none of it started here:

10. **PR7 -- rich analysis UI.** A campaign dashboard, full provenance per run,
    campaign comparison, and detection / geometry / solution-confidence
    measures.
11. **Later only.** An analyzer abstraction for P25, VOR, ATIS, DMR and other
    signal types. Not before the above.

## A campaign is open until it is closed

A campaign manifest carries one optional key:

```yaml
status: open        # or: closed
```

Absent means `open`. Every campaign declared before this key existed is
therefore open, which is what it was, and no manifest has to be rewritten. An
open campaign's manifest is rendered byte-for-byte as it was before the key
existed, so a file written by an older build still compares equal and
`project campaign new` still reports it unchanged rather than as a conflict.

It is `open`/`closed` and deliberately **not** `active`, because those are
answers to two different questions:

| | the question | where the answer lives |
|---|---|---|
| **current campaign** | which campaign is this deployment recording into | `FIELD_CAMPAIGN`, in the environment file the service reads |
| **open / closed** | does this round still accept evidence | `status`, in the campaign manifest |

The same manifest is read by the laptop doing analysis and by the Pi in the
car, and only one of them is recording. A campaign that called itself
"active" would be claiming something about a machine it knows nothing about.

**Closed refuses new acquisition and takes nothing away from reading.** The
manifest still loads, the campaign still lists, the field app still shows the
round in Legacy or under its own name, and `geo measurements`, `geo solve`,
`geo sites`, `geo export` and `scripts/campaign_digest.py` all read it exactly
as before. What stops is capture, drive, a pull-over hold, and editing that
round's stops.

The refusal is one function, `require_open_campaign`, called at each of the
three doors that lead to a recording rather than reimplemented at each:

- `web serve --campaign <closed>` fails while manifests are still just files:
  before the project binding, before the database is opened, before the
  recordings directory is made and long before the SDR. A service pointed at
  a closed campaign leaves nothing behind.
- `survey capture --project P --campaign <closed>` and `live stop --project P
  --campaign <closed>` fail before the radio is probed, and `survey run
  --project P --campaign <closed>` fails before the recording is read.
  `--project` is optional on all three and does exactly one thing: it checks
  the round against that project's manifests. It deliberately does **not**
  resolve band, site or gain from the manifest, because those commands have
  never read one and making them do so would change what a stop is recorded
  with. `survey run` is included because it writes a `survey_runs` row under
  the campaign exactly as the other two do, and filing a day's recordings is
  the work most likely to happen after the round was closed.
- A campaign closed **while a service is running** stops taking stops at
  once. The startup check cannot cover that case -- it ran before the close
  -- so the doors that write new evidence (a capture, a drive, a pull-over
  hold, analysing a recording, and editing a stop) re-read the manifest each
  time they are asked. Without that, the running service kept recording into
  a finished round until its next restart, and that restart then failed,
  which on a Pi means at the side of a road. A manifest that cannot be read
  leaves the service behaving exactly as it did before the check existed:
  refusing there would take a working deployment down over a path that
  moved, and startup already proved the campaign was open.

Closing edits the manifest rather than re-rendering it: one line is replaced
or inserted and every other byte, comments included, is left alone. A campaign
file is one an operator may have annotated, and closing a round is not an
occasion to drop their notes. The rewrite keeps the file's mode and owner,
because closing under `/etc` is done as root and a manifest left root-only is
one the service user can no longer read at startup.

Nothing about closing touches the database. No row is moved, relabelled or
deleted, and there is no migration.

## Managing campaigns on the Pi

`fieldctl campaign` is the operations half. Everything that reads or writes a
*manifest* is delegated to `dmr-surveyor project campaign`, which owns the
schema, the single campaign-id validator and the atomic write; what lives in
the shell is the part only the host knows -- which campaign this deployment
records into, whether a job is running, and how to change that without losing
a stop.

```
fieldctl campaign list      every campaign, with status, size and settings
fieldctl campaign current   what this deployment records into, and from where
fieldctl campaign new ID    declare a round (reports only, unless --write)
fieldctl campaign use ID    record into it from now on (ditto)
fieldctl campaign close ID  finish a round (ditto)
```

`list` and `current` are read-only and need no privileges. `current` is the
one that answers the question `status` alone cannot: it prints the effective
project and campaign, **which of the two environment files each came from**,
the campaign the assembled argv would pass, and -- when the service is up --
the campaign the API says it is actually recording into. A file edited without
a restart is a disagreement nothing else surfaces.

`new`, `use` and `close` report and change nothing unless `--write` is given.
A write under `/etc` refuses without root and prints the exact `sudo` line;
`fieldctl` never calls `sudo` for you, because a command that silently
escalates is one nobody can predict the blast radius of.

`use` is the one that has to be right, and its order is the design:

1. take an exclusive lock, so two operators cannot switch at once;
2. ask the API whether any job has not finished, and refuse if one has not --
   *terminal* is the job model's own word, so `succeeded`, `failed` and
   `cancelled` are finished and a submitted-but-not-started capture is not;
3. stop the service, so nothing can start a capture between that answer and
   the switch;
4. rewrite only `FIELD_PROJECT` and `FIELD_CAMPAIGN` in `field.env.local`,
   keeping every other line, comment and override, and leaving `field.env`
   untouched;
5. start the service, wait for the API, and verify **both** the project and
   the campaign it reports -- a campaign id is not an identity, the same id
   can exist in another project, and a missing field is not an answer of
   "none";
6. check the token is not in the service's argv.

Any failure puts back **both halves** of what the switch changed. Which half
depends on how far it got, and the command tracks that rather than guessing:
before the stop there is nothing to undo; after it the service has to be
started again; after the write the file has to be restored first; and after
the start the running process has to be **stopped** before any of that means
anything, because `systemctl start` against a unit that is already active
starts nothing and re-reads nothing -- so a rollback that restored the file
and called `start` left the old configuration on disk and the new one in the
radio. The same recovery runs when the command is interrupted: a SIGINT or
SIGTERM between the stop and the end of the switch brings the service back
before re-raising the signal, because a Pi left not recording is the one
outcome worse than a failed switch. Each step is marked as begun *before* the
call that performs it, not after it returns. A call that has had its effect
and not yet returned is indistinguishable from one that never ran, and an
interrupt lands in that window as readily as anywhere else -- so an undo that
trusted the return left the file restored and the radio still on the new
campaign. Atomicity does not help here either: it promises no reader sees a
half-written file, not that every failure happened before the rename. It then verifies the previous campaign
came back and exits non-zero saying what is actually in force. `use` refuses a closed campaign, a
campaign the project does not declare, and a `field.env.local` that is a
symbolic link (writing through one would replace the link and leave its target
untouched) -- all of that before the service is stopped.

`close` refuses to close the campaign this deployment is recording into.
Switch to another one first; otherwise the service would be left pointed at a
campaign it may no longer write to and would not find out until its next
start, which is a restart nobody planned, at the side of a road.

It asks the **service**, not only the file, and does so under the same lock
that guards the write. A configuration edited without a restart leaves the
file naming one campaign while the radio still writes another, and the file
alone then waves through a close of the round being recorded. A service whose
API cannot be read is not permission to proceed: the command refuses rather
than falling back to the file. `use` applies the same rule to its own no-op --
"already the campaign this deployment records into" is a claim about a running
process, so it is confirmed against the API before it is made. A disagreement
is refused with both states named rather than reported as nothing to do, and
so is an answer that cannot be read: nothing is changed either way, but a
claim about the service that no answer from the service supports is not worth
an exit status of zero.

`new` copies the current campaign's band, site, hardware and capture settings
by default, so the second round of a survey is declared by naming what changed
rather than by retyping what did not. It never overwrites an existing
manifest, and declaring a campaign does not switch the deployment to it: that
is `use`, and it is a separate, deliberate step.

## Assigning historical runs to a campaign

A run recorded before campaigns existed carries `campaign_id IS NULL`, and
adoption deliberately left it that way. Filing those runs is an operator's
judgement, so there is a command that takes it and nothing that guesses:

```bash
dmr-surveyor project campaign assign-runs \
  --project p25 --campaign-id 2026-09-11_g4_geolocation_validation \
  --run-id 20260801_100000 --run-id 20260801_113000 \
  --reason "day one, coastal road" --write
```

`--run-id` is repeatable. The alternative is an explicit range:

```bash
  --since 2026-08-01T00:00:00+00:00 --until 2026-08-02T00:00:00+00:00
```

The two are mutually exclusive -- naming runs *and* giving a range asks two
questions whose answer is not their union -- and **there is no `--all`**. A run
is never selected because its filename, its site, its date or its position
looked like they belonged together; only because its id was typed, or its
recorded capture time falls in a range that was typed.

The range reads `capture_start_utc` and nothing else, never `imported_at`,
which says when a file was filed rather than when the RF was recorded. It is
half-open, `[since, until)`, so two adjacent ranges partition a day without the
stop on the boundary landing in both. Both bounds must carry a UTC offset: the
column is UTC, but a bound typed without one is as likely to be local wall
clock, and guessing wrong moves the window by hours. **A run whose capture time
is missing is never swept in.** `capture_time_source` is `unknown` for those --
the time is absent, not late -- and a range cannot say whether an absent time
falls inside it, so the command lists them and leaves them for `--run-id`.

### The dry run

It reports and changes nothing without `--write`, and **a dry run leaves the
database byte for byte as it was found**. It prints the project and the target,
the count, and per run: the id, the capture time and where that time came from,
the source basename, the site, the GPS state and the gain with the tier it
rests on. Then what the assignment would do to the derived analysis, and
warnings for mixed gain, missing hardware provenance, a missing capture time
and a missing position. None of those becomes an inferred value; they stay
visible as what is not known.

### What `--write` refuses

Before anything is written, and again under the write lock, because the answer
can change between the two:

- a run id that is not in the database -- a typo or the wrong database, and one
  bad id refuses the whole batch rather than letting the rest through;
- a run that already belongs to **another** campaign. There is no reassignment
  here: moving a run would change two rounds' conclusions at once, and the one
  it left may already have been reported on;
- a target campaign that does not exist, and a target that is **closed** --
  closing says the evidence in a round is final;
- a database with no `project_meta` claim, or one claimed by another project.

A run already in the target is a reported no-op, and writes no audit row: an
entry for a move that did not happen is a false record in the one place meant
to say what did.

### One transaction, and what could not join it

Every successful assignment appends to `campaign_assignments` -- the run, the
previous campaign, the target, a UTC timestamp, the `--reason`, and the tool
version. `--reason` is required to write, because a membership change is the
one thing in this database that no measurement explains.

The membership change, the rebuilt measurements and the audit rows are one
`BEGIN IMMEDIATE`. They have to be, because a campaign's reference gain and
noise-floor median are computed from the runs in it: moving one run changes the
yardstick every *other* run in that campaign was measured against, and changes
it again for the runs left unassigned. So both populations are rebuilt, not
just the runs that moved. Rebuilding the unassigned population touches no
solution -- `geo_measurements` is derived data keyed by run, and the historical
whole-database solves are left exactly as they are.

The solve **cannot** join that transaction, and this is deliberate rather than
unfinished. `solve_all_sites` opens its own connections, the store helpers
commit per run and per site, and the grid search is minutes of work the repo
already documents as such. A write lock held across it would lock the field app
out of its own database at the side of a road.

So the transaction carries the *invalidation* instead of the recompute.
`campaign_analysis_state` records that the target's stored conclusions predate
its membership, written in the same commit as the runs that moved, and a
completed solve for that campaign is what clears it. A failure, a crash or an
interrupt after the commit therefore leaves the mark standing:

> **A new membership is never paired with an old analysis presented as
> current.** The stored solutions stay as history -- nothing is deleted or
> relabelled -- but until `geo solve --campaign <id>` has run again, the round
> has no current conclusion rather than a stale one wearing that name.

### Where that is enforced

In the read layer, not in the command that wrote the mark. A mark only its
writer consults is a comment: the round goes on serving its old mode, region
and next stop to everything that did not think to ask.

`latest_solutions`, `latest_solutions_by_campaign` and `latest_plan` are the
three accessors that answer *what is current*, and every consumer reaches a
stored conclusion through one of them -- `geo sites`, `geo plan`, the exports,
the digest, the map, the site overview and the field app alike. So the
predicate lives there, once:

| what | under a superseded campaign |
|---|---|
| `latest_solutions` | no rows: the round has no current conclusion |
| `latest_plan` | `None`; no next stop is offered |
| the map, scoped | no mode and no region drawn |
| the map, `all` | the round's geometry is **not** drawn |
| `site_overview` | the round is still listed, each entry carrying `superseded_at` |
| `/api/plan` | `analysis_status: superseded`, with the time and the reason |

The overview keeps the row on purpose. It is true as *history* -- it is what
that round concluded from the stops it had -- and dropping it would lose the
record. What it stops being is an answer, so nothing draws it: a polygon on a
map reads as a current claim about where a transmitter is, however carefully
the popup beside it is worded.

The API distinction matters as much as the geometry. An empty plan panel
saying *"no solve has run yet"* sends the operator to a button they have
already pressed; `analysis_status: superseded` with the timestamp says the
solve ran and its conclusions were withdrawn when the membership changed.

A solve clears the mark only after every solution **and** the plan are stored,
so one that dies half-way leaves the mark standing and the old analysis
hidden.

`input_run_ids_json` on the solve that follows contains only runs from the
target campaign, because the scope is a join through `survey_runs` and
membership has already moved.

Nothing here touches a solution carrying `campaign_id IS NULL`. Those mean
*this solve read the whole database*, they are `Historical whole-database
analysis`, and they are neither relabelled nor removed by a curation that had
nothing to do with them.

`campaign_assignments` deliberately carries **no foreign key** to
`survey_runs`. It began with `ON DELETE CASCADE`, which meant deleting a stop
also deleted the record that somebody had once filed it into a round --
destroying the evidence at exactly the moment it is most worth having. The run
id is plain text, so the audit row outlives the run it describes.

The write runs under `project_binding`, so the claim is checked inside the
single `sqlite3.connect` in the codebase and **before** any schema statement.
Asserting it after `connect_geo_database` returned would already have migrated
whatever file was at that path -- including one swapped in between the
read-only preflight and the write.

The two tables are additive and arrive by opening the database, so an existing
database upgrades in place with no manual step and no data loss.

## What this does not do

- It does not move, rewrite or reinterpret any existing row.
- It does not backfill. A historical run is assigned only by `project campaign
  assign-runs`, one an operator named and gave a reason for; no migration, no
  inference from a filename, a site, a date or a position, and no `--all`.
- It does not change the estimator. Campaign scoping decides *which* evidence
  is read; the mathematics that reads it is untouched.
- It does not add an analyzer. There is still exactly one, and site attribution
  is still by frequency alone.
- A view does not change what a campaign *is*. `survey_runs.campaign_id` keeps
  the meaning it has always had; the view is a question asked of it, never an
  edit to it.
