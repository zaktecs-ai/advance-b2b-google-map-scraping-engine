"""Configuration loading, env interpolation, and validation.

One human-editable YAML file drives the engine. Secrets live in ``.env`` and
are referenced as ``${VAR}``. Validation runs *before* any scraping; invalid
values produce a clear error naming the key, the bad value, the allowed range,
and a recommendation for the target VPS.

``pydantic`` enforces types/ranges so mistakes fail fast rather than silently
misbehaving mid-run.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """User-facing configuration error."""


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class JobConfig(BaseModel):
    client_name: str = "campaign"
    output_dir: str = "output"
    output_filename: str = ""
    default_country: str = "US"
    max_results_per_query: int = Field(default=0, ge=0)
    max_total_results: int = Field(default=0, ge=0)

    @field_validator("default_country")
    @classmethod
    def _validate_default_country(cls, value: str) -> str:
        code = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", code):
            raise ValueError("default_country must be an ISO alpha-2 country code")
        return code


class MapsConfig(BaseModel):
    headless: bool = True
    hl: str = "en"
    gl: str = "us"
    zoom: int = Field(default=16, ge=0, le=21)
    max_results_per_query: int = Field(default=0, ge=0)
    max_total_results: int = Field(default=0, ge=0)
    scroll_pause_seconds: float = Field(default=2.0, ge=0.1, le=30.0)
    max_scrolls: int = Field(default=0, ge=0)  # 0 = scroll until no new results
    include_permanently_closed: bool = False
    browser_restart_after_queries: int = Field(default=5, ge=0)
    scroll_delay_min_ms: int = Field(default=800, ge=0)
    scroll_delay_max_ms: int = Field(default=1600, ge=0)
    page_navigation_timeout_ms: int = Field(default=30_000, ge=1000)
    # PERF: parallel Maps discovery workers. 1 = the historical single-page
    # serial flow (identical behavior). 2-3 = additional isolated browser
    # contexts working the SAME query's card list in alternating slices —
    # discovery is the measured critical path (see docs/PERFORMANCE.md).
    # On a bot challenge all workers stop and the engine falls back to 1.
    workers: int = Field(default=1, ge=1, le=4)
    # PERF: deep-scroll the panel to hydrate "From the owner" posts
    # (has_recent_post / latest_post_date). Default false — the deep scroll
    # costs ~3s per listing and these columns are unused in production.
    # Set true to restore the full old behavior.
    extract_owner_posts: bool = False


class ReviewsConfig(BaseModel):
    enabled: bool = True
    per_business: int = Field(default=5, ge=1, le=50)
    min_len: int = Field(default=0, ge=0)
    max_len: int = Field(default=1000, ge=10)


class WebsiteConfig(BaseModel):
    require_website: bool = False
    enable_playwright_fallback: bool = True
    enable_sitemap: bool = True
    max_pages_per_site: int = Field(default=3, ge=1, le=50)
    overall_site_timeout_seconds: float = Field(default=120.0, ge=1.0)
    http_connect_timeout_seconds: float = Field(default=10.0, ge=1.0)
    http_read_timeout_seconds: float = Field(default=20.0, ge=1.0)
    http_retries: int = Field(default=1, ge=0, le=10)
    page_navigation_timeout_seconds: float = Field(default=30.0, ge=1.0)
    use_wappalyzer: bool = True
    # Transient-failure retry policy (429/503/timeouts/connection errors):
    # exponential backoff with jitter, honoring Retry-After when present.
    # Backoff sleeps happen OUTSIDE the per-domain slot so a slow site never
    # blocks unrelated work and retry storms cannot build up.
    retry_backoff_base_seconds: float = Field(default=1.0, ge=0.0, le=30.0)
    retry_backoff_cap_seconds: float = Field(default=20.0, ge=1.0, le=120.0)


class EmailConfig(BaseModel):
    enabled: bool = True
    max_email_length: int = Field(default=120, ge=10, le=300)
    enable_mx_check: bool = False


class SMTPConfig(BaseModel):
    enabled: bool = False
    workers: int = Field(default=3, ge=1, le=8)
    retries: int = Field(default=1, ge=0, le=5)
    connection_timeout_seconds: float = Field(default=10.0, ge=1.0)
    verification_timeout_seconds: float = Field(default=20.0, ge=1.0)
    from_email: str = "verify@example.com"

    @model_validator(mode="after")
    def _require_real_from_email_when_enabled(self):
        # A placeholder `@example.com` sender silently broke SMTP verification
        # (F23). Fail fast when enabled with a placeholder sender.
        if self.enabled and self.from_email.endswith("@example.com"):
            raise ValueError(
                "smtp.from_email must be a real domain (not @example.com) "
                "when smtp.enabled is true"
            )
        return self


class EnrichmentConfig(BaseModel):
    emails: bool = True
    social: bool = True
    tech_detect: bool = True
    decision_makers: bool = False  # off by default (extra pass)
    mx_verify: bool = False        # off by default
    smtp_verify: bool = False      # off by default
    require_website: bool = False
    # CSS selectors stripped before email/decision-maker extraction. The risky
    # `.author`/`blockquote`/`.quote`/`cite` selectors were REMOVED from the
    # default because many themes use them for real team bios (F33).
    exclude_selectors: list = Field(default_factory=lambda: [
        ".testimonial", ".testimonials", ".review", ".reviews", ".review-body",
        ".comment", ".comments", ".wp-block-comment", "figcaption",
    ])


class LLMHookConfig(BaseModel):
    """AI personalized pitch-hook toggle — the single control point."""
    enabled: bool = False
    provider: str = "openai"        # "openai" | "deepseek"
    model: str = ""                 # empty = provider default
    api_key_env: str = ""           # empty = provider default env var
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    max_calls: int = Field(default=0, ge=0)  # 0 = unlimited; caps per-run spend
    retries: int = Field(default=2, ge=0, le=5)


class ConcurrencyConfig(BaseModel):
    """Worker counts + backpressure knobs for the continuous enrichment stage.

    Architecture: Maps discovery is a *producer* that feeds a bounded work
    queue; a long-lived pool of ``website_workers`` HTTP worker threads is
    created ONCE for the whole run and continuously consumes newly discovered
    websites (no per-batch executor, no drain-wait between batches).

    - ``website_workers``: global HTTP enrichment workers (I/O-bound).
    - ``per_domain_concurrency``: simultaneous in-flight requests allowed per
      target domain (1 = polite default; protects individual sites while the
      global pool stays busy across many different domains).
    - ``max_in_flight``: bounded queue size for prepared-but-not-yet-enriched
      records (0 = auto: workers x 4). Prevents uncontrolled memory growth.
    - ``respect_retry_after``: honor a server's Retry-After header before
      retrying 429/503 responses.
    - ``playwright_workers``: persistent Chromium browser workers used by the
      JS-required fallback pool (browsers are REUSED across sites; HTTP stays
      the primary path). The Maps collector remains ONE sequential browser by
      design (anti-bot pacing) — this knob does not parallelize Maps.
    """
    website_workers: int = Field(default=20, ge=1, le=32)
    playwright_workers: int = Field(default=2, ge=1, le=4)
    per_domain_concurrency: int = Field(default=1, ge=1, le=8)
    max_in_flight: int = Field(default=0, ge=0)
    respect_retry_after: bool = True


class DelaysConfig(BaseModel):
    # PERF (Fix G): measured end-to-end, click-through panel navigation emits
    # far fewer high-risk signals than full page loads, and the engine already
    # backs off (cooldown + proxy rotation feedback) on a bot challenge.
    # Old conservative default was 2.0-5.0s per listing; new default keeps a
    # polite floor. To restore the old behavior: maps_min_seconds: 2.0,
    # maps_max_seconds: 5.0.
    maps_min_seconds: float = Field(default=0.4, ge=0.0, le=30.0)
    maps_max_seconds: float = Field(default=1.2, ge=0.0, le=30.0)
    site_min_seconds: float = Field(default=0.3, ge=0.0, le=10.0)
    site_max_seconds: float = Field(default=0.8, ge=0.0, le=10.0)
    cooldown_seconds: float = Field(default=60.0, ge=0.0, le=600.0)


class VNCConfig(BaseModel):
    display: str = ":2"
    port: int = Field(default=5902, ge=5900, le=5999)
    resolution: str = "1366x900"


class LoggingConfig(BaseModel):
    """Console/log verbosity — keeps the terminal clean while full logs go to file."""
    level: str = "INFO"          # file log level (DEBUG/INFO/WARNING/ERROR)
    quiet: bool = False          # true = suppress the progress lines entirely


class RuntimeConfig(BaseModel):
    """Tunables consumed by the runtime loop (pacing, timeouts, idle-exit).

    Worker counts live ONLY in ``ConcurrencyConfig`` — a second set of knobs
    here previously validated but was never read, which misled operators into
    thinking parallelism was configurable in two places.
    """
    request_timeout: float = Field(default=20.0, ge=1.0, le=120.0)
    idle_exit_seconds: int = Field(default=0, ge=0)
    pacing: float = Field(default=1.0, ge=0.0, le=30.0)


class GridConfig(BaseModel):
    enabled: bool = False
    cell_size_km: float = Field(default=3.0, ge=0.1, le=50.0)


class GeoConfig(BaseModel):
    polygons: list = Field(default_factory=list)


class ProxyConfig(BaseModel):
    enabled: bool = False
    urls: list = Field(default_factory=list)
    file: str = ""
    rotation: Literal["round_robin", "random"] = "round_robin"
    http: str = ""
    https: str = ""
    pool: list = Field(default_factory=list)


class AnalysisConfig(BaseModel):
    enabled: bool = True
    lexicon_hint: str = ""  # optional path to a custom sentiment lexicon


class FilterConfig(BaseModel):
    include_all: list = Field(default_factory=list)
    include_any: list = Field(default_factory=list)
    exclude_all: list = Field(default_factory=list)
    exclude_any: list = Field(default_factory=list)


class SignalsConfig(BaseModel):
    pass


class SignalsConfig(BaseModel):
    """Config-driven custom website signals.

    Each entry creates a YES/NO export column:
        signals:
          custom:
            emergency_service:
              column: signal_emergency_service   # export column name
              match: any                         # any = OR, all = AND
              keywords: ["24/7", "emergency plumber"]
              enabled: true
    """

    custom: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_specs(self):
        seen_columns: set[str] = set()
        for name, spec in self.custom.items():
            if not re.fullmatch(r"[a-z0-9_]+", str(name)):
                raise ValueError(
                    f"signals.custom name {name!r} must be lowercase "
                    f"[a-z0-9_]")
            if not isinstance(spec, dict):
                raise ValueError(f"signals.custom.{name} must be a mapping")
            column = str(spec.get("column") or f"signal_{name}").strip().lower()
            if not re.fullmatch(r"[a-z0-9_]{1,64}", column):
                raise ValueError(
                    f"signals.custom.{name}.column {column!r} must be "
                    f"[a-z0-9_] (max 64 chars)")
            if column in seen_columns:
                raise ValueError(
                    f"signals.custom.{name}: duplicate column {column!r}")
            seen_columns.add(column)
            keywords = spec.get("keywords") or []
            regexes = spec.get("regex") or []
            if spec.get("enabled", True) and not keywords and not regexes:
                raise ValueError(
                    f"signals.custom.{name}: enabled signals need at least "
                    f"one keyword or regex")
            if str(spec.get("match", "any")).lower() not in ("any", "all"):
                raise ValueError(
                    f"signals.custom.{name}.match must be 'any' or 'all'")
        return self


class SummaryConfig(BaseModel):
    """summary.json generation (per-campaign resource + lead report)."""

    enabled: bool = True
    sample_interval_seconds: float = Field(default=2.0, ge=0.5, le=60.0)


class CountryConfig(BaseModel):
    default: str = "US"


class AppConfig(BaseModel):
    job: JobConfig = Field(default_factory=JobConfig)
    queries: list = Field(default_factory=list)
    maps: MapsConfig = Field(default_factory=MapsConfig)
    reviews: ReviewsConfig = Field(default_factory=ReviewsConfig)
    website: WebsiteConfig = Field(default_factory=WebsiteConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    smtp: SMTPConfig = Field(default_factory=SMTPConfig)
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    ai_hook: LLMHookConfig = Field(default_factory=LLMHookConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    delays: DelaysConfig = Field(default_factory=DelaysConfig)
    vnc: VNCConfig = Field(default_factory=VNCConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    grid: GridConfig = Field(default_factory=GridConfig)
    geo: GeoConfig = Field(default_factory=GeoConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    signals: SignalsConfig = Field(default_factory=SignalsConfig)
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    country: CountryConfig = Field(default_factory=CountryConfig)

    @model_validator(mode="after")
    def _check_queries(self):
        if not self.queries:
            raise ValueError("config must define at least one query under `queries:`")
        return self


# ---------------------------------------------------------------------------
# Env resolution + loader
# ---------------------------------------------------------------------------

class _EnvResolver:
    def __init__(self) -> None:
        self._missing: list = []

    def resolve(self, value):
        if isinstance(value, str):
            def _sub(m):
                name = m.group(1)
                val = os.environ.get(name)
                if val is None:
                    self._missing.append(name)
                    return m.group(0)
                return val
            return ENV_VAR_RE.sub(_sub, value)
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        return value


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load, env-resolve, and validate config; returns an AppConfig."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    if raw is None:
        raise ConfigError(f"{path} is empty.")

    if Path(".env").exists():
        load_dotenv(".env")

    resolver = _EnvResolver()
    resolved = resolver.resolve(raw)
    if resolver._missing:
        listed = ", ".join(sorted(set(resolver._missing)))
        raise ConfigError(
            f"Missing environment variable(s): {listed}. Define them in `.env`."
        )

    try:
        cfg = AppConfig.model_validate(resolved)
    except Exception as e:  # noqa: BLE001 — pydantic ValidationError
        raise ConfigError(f"Config validation failed:\n{e}") from e
    return cfg
