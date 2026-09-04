# Timestamp spelling in sync hashes

Scope for generalising the `zork_saves` fix in `f2d1337` to every scope that
hashes a timestamp. Not yet implemented.

---

## The defect

A record's sync hash is built from the *string* form of its timestamp. Two
nodes holding the identical record can store that instant in two different
spellings, so they hash it differently and each reads the other as missing
it.

The two spellings come from two writers:

| Path | Format | Example |
| --- | --- | --- |
| Local write (`upsert_zork_save`, `datetime.now().strftime`) | `%Y-%m-%d %H:%M:%S` | `2026-09-03 20:24:24` |
| Received from a peer (`utils.decode_ts_second`, `utils.py:971`) | `%Y-%m-%dT%H:%M:%S` | `2026-09-03T20:24:24` |

A save made here and a save that arrived over the wire are the same instant
written two ways. `encode_ts_second` sends epoch seconds when the peer
advertises the `epoch` capability; `decode_ts_second` turns them back into
the `T` form, and the receiving node stores that verbatim.

**This is not theoretical.** Backing up one `game_scores` row off both nodes
before deleting it produced:

```
bbs:       ('3758096387', 'trivia', 'baconbot', 2200, 0, 12, '2026-09-03 20:24:24')
forgecam:  ('3758096387', 'trivia', 'baconbot', 2200, 0, 12, '2026-09-03T20:24:24')
```

One row, two nodes, two spellings. And the mixture exists *within* a single
table on a single node:

```
mail:       3 rows T-form,  2 rows space
bulletins:  4 rows T-form,  5 rows space
```

### Why it never resolves

The manifest diff says the record is missing. The apply path says the
incoming copy is not newer. Neither can move, so the peer asks again every
cycle. On the live mesh one 366-byte zork save went out **116 times in three
hours** on exactly this loop.

A second bug rides along: comparisons like `_should_replace_zork_save` were
raw string compares, so with equal dates the separator decided the winner —
`'T'` is `0x54`, `' '` is `0x20` — and an older `T`-form record displaced a
newer space-form one.

---

## The surface

Two functions hash rows, and **they do not hash the same columns**. Both
must be changed together for any scope, or they disagree about what a record
is — the scope reports a mismatch the record diff then finds nothing to fix,
and the repair cycle runs forever finding nothing. (That state existed
briefly in `8779c14`, which normalised only the manifest.)

### `get_local_record_counts` — aggregate, drives SYNCSTATE scope mismatch

| Scope | Timestamp columns | Status |
| --- | --- | --- |
| `bulletins` | none | — |
| `mail` | none | — |
| `channels` | `channel_comments.date` | **affected** |
| `zork_saves` | `updated_at` | fixed in `f2d1337` |
| `profiles` | none | — |
| `game_scores` | `achieved_at` | **affected** |
| `public_chatter` | `message_timestamp`, `expires_at` | **affected, see caveat** |

### `get_record_hash_manifest` — per-record, drives which records move

| Scope | Timestamp columns | Status |
| --- | --- | --- |
| `bulletins` | `source_timestamp` | **affected** |
| `mail` | `source_timestamp` | **affected** |
| `channels` | none | — |
| `channel_comments` | `date`, `source_timestamp` | **affected** |
| `public_chatter` | `message_timestamp`, `expires_at` | **affected, see caveat** |
| `profiles` | none | — |
| `game_scores` | `achieved_at` | **affected** |
| `zork_saves` | `updated_at` | fixed in `f2d1337` |
| `tombstones` | key only — `deleted_at` is not hashed | — |

### The asymmetry is itself a finding

`bulletins` and `mail` hash `source_timestamp` in the **manifest** but not in
the **aggregate**. So spelling drift in those two scopes is *dormant*: the
aggregate matches, no repair is triggered, and nothing is noticed.

It stops being dormant the moment the aggregate mismatches for any other
reason. The record diff then reports phantom missing records — present on
both sides, spelled differently — and the loop starts. Any work here should
decide deliberately whether the two functions should hash the same columns,
rather than leaving that to chance.

`mail` is currently reported mismatched against all three peers. That was
measured, and it is **not** spelling drift — see below. It is worth stating
because the two look identical from the mismatch list, and assuming would
have sent this work after the wrong bug.

---

## A separate defect found while scoping this: node ids used as unique_id

All four nodes hold `mail=5` and `bulletins=9`, and `bulletins_hash` is
identical everywhere. Only `mail_hash` differs, and only on bbs
(`oy_Bzx7W8kA` against `TS0EN_oujcE` on both other nodes). The mail
aggregate hashes no timestamp, so drift cannot be the cause.

Comparing the rows found it:

```
bbs:       uid=mqtt:baconbbsv…  subj='This is a not test m…'
forgecam:  uid=mqtt:baconbbsv…  subj='This is a test messa…'
```

Same `unique_id`, different content, permanently. And that `unique_id` is a
**node id** — the other four rows carry UUIDs (`1236d39a-…`,
`137801ea-…`, `e352f2b6-…`).

The same shape appeared in a channel comment during unrelated work: comment
id 15 has `unique_id = 'mqtt:baconbbsvt:Chattanooga'` and sync delimiters
(`0|8a293b60-…`) inside its stored body.

So a control frame is being mis-split somewhere, and a field that should
hold a record id is getting a node id instead. Two different messages then
collide on one bogus id: the id says they are the same record, the content
says otherwise, and neither node can ever win. It is invisible in the counts
— both nodes have five messages and think the other is the one that is
wrong.

This is a **different root cause** from the timestamp drift and wants its
own investigation, starting from the `MAIL|` and `CHANNELCOMMENT|` parsers
in `message_processing.py`. Worth doing first: it is corrupting record
identity, which is a worse failure than a redundant re-send.

---

## Approach

Three parts. The first two are the fix; the third is optional tidying.

**1. Canonicalise at the source.** `decode_ts_second` returns the `T` form
while every local writer uses the space form. Making the decoder agree with
the writers stops new drift entering the database at all. One line, but it
only helps rows written after it ships.

**2. Normalise at hash time, in both functions, for every affected scope.**
This is what makes existing rows converge without rewriting them, and what
lets a node converge with a peer that has *not* been updated — whichever
spelling that peer holds, the hashes match.

Reuse the existing helper, renamed from `_normalize_zork_timestamp` to
something scope-neutral. Prefer normalising in Python over the SQL `CASE`
expression currently used in the aggregate, which is harder to read and
duplicates the rule.

**3. Normalise on write** in the synced-apply paths, so stored data
converges too. Not required for correctness once (2) is in place — the value
is that `sqlite3` output stops being confusing to read.

Every comparison that string-compares two timestamps needs the same
treatment; `_should_replace_zork_save` was one, and the others should be
found by grep rather than assumed absent.

### The `public_chatter` caveat

`expires_at` is derived (`message_timestamp + RETENTION_HOURS`) and rows
leave the scope when they expire, so this hash churns constantly by design.
It is also the scope most likely to be mid-flight during any rollout.
Normalising it is correct but delivers the least, and it carries the most
noise. Consider shipping it separately from the others.

---

## Risks

**Every node must update together.** Changing a hash means an updated node
disagrees with a non-updated one on that scope until both are current. Our
two nodes deploy together so this is a non-issue for them. Chattanooga is
stuck on `0.1.546` and does not accept fleet targets (see HANDOFF.md), so it
will disagree regardless — this changes which scopes it disagrees about, not
whether it does.

**Expect a burst of reconcile traffic on first deploy.** Every affected
scope's hash moves at once, so the first cycle after the switch will look
like a large mismatch. It should settle within a cycle or two. Watch for it;
do not mistake it for a regression.

**One-line changes here are not small.** `decode_ts_second` is shared by
bulletins, mail, channel comments and zork saves (`message_processing.py`
lines 1878, 1914, 1983, 2008, 2432, 2489, 2539). Changing it moves the
stored spelling for all of them at once.

---

## Verification

1. A unit test per affected scope asserting the two spellings of one instant
   produce the same hash, **from both functions**, in the shape of
   `tests/test_zork_save_timestamp_drift.py`.
2. A test asserting the aggregate and manifest agree on the row count for
   each scope — the guard against fixing one and not the other.
3. Mutation-test each: revert the normalisation per scope and confirm a
   named test fails. The zork work had a mutation that "passed" only because
   it raised a `SyntaxError`; check the failure is an assertion.
4. Full suite. Expect `tests/test_sync_state_hashing.py` to need updating —
   it pins hash behaviour deliberately.
5. On the nodes after deploy: confirm `get_mismatched_peer_scopes()` between
   our own two nodes converges to empty (or to `public_chatter` alone, which
   churns). That is the real proof, and it is available today as a baseline
   — take it before shipping so there is something to compare against.

---

## Estimate

Parts 1 and 2, excluding `public_chatter`: a few hours including tests and
mutation checks. `public_chatter` and part 3 are separable and can follow.

The honest reason to do it: the `zork_saves` loop was found only because it
was noisy enough to notice. `bulletins` and `mail` carry the same defect
silently, and the conditions that wake it up are ordinary.
