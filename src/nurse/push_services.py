"""D1 registry transactions. Lock order: user, session, registry, destinations.

The registry row serializes enrollment/CAS/quota writes in this closed first lot.
This intentionally favours correctness over throughput; B06/B08 are separate.
"""

import hmac
import json
import secrets
import uuid
from datetime import timedelta
from functools import wraps

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from main.models import AuthSession, AuthToken
from main.session_services import fingerprint
from nurse import push_crypto as crypto
from nurse.push_custody import associated_data
from nurse.push_interfaces import (
    ClosedBarrier,
    RegistryError,
    private_push,
    revocation_result,
)
from nurse.push_models import (
    PushBinding,
    PushDestination,
    PushEnrollment,
    PushQuota,
    PushRegistryState,
    PushTokenVersion,
)

PENDING_TTL = timedelta(minutes=5)
NONCE_TTL = timedelta(seconds=120)
RESULT_TTL = timedelta(hours=24)
RETENTION = timedelta(days=30)


def private_operation(function):
    @wraps(function)
    def call(*args, **kwargs):
        with private_push():
            try:
                return function(*args, **kwargs)
            except RegistryError:
                raise
            except Exception:
                # SDK/DB exceptions must not export sensitive arguments.
                raise RegistryError() from None

    return call


def _context(auth):
    """Re-read authorisation under the same user lock as reset/revoke/delete."""
    session_id, token_id = auth
    candidate = AuthSession.objects.filter(pk=session_id).first()
    if candidate is None:
        raise RegistryError("invalid_session", 401)
    user = (
        get_user_model()
        .objects.select_for_update()
        .filter(pk=candidate.user_id)
        .first()
    )
    session = AuthSession.objects.select_for_update().filter(pk=session_id).first()
    token = AuthToken.objects.filter(
        pk=token_id, session_id=session_id, kind="access"
    ).first()
    if user is None or session is None or token is None:
        raise RegistryError("invalid_session", 401)
    now = timezone.now()
    if (
        session.revoked_at
        or session.expires_at <= now
        or token.revoked_at
        or token.expires_at <= now
        or session.password_fingerprint != fingerprint(user)
    ):
        raise RegistryError("invalid_session", 401)
    if not user.is_active:
        raise RegistryError("not_authorized", 403)
    return user, session


def _scrub(enrollment, status):
    enrollment.status = status
    enrollment.commitment = enrollment.public_key = ""
    enrollment.envelope = enrollment.context = {}
    enrollment.save(
        update_fields=(
            "status",
            "commitment",
            "public_key",
            "envelope",
            "context",
            "failures",
        )
    )


def _revoke_bindings(query, now):
    """Caller owns relevant user/destination locks. No egress ACK is implied."""
    ids = list(query.filter(revoked_at__isnull=True).values_list("pk", flat=True))
    PushTokenVersion.objects.filter(binding_id__in=ids).delete()
    PushBinding.objects.filter(pk__in=ids).update(
        revoked_at=now, barrier_confirmed=False
    )
    return ids


def _invalidate(query, pending, now):
    destination_ids = sorted(set(query.values_list("destination_id", flat=True)))
    for destination in (
        PushDestination.objects.select_for_update()
        .filter(pk__in=destination_ids)
        .order_by("pk")
    ):
        if _revoke_bindings(query.filter(destination=destination), now):
            destination.epoch += 1
            destination.retain_until = now + RETENTION
            destination.save(update_fields=("epoch", "retain_until"))
    for enrollment in pending.filter(status="pending"):
        _scrub(enrollment, "revoked")


