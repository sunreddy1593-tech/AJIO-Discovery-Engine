"""The coding frame every document is tagged against (`architecture.md` §7.1).

Each dimension is an enum rather than a free string so an invalid tag cannot be
persisted: the value is rejected by pydantic, and — because these same enums
generate the Groq JSON schema — the model is prevented from emitting it in the
first place.

Adding, removing, or renaming any value is a breaking change to the corpus.
Bump :data:`TAXONOMY_VERSION`, which is part of the cache key and of the
``doc_tags`` primary key, so old and new codings never silently mix.
"""

from __future__ import annotations

from enum import StrEnum

TAXONOMY_VERSION = "v1"


class WishlistMotivation(StrEnum):
    """Why the item was saved rather than bought."""

    PRICE_WATCH = "price_watch"
    DECIDE_LATER = "decide_later"
    COMPARE_OPTIONS = "compare_options"
    AWAITING_OCCASION = "awaiting_occasion"
    BUDGET_TIMING = "budget_timing"
    INSPIRATION_BOOKMARK = "inspiration_bookmark"
    SIZE_UNAVAILABLE = "size_unavailable"
    SEEKING_OPINION = "seeking_opinion"
    CART_PROXY = "cart_proxy"


class BlockerType(StrEnum):
    """What stands between the saved item and the purchase."""

    FIT_SIZE_UNCERTAINTY = "fit_size_uncertainty"
    QUALITY_DOUBT = "quality_doubt"
    COLOR_FABRIC_ACCURACY = "color_fabric_accuracy"
    RETURN_FRICTION = "return_friction"
    DELIVERY_UNCERTAINTY = "delivery_uncertainty"
    TRUST_AUTHENTICITY = "trust_authenticity"
    CHOICE_OVERLOAD = "choice_overload"
    STYLING_UNCERTAINTY = "styling_uncertainty"
    SOCIAL_VALIDATION_NEEDED = "social_validation_needed"
    CHECKOUT_FRICTION = "checkout_friction"
    PRICE_ABSOLUTE = "price_absolute"
    PRICE_EXPECTATION = "price_expectation"


class UncertaintyType(StrEnum):
    """The unanswered question, phrased as the user would ask it."""

    WILL_IT_FIT = "will_it_fit"
    HOW_DOES_IT_LOOK_ON_ME = "how_does_it_look_on_me"
    IS_QUALITY_WORTH_IT = "is_quality_worth_it"
    TRUE_COLOR = "true_color"
    OCCASION_APPROPRIATE = "occasion_appropriate"
    CAN_I_RETURN = "can_i_return"
    BETTER_ALTERNATIVE_EXISTS = "better_alternative_exists"


class InfoSoughtElsewhere(StrEnum):
    """Where the user went to resolve the uncertainty, if they left the app.

    There is deliberately no ``none`` member: an empty list already means "went
    nowhere", and every other multi-label dimension expresses absence the same
    way. The redundant sentinel was actively harmful — a live tagging call put
    ``"none"`` into ``wishlist_motivation``, where it is not a legal value,
    because the model saw the token as available somewhere in the schema.
    """

    YOUTUBE_HAUL = "youtube_haul"
    FRIEND_FAMILY_OPINION = "friend_family_opinion"
    OTHER_MARKETPLACE_REVIEWS = "other_marketplace_reviews"
    BRAND_SITE_SIZE_CHART = "brand_site_size_chart"
    INSTAGRAM_STYLING = "instagram_styling"
    OFFLINE_STORE_TRYON = "offline_store_tryon"


class SegmentCue(StrEnum):
    """Self-described segment signals, used only for breakdowns, never inferred."""

    FIRST_TIME_ONLINE_BUYER = "first_time_online_buyer"
    FREQUENT_SHOPPER = "frequent_shopper"
    BUDGET_CONSCIOUS = "budget_conscious"
    PREMIUM_SEEKER = "premium_seeker"
    OCCASION_SHOPPER = "occasion_shopper"
    PLUS_OR_PETITE_SIZE = "plus_or_petite_size"
    MENSWEAR = "menswear"
    WOMENSWEAR = "womenswear"
    TIER2_3_CITY = "tier2_3_city"


class IntentClass(StrEnum):
    """Whether the save was real purchase intent or just a bookmark."""

    GENUINE_INTENT = "genuine_intent"
    BOOKMARK_ONLY = "bookmark_only"
    AMBIGUOUS = "ambiguous"


class OutcomeMentioned(StrEnum):
    """What the author says actually happened, if anything."""

    PURCHASED = "purchased"
    ABANDONED = "abandoned"
    STILL_DECIDING = "still_deciding"
    NOT_STATED = "not_stated"


#: The five multi-label dimensions. Asserting a value in any of these obliges the
#: tagger to supply a supporting quote, which is what separates this from
#: sentiment analysis. Order is fixed so generated schemas are stable.
MULTI_LABEL_DIMENSIONS: tuple[tuple[str, type[StrEnum]], ...] = (
    ("wishlist_motivation", WishlistMotivation),
    ("blocker_type", BlockerType),
    ("uncertainty_type", UncertaintyType),
    ("info_sought_elsewhere", InfoSoughtElsewhere),
    ("segment_cue", SegmentCue),
)

SINGLE_LABEL_DIMENSIONS: tuple[tuple[str, type[StrEnum]], ...] = (
    ("intent_class", IntentClass),
    ("outcome_mentioned", OutcomeMentioned),
)

def _evidence_tag_values() -> tuple[str, ...]:
    values: list[str] = []
    for _, enum_cls in MULTI_LABEL_DIMENSIONS:
        values.extend(member.value for member in enum_cls)
    return tuple(values)


#: Every value that may legitimately appear as ``EvidenceSpan.tag``. Exposed as an
#: enum so constrained decoding cannot invent a tag name that no dimension
#: defines — a failure mode that would otherwise only surface as a mismatch
#: during quantification.
EvidenceTag = StrEnum("EvidenceTag", {value.upper(): value for value in _evidence_tag_values()})
EvidenceTag.__doc__ = "Union of all multi-label taxonomy values, for evidence attribution."

SEVERITY_MIN = 1
SEVERITY_MAX = 5
