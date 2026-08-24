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

## F-005 - Carve-out tests passed while the carve-out rule was disabled
**Date:** Day 5

**What broke.** `test_high_value_case_is_carved_out_and_never_holdout` and
`test_lending_emi_case_is_carved_out_and_never_holdout` both asserted
`arm != HOLDOUT` against fixture customer `cust_TESTFIXTURE042`. That customer
hashes to RULES with or without the carve-out rule, so the assertion that
carved cases are never held out was vacuously true. Both tests still passed with
the carve-out branch deleted from `arm_for_customer`.

**Why.** The fixture customer id was picked for readability, not for the
property under test. A test asserting "X never lands in bucket B" says nothing
if X was never going to land in bucket B.

**How detected.** Mutation check, not a failing run: the carve-out branch was
deliberately removed and the suite re-run. Only the 500-customer sweep test
caught it; the two named tests did not.

**What changed.** Carve-out tests now use `HOLDOUT_CUSTOMER =
"cust_TESTFIXTURE003"`, whose untreated draw genuinely is HOLDOUT. With the
rule removed, all three carve-out tests now fail.

**Regression test.** `tests/test_assign.py::test_holdout_fixture_customer_really_is_holdout`
asserts the fixture's untreated arm, so the tests cannot go quietly vacuous
again if `ASSIGNMENT_SALT` or `ARM_WEIGHTS` changes.

---

## F-006 - Restore after a mutation check silently did nothing
**Date:** Day 5

**What broke.** During the F-005-style mutation check on `warrant/core.py`, the
version guard was temporarily removed from the conditional UPDATE and the file
restored with `git checkout -- warrant/core.py`. `core.py` was new and untracked,
so the checkout was a no-op that only printed `error: pathspec ... did not
match`. The mutated, guard-less `core.py` stayed in the working tree.

**Why.** `git checkout --` restores tracked files. A brand-new module has no
committed version to restore from, and the failure is a message on stderr, not a
non-zero pipeline abort in the middle of a chained command.

**How detected.** The verification run immediately after the restore still
reported `3 failed`. Reading the count rather than assuming the restore worked
is what caught it.

**What changed.** Restored from a filesystem copy taken before the mutation, and
mutation checks now back up to a scratch copy rather than relying on git for
untracked files. Suite re-verified green afterwards.

**Regression test.** None; this is a process failure, not a code one. The
guard's own coverage is `tests/test_core.py::test_stale_expected_version_raises_and_leaves_state_unchanged`,
which is what reported the 3 failures.

---

## F-007 - The specified state machine made the retry cap unreachable
**Date:** Day 4

**What broke.** A strictly forward-only transition table has no edge back to
`action_queued`. `MAX_ATTEMPTS_PER_CASE = 3` would then be dead configuration:
no case could ever reach a second attempt, and the Day 6 gate proving the cap
rejects a fourth attempt would have nothing to reject.

**Why.** The state list and the attempt cap were specified in different places
and never checked against each other.

**What changed.** `action_executed -> action_queued` is the one deliberate cycle
in `ALLOWED`, documented as such. The cap stays a policy gate (Day 6): the state
machine says what is structurally possible, the policy engine says what is
permitted.

**Regression test.** `tests/test_core.py::test_retry_cycle_is_legal`.

---

## F-008 - Two frozen documents disagreed about the set of case types
**Date:** Day 5

**What broke.** `CALIBRATION.md` lists exactly two case types,
`one_time_link` and `upi_autopay`. `EXPERIMENT.md` pre-registers a carve-out on
`case_type == 'lending_emi'`. Validating `case_type` against the calibration
list -- the obvious reading of the Day 4 spec -- would have made the
pre-registered carve-out unreachable and untestable.

**Why.** The calibration file describes what the *simulator generates*; the
experiment file describes what the *assignment layer must handle*. Those are not
the same set, and neither document said so.

**How detected.** Reading both files before writing `core.py`, while deciding
whether `case_type` should be a closed enum.

**What changed.** `core.SIMULATED_CASE_TYPES` documents the two generated types
without enforcing them. `lending_emi` remains constructible and carved out. The
distinction is written into both module docstrings so it is not re-litigated.

**Regression test.** `tests/test_assign.py::test_lending_emi_case_is_carved_out_and_never_holdout`.

---

## F-009 - The regulatory gate is unreachable under the shipped configuration
**Date:** Day 6

**What broke.** `npci_autopay_cap` fires at `NPCI_AUTOPAY_MAX_ATTEMPTS` (4), but
`attempt_cap` fires at `MAX_ATTEMPTS_PER_CASE` (3) and is evaluated first. No
case can ever reach four attempts, so the one gate in the engine that enforces a
real regulation can never be the gate that owns a verdict.

**Why.** The two caps were set in different sections of `config.py` for
different reasons -- one product policy, one NPCI -- and the gate order was
specified separately from both. Nothing forced the three to be read together.

**How detected.** Writing the test for the gate: there is no attempt count that
satisfies `attempts >= 4` without first satisfying `attempts >= 3`.

**What changed.** Nothing in the order or the values -- being stricter than the
regulation is the correct direction, and reordering to make the gate fire would
mean loosening the product cap. The gate is kept and documented as shadowed,
because product policy is ours to loosen later and the regulation is not. The
two NPCI tests lift the product caps with `monkeypatch` so the regulatory gate
is exercised on its own terms.

