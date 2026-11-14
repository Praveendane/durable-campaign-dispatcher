# AI Usage Disclosure

## Tools used

- ChatGPT, Claude

## What I used them for

- Understanding the assignment requirements and breaking the implementation into manageable steps.
- Reviewing the scheduling, dispatcher, webhook, and finalization logic against the business rules.
- Debugging Python and pytest errors.
- Getting guidance on handling provider timeouts, ambiguous 504 responses, duplicate webhooks, and lost webhook recovery.
- Developing regression tests for callback handling and maximum-attempt finalization.
- Reviewing test results and preparing technical documentation.

## What I wrote or decided myself

- Worked on the project implementation and executed the commands in my local development environment.
- Reviewed the suggested code changes before applying them.
- Made decisions about the database-backed state machine, SQLite persistence, request tracking, and project structure.
- Ran the application and evaluated the results against the assignment requirements.
- Decided which changes to retain based on test results and observed behavior.

## Where the AI was wrong or unhelpful

During finalization work, an initial test-file update did not preserve the original test fixture and existing tests. This caused a pytest error because the required fixture was missing. I identified the issue from the test output and restored the original tests alongside the new regression tests.

The callback finalization logic also required correction. A callback should remain pending when attempts are available, but a lead that has reached its maximum attempts must become exhausted rather than receive another call.

## How I verified AI-generated code

- Ran the complete test suite using `python -m pytest -q` — 43 tests passed.
- Ran the finalization tests separately — 7 tests passed.
- Reviewed the relevant Python implementation and test files.
- Executed the dispatcher against the mock telephony provider using the clean development configuration.
- Checked the provider report and confirmed zero calling-rule violations in the recorded simulation.
- Verified that the final CSV export compares lead states, accepted attempt counts, last call status, and last disposition against the provider's report.

I reviewed and tested AI-assisted suggestions before retaining them. Scenarios that were not independently verified have not been claimed as successfully tested.