# PR4 acceptance — capture campaign and view scope, on the Pi

**Status: PASS on `76fd80f9ce1933c3a08cbad667b5b8eaefea12d0`, 2026-09-11.**

This is the acceptance run for PR4: the capture campaign separated from the
view scope, the legacy analyses readable again, and the Current / Legacy / All
selector (`docs/projects-and-campaigns.md`, "Capture campaign, view scope, and
browsing safely"). It answers one question — after the update, does the Pi
still record into the campaign it is configured for while showing the 51
rounds that predate campaigns, without either view being able to change
anything — and the answer was yes.

Run by the operator on the Pi. **Recorded here from the operator's report;
this session had no access to the Pi and observed none of it directly.** No
coordinates are recorded here, by instruction, and neither the API token, the
bookmark URL that carries it, nor the TLS private key appears anywhere in this
file. The operator's screenshots are deliberately not included: they show the
map, and the map shows where the receiver is.

## What was updated

| | |
|---|---|
| Merge commit | `76fd80f` (pull request #34) |
| Pi was running | `0c969e8` |
| Pi now running | `76fd80f` |
| Backup taken before the update | `/var/lib/dmr-field/backups/pr4-pre-gCD3asle` |

The backup was taken before the update and was not needed. It is recorded
because the value of naming it is that the next person knows one exists.

## The three views

| View | Shows | Observed |
|---|---|---|
| Current | the campaign being recorded into | 2 runs |
| Legacy | `campaign_id IS NULL` | 51 runs |
| All | every round, grouped and labelled | grouped overview |

Legacy is the one that mattered. Setting `FIELD_CAMPAIGN` had previously
emptied the map and the stop list of every round recorded before campaigns
existed, and the page said "No stops recorded yet." over work that was still
in the file. Those 51 runs are on screen again.

## The historical analyses came back with them

| Check | Observed |
|---|---|
| 868 MHz transmitter analyses in Legacy | present |
| How they are labelled | `Historical whole-database analysis` |
| Shared plan offered on All | none |

The label is the point, not decoration: a conclusion stored without a campaign
was computed from every round in the database at the time, not from the stops
listed beside it. All groups each round separately and offers no next-stop
plan at all, because a plan is computed from one round's evidence and only
means anything inside it.

## Only the current campaign can be changed

| Check | Observed |
|---|---|
| Legacy | read-only |
| All | read-only |
| Current | writable, as before |

## Nothing else moved

| Check | Observed |
|---|---|
| `/etc/dmr-field/field.env` | unchanged |
| API token | unchanged; the phone bookmark kept working |
| TLS certificate and fingerprint | unchanged; no new warning on the phone |
| Database row counts | unchanged |
| Token occurrences in the journal | **0** |

No row was moved, relabelled or backfilled, and none was expected to be: PR4
changed which rows a request reads, never what `campaign_id` holds.

## Not covered by this record

- The application protocol in `pi-smoke-v0.10.md` was not re-run, and neither
  was the reboot protocol in `ops-field-service-acceptance.md`.
- No new capture was taken for this run. The 2 runs in Current were already
  there; what was checked is what the update does to what is on screen.
- No coordinates, by instruction. The screenshots the operator took are not
  in this repository for the same reason.
- Assigning a historical run to a campaign was not exercised, because it does
  not exist. That is PR6's subject and there is deliberately no backfill.
