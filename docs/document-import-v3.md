# Document import API v3 — B03-233-PR2

This document describes the import lifecycle delivered by PR2 on top of the
additive schema and closed-error dispatcher from PR1. The import routes are
Bearer-only, patient-scoped, private and no-store. Every object lookup first
checks the authenticated nurse's patient association; a missing or foreign
resource returns the same 404. No original filename, storage key, signed URL,
content, or patient data is written to application logs.

## Routes

| Method | Route | Result |
| --- | --- | --- |
| GET | /api/v3/document-capabilities/ | Contract revision and numeric limits |
| GET, POST | /api/v3/patients/{patient_id}/document-imports/ | Recoverable imports; multipart reservation |
| GET, PATCH, DELETE | /api/v3/patients/{patient_id}/document-imports/{import_uuid}/ | Read, revise or abandon one draft |
| PUT | /api/v3/patients/{patient_id}/document-imports/{import_uuid}/confirmation/ | Idempotent prescription creation |
| GET | /api/v3/patients/{patient_id}/document-imports/{import_uuid}/assets/{asset_id}/content/ | Authorized original bytes |

All request and response JSON objects are closed. Lists use the M03 page and
page_size syntax and response keys. HEAD returns the corresponding GET status
and headers without a body; OPTIONS is subject to the same authentication and
patient authorization. OpenAPI exposes only the methods listed in the table;
contract tests assert the complete method set for each import route. Binary responses use a fixed attachment filename,
validated content type, nosniff, private/no-store and no redirect.

## Closed payloads

`metadata` is a JSON object with exactly `prescribing_doctor` (string, up to
300 characters), `start_date` and `end_date` (ISO date or `null`), `source_kind`
(`camera`, `image_import` or `pdf_import`), and `pages` (1–20 entries). Each
page has exactly `asset_index` (zero-based integer), `source_page` (positive
integer), `rotation` (0, 90, 180 or 270) and `crop` (null or `{x,y,width,height}`
with finite normalized numbers inside the page). Multipart contains exactly
one `metadata` part and `original_0` through `original_n`; no filename or MIME
claim is trusted. Unknown dates remain `null` throughout import and confirmation.

An import response has exactly `import_id`, `patient_id`, `state`, `revision`,
`created_at`, `expires_at`, `metadata`, `assets`, `pages`, `prescription_id` and
`error_code`. Before ready, metadata is null and assets/pages are empty. Each
asset has `asset_id`, `mime_type`, `byte_size`, `page_count`, `sha256`, `role`,
`original_index` and `source_asset_id`. Each page has `source_asset_id`,
`source_page`, `display_asset_id`, `display_page`, `rotation` and `crop`.
Transformed previews use a distinct derived asset; the original asset and its
bytes stay unchanged.

The import list is exactly `{count,page,page_size,next_page,previous_page,results}`.
Confirmation PUT accepts exactly `{revision,patient_confirmed,pages_confirmed,
dates_reviewed}`; all three booleans must be true. The Recorded response is
exactly `import_id`, `patient_id`, `prescription_id`, `revision`,
`document_version`, `prescribing_doctor`, `start_date`, `end_date`, `recorded_at`,
`assets` and `pages`. First confirmation returns 201; replay of the confirmed
revision returns the identical record with 200. DELETE returns 204 with no body.
Errors return `{code,message,fields}` with generic messages and no document
content. No endpoint returns a storage URL.

## Limits and validation

Capabilities advertise JPEG, PNG and unencrypted PDF; 20,971,520 bytes of
combined originals, 20 pages, 40,000,000 pixels per image, 200,000,000 total
image pixels, 20,971,520 bytes of combined derived previews, 65,536 bytes of metadata and a 86,400-second draft TTL. A PDF is
one original and must contain 1–20 readable pages. An image request contains
1–20 single-frame images. Mixed PDF/image requests and all other formats fail.
The server checks signatures and parses every file; extension and declared MIME
are ignored. Dates are ISO civil dates or null; no date is inferred.

Each original is scanned through a local ClamAV Unix socket before parsing.
PDF and image parsing runs in a fixed-argument Bubblewrap subprocess with user,
PID, IPC, UTS and network namespaces, read-only Python/runtime mounts only,
private /tmp and /run, no host sockets or secrets, empty environment and no
capabilities. Document bytes arrive only through stdin; a normalized transform
recipe may be passed as a validated argument. The worker is limited to 5 CPU seconds,
512 MiB address space, zero regular-file output, 16 open file descriptors,
64 KiB validation output or 20 MiB plus framing for a derivative, at most two
concurrent parser workers per process and an 8-second parent timeout. Timeout
kills the worker process group. Missing ClamAV, unavailable namespace setup or a breached limit returns
503 and never moves the import to ready. This deployment capability is not
qualified by SQLite tests; the current local environment denies network
namespace setup and is expected to fail closed.

## Idempotency, states and confirmation

Every authenticated POST attempt is rate-limited to three per account per
minute in PostgreSQL before DRF parses the multipart body. Attempt records hold
only the account and timestamp and are purged after 24 hours. POST requires an
Idempotency-Key containing a client UUIDv4. The server
reserves account+UUID before validation and fingerprints the patient, canonical
metadata and ordered original SHA-256 values. Repeating the same request returns
its current import; a different body or patient conflicts. A 120-second lease
allows safe takeover after a worker disappears. The same key and bytes resume
a retryable import. A response timeout is resolved by GET and, for a confirmed
operation, an exact confirmation replay; clients must not mint another key.

States are receiving, validating, ready, retryable, rejected, confirmed,
abandoned and expired. Ready means validated private originals, required
derived previews and reviewable metadata exist; it does not mean an ordonnance
exists. An image crop or rotation is applied inside the isolated worker to a
JPEG derivative linked to its unchanged original. The manifest points display
reads to that derivative and records the already-applied recipe. PATCH requires
the exact If-Match revision header, regenerates affected previews, increments
revision and invalidates previous confirmation before returning to ready.
DELETE abandons an unconfirmed draft and purges only staging objects; confirmed
originals are never deleted by import cleanup.

Confirmation requires the current revision plus patient_confirmed,
pages_confirmed and dates_reviewed all equal to true. One database transaction
locks the import and creates the prescription, version and asset links. A
replay of the same revision returns the same prescription ID. Dates remain null
when unknown. The uploaded bytes remain unchanged and are never replaced by a
derivative. Recorded responses include the immutable version's assets and page
manifest.

The purge command expires drafts after 24 hours and sweeps unreferenced storage
objects, including writes left between object storage and database insertion by
a crashed worker. It excludes confirmed imports, versions and their originals.
Run python src/manage.py purge_document_imports from the backend worktree.
Scheduling, S3 lifecycle/IAM and production activation remain deployment
decisions and are not performed by this change.

## Legacy isolation

All v1 and v2 prescription list, detail, file URL, upload, PATCH and DELETE
queries exclude prescriptions with a v3 document version before pagination,
serialization or storage access. Historical prescriptions retain their
existing routes and response shapes. New v3 records are not projected into
legacy APIs.

## Synthetic verification

The tests test_document_imports.py, test_document_import_validation.py,
test_document_import_purge.py and test_legacy_prescription_import_isolation.py
cover closed schemas, replay, revision and confirmation gates, scanner
protocol/failure, malformed and active/encrypted PDFs, image/page/byte limits,
worker resource enforcement, namespace failure, cleanup and legacy isolation.
PostgreSQL race and API contract evidence is recorded in the story Verify
artifact after execution.