**Regression test.** `tests/test_policy.py::test_npci_cap_refuses_an_autopay_case_at_the_regulatory_limit`
(the gate works when reachable) and
`::test_npci_cap_does_not_fire_on_a_one_time_link_at_four_attempts` (it stays
scoped to autopay). The second fails if the `case_type` check is removed --
verified by mutation.

---

## F-010 - Two gate tests asserted a state the caps make unreachable
**Date:** Day 6

**What broke.** `test_one_attempt_below_the_cap_still_executes` and the
one_time_link NPCI test both expected EXECUTE at 2 and 4 attempts respectively.
Both returned DO_NOTHING under `frequency_cap`. A case's own attempts are also
contacts to that case's customer, and the customer cap (2 per 7 days) is
stricter than the per-case cap (3), so a case that burns its attempts inside one
week is stopped at two -- by the customer gate, not the case gate.

**Why.** The two caps read as independent because they are configured
independently and named for different units, per-case and per-customer. They
share a counter: every attempt is a contact.

**How detected.** The first run of `tests/test_policy.py`: 2 failed, 26 passed.
The failure message named `frequency_cap` as the rule that fired.

**What changed.** The behaviour is correct and was left alone -- a customer with
three failed payments is still one person being messaged. The tests were wrong,
not the engine. `test_frequency_cap_shadows_the_attempt_cap_inside_the_window`
now pins the interaction explicitly, and the NPCI tests lift both product caps
rather than only the per-case one.

**Regression test.** `tests/test_policy.py::test_frequency_cap_shadows_the_attempt_cap_inside_the_window`.

---

## F-011 - Mutation sweep of all nine gates
**Date:** Day 6

**Not a failure.** Recorded because the absence of one is the claim, and F-005
showed that "the tests pass" and "the tests would catch it" are different
statements.

Each gate was removed from `policy.GATES` in turn (via a scratch pytest plugin,
not committed) and the policy suite re-run. Every gate was caught by at least
the test written for it:

| dropped gate | tests that failed |
|---|---|
| `arm_holdout` | 2 |
| `terminal_state` | 2 |
| `attempt_cap` | 3 |
| `npci_autopay_cap` | 1 |
| `frequency_cap` | 2 |
| `cohort_spend` | 1 |
| `low_confidence` | 1 |
| `unknown_action` | 1 |
| `ev_threshold` | 5 |

Separately, removing the `case_type == 'upi_autopay'` condition from
`gate_npci_autopay_cap` -- making the regulatory gate universal, the F-002
over-claim in code form -- was caught by
`test_npci_cap_does_not_fire_on_a_one_time_link_at_four_attempts`. Note that the
shipped-config variant of that test does NOT catch it, because `attempt_cap`
fires first (F-009); the monkeypatched variant is the one that bites.

---

## F-012 - Mutation check of the actuator's two load-bearing rules
**Date:** Day 7

**Not a failure.** Nothing broke while building `warrant/act.py`: the 26 tests
passed on their first run. Recorded because "the tests passed" is not the claim
being made -- F-005 is the reason that distinction is now written down every
time.

**Mutation A - call first, record afterwards.** `act.execute` was replaced with
an actuator that makes the external call and then writes the intent, which is
the design the module exists to reject. Caught by 7 tests, including both
ordering tests:
`test_intent_row_exists_before_the_external_call` (the injected callable asserts
its own precondition) and
`test_intent_is_committed_before_the_call_not_merely_written` (a second
connection reads the ledger from inside the call, so a row written but not
committed would not be visible).

**Mutation B - timeout becomes FAILED.** The `TimeoutError` branch was changed
to settle FAILED instead of UNKNOWN. Caught by 14 tests. The blast radius is the
point: UNKNOWN is not a status the module mentions once, it is the state the
whole reconcile/retry/cancel path is built around, so misclassifying it takes
half the suite with it.

**Restore verified by checksum**, not by assumption -- `warrant/act.py` was
untracked at the time and `git checkout --` would have silently done nothing
(F-006).

---

## F-013 - Two attempt counters that can disagree
**Date:** Day 7

**What broke.** Nothing yet, and that is why it is here rather than discovered
on Day 8. There are now two ways to count attempts on a case:
`policy.attempts_for_case()` counts transitions into `action_queued`, while
`act.next_attempt()` counts rows in the intent ledger. They agree only if the
controller writes both for every attempt.

**Why.** Day 6 needed an attempt count before the intent ledger existed, so it
counted the only record available at the time -- the state machine. Day 7 added
a second record with a better claim to the number.

**How detected.** Writing `next_attempt()` and noticing it answered a question
`policy.attempts_for_case()` already answers differently.

**What changed.** Nothing, deliberately. Changing the Day 6 gate to read the
ledger would mean editing a module whose gates are already mutation-verified,
for a wiring problem that belongs to the controller. `act.py` documents that it
does not move the case through its state machine -- `core.transition()` needs
the authoritative version at write time, and an actuator that guessed it would
race the deliveries the version guard exists to survive.

**Regression test.** None yet. The Day 8 controller must write the
`action_queued` transition and create the intent in the same step, and that is
where the test belongs.

---
