# Prescription document storage v3

This additive storage schema prepares B03 document imports. It does not change
the existing v1/v2 prescription fields or response contracts. The import and
asset models use account-scoped idempotency UUIDs, nullable source dates,
versioned prescription metadata, opaque private-storage keys, content hashes,
and explicit draft state/lease fields. Application services must still enforce
patient authorization, immutable object writes, validation before ready,
atomic confirmation and cleanup. The schema alone does not qualify those
behaviors or production storage.

The v3 URL namespace is mounted with Bearer-only lazy dispatch. Until the
dependent import/read handlers are delivered, matching routes fail closed with
503 service_unavailable; legacy v1/v2 remain the only functional APIs in this
intermediate PR. Merge/activate the complete B03 sequence before clients use
v3.

Historical Prescription rows remain unchanged. A v3 read adapter must
synthesize their first document representation from the historical image while
new imports use versioned assets. No migration copies or deletes existing file
objects.
