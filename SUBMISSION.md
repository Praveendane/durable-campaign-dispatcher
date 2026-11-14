# Submission

## Candidate

- **Name:** Praveen Dane
- **Assignment chosen:** B (Workflow)
- **Time spent (honest estimate):** 10

## How to run

Run the following commands from the project root.

### 1. Create and activate a virtual environment

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 3. Start the mock telephony provider

Open Terminal 1:

```bash
cd assignment-b-workflow
python -m mock_telephony
```

For a clean development run without injected failures:

```bash
python -m mock_telephony --failure-scale 0
```

### 4. Start the webhook receiver

Open Terminal 2 from the project root:

```bash
uvicorn solution.app.main:app --host 127.0.0.1 --port 8000
```

### 5. Start the dispatcher

Open Terminal 3 from the project root:

```bash
python -m solution.app.runner
```

### 6. Run tests

```bash
python -m pytest -q
```

Finalization tests:

```bash
python -m pytest solution/tests/test_finalization.py -q
```

The dispatcher exports `final_state.csv` to the project root after validating the final lead states against the provider.

## Design

I implemented a database-backed state machine using Python, FastAPI, SQLAlchemy, and SQLite.

The application has two processes:
- **Webhook receiver:** verifies provider signatures and processes call-result events.
- **Dispatcher runner:** imports fixtures, schedules eligible leads, submits calls, reconciles unfinished requests, finalizes leads, and exports the final CSV.

The database stores campaigns, leads, calls, webhook events, phone suppressions, and active-call locks.

### Lead state machine

```text
pending
   |
   v
submitting
   |
   +---- Accepted ----> calling
   |                      |
   |                      v
   |                 Webhook result
   |                      |
   |          +-----------+-----------+
   |          |           |           |
   |       Terminal    Callback     Retry
   |          |           |           |
   |          v           v           v
   |      completed  callback_wait  retry_wait
   |                      |           |
   |                      +-----+-----+
   |                            |
   |                            v
   |                         pending
   |
   +---- Unknown outcome ----> uncertain
   |                              |
   |                              v
   |                         Reconciliation
   |
   +---- Confirmed rejection --> not_placed

Final states:
completed / exhausted / dnd_blocked
```

### At-most-once dialling and concurrency

- A unique request ID is generated and persisted before contacting the provider.
- The same request ID is reused during reconciliation, allowing the provider's idempotency mechanism to prevent duplicate accepted calls.
- The `active_call_locks` table uses `phone_hash` as its primary key, preventing simultaneous calls to the same phone across campaigns.
- A unique `(tenant_id, slot_number)` constraint enforces tenant-wide concurrency limits.
- Locks are released after confirmed rejection or call-result processing.
- Calls in uncertain states are reconciled before new leads are dispatched.

The implementation is designed for a single dispatcher process; horizontal scaling has not been verified.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| SQLite with SQLAlchemy | Provides persistent state and database constraints without requiring an external database server | PostgreSQL |
| Database-backed state machine | Keeps lead and call progress durable across process restarts | Temporal workflow engine |
| UUID request IDs | Provides a stable idempotency key for provider requests | Generating a new ID for every retry |
| Phone hash as lock key | Prevents simultaneous calls to the same phone across campaigns | In-memory phone locks |
| Tenant concurrency slots | Enforces a shared concurrency limit across campaigns | Per-campaign concurrency counters |
| Provider simulated clock | Business rules must use provider time rather than machine time | Local system clock |
| Reconcile before new dispatch | Recovers unfinished requests and lost results before submitting more calls | Dispatching new leads first |
| Callbacks before retries and fresh leads | Follows the specified scheduling priority | FIFO scheduling |

## Failure modes considered

| Failure | Handling |
|---|---|
| Duplicate webhook | Unique event key and processed-event check |
| Invalid webhook signature | Rejects the request before processing |
| Provider 503 or 429 | Records confirmed rejection without incrementing accepted attempts |
| Ambiguous 504 | Looks up the original request ID and retries with the same ID when appropriate |
| Lost webhook | Reconciles active and uncertain calls using the provider API |
| Early webhook | Preserves the webhook result when the provider response arrives afterward |
| Process interruption | Persists requests and reconciles unfinished calls on the next runner cycle |
| Duplicate phone activity | Database phone lock |
| Tenant concurrency exceeded | Unique tenant slot constraint |
| DND disposition | Creates global phone suppression and blocks other eligible leads |

### Known limitations

- A separate end-to-end `kill -9` restart test has not been confirmed.
- Horizontal scaling with multiple dispatcher processes has not been tested.
- Dedicated automated tests for every provider timeout, webhook redelivery, and ambiguous-response scenario would improve coverage.
- The current SQLite implementation is intended for a single dispatcher process rather than distributed workers.

## Testing

### Automated tests

Executed:

```bash
python -m pytest -q
```

Result: **43 passed**

Finalization tests:

```bash
python -m pytest solution/tests/test_finalization.py -q
```

Result: **7 passed**

### Manual verification

- Ran the dispatcher against the mock telephony provider using the clean development configuration.
- Verified that the provider reported zero calling-rule violations.
- Verified that no calls remained active at completion.
- Checked that the final CSV export validates lead states, accepted attempts, last status, and last disposition against the provider's authoritative report.
- Added regression coverage for callback preservation and exhaustion at the maximum attempt count.

A separate process-kill/restart integration test has not been confirmed.

## Results

Recorded clean simulation:

- Total leads processed: **200**
- Completed: **139**
- Exhausted: **60**
- DND blocked: **1**
- Total provider calls: **407**
- Active calls at completion: **0**
- Provider violations: **0**
- Webhook events recorded: **407**

The runner successfully exported `final_state.csv` and checked its lead states, attempt counts, last status, and last disposition against the provider's report.

The recorded run used the clean provider configuration (`--failure-scale 0`).

## If I had another day

1. Perform and document multiple `kill -9` and restart tests.
2. Add automated tests for ambiguous 504 responses and lost webhook recovery.
3. Test concurrent webhook delivery and duplicate processing.
4. Run the dispatcher against failure-injected provider scenarios.
5. Add structured logging and per-campaign progress monitoring.
6. Evaluate PostgreSQL and multi-worker coordination for production-scale workloads.