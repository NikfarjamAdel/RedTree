# Design decisions

The choices that shaped this project, with the reasoning behind each and
what was given up for it. Some are design principles; some came from
mistakes, and say so.

## Data model

**Bookings are stored as date ranges and also as one row per night.**
Almost every question is asked per night: occupancy, revenue by month,
seasonality. A stay that crosses a month end belongs to both months, which
is a `GROUP BY` on nights and awkward date arithmetic on ranges. The
per-night table also makes double-booking impossible: a unique constraint
on (listing, night) enforces what no `CHECK` constraint can express on
ranges. Status works the same way: changes are a dated history, not a
column, because the questions are about *when* a booking changed.
*Trade-off:* the nightly rows are derived data. They are regenerated
whenever a booking's dates change, never edited, and a cancelled booking's
nights are deleted, with the cancellation kept in its history.

**Business rules live in views; the star schema only presents them.**
The operational tables are normalised for recording; a star schema of
facts and dimensions is built over them for reading, and rebuilt in full
on every load. The first version loaded the star straight from the tables,
so a rule could exist twice and drift: for a while the nightly fact carried
the price before the platform's fee while the profit view used the net
payout. The direction now is that each fact is loaded from the same view
the reports use (expenses already are), reconciliation checks prove the
star and the views agree, and the dashboard reads only the star.
*Trade-off:* a full rebuild is slower than an incremental load, which is
acceptable at a few thousand rows, and everything downstream inherits any
mistake in a view, so the views carry the tests.

## Ingestion and raw layers

**Raw inputs are kept unchanged in blob storage, written before any row.**
Platform exports, daily calendar snapshots, receipt files, and every paid
service response (document extraction, model answers) are stored as
received. Past calendar states can't be fetched again, extraction models
change over time, and both cost money, so they are treated as raw data that
the database indexes and can be rebuilt from. Writing the file first also
makes failures heal themselves: if the insert fails, the next run finds the
file and completes it, whereas the reverse order leaves rows pointing at
files that don't exist. *Trade-off:* two stores to keep consistent, and
storage that only grows.

**A date the export gets wrong is corrected by a row, applied before
anything is derived from it.**
The platform's earnings export reports a booking's original end date, so an
early departure is invisible in it. The first fix was an `UPDATE` for one
booking, in a script that ran after the nightly rows were generated. It
worked locally only because it had once been applied by hand, and a fresh
deployment failed on the per-night unique constraint. Corrections are now
rows in a table, entered in the property's configuration or detected from
calendar changes, applied before nights are derived, with the latest
correction winning. *Trade-off:* the earnings export stays the source, with
corrections layered on top. The calendar feed has current dates but no
money, so it can't replace the export.

## Receipts and AI

**The language model suggests; a person decides.**
For each receipt the model proposes a category and one action from a fixed
set (accept, not an expense, correct one named field, needs a person), and
the answer is checked in code before it is stored. Free-text suggestions
were tried first and dropped: they couldn't be applied, and tended towards
"contact the supplier". The model never writes an expense. Its actions have
money consequences and its output can't be reproduced, so every expense is
built by one script from the pipeline's values or a person's review. When
review keeps resolving the same supplier the same way, that becomes a
deterministic merchant rule instead of another model call. *Trade-off:*
about one receipt in five still reaches a person. Prompt tuning stopped
when each change helped fewer receipts than the last.

**Model answers are cached by a hash of the exact request.**
With identical inputs, fresh answers on a second database disagreed on 11
receipts that the models had agreed on in the first. So answers are kept
like raw data, keyed by the whole request (model, prompt, schema and
message). A key based on the prompt version alone was rejected, because the
message also carries the merchant rule and the open issues, so a prompt-only
key would return stale answers once either changed. Two databases then get
the same answers, and nothing is paid for twice. *Trade-off:* the cache never
expires; asking again on purpose means changing the request.

**A check that is nearly always a false alarm is removed.**
Reconciling line items against each receipt's total flagged about a
quarter of receipts, and every time the total was right and the lines were
misread: a summary row read as an item, a coupon read with the wrong sign.
Line amounts are now ignored. The total, which is what reaches the books,
is checked from header fields only (subtotal and GST). A check that is
almost always wrong wastes review time and teaches the reviewer to ignore
warnings. *Trade-off:* a genuinely wrong line item goes unnoticed. Only the
total affects the figures.

