from bourso_mcp.store.fingerprint import compute_fingerprints
from tests.factories import make_tx


def test_fingerprints_rank_identical_transactions():
    a, b = make_tx(), make_tx()
    fps = compute_fingerprints("cc1", [a, b])
    assert fps[0] != fps[1] and compute_fingerprints("cc1", [a, b]) == fps


def test_fingerprint_depends_on_scope_key():
    assert compute_fingerprints("cc1", [make_tx()]) != compute_fingerprints(
        "cc1|pending", [make_tx()]
    )
