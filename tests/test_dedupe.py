"""Phase 3 dedup: exact + near-duplicate marking, chains, short-text exemption."""

from datetime import datetime, timezone

from src.common.hashing import doc_id
from src.common.schemas import Document
from src.store.dedupe import mark_duplicates

FILTER = dict(near_duplicate_hamming=12, near_duplicate_min_words=25)


def _doc(native_id: str, text: str, source: str = "mouthshut") -> Document:
    return Document(
        doc_id=doc_id(source, native_id),
        source=source,
        source_native_id=native_id,
        text=text,
        ingested_at=datetime.now(timezone.utc),
    )


def test_exact_duplicate_is_marked():
    text = "the ajio kurta fit me well but the fabric felt thin for the price paid"
    docs = [_doc("a", text), _doc("b", text)]
    counts = mark_duplicates(docs, **FILTER)
    marked = [d for d in docs if d.is_duplicate_of is not None]
    assert counts["exact_duplicates"] == 1
    assert len(marked) == 1
    # the survivor is a canonical (points at nothing)
    canonical = [d for d in docs if d.is_duplicate_of is None]
    assert len(canonical) == 1
    assert marked[0].is_duplicate_of == canonical[0].doc_id


def test_unrelated_documents_are_not_merged():
    a = _doc("a", "the delivery was late and the packaging arrived torn on both of the corners")
    b = _doc("b", "this floral dress fit beautifully and the colour matched the product photos exactly")
    mark_duplicates([a, b], **FILTER)
    assert a.is_duplicate_of is None
    assert b.is_duplicate_of is None


def test_near_duplicate_within_threshold_is_marked():
    base = (
        "i really liked this ajio kurta the fabric quality is good and the "
        "stitching held up well after several washes but the size ran a little small for me"
    )
    reworded = (
        "i really liked this ajio kurta the fabric quality is nice and the "
        "stitching held up well after several washes though the size ran a bit small for me"
    )
    a, b = _doc("a", base), _doc("b", reworded)
    counts = mark_duplicates([a, b], **FILTER)
    assert counts["near_duplicates"] == 1
    assert (a.is_duplicate_of is None) ^ (b.is_duplicate_of is None)


def test_short_text_is_exempt_from_near_pass():
    # under near_duplicate_min_words (25): similar but each kept as its own row
    a = _doc("a", "nice kurta but size ran small for me overall")
    b = _doc("b", "nice kurta but size ran small for him overall")
    counts = mark_duplicates([a, b], **FILTER)
    assert counts["near_duplicates"] == 0
    assert a.is_duplicate_of is None and b.is_duplicate_of is None


def test_no_self_reference_and_no_chains():
    text = "the ajio kurta fit me well but the fabric felt thin for the price paid overall"
    docs = [_doc(x, text) for x in ("a", "b", "c", "d")]
    mark_duplicates(docs, **FILTER)
    by_id = {d.doc_id: d for d in docs}
    for d in docs:
        assert d.is_duplicate_of != d.doc_id  # no self-reference
        if d.is_duplicate_of is not None:
            # every target is a root canonical -> no chains, FK-safe
            assert by_id[d.is_duplicate_of].is_duplicate_of is None


def test_excluded_rows_do_not_anchor_groups():
    text = "the ajio kurta fit me well but the fabric felt thin for the price paid"
    keep = _doc("a", text)
    dropped = _doc("b", text)
    dropped.exclusion_reason = "too_short"
    mark_duplicates([keep, dropped], **FILTER)
    # the excluded row is skipped entirely, so nothing is marked a duplicate
    assert keep.is_duplicate_of is None
    assert dropped.is_duplicate_of is None
