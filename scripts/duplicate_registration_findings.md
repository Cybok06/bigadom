# Duplicate registration review — 28 September 2026

A read-only scan of the configured MongoDB database examined 292 loans and 774 customers. No database records were changed.

## Screenshot confirmed

| Loan | Created at (UTC) | Principal | Status at audit |
| --- | --- | --- | --- |
| LNA-20260927-D5D9 | 2026-09-27 20:56:13.646 | GHS 1,000 | active |
| LNA-20260927-135C | 2026-09-27 20:56:14.638 | GHS 1,000 | active |

These loans share the same customer ID and creator, with matching customer and guarantor details. They were created 0.992 seconds apart. The screenshot therefore shows two loan records for one customer, not proof of two customer registrations. Their status has changed from the screenshot's pending state to active.

Other loans show the same rapid repeat pattern, including intervals below one second and sequences of three or four records. The customer scan also found 25 groups with the same trimmed, case-insensitive name, digit-only phone and agent ID. These are review candidates, not confirmed erroneous customers; phone country-code variants were not reconciled.

## Cause and limits of evidence

The loan POST handler previously performed one unconditional insert per request. The form had no submission lock or server-side replay protection. The customer POST handler also inserted unconditionally; its frontend disabled the button but re-enabled it in `finally`, even while navigating after success, and had no in-flight guard.

The stored data strongly supports repeated submissions being accepted. It cannot establish whether a person tapped twice, the browser resubmitted, or a network intermediary retried. No historical HTTP request trace was available in this review. There is no second insert in the reviewed loan application handler.

## Local fix

Both forms now carry signed IDs bound to the submitting user and form type. MongoDB's existing unique `_id` constraint makes repeated or simultaneous submissions of that form resolve to one record. Retried customer submissions return the original customer ID. The browser locks submission while sending; customer success stays locked during navigation. Invalid/missing tokens require reloading the form. A deliberately opened new form gets a new ID and can create a new record; this change does not impose a one-loan-per-customer policy.

Five isolated regression tests passed, covering simultaneous insertion, token tampering/user/type isolation, repeated loan/customer POSTs, missing tokens, a new application form, and unrelated unique-index failures. Tests use database doubles and do not write to production.

Changes have not been deployed. Previously opened forms must be refreshed after deployment. Existing duplicate candidates, especially active loans and any associated payments, require separate reconciliation; this patch does not delete, cancel, or merge them.

Run the read-only audit with `python scripts/audit_duplicate_registrations.py`.