## Finance model

**Revenue is the net payout, counted in the month each night was stayed.**
Revenue is what reached the bank after the platform's fee; the fee is
neither revenue nor an expense. The payout is spread evenly over the
booking's nights, because the platform pays shortly after check-in.
Counting by payout date would put a stay that crosses a month end entirely
in one month. *Trade-off:* a payout with no nights, such as a cancellation
fee, has nothing to follow and counts in the month it was paid.

**An expense is stored as the full bill; periods and shares are derived
month by month.**
Each expense records what was actually paid. A bill for a period is spread
over it: a quarter's electricity over three months (billed in arrears), a
year's insurance over twelve, a capital item over five years. Each month is
then shared among the property's listings at the rate in force at that
month's end. A rate fixed at purchase would leave a capital item on an old
rate for years, and a rate change is a new dated rule, so history keeps the
rate that was true at the time. *Trade-off:* the bill date stands in for
the service period, so a late bill lands a few days off. These are
management figures, not tax depreciation.

**Costs that don't cost cash are still costed.**
An owner's own clean is recorded at the outsourced rate per turnover, not
at zero. Zero would overstate profit and make outsourcing look worse than
it is. Cleaning is kept apart from other expenses, so its margin against
the guest's cleaning fee stays visible. Loan interest is applied only in
the monthly report, as the listing's share of the interest. It is split
between months by the days each charge covers, and a month is flagged
incomplete until every statement it depends on is entered. *Trade-off:* the
imputed rate is an estimate, and there are two profit figures (operating
profit, and profit after interest), which every report has to label.

## Automation and security

**Secrets live in a key vault; the app is published from an explicit file
list.**
The cloud function reads its secrets through key vault references, using
its own managed identity, which can only read them. An early deployment
relied on the deployment tool's ignore file, and the published package
contained the local `.env` file and part of the data folder. The ignore
pattern didn't match `.env` itself, and the tool's own file listing skips
the ignore file, so it couldn't be used as a check. Worse, the deployed
`.env` quietly supplied a missing setting, so nothing failed. Publishing
now copies an allowlist into a clean folder, refuses if anything
secret-looking is in it, and runs only after the tests pass; the exposed
keys were rotated. *Trade-off:* a new runtime file stays out until it is
added to the list on purpose.

**The live database is migrated, never rebuilt.**
The schema script drops and recreates every table. That is right for a
disposable local database, and wrong for one holding receipts and review
decisions. The build refuses a cloud database unless it has no tables at
all. Every script also refuses to run in a system database, after an early
script created a full set of tables in `master`. *Trade-off:* a schema
change to the live database is written twice, once in the schema script
and once as a guarded migration.

## Testing

**Negative tests must fail for the right reason.**
An early set of constraint tests went unrun for weeks while the schema
moved. Every test still "failed", but on missing columns rather than on the
constraints they were written for, and one quietly succeeded. Now each test
is valid in every respect except the one thing under test, and passes only
if the error names the constraint it expects. Positive tests sit alongside
them, because a constraint that has become too strict is invisible to a
negative test. The rewritten tests immediately found two columns with the
wrong nullability. *Trade-off:* tests have to move with the schema, so
they run on every build, where they can't rot quietly.

**Constraints for what is impossible, assertions for what is suspicious.**
A checkout before check-in, a tax number failing its check digit, or the
same supplier invoice entered twice can't be real, so the database refuses
them. A negative stock balance or two overlapping bookings is only
suspicious. Stock use is estimated between stocktakes, and an overlap is
how an early departure shows up when the freed night is resold. So these
are queries that must return no rows after every load, and the load fails
if any does. One of them compares the staged rows with the loaded bookings,
because an inner join on the listing name would otherwise drop a booking
silently. *Trade-off:* an assertion catches a bad row after it is in,
rather than preventing it.

**Demo data is generated through the real pipeline, not stored.**
The original synthetic history was written once and drifted from the model
as the model changed, especially for expenses, until it misled more than it
showed, so it was removed. The demo build now writes a year of
platform-format exports and loads them through the same transform,
corrections and checks as real data. It adds expenses and loan interest,
and uses a fixed seed, so every build is identical. Its profile is shaped
like a real listing but blurred: money is scaled by an undisclosed factor,
values are rounded, the year is different and every name is invented.
*Trade-off:* the generator has to follow the export's format, so it is
tested against the transform's own formulas.
