from context_gate.normalization import normalize_event

from .helpers import event


def test_normalization_derives_hash_and_canonical_keys() -> None:
    normalized = normalize_event(
        event(entity_id="  NOVA-SUMMIT ", field_name=" Venue ")
    )
    assert normalized.entity_id == "nova-summit"
    assert normalized.field_name == "venue"
    assert normalized.content_hash is not None
    assert len(normalized.content_hash) == 64
