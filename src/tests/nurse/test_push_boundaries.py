from unittest.mock import patch

import pytest

from nurse.push_custody import decrypt_for_transport
from nurse.push_interfaces import (
    BarrierResult,
    SelectedRoute,
    snapshot_replace_allowed,
    summarize_results,
    unambiguous_routes,
)
from tests.nurse.test_push_registry import Harness

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "evidence,status",
    [
        (BarrierResult(), 503),
        (BarrierResult(True, "none"), 200),
        (BarrierResult(True, "unknown_or_present"), 202),
        (BarrierResult(True, "invented"), 503),
    ],
)
def test_revocation_never_fabricates_barrier_or_retracts_engaged_attempt(
    evidence, status
):
    h = Harness()
    _, auth, _ = h.account()
    client, _, _ = h.enroll(auth)
    active, _ = h.confirm(auth, client)
    with patch.object(h.registry.barrier, "confirm", return_value=evidence):
        result, actual = h.registry.revoke(auth, active["binding_id"])
    assert actual == status and result["database_revoked"] is True
    assert h.registry.bindings(auth) == []
    state = h.registry.binding_status(auth, active["binding_id"])
    assert state["barrier_confirmed"] == (status != 503)


def test_ciphertext_cannot_move_across_namespace_generation_or_restore():
    h = Harness()
    _, auth, _ = h.account()
    h.enroll(auth)
    delivery = h.control.deliveries[0]
    assert (
        decrypt_for_transport(delivery["envelope"], delivery["aad"], h.kms)
        == "synthetic.destination.237"
    )
    with pytest.raises(ValueError, match="custody_unavailable"):
        decrypt_for_transport(delivery["envelope"], delivery["aad"] + b"changed", h.kms)
    assert not hasattr(h.registry.custody, "unwrap")


def test_fanout_empty_ambiguous_and_snapshot_ports_do_not_add_fallback():
    native = SelectedRoute("device1", "native", "b1", 1, 1, True)
    web = SelectedRoute("device1", "web", "w1", 1, 1, True)
    other = SelectedRoute("device2", "native", "b2", 1, 1, True)
    unproven = SelectedRoute("device3", "native", "b3", 1, 1, False)
    assert unambiguous_routes([]) == ()
    assert unambiguous_routes([native, other]) == (native, other)
    assert unambiguous_routes([native, web, other, unproven]) == (other,)
    assert snapshot_replace_allowed(1, 1, absence_of_admission_proven=True)
    assert not snapshot_replace_allowed(1, 1, absence_of_admission_proven=False)
    assert not snapshot_replace_allowed(1, 2, absence_of_admission_proven=True)
    summary = summarize_results(["accepted", "uncertain"])
    assert summary["partial"] and summary["counts"]["uncertain"] == 1
    assert "retry" not in summary and "fallback" not in summary
