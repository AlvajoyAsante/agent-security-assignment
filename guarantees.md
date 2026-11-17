# Bounded Security and Safety Guarantees

## Boundary and failure model

The boundary is the provided Python web UI/CLI, `ToolCallingAgent`, its `ToolManager`, and one
airline MCP server process using the supplied JSON database. Assume the model may return arbitrary
text and tool names/arguments at every turn; network calls between the agent and MCP server deliver
the request or fail; and users interact only through the provided UI/CLI. The trusted computing
base (TCB) is the user-confirmation callback/UI, agent tool-call mediation, MCP handlers, the
database lock, and the local filesystem providing atomic `os.replace`. The LLM and prompt-screen
heuristics are not trusted. Actions are committed with their receipts to the ignored
`data/airline/.db.runtime.json` snapshot. Mutations are serialized by one process-wide `RLock`;
confirmation grants are short-lived and single-use. If persistence fails, action state is rolled
back and the grant remains consumed. Restart reloads the durable snapshot. Multiple server
processes, non-atomic network filesystems, administrator edits, deliberate `reset`, corrupt storage,
and third-party payment processors are outside scope.

## Property 1 — Policy-limited, at-most-once compensation

**Claim and scope.** The server can issue only the fixed compensation types and amounts for an
eligible reservation event, at most once per reservation/reason. For a cancelled flight the
server requires a cancelled segment and derives $100 per passenger. For delay it requires a
recorded delayed itinerary followed by a successful reservation change/cancellation and derives
$50 per passenger. The user must also satisfy the policy membership/insurance/business condition
and explicitly choose the UI's **Request compensation** action (or type `REQUEST COMPENSATION` in
the CLI).

**Enforcement and complete mediation.** `send_certificate` in
[tools.py](python/mcp_airline/src/mcp_airline/tools.py) accepts no amount argument, verifies
reservation ownership and event state, and uses the persistent `_security.compensations` key
under the database lock. All certificates in the built-in server can only be created through this
tool; the agent additionally requires an explicit “Request compensation” UI/CLI decision.
Duplicate and concurrent calls see the same key; the durable snapshot preserves it after restart.

**Tests and residual risk.** [test_security_guarantees.py](python/mcp_airline/tests/test_security_guarantees.py)
tests event eligibility, derived amount, concurrent duplicates, and restart/replay. The server
cannot determine whether a user complained or asked for compensation in natural language; the
separate explicit request action is the trusted attestation for that condition. Incorrect source
flight/reservation data and administrative reset are excluded.

## Property 2 — Policy-limited cancellation

**Claim and scope.** A reservation is cancelled only if it belongs to the supplied user, no segment
is flying or landed, and at least one policy condition holds: created within 24 hours, an airline
cancellation, business cabin, or insurance with a health/weather reason. A previously cancelled
reservation cannot be refunded again.

**Enforcement and complete mediation.** The only built-in cancellation mutation is
`cancel_reservation` in [tools.py](python/mcp_airline/src/mcp_airline/tools.py). It obtains
ownership, time, cabin, insurance, and flight status from server-side records; the model-supplied
reason is restricted to an enum. It checks eligibility before consuming confirmation and applies
the refund/status update under the same lock. No prompt decision can bypass this handler.

**Tests and residual risk.** [test_security_guarantees.py](python/mcp_airline/tests/test_security_guarantees.py)
checks an allowed cancellation, an ineligible reservation, a flown segment, and non-owner access.
The guarantee depends on accurate flight status and the simulation clock/data; humans or external
systems changing those records incorrectly are excluded.

## Property 3 — Explicit, exact payment-related confirmation

**Claim and scope.** The built-in booking, cancellation/refund, baggage, flight, passenger, and
compensation mutations execute only after separate user approval of the exact tool name and
arguments shown, and only once for that approval.

**Enforcement and complete mediation.** [agent.py](python/agent/src/agent/agent.py) gates every
protected tool call, rejects model-supplied confirmation IDs, and requests approval before
execution. [tool_manager.py](python/agent/src/agent/tool_manager.py) removes the internal
`authorize_action` tool and `confirmation_id` field from the model-visible schema. The web UI
renders the exact proposal in [webui.html](python/agent/src/agent/templates/webui.html) and posts
the user's decision; the CLI requires a typed phrase.
`authorize_action` records a five-minute, one-use grant hashed over the exact tool name and
arguments. Each mutation consumes and verifies it before changing state. All built-in payment or
refund paths use these handlers.

**Tests and residual risk.** Tests cover absent approval, changed arguments, replay, declined
approval, model attempts to call the hidden grant tool, and persistence failure rollback. Direct
MCP access by users is excluded by the stated threat model; anyone who can invoke the server's
internal grant tool outside the provided agent could bypass the human UI.

## Property 4 — Detectable, server-recorded actions

**Claim and scope.** Every successful built-in mutation has a durable server-authored receipt
containing its action ID, operation, status, user, summary, and relevant details; the UI can
retrieve receipts without asking the model to summarize them.

**Enforcement and complete mediation.** Each mutation appends its receipt in the same atomic
snapshot as its state change. The agent streams receipts separately from model prose and appends
receipt IDs to its final response. The **Verify server-recorded actions** panel calls the
read-only `get_action_history` tool directly through
[webui.py](python/agent/src/agent/webui.py); that tool filters by the user's private ID.

**Tests and residual risk.** [test_security_guarantees.py](python/mcp_airline/tests/test_security_guarantees.py)
checks persisted receipts after restart and rollback on a simulated write failure;
[test_hardening.py](python/agent/tests/test_hardening.py) checks the direct history endpoint.
This proves only the simulated server state, not completion at an external payment/refund
processor. Users must protect their private user ID; compromised local storage is excluded.
