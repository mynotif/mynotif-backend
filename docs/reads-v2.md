# Private patient reads v2 — M03-C

Normative design: [mobile contracts-v2](https://github.com/mynotif/ordopro_mobile/blob/main/docs/agentic/work/OP-M03/contracts-v2.md), blob `821310cbe0da90aa58a181a6c98cf2458ba13e3f`, confirmed by mobile PR #73. Backend tickets #267/#268; mobile #71 remains open until adapters and Android evidence are delivered. Availability is established only by the recorded synthetic tests, not by this design or a production deployment.

`GET /api/v2/patients/` uses only the opaque M02 `Authorization: Bearer <access_token>` scheme. Historical `Token` credentials are rejected with 401 and `WWW-Authenticate: Bearer`. v1 URLs, array responses, `fields` selection and historical/Bearer authentication remain available. M02 session responses are unchanged.

The queryset is restricted to the current account's existing Nurse associations; staff/admin status grants no extra rights. Reads never create a Nurse. A missing Nurse produces an empty patient page. HEAD returns the GET status/headers without a body. Authenticated OPTIONS returns only `{"allowed_methods":["GET","HEAD","OPTIONS"]}`. Mutations on this valid route return 405 after authentication.

The patient summary has exactly `id`, `firstname`, `lastname`, `city`, `phone`, `birthday`; the last field is an ISO date or null. No email, document URL, clinical status, address or health-card data appears in this schema. Only these columns are selected. OpenAPI is generated through `/swagger.json` and `/swagger.yaml`, with Bearer security and closed components `PatientSummary`, `PatientPage`, `ReadError`. HEAD/OPTIONS behavior is also described on the GET operation.

## Query and pagination

Only one occurrence each of `page`, `page_size`, `search` is accepted. Unknown fields, including `fields`, `ordering`, `patient`, and any duplicate, including empty or identical `search`, return 400. `page` and `page_size` follow ASCII `[1-9][0-9]*` without coercion: page 1…2147483647, default 1; size 1…100, default 20. Oversized numbers are rejected before integer conversion.

Search accepts at most 100 Unicode code points before normalization. Apply NFD, remove Unicode category M, casefold, then split on Unicode whitespace. Empty/whitespace-only search does not restrict results. Each term must occur in at least one of firstname, lastname, city, phone (AND terms, OR fields). `%`, `_`, `*` are literal; native database substring-position functions (INSTR on SQLite, STRPOS on PostgreSQL) compare them literally, including NUL on SQLite. PostgreSQL text cannot contain NUL, so a NUL-containing term yields no matches without binding it. No NFKD/unaccent approximation is used. The four internal fields introduced by #267 are not API fields. See [normalization design](agentic/work/267-m03-search/design.md) for maintenance and rollout gates.

Rights → global SQL search → distinct total → `id ASC` → SQL LIMIT/OFFSET. No unbounded patient materialization in Python. The envelope has exactly `count`, `page`, `page_size`, `next_page`, `previous_page`, `results`. Neighbors are adjacent integers or null, never URLs. Empty page 1 has count 0, both neighbors null, results empty; pages beyond the last (at least 1) return `page_not_found`. Ordering is stable on unchanged data, without a cross-request snapshot guarantee.

## Errors and privacy

All controlled errors have exactly `code` and `message`:

| HTTP | code                    | message                                |
| ---- | ----------------------- | -------------------------------------- |
| 400  | invalid_query           | Paramètres de lecture invalides.       |
| 401  | authentication_required | Authentification requise.              |
| 404  | not_found               | Ressource indisponible.                |
| 404  | page_not_found          | Page indisponible.                     |
| 405  | method_not_allowed      | Méthode non autorisée.                 |
| 429  | rate_limited            | Trop de demandes. Réessayez plus tard. |
| 503  | service_unavailable     | Service indisponible. Réessayez.       |

No operational throttle rate is enabled for reads by this change; if configured later, 429 carries a nonnegative integer Retry-After. Controlled database connection failures return 503 without their raw messages. Unexpected HTTP 500 is outside the normative stable-error table: it keeps status 500 and uses the existing generic Django REST JSON `{error: "Server Error (500)"}`, including under DEBUG. It carries private headers and exposes no exception body. This is a security fallback, not a new stable mobile error schema or a success response. Successes and controlled errors carry `Cache-Control: private, no-store` and `Pragma: no-cache`, without ETag/304. JSON is used even for an HTML Accept header. Configured CORS origins/headers and legacy-route preflights stay in place. On these new reads, CORS preflight OPTIONS also passes through the required Bearer/object checks; it cannot short-circuit authentication. An unauthenticated browser preflight receives 401. Mobile HTTP clients are unaffected.

A middleware scopes diagnostics to read route templates. Log records are redacted after structured extras are attached: query, credentials, IDs, request objects, SQL parameters and exception bodies are removed. Sentry events/transactions/breadcrumbs for these reads are suppressed. Gunicorn's explicitly loaded configuration omits URI, query, headers, user and bodies, including in Docker. Synthetic marker tests exercise these boundaries.

**Real proxy/App Runner/hosting log settings have not been qualified.** Before real data, independently prove that upstream access logs, analytics and custom handlers omit the entire query string and patient/prescription IDs. Application tests and Gunicorn's format do not establish that external guarantee. No production setting, database or deployment was changed.

## Verification and rollback

Use `scripts/m03-synthetic.sh` for pytest/make checks; it rejects ancestor `.env` files and clears inherited credentials. `scripts/m03-http-proof.py` creates its own disposable SQLite database, seeds fictional patients, starts a loopback Gunicorn worker and prints only route templates/methods/statuses. Verify contains the executed commands and results.

Deployment requires separate authorization. Retire old writers and finish/check normalized-field repair before enabling v2 search on existing records. Roll back routes first; keep v1 and the additive columns. Do not roll back columns beneath code that references them. No adapter or Android flow is validated: a backend 401 after refresh proves only that response, not mobile purge or navigation to login.

## Existing PostgreSQL RLS limitation

Historical main migrations enable and FORCE RLS on nurse_nurse_patients without defining a policy (main/migrations_helpers.py and main.0002_row_level_security). A database role without superuser/BYPASSRLS cannot read those associations; SQLite and PostgreSQL superuser tests do not qualify that role. This is an existing database boundary, not an implicit staff/admin grant at the API. No policy has been changed. Before claiming PostgreSQL/runtime availability, prove the intended database role and existing policies in an isolated synthetic environment; if a restricted role is required, separate reviewed RLS work is needed. No production role or data was inspected.
