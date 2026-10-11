# Account and private file API

All endpoints below require HTTPS outside synthetic local development. Authentication
credentials and signed URL query strings must not be logged or persisted in shared
caches. Responses containing sessions or private file URLs use `private, no-store`.

## Registration and profile

`POST /api/v2/account/register` accepts `email`, `password`, and optional `first_name`
and `last_name`. It returns 201 with the user profile and Nurse representation; it
never returns the password. Username is derived from email. Existing v1 registration
and Djoser `/api/v1/auth/users/` retain their required username input. Django password
validators apply and account/Nurse creation is atomic.

`GET /api/v1/profile/` and the current-user/Djoser profile routes preserve the fields
`id`, `username`, `email`, `first_name`, `last_name`, `is_staff`, and `nurse`.
Only names can be updated through `PUT`/`PATCH /api/v1/user/current/` or the equivalent
Djoser own-user routes. Read-only identity fields are ignored. Supplying a password
rejects the whole update with 400. Djoser username-change/reset routes return 405.

Password changes use `/api/v1/auth/users/set_password/` with `current_password` and
`new_password`. Reset confirmation uses `/api/v1/auth/users/reset_password_confirm/`
with `uid`, `token`, and `new_password`. These operations revoke all opaque sessions
and the legacy DRF token within the password transaction. Reset tokens are revalidated
under the user lock, preventing concurrent reuse.

## Opaque sessions

| Method and path                              | Authentication          | Successful response     |
| -------------------------------------------- | ----------------------- | ----------------------- |
| POST `/api/v2/auth/sessions/`                | Public                  | 201 token pair          |
| POST `/api/v2/auth/sessions/refresh/`        | Public, refresh in body | 200 token pair          |
| GET `/api/v2/auth/sessions/`                 | Bearer access           | 200 own active sessions |
| DELETE `/api/v2/auth/sessions/current/`      | Bearer access           | 204                     |
| DELETE `/api/v2/auth/sessions/{session_id}/` | Bearer access, owner    | 204                     |

Login accepts email, password, and an optional device label of at most 80 characters.
The pair contains `session_id`, `access_token`, `refresh_token`, `token_type` (Bearer),
`access_expires_at`, `expires_in`, `refresh_expires_at`, and `session_expires_at`.
Dates are ISO timestamps; `expires_in` is the remaining number of seconds.
Sessions are listed as an unpaginated array with `session_id`, `device_label`,
`created_at`, `expires_at`, `last_seen_at`, and `current`. Lists exclude expired or
revoked sessions and never expose another user's sessions.

A session expires absolutely after 30 days by default; refresh never extends it.
Access expires after 15 minutes, bounded by the session deadline. Send
`Authorization: Bearer <access_token>`. Existing resource routes still accept
`Authorization: Token <legacy_token>`; session management routes require Bearer.

Refresh accepts `refresh_token` and a UUIDv4 `request_id`. The client must save one
operation ID with its parent refresh before sending and use a single in-flight
rotation. If the response is lost, retry the same parent and operation within the
60-second recovery window. This returns the same child pair and original deadlines,
with a decreasing `expires_in`. A consumed parent with a different operation ID
revokes the session. A retry whose child generation was already superseded returns 409. Logout/reset/absolute expiry also prevent recovery.

Invalid credentials return 401, malformed inputs 400, throttled requests 429, and
unknown/foreign sessions 404. Service-level session errors contain `code` and
`message`; field validation and authentication failures retain DRF error shapes.
Clients must check HTTP status before consuming a success payload.

## Private prescription files

`GET /api/v1/prescription/{id}/file-url/` requires normal resource authentication
and verifies prescription access before consulting storage. It returns `url`,
`expires_at`, and `expires_in`. Unknown or foreign prescriptions and unavailable
files return 404; temporary storage failures return 503 without storage details.

The default URL lifetime is 300 seconds. Existing `photo_prescription` responses
contain a temporary URL or the empty string when no file is attached. Fetch a fresh
URL when opening a file rather than retaining a URL from an old patient list. A link
already issued remains a capability until expiration even after logout or access
removal; never treat a signed link as a permanent resource identifier.

## Configuration and activation

| Setting                         | Default | Allowed seconds |
| ------------------------------- | ------- | --------------- |
| `AUTH_SESSION_MAX_AGE_SECONDS`  | 2592000 | 1–2592000       |
| `AUTH_ACCESS_TTL_SECONDS`       | 900     | 1–900           |
| `AUTH_REFRESH_RECOVERY_SECONDS` | 60      | 1–60            |
| `PRIVATE_FILE_URL_TTL_SECONDS`  | 300     | 1–900           |

Apply `main.0003_auth_sessions` before enabling the session endpoints. The migration
adds session/token tables without converting legacy tokens or business records.
The dependency correction in `main.0002_row_level_security` orders Nurse creation
before historical PostgreSQL RLS SQL.

The existing web client continues using legacy Token login. Web session controls and
renewal of private URLs require a separate frontend integration. Before activating
private-file behavior, verify a page left open beyond the URL lifetime, fresh opening,
deleted files, removed permissions, authentication expiry, and a bounded retry after
storage failure. Synthetic backend tests and schema validation do not qualify a
production deployment or real storage permissions.