@private_operation
def invalidate_session(session_id, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        session = AuthSession.objects.filter(pk=session_id).first()
        if session:
            get_user_model().objects.select_for_update().filter(
                pk=session.user_id
            ).first()
            AuthSession.objects.select_for_update().filter(pk=session_id).first()
        _invalidate(
            PushBinding.objects.filter(session_id=session_id),
            PushEnrollment.objects.filter(session_id=session_id),
            now,
        )


@private_operation
def invalidate_account(user_id, *, erase=False):
    with transaction.atomic():
        # Also protects direct ORM/admin deletion, not just the HTTP controller.
        get_user_model().objects.select_for_update().filter(pk=user_id).first()
        _invalidate(
            PushBinding.objects.filter(owner_id=user_id),
            PushEnrollment.objects.filter(owner_id=user_id),
            timezone.now(),
        )
        if erase:
            PushEnrollment.objects.filter(owner_id=user_id).delete()
            PushBinding.objects.filter(owner_id=user_id).update(
                owner=None, session=None
            )


class Registry:
    def __init__(
        self,
        *,
        restore_epoch,
        namespaces,
        lookup_key,
        commitment_key,
        quota_key,
        custody,
        control,
        barrier=None,
        emission_budget=None,
    ):
        keys = (lookup_key, commitment_key, quota_key)
        if (
            not restore_epoch
            or not namespaces
            or any(not isinstance(k, bytes) or len(k) < 32 for k in keys)
            or len(set(keys)) != 3
            or not emission_budget
            or custody is None
            or control is None
        ):
            raise RegistryError()
        self.restore_epoch = restore_epoch
        self.namespaces = namespaces
        self.lookup_key, self.commitment_key, self.quota_key = keys
        self.custody, self.control = custody, control
        self.barrier = barrier or ClosedBarrier()
        self.emission_budget = emission_budget

    def _state(self):
        state = PushRegistryState.objects.select_for_update().filter(pk=1).first()
        if (
            state is None
            or not state.accepting
            or str(state.restore_epoch) != str(self.restore_epoch)
        ):
            raise RegistryError()

    def _destination(self, namespace, token, now):
        lookup = crypto.keyed_digest(
            self.lookup_key, namespace.encode("ascii"), token.encode("ascii")
        )
        destination, _ = PushDestination.objects.get_or_create(
            namespace=namespace,
            lookup=lookup,
            defaults={"retain_until": now + RETENTION},
        )
        return PushDestination.objects.select_for_update().get(pk=destination.pk)

    def _quotas(self, destination, session, user, ip, now):
        # Sliding windows, not wall-clock buckets that allow boundary bursts.
        stamp = now.timestamp()
        rules = (
            ("destination", str(destination.pk), ((60, 1), (3600, 5))),
            ("session", str(session.pk), ((3600, 5),)),
            ("account", str(user.pk), ((86400, 20),)),
            ("ip", ip, ((3600, 60),)),
            ("global", "control", ((3600, self.emission_budget),)),
        )
        records = []
        for kind, identity, limits in rules:
            key = crypto.keyed_digest(self.quota_key, kind.encode(), identity.encode())
            record, _ = PushQuota.objects.get_or_create(
                key=key, defaults={"expires_at": now + RESULT_TTL}
            )
            events = [t for t in record.events if t > stamp - 86400]
            if any(
                sum(t > stamp - window for t in events) >= maximum
                for window, maximum in limits
            ):
                raise RegistryError("rate_limited", 429)
            records.append((record, events))
        for record, events in records:
            record.events = events + [stamp]
            record.expires_at = now + RESULT_TTL
            record.save(update_fields=("events", "expires_at"))

    @private_operation
    def enroll(self, auth, body, ip):
        from nurse.push_serializers import EnrollmentSerializer

        serializer = EnrollmentSerializer(data=body)
        if not serializer.is_valid():
            raise RegistryError("invalid_request", 400)
        body = serializer.validated_data
        namespace, token = body["namespace"], body["token"]
        provider = self.namespaces.get(namespace)
        if provider not in ("fcm", "apns") or (
            provider == "apns"
            and (len(token) != 64 or any(c not in "0123456789abcdef" for c in token))
        ):
            raise RegistryError("invalid_request", 400)
        if body["permission"] != "granted":
            raise RegistryError("permission_required", 403)
        encoded_body = json.dumps(
            body, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        body_digest = crypto.keyed_digest(self.commitment_key, b"request", encoded_body)
        with transaction.atomic():
            now = timezone.now()
            user, session = _context(auth)
            self._state()
            user, session = _context(auth)
            now = timezone.now()
            old = PushEnrollment.objects.filter(
                session=session, request_id=body["request_id"]
            ).first()
            if old:
                if not hmac.compare_digest(old.body_digest, body_digest):
                    raise RegistryError("request_conflict", 409)
                return self._enrollment_result(old, now), 202
            destination = self._destination(namespace, token, now)
            user, session = _context(auth)
            now = timezone.now()
            for expired in PushEnrollment.objects.filter(
                session=session,
                destination=destination,
                status="pending",
                nonce_expires_at__lte=now,
            ):
                _scrub(expired, "expired")
            if PushEnrollment.objects.filter(
                session=session, destination=destination, status="pending"
            ).exists():
                raise RegistryError("pending_exists", 409)
            key_digest = crypto.digest(crypto.b64decode(body["public_key"], 65))
            if (
                PushBinding.objects.filter(public_key_digest=key_digest).exists()
                or PushEnrollment.objects.filter(
                    status="pending", context__public_key_digest=key_digest
                ).exists()
            ):
                raise RegistryError("fresh_key_required", 409)
            previous = None
            if body["operation"] == "rotate":
                previous = PushBinding.objects.filter(
                    pk=body["previous_binding_id"],
                    owner=user,
                    session=session,
                    revoked_at__isnull=True,
                    expires_at__gt=now,
                ).first()
                if previous is None:
                    raise RegistryError("binding_not_found", 404)
            self._quotas(destination, session, user, ip, now)
            enrollment = PushEnrollment(
                owner=user,
                session=session,
                destination=destination,
                request_id=body["request_id"],
                body_digest=body_digest,
                restore_epoch=self.restore_epoch,
                expected_epoch=destination.epoch,
                expected_generation=destination.generation,
                previous_binding_id=previous.pk if previous else None,
                previous_epoch=previous.epoch if previous else None,
                previous_generation=previous.generation if previous else None,
                public_key=body["public_key"],
                created_at=now,
                expires_at=now + PENDING_TTL,
                nonce_expires_at=now + NONCE_TTL,
                result_until=now + RESULT_TTL,
            )
            nonce = crypto.b64encode(secrets.token_bytes(32))
            enrollment.context = {
                "operation": body["operation"],
                "enrollment_id": str(enrollment.pk),
                "request_id": str(body["request_id"]),
                "account_ref": str(uuid.uuid4()),
                "session_ref": str(uuid.uuid4()),
                "namespace": namespace,
                "token_digest": crypto.digest(token.encode("ascii")),
                "public_key_digest": key_digest,
                "expected_epoch": str(destination.epoch),
                "expected_generation": str(destination.generation),
                "expires_at": str(int(enrollment.nonce_expires_at.timestamp())),
            }
            enrollment.commitment = crypto.keyed_digest(
                self.commitment_key,
                crypto.canonical({**enrollment.context, "nonce": nonce}),
            )
            aad = associated_data(
                namespace,
                enrollment.binding_id,
                destination.generation + 1,
                self.restore_epoch,
            )
            enrollment.envelope = self.custody.seal(token, aad)
            _context(auth)
            if timezone.now().timestamp() >= int(enrollment.context["expires_at"]):
                raise RegistryError("proof_expired", 410)
            enrollment.save()
            result = self._enrollment_result(enrollment, now)
        # Durable pending before the one attempt. A crash cannot enqueue a replay.
        try:
            self.control.deliver(
                enrollment_id=str(enrollment.pk),
                nonce=nonce,
                namespace=namespace,
                envelope=enrollment.envelope,
                aad=aad,
            )
        except Exception:
            with transaction.atomic():
                failed = PushEnrollment.objects.select_for_update().get(
                    pk=enrollment.pk
                )
                if failed.status == "pending":
                    _scrub(failed, "abandoned")
            raise RegistryError("control_unavailable", 503) from None
        return result, 202

    def _enrollment_result(self, enrollment, now):
        if enrollment.result_until <= now:
            raise RegistryError("result_expired", 410)
        if enrollment.status == "confirmed":
            return self._current_result(enrollment, now)
        if enrollment.status == "superseded":
            raise RegistryError("superseded", 412)
        if enrollment.status != "pending" or enrollment.nonce_expires_at <= now:
            raise RegistryError("proof_expired", 410)
        return {"state": "pending_control", "context": enrollment.context}

    def _current_result(self, enrollment, now):
        binding = PushBinding.objects.filter(
            pk=enrollment.binding_id,
            owner=enrollment.owner,
            session=enrollment.session,
            revoked_at__isnull=True,
            expires_at__gt=now,
            restore_epoch=self.restore_epoch,
        ).first()
        if binding is None:
            raise RegistryError("superseded", 412)
        destination = PushDestination.objects.get(pk=binding.destination_id)
        if (
            destination.epoch != binding.epoch
            or destination.generation != binding.generation
        ):
            raise RegistryError("superseded", 412)
        return enrollment.result

    @private_operation
    def confirm(self, auth, enrollment_id, nonce, signature):
        error = None
        with transaction.atomic():
            now = timezone.now()
            user, session = _context(auth)
            self._state()
            user, session = _context(auth)
            now = timezone.now()
            enrollment = PushEnrollment.objects.filter(
                pk=enrollment_id, owner=user, session=session
            ).first()
            if enrollment is None:
                raise RegistryError("enrollment_not_found", 404)
            previous = (
                PushBinding.objects.filter(pk=enrollment.previous_binding_id).first()
                if enrollment.previous_binding_id
                else None
            )
            ids = {enrollment.destination_id}
            if previous:
                ids.add(previous.destination_id)
            destinations = {
                d.pk: d
                for d in PushDestination.objects.select_for_update()
                .filter(pk__in=ids)
                .order_by("pk")
            }
            enrollment.refresh_from_db()
            destination = destinations[enrollment.destination_id]
            user, session = _context(auth)
            now = timezone.now()
            if enrollment.result_until <= now:
                raise RegistryError("result_expired", 410)
            if enrollment.status == "confirmed" and enrollment.result_until > now:
                return self._current_result(enrollment, now), 200
            if enrollment.status == "superseded" or (
                destination.epoch,
                destination.generation,
            ) != (enrollment.expected_epoch, enrollment.expected_generation):
                raise RegistryError("superseded", 412)
            if (
                enrollment.status != "pending"
                or enrollment.nonce_expires_at <= now
                or now.timestamp() >= int(enrollment.context["expires_at"])
            ):
                _scrub(enrollment, "expired")
                error = RegistryError("proof_expired", 410)
            else:
                try:
                    context = {**enrollment.context, "nonce": nonce}
                    commitment = crypto.keyed_digest(
                        self.commitment_key, crypto.canonical(context)
                    )
                    if not hmac.compare_digest(enrollment.commitment, commitment):
                        raise crypto.InvalidProof()
                    crypto.verify(enrollment.public_key, signature, context)
                except (crypto.InvalidProof, TypeError):
                    enrollment.failures += 1
                    if enrollment.failures >= 5:
                        _scrub(enrollment, "abandoned")
                    else:
                        enrollment.save(update_fields=("failures",))
                    error = RegistryError("invalid_proof", 400)
            if error is None:
                user, session = _context(auth)
                now = timezone.now()
                if now.timestamp() >= int(enrollment.context["expires_at"]):
                    raise RegistryError("proof_expired", 410)
                if enrollment.previous_binding_id:
                    if previous:
                        previous.refresh_from_db()
                    if (
                        previous is None
                        or previous.revoked_at
                        or previous.owner_id != user.pk
                        or previous.session_id != session.pk
                        or previous.expires_at <= now
                        or (previous.epoch, previous.generation)
                        != (enrollment.previous_epoch, enrollment.previous_generation)
                    ):
                        raise RegistryError("superseded", 412)
                    _revoke_bindings(PushBinding.objects.filter(pk=previous.pk), now)
                    if previous.destination_id != destination.pk:
                        prior_dest = destinations[previous.destination_id]
                        prior_dest.epoch += 1
                        prior_dest.save(update_fields=("epoch",))
                _revoke_bindings(
                    PushBinding.objects.filter(destination=destination), now
                )
                destination.epoch += 1
                destination.generation += 1
                destination.retain_until = now + RETENTION
                destination.save(update_fields=("epoch", "generation", "retain_until"))
                binding = PushBinding.objects.create(
                    id=enrollment.binding_id,
                    destination=destination,
                    owner=user,
                    session=session,
                    restore_epoch=self.restore_epoch,
                    epoch=destination.epoch,
                    generation=destination.generation,
                    public_key_digest=enrollment.context["public_key_digest"],
                    created_at=now,
                    expires_at=min(session.expires_at, now + RETENTION),
                )
                PushTokenVersion.objects.create(
                    binding=binding, envelope=enrollment.envelope
                )
                enrollment.result = {
                    "state": "active",
                    "binding_id": str(binding.pk),
                    "epoch": binding.epoch,
                    "generation": binding.generation,
                    "selected": False,
                }
                enrollment.result_until = now + RESULT_TTL
                enrollment.save(update_fields=("result", "result_until"))
                _scrub(enrollment, "confirmed")
                for pending in PushEnrollment.objects.filter(
                    destination=destination, status="pending"
                ).exclude(pk=enrollment.pk):
                    _scrub(pending, "superseded")
                result = enrollment.result
        if error:
            raise error
        return result, 201

    @private_operation
    def enrollment(self, auth, enrollment_id):
        with transaction.atomic():
            now = timezone.now()
            user, session = _context(auth)
            self._state()
            user, session = _context(auth)
            now = timezone.now()
            enrollment = PushEnrollment.objects.filter(
                pk=enrollment_id, owner=user, session=session
            ).first()
            if enrollment is None:
                raise RegistryError("enrollment_not_found", 404)
            return self._enrollment_result(enrollment, now)

    @private_operation
    def bindings(self, auth):
        with transaction.atomic():
            now = timezone.now()
            user, session = _context(auth)
            self._state()
            user, session = _context(auth)
            now = timezone.now()
            return [
                {
                    "binding_id": str(b.pk),
                    "namespace": b.destination.namespace,
                    "epoch": b.epoch,
                    "generation": b.generation,
                    "selected": False,
                }
                for b in PushBinding.objects.filter(
                    owner=user,
                    revoked_at__isnull=True,
                    expires_at__gt=now,
                    restore_epoch=self.restore_epoch,
                    session__revoked_at__isnull=True,
                    session__expires_at__gt=now,
                    session__password_fingerprint=fingerprint(user),
                    epoch=F("destination__epoch"),
                    generation=F("destination__generation"),
                ).select_related("destination")
            ]

    @private_operation
    def binding_status(self, auth, binding_id):
        with transaction.atomic():
            user, _ = _context(auth)
            binding = PushBinding.objects.filter(pk=binding_id, owner=user).first()
            if binding is None:
                raise RegistryError("binding_not_found", 404)
            return {
                "binding_id": str(binding.pk),
                "database_revoked": binding.revoked_at is not None,
                "barrier_confirmed": binding.barrier_confirmed,
                "in_flight": binding.in_flight,
            }

    @private_operation
    def revoke(self, auth, binding_id):
        with transaction.atomic():
            now = timezone.now()
            user, _ = _context(auth)
            binding = PushBinding.objects.filter(pk=binding_id, owner=user).first()
            if binding is None:
                raise RegistryError("binding_not_found", 404)
            PushDestination.objects.select_for_update().get(pk=binding.destination_id)
            _context(auth)
            now = timezone.now()
            _invalidate(
                PushBinding.objects.filter(pk=binding.pk),
                PushEnrollment.objects.filter(binding_id=binding.pk),
                now,
            )
            binding.refresh_from_db()
        try:
            evidence = self.barrier.confirm(
                binding_id=str(binding.pk),
                epoch=binding.epoch,
                generation=binding.generation,
            )
        except Exception:
            evidence = None
        result, status = revocation_result(binding, evidence)
        if status in (200, 202):
            PushBinding.objects.filter(pk=binding.pk, revoked_at__isnull=False).update(
                barrier_confirmed=True, in_flight=evidence.in_flight
            )
        return result, status


@private_operation
def invalidate_callback(binding_id, generation):
    """An unknown/old callback is a no-op; never look up the current token."""
    with transaction.atomic():
        binding = PushBinding.objects.filter(
            pk=binding_id, generation=generation
        ).first()
        if binding is None:
            return False
        _invalidate(
            PushBinding.objects.filter(pk=binding.pk, generation=generation),
            PushEnrollment.objects.none(),
            timezone.now(),
        )
        return True


@private_operation
def purge_registry(now=None):
    now = now or timezone.now()
    with transaction.atomic():
        # Same global order as mutations; maintenance never acquires a user lock.
        PushRegistryState.objects.select_for_update().filter(pk=1).first()
        expired = PushBinding.objects.filter(revoked_at__isnull=True).filter(
            Q(expires_at__lte=now)
            | Q(session__expires_at__lte=now)
            | Q(session__revoked_at__isnull=False)
            | Q(session__isnull=True)
            | Q(owner__isnull=True)
            | Q(owner__is_active=False)
        )
        # Password fingerprints are hashes, so check the small active set without
        # loading token envelopes. Query-time eligibility already closes expiry.
        ids = set(expired.values_list("pk", flat=True))
        for binding in PushBinding.objects.filter(
            revoked_at__isnull=True, owner__isnull=False, session__isnull=False
        ).select_related("owner", "session"):
            if binding.session.password_fingerprint != fingerprint(binding.owner):
                ids.add(binding.pk)
        _invalidate(
            PushBinding.objects.filter(pk__in=ids), PushEnrollment.objects.none(), now
        )
        for pending in PushEnrollment.objects.filter(status="pending").filter(
            Q(expires_at__lte=now) | Q(nonce_expires_at__lte=now)
        ):
            _scrub(pending, "expired")
        enrollments = PushEnrollment.objects.filter(result_until__lte=now).delete()[0]
        bindings = PushBinding.objects.filter(revoked_at__lte=now - RETENTION).delete()[
            0
        ]
        quotas = PushQuota.objects.filter(expires_at__lte=now).delete()[0]
        PushDestination.objects.filter(
            retain_until__lte=now, bindings__isnull=True, enrollments__isnull=True
        ).delete()
    return {
        "enrollments_purged": enrollments,
        "bindings_purged": bindings,
        "quotas_purged": quotas,
    }


@private_operation
def close_restored_registry():
    """Mandatory restore fence: erase authority first; no reopen operation here."""
    with transaction.atomic():
        state, _ = PushRegistryState.objects.select_for_update().get_or_create(pk=1)
        state.accepting = False
        state.restore_epoch = uuid.uuid4()
        state.save(update_fields=("accepting", "restore_epoch"))
        for destination in PushDestination.objects.select_for_update().order_by("pk"):
            _revoke_bindings(
                PushBinding.objects.filter(destination=destination), timezone.now()
            )
            destination.epoch += 1
            destination.save(update_fields=("epoch",))
        for pending in PushEnrollment.objects.filter(status="pending"):
            _scrub(pending, "restored")
    return {"state": "closed"}


def get_registry():
    """No configured production adapter exists in B07-PR1. Always fail closed.

    Tests patch this constructor seam. No setting imports or activates a double;
    a later integration PR must supply qualified custody/control/egress ports.
    """
    raise RegistryError()
