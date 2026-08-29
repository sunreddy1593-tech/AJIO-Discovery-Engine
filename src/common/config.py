"""Typed configuration for the discovery pipeline.

Two sources are merged here and nowhere else:

* ``.env``       -> credentials, held as ``SecretStr`` so they cannot be logged by accident
* ``config.yaml`` -> everything else, hashed into ``config_hash`` for run provenance

Accessing ``config.settings`` raises :class:`MissingConfigError` naming the exact
keys that are absent, so a run fails at startup rather than three hours in.
``get_settings()`` and ``missing_credentials()`` are the non-raising entry points
used by ``scripts/check_credentials.py``, which has to work precisely when
credentials are broken.
"""

from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"

#: Credentials without which no run can start.
REQUIRED_ENV_VARS: tuple[str, ...] = (
    "GROQ_API_KEY",
    "YOUTUBE_API_KEY",
    "HASH_SALT",
)

#: Credentials for sources that are disabled by default. Absent is not an error;
#: the collector for that source refuses to run and says why.
OPTIONAL_ENV_VARS: tuple[str, ...] = (
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USER_AGENT",
)

CREDENTIAL_ENV_VARS: tuple[str, ...] = REQUIRED_ENV_VARS + OPTIONAL_ENV_VARS


class MissingConfigError(RuntimeError):
    """Raised when a required credential or config file is absent."""


class ConfigFileError(RuntimeError):
    """Raised when config.yaml is missing, unparseable, or has unknown keys."""


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


