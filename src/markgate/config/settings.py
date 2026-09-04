import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

_CONFIG_DIR = Path(__file__).parent

# Populate os.environ so ${VAR} placeholders in backend_config.toml can be resolved.
# override=False: real environment variables take precedence.
# No-op if .env_secret does not exist (e.g. in Docker where vars come from env_file:).
load_dotenv(_CONFIG_DIR / ".env.secret", override=False)


class Settings(BaseSettings):
    """Application settings — infrastructure only. Backend configs live in backend_config.toml.

    Priority (highest to lowest):
      1. Environment variables
      2. .env_secret file
      3. server_config.toml  (default: src/markgate/config/server_config.toml)
      4. Field defaults
    """

    # Logging
    log_level: str = "INFO"
    log_file: Optional[str] = None
    log_max_bytes: int = 10 * 1024 * 1024  # 10 MB
    log_backup_count: int = 5

    # S3 Cache Storage
    s3_endpoint: str = "http://localhost:3900"
    s3_access_key: str = ""  # Secret — set in .env_secret
    s3_secret_key: str = ""  # Secret — set in .env_secret
    s3_bucket: str = "markgate-cache"
    s3_region: str = "garage"

    # Redis (Distributed Locks)
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_socket_timeout: float = 5.0  # shouldn't be edited

    # Processing timeouts
    redis_lock_timeout: int = (
        300  # Initial lock TTL (s). Auto-extended during upstream calls.
    )
    redis_blocking_timeout: int = (
        9999999  # Max wait on a locked hash before returning 504
    )
    upstream_timeout: float = (
        9999999  # Max wait for upstream backend (should ≈ REDIS_BLOCKING_TIMEOUT)
    )

    # Failed request archiving
    failed_requests_s3_prefix: str = "failed_requests"
    failed_requests_local_dir: str | None = "/tmp/markgate_failed"

    # Cache
    s3_cache_enabled: bool = True

    # Error reporting
    verbose_errors: bool = False
    """When True, dependency error details (upstream body, Redis/S3 messages) are forwarded to the client. Disable in production."""

    # Config paths
    backend_config_path: str = "backend_config.toml"
    """Path to backend_config.toml. Relative to CWD or absolute."""

    # Auto backend selection (optional — see auto_routes.py / routing.py)
    auto_route_enabled: bool = False
    """Registers /auto/process, /md/auto/process and /auto/process/download when true. Off by
    default — the feature and its routes don't exist at all unless explicitly turned on."""

    auto_selector_path: str | None = None
    """Filesystem path to the .py file implementing `select(ctx) -> BackendSelection`. Required
    (and loaded, fail-fast, at startup) when auto_route_enabled is true."""

    client_api_key_auto: str = ""  # Secret — set in .env_secret
    """Bearer token for /auto/* routes. Independent of any single backend's authorized_api_key,
    since the backend isn't known until after the file has been inspected. Empty = /auto/* routes
    always reject (fail closed)."""

    max_upload_size_bytes: int | None = None
    """Reject uploads over this size with 413 before running any auto-selection logic. None = no
    limit. Currently enforced only on /auto/* — the natural place for it, since it exists
    specifically to stop a selector from doing work on an oversized file before a backend is even
    chosen; extending the same check to the explicit /{version}/process routes is a straightforward,
    separate follow-up."""

    model_config = SettingsConfigDict(
        env_file=_CONFIG_DIR / ".env.secret",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        **_kwargs: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(
                settings_cls, toml_file=_CONFIG_DIR / "server_config.toml"
            ),
        )


settings = Settings()


def setup_logging() -> None:
    """Configure the "markgate" logger's handlers/formatter from `settings`. Every module gets
    its own `logging.getLogger("markgate")` — same name, same singleton logger instance — so
    calling this once (from main.py, at app startup) is enough to route all of them. Mirrors
    foil-serve's settings.py::setup_logging()."""
    logger = logging.getLogger("markgate")
    logger.setLevel(settings.log_level)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if settings.log_file:
        file_handler = RotatingFileHandler(
            settings.log_file,
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
