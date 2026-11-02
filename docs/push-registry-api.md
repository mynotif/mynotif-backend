# B07 closed server registry

This additive lot implements the server side of the accepted B07 D1–D4 contract
(Backend #237). It is **closed to real use**: `B07_API_ENABLED` is false and
`get_registry()` always refuses assembly. Setting a flag alone cannot activate
it. No provider adapter, credentials, KMS client, sender or test double is
installed by the application. Test doubles live only under `src/tests/` and are
injected by tests. No Expo push service is used; future control delivery must use
direct FCM/APNs, under the qualification gates below.

## HTTP contract

All routes are under `/api/v2/push/`, require a valid B01 Bearer access token,
reject legacy Token authentication and return `Cache-Control: private, no-store`.
The closed assembly returns 503 for authenticated requests. The following
behaviour is exercised through an injected synthetic assembly only.

| Method/path                      | Input                                                                                                           | Result                                                                   |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| POST `enrollments/`              | `operation`, UUID `request_id`, allowlisted `namespace`, write-only `token`, P256 `public_key`, OS `permission` | 202 public pending context; no nonce                                     |
| GET `enrollments/{id}/`          | Same session                                                                                                    | Pending context or current nonsecret result; never a nonce/token         |
| POST `enrollments/{id}/confirm/` | Write-only `nonce`, `signature`; same session                                                                   | 201 active, or 200 recovered confirmation                                |
| GET `bindings/`                  | Current account                                                                                                 | Eligible bindings, versions, namespace; no token or key                  |
| GET `bindings/{id}/`             | Current account                                                                                                 | Database revocation, barrier confirmation and in-flight state separately |
| DELETE `bindings/{id}/`          | Current account                                                                                                 | Database revocation, then conditional 503/200/202 barrier result         |

Unknown fields are rejected. Account/session/ownership versions are derived
server-side. Namespaces identify the exact provider/app/environment and must be
explicitly allowlisted by the future assembly. FCM tokens are bounded ASCII;
APNs tokens are 64 lowercase hex characters. Permission `denied` cannot enroll;
a client losing permission must revoke the binding. A rotation uses
`operation=rotate` plus `previous_binding_id`, a new token and a fresh key. Its
old binding remains eligible until successful confirmation commits.

Errors contain an allowlisted code only: 400 invalid request/proof, 401 invalid
session, 403 no authorisation/permission, 404 foreign or missing object, 409 body
conflict/pending/fresh-key conflict, 410 expiry, 412 superseded, 429 quota and
503 unavailable or unconfirmed barrier. A quota refusal returns `Retry-After: 60`;
this is a conservative polling delay, not a promise that a longer quota has ended.

## D1 proof and transactions

The signed bytes are ASCII `ORDOPRO-B07-CONTROL-V1`, followed by each UTF-8 field
with a uint32 big-endian byte length, in this exact order:

`operation`, `enrollment_id`, `request_id`, `account_ref`, `session_ref`,
`namespace`, `token_digest`, `public_key_digest`, `expected_epoch`,
`expected_generation`, `expires_at`, `nonce`.

UUIDs are lowercase canonical strings, numbers decimal without leading zeroes,
digests lowercase SHA256 hex. Public keys are uncompressed 65-byte SEC1 points
on P-256. Signatures are strict ASN.1 DER ECDSA-SHA256 (no double hashing).
Binary fields use canonical base64url without padding. The signature is never
the idempotency key. The 32-byte CSPRNG nonce exists only in memory and the
control delivery; the database retains a distinct-key HMAC commitment.

The pending request lasts at most 5 minutes; nonce use ends at 120 seconds.
One control delivery is attempted after the pending transaction commits. An
initial replay never redelivers, including after a crash. A failed delivery
abandons and scrubs pending secrets. A valid proof atomically consumes the
pending, compares destination epoch/generation, revokes its previous binding,
advances versions, and creates the new binding. Other old pending requests are
superseded. A new pending alone does not evict another pending or an active
binding. Known old public-key digests are refused for a new enrollment.

The user row, session and access token are re-read inside the committing
transaction; eligibility is an active account and current B01 session/token,
including password fingerprint and expiry. B01 logout, revoke-all, password
change/reset and account deletion use the same user lock. Destination row locks,
database uniqueness and CAS protect ownership. This first closed lot also
serializes enrollment/quota writes on a registry row; throughput qualification
and finer locking are future work, not implied by these correctness tests.

Confirmation recovery lasts 24 hours only while that same binding remains
current and the session authorised. Superseded results never restore ownership.
After result expiry the caller must read current state and perform a new full
enrollment if needed. Callbacks identify binding plus generation; unknown or old
callbacks never look up a current destination by token.

A fresh A request after transfer to B cannot activate from a token or key alone.
The honest client B ignores a control without its exact locally initiated pending
for the current session. New enrollment always needs a fresh client key. After
reinstallation/lost non-backed-up marker, the client must discard inherited
keys/pending and prove control again. These are tested client simulations here;
compromised OS, stolen current sessions and colluding relays are outside D1's
claim. Physical Android/iOS behaviour still requires C05 qualification.

Sliding-window quotas are charged atomically before delivery: 1/60 seconds and
5/hour/destination, 5/hour/session, 20/day/account, 60/hour/IP plus an explicit
global hourly budget. Missing budget/keys/ports fail closed. Counter identifiers
use a separate HMAC key and expire after 24 hours. The immediate peer IP is used;
proxy/NAT configuration and global provider budgets need qualification. Five
invalid confirmations abandon the pending; quotas never evict another owner.

## Revocation, retention and restore

B01's existing 204 response continues to acknowledge **session revocation**. It
does not claim an egress barrier or withdrawal of provider/OS notifications.
B07 removes eligible bindings and active ciphertext in the same transaction.
Its barrier port returns 503 without explicit evidence; confirmed evidence
returns 200 only for `in_flight=none`, otherwise 202
`in_flight=unknown_or_present`. A database revoke, a confirmed egress barrier and
an attempt already engaged are distinct. The default port never confirms.

Ingestion can only seal tokens: fresh AES-256-GCM data key per binding/version,
96-bit random nonce, 128-bit tag. AAD binds schema version, namespace, binding,
generation and restore epoch. Key wrapping/unwrapping are separate capabilities;
the future KMS/IAM adapter is absent. The transport-only decryption primitive has
no registry/API caller and does not grant D2 admission. Active tokens expire with
their session or after at most 30 days, and are deleted at revoke/rotation.

`purge_push_registry` scrubs expired pending authority and purges 24-hour results
and counters and 30-day tombstones, without decrypting. Logical expiry applies
even when maintenance is delayed. Deployment must schedule expiry maintenance
plus daily catch-up; after outage, purge precedes reopening. Account deletion
removes pending data and owner/session references from retained tombstones.

`purge_push_registry --close-restored` changes the restore epoch, closes the
registry and erases restored token authority. There is no reopen command in this
lot. Provider network must already be closed before restoration and remain so;
sessions or keys from a backup cannot reactivate bindings. Backups/replicas must
expire within 30 days under Infra custody controls; this code does not attest
that hosting policy. Rollback keeps the additive tables, generations and
revocations, and cuts admissions. Do not reverse schema, replay history or return
to an unconstrained sender.

## Integration gates

B06 owns the event reservation, explicit device selection and immutable route
snapshot. Enrollment never selects a device. The B07 ports preserve empty
selection, exclude ambiguous/unproven routes, require proven absence of
admission for snapshot replacement and expose partial result counts. Each
selected physical device uses one channel. Physical route correspondence is an
upstream qualified assertion, never inferred from a client identifier.

B08 owns the exclusive serialized egress, attempt state and uncertain provider
results. Its T20 “accepted, response lost” recovery remains blocked. There is no
native retry or web fallback here, and registry/outbox/fence/collapse alone do
not prove zero visible duplicates. Old OneSignal subscriptions and senders are
unchanged; activating coexistence requires removal of every bypass through B06,
B08, Web and Infra first. No M08 work is included.

Before external trials or activation: C05 must qualify control reception,
P256 bridges, permission, A→B, reinstall/backup and multi-device Android/iOS;
B06/B08/Web must qualify reservation/snapshot/concurrent senders and T20;
Infra/C04 must qualify KMS/IAM, exclusive network paths, process/socket
neutralisation, restoration, backup expiry and observability; C04/C15 must
supply the required domain opinions and authorisations. Synthetic tests are not
these integrated proofs. No real tokens, patient data or provider sends are
authorised by this lot.