class Credentials(BaseSettings):
    """Secrets from ``.env``. Every field is required."""

    model_config = SettingsConfigDict(
        env_file=ENV_PATH,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    groq_api_key: SecretStr
    youtube_api_key: SecretStr
    hash_salt: SecretStr

    # Reddit is disabled by default; these are only needed if it is re-enabled.
    reddit_client_id: SecretStr | None = None
    reddit_client_secret: SecretStr | None = None
    reddit_user_agent: str | None = None

    @property
    def has_reddit(self) -> bool:
        return bool(
            self.reddit_client_id
            and self.reddit_client_secret
            and self.reddit_user_agent
        )


# --------------------------------------------------------------------------
# Non-secret run config (mirrors config.yaml)
# --------------------------------------------------------------------------


class _Strict(BaseModel):
    """Unknown keys are an error: a typo in config.yaml should not be silently ignored."""

    model_config = ConfigDict(extra="forbid")


class ModelConfig(_Strict):
    provider: Literal["groq"]
    name: str
    triage_name: str
    temperature: float
    seed: int
    response_format: Literal["json_schema", "json_object"]
    strict: bool
    reasoning_effort: Literal["low", "medium", "high"]
    include_reasoning: bool
    docs_per_request: int = Field(gt=0)
    triage_docs_per_request: int = Field(gt=0)
    max_docs_per_run: int = Field(gt=0)
    max_completion_tokens: int = Field(gt=0)
    max_doc_tokens: int = Field(gt=0)


class RateLimitConfig(_Strict):
    rpm: int
    rpd: int
    tpm: int
    tpd: int


class RateLimitsConfig(_Strict):
    tagging: RateLimitConfig
    triage: RateLimitConfig


class _Source(_Strict):
    """Every source can be switched off without deleting its configuration."""

    enabled: bool = True


class PlayStoreConfig(_Source):
    app_ids: list[str]
    languages: list[str]
    countries: list[str]
    max_reviews: int


class AppStoreConfig(_Source):
    app_ids: list[str]
    countries: list[str]
    max_pages: int


class YouTubeConfig(_Source):
    query_terms: list[str]
    max_videos_per_term: int
    max_comments_per_video: int


class MouthShutConfig(_Source):
    listing_urls: list[str]
    max_pages_per_listing: int
    max_reviews: int


class TrustpilotConfig(_Source):
    domains: list[str]
    max_pages: int


class ComplaintsBoardConfig(_Source):
    company_paths: list[str]
    max_pages_per_company: int


class ConsumerComplaintsInConfig(_Source):
    company_paths: list[str]
    max_pages_per_company: int


class AjioOnsiteConfig(_Source):
    category_urls: list[str]
    product_urls: list[str]
    max_products: int
    max_reviews_per_product: int
    max_qa_per_product: int
    browser_user_agent: str
    # AJIO is a single-page app, so reviews and Q&A arrive from JSON endpoints
    # rather than the product HTML. The templates live here rather than in code so
    # that correcting them after a live probe is a config edit, which is the
    # difference between a five-minute fix and a code change mid-collection.
    # Empty strings disable the API path and fall back to parsing the page, and
    # empty is the shipped default: AJIO's robots.txt disallows /api/*, so the
    # collector refuses these paths anyway (see the note in config.yaml).
    review_api_template: str = ""
    qa_api_template: str = ""


class AjioManualConfig(_Source):
    """Hand-collected AJIO Q&A and reviews. Nothing here touches the network:
    the on-site collector is refused by Akamai (edge-case 1.1.13), and the answer
    to a refusal is a person reading the site, not a better-disguised client."""

    import_dir: str


class QuoraManualConfig(_Source):
    """Manual import only. Quora's robots.txt prohibits bot use of its content
    for AI/ML systems, so nothing here fetches anything over the network."""

    import_dir: str


class RedditConfig(_Source):
    subreddits: list[str]
    queries: list[str]
    max_posts: int
    include_comments: bool


class FloorsConfig(_Strict):
    """Corpus-size expectations, reported by collection and by the corpus build.

    Two units, and the distinction is the whole point (plan §3.3). The ``_records``
    floors count what was collected; the ``_documents`` floors count what survives
    the hard exclusions and so is the only unit the tagger and every downstream
    metric ever see. The record floors passed at 4,494 pre-purchase while 180
    documents reached the corpus, so they are kept as leading indicators and the
    document floors are the gate.
    """

    pre_purchase_records: int = Field(ge=0)
    total_records: int = Field(ge=0)
    pre_purchase_documents: int = Field(ge=0)
    total_documents: int = Field(ge=0)


class CollectionConfig(_Strict):
    request_delay_seconds: float
    max_requests_per_run: int
    respect_robots_txt: bool
    per_domain_delay_seconds: float
    scraper_user_agent: str
    floors: FloorsConfig
    play_store: PlayStoreConfig
    app_store: AppStoreConfig
    youtube: YouTubeConfig
    mouthshut: MouthShutConfig
    trustpilot: TrustpilotConfig
    complaints_board: ComplaintsBoardConfig
    consumer_complaints_in: ConsumerComplaintsInConfig
    ajio_onsite: AjioOnsiteConfig
    ajio_manual: AjioManualConfig
    quora_manual: QuoraManualConfig
    reddit: RedditConfig

    def enabled_sources(self) -> list[str]:
        names = (
            "play_store",
            "app_store",
            "youtube",
            "mouthshut",
            "trustpilot",
            "complaints_board",
            "consumer_complaints_in",
            "ajio_onsite",
            "ajio_manual",
            "quora_manual",
            "reddit",
        )
        return [name for name in names if getattr(self, name).enabled]


class FiltersConfig(_Strict):
    """The hard exclusions, plus the two thresholds that keep them honest at low
    ``min_words``.

    ``language_min_words`` exists because the exclusion order used to carry a
    guarantee it no longer carries. With ``min_words: 8`` running first, nothing
    shorter than eight words could reach langdetect, which is what edge-case 3.3.2
    relied on. At ``min_words: 3`` that protection is gone, so it is stated here
    instead of being an emergent property of another threshold's value.
    """

    min_words: int = Field(gt=0)
    exclude_emoji: bool
    excluded_languages: list[str]
    language_confidence: float = Field(ge=0.0, le=1.0)
    language_min_words: int = Field(gt=0)
    min_chars: int
    min_content_words: int
    near_duplicate_min_words: int
    near_duplicate_hamming: int
    relevance_keywords_path: str


class QuantificationConfig(_Strict):
    recency_half_life_days: int
    low_confidence_min_docs: int
    min_distinct_authors: int
    max_docs_per_author_per_tag: int
    cluster_jaccard_min: float


class PathsConfig(_Strict):
    raw_dir: str
    #: A quantitative side-channel, not part of the text corpus. Holds AJIO's own
    #: rating and fit/quality percentages, read by Phase 6 through
    #: ``src/store/aggregates.py`` and by nothing else. Deliberately absent from
    #: ``ensure_dirs`` below: Collect must not touch this directory at all.
    aggregates_dir: str
    interim_db: str
    processed_dir: str
    outputs_dir: str
    logs_dir: str


class RunConfig(_Strict):
    model: ModelConfig
    rate_limits: RateLimitsConfig
    collection: CollectionConfig
    filters: FiltersConfig
    quantification: QuantificationConfig
    paths: PathsConfig


# --------------------------------------------------------------------------
# Combined settings
# --------------------------------------------------------------------------


class Settings:
    """Resolved credentials + run config + derived paths."""

    def __init__(self, credentials: Credentials, run: RunConfig, raw_config: dict[str, Any]):
        self.credentials = credentials
        self.run = run
        self.config_hash = _hash_config(raw_config)
        self.project_root = PROJECT_ROOT

    # Paths are resolved against the project root so behaviour does not depend
    # on the working directory a script happens to be launched from.
    def path(self, key: str) -> Path:
        value = getattr(self.run.paths, key)
        return (PROJECT_ROOT / value).resolve()

    @property
    def raw_dir(self) -> Path:
        return self.path("raw_dir")

    @property
    def aggregates_dir(self) -> Path:
        return self.path("aggregates_dir")

    @property
    def interim_db(self) -> Path:
        return self.path("interim_db")

    @property
    def processed_dir(self) -> Path:
        return self.path("processed_dir")

    @property
    def outputs_dir(self) -> Path:
        return self.path("outputs_dir")

    @property
    def logs_dir(self) -> Path:
        return self.path("logs_dir")

    def ensure_dirs(self) -> None:
        for directory in (
            self.raw_dir,
            self.interim_db.parent,
            self.processed_dir,
            self.outputs_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:  # never render credentials
        return f"<Settings model={self.run.model.name!r} config_hash={self.config_hash[:12]}>"


def _hash_config(raw_config: dict[str, Any]) -> str:
    """Stable sha256 over the non-secret config. Secrets are never included."""
    canonical = json.dumps(raw_config, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_run_config(path: Path = CONFIG_PATH) -> tuple[RunConfig, dict[str, Any]]:
    if not path.exists():
        raise ConfigFileError(f"config.yaml not found at {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigFileError(f"config.yaml is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigFileError("config.yaml must contain a top-level mapping")
    try:
        return RunConfig.model_validate(raw), raw
    except Exception as exc:
        raise ConfigFileError(f"config.yaml failed validation:\n{exc}") from exc


def missing_credentials() -> list[str]:
    """Names of *required* credentials that are absent or blank. Never raises."""
    values = _env_values()
    return [name for name in REQUIRED_ENV_VARS if not values.get(name, "").strip()]


def missing_optional_credentials() -> list[str]:
    """Names of optional credentials that are absent. Not an error. Never raises."""
    values = _env_values()
    return [name for name in OPTIONAL_ENV_VARS if not values.get(name, "").strip()]


def _env_values() -> dict[str, str]:
    """Merge of ``.env`` and the process environment; the process environment wins."""
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip().upper()] = value.strip().strip('"').strip("'")
    for name in CREDENTIAL_ENV_VARS:
        env_value = os.environ.get(name)
        if env_value:
            values[name] = env_value
    return values


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache settings. Raises a named error listing what is missing."""
    absent = missing_credentials()
    if absent:
        raise MissingConfigError(
            "Missing required credentials: "
            + ", ".join(absent)
            + f"\nAdd them to {ENV_PATH} (copy .env.example) or set them as environment variables."
        )
    run_config, raw = load_run_config()
    try:
        credentials = Credentials()  # type: ignore[call-arg]  # values come from .env
    except Exception as exc:
        raise MissingConfigError(f"Credentials failed validation:\n{exc}") from exc
    return Settings(credentials=credentials, run=run_config, raw_config=raw)


def __getattr__(name: str) -> Any:
    """Module-level ``settings`` so importing it fails loudly when config is broken.

    PEP 562 hook: ``from src.common.config import settings`` triggers the load,
    while ``get_settings()`` stays available for callers that must handle failure.
    """
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
