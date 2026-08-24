# What broke, and how it was fixed

Written as it happens. Not reconstructed at the end.

Format: what broke -> why -> how it was detected -> what changed -> what test
now prevents regression.

---

## F-001 - Sample size inconsistent with the quoted effect size
**Date:** Day 2 (pre-code)

**What broke.** The design draft paired a "61% vs 34%" headline with
n=1500/arm. Those are incompatible: a 27pp effect needs ~50 per arm; 1500 is
the number for a 5pp effect.

**Why.** The headline number was chosen for demo impact and the sample size
was chosen from a different, unstated effect size. Nobody reconciled them.

**How detected.** Design review, before any code existed. Would have been
caught in the panel Q&A by anyone who has run an experiment.

**What changed.** Effect size restated as 34% -> 39% (+5pp), which is the
order of magnitude real payment interventions produce. n=1452 derived from
that, in code, not asserted in prose.

**Regression test.** `tests/test_stats.py::test_preregistered_sample_size_is_1452`
and `::test_large_effect_needs_far_fewer`.

---

## F-002 - Regulatory over-claim
**Date:** Day 2 (pre-code)

**What broke.** A draft treated an RBI 08:00-19:00 contact window as a hard
gate on all recovery messaging.

**Why.** That rule governs digital lending / recovery agents. It was applied
to subscription and D2C dunning without verifying scope.

**How detected.** Source check during calibration.

**What changed.** Removed. Timing and frequency limits are now labelled
product policy. The one regulatory gate we do enforce - NPCI's one execution
plus three retries - is scoped to `case_type == 'upi_autopay'` only, where it
is verified to apply.

**Regression test.** To be added with the policy engine on Day 6: a
`one_time_link` case must not trigger the NPCI gate.

---

## F-003 - FastAPI startup handler was deprecated
**Date:** Day 3

**What broke.** The webhook app used `@app.on_event("startup")`. Tests passed
but emitted 13 DeprecationWarnings.

**Why.** Written from an older FastAPI idiom without checking the current one.

**How detected.** Running the suite and reading the warnings instead of only
the pass count. Warnings are not noise.

**What changed.** Replaced with an `asynccontextmanager` lifespan handler
passed to `FastAPI(lifespan=...)`.

**Regression test.** None added. The existing `tests/test_app.py` exercises
startup on every run; the warning count is visible in CI output.

---

## F-004 - .env.example silently truncated to zero bytes
**Date:** Day 3

**What broke.** A `cp` during Day 2 packaging emptied `.env.example`. Anyone
cloning would have had no idea which environment variables to set.

**Why.** A shell brace-expansion `cp` failed partway and was re-run in a form
that created an empty file instead of copying content.

**How detected.** Day 3 repository inspection, before writing code.

**What changed.** Rewritten with all four variables and a note that the webhook
secret is chosen by the developer and is not the API key secret.

**Regression test.** None. Documentation file; no runtime dependency.

---
