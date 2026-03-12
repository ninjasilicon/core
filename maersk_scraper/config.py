"""
Configuration — loaded from environment variables or a .env file.

Copy .env.example to .env and fill in your values before running.
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    # ── Database ───────────────────────────────────────────────────────────
    db_host: str = field(default_factory=lambda: os.getenv("DB_HOST", "localhost"))
    db_port: int = field(default_factory=lambda: int(os.getenv("DB_PORT", "3306")))
    db_name: str = field(default_factory=lambda: os.getenv("DB_NAME", "maersk_schedules"))
    db_user: str = field(default_factory=lambda: os.getenv("DB_USER", "root"))
    db_password: str = field(default_factory=lambda: os.getenv("DB_PASSWORD", ""))

    # ── Browser ────────────────────────────────────────────────────────────
    headless: bool = field(
        default_factory=lambda: os.getenv("HEADLESS", "true").lower() == "true"
    )
    browser_timeout_ms: int = field(
        default_factory=lambda: int(os.getenv("BROWSER_TIMEOUT_MS", "60000"))
    )
    # Extra wait after search results appear (ms) — increase on slow connections
    results_wait_ms: int = field(
        default_factory=lambda: int(os.getenv("RESULTS_WAIT_MS", "8000"))
    )

    # ── Scraper ────────────────────────────────────────────────────────────
    # Delay between consecutive searches to avoid rate-limiting (seconds)
    request_delay_s: float = field(
        default_factory=lambda: float(os.getenv("REQUEST_DELAY_S", "3.0"))
    )
    # User-agent to use. Leave empty to use Playwright default.
    user_agent: Optional[str] = field(
        default_factory=lambda: os.getenv("USER_AGENT", None) or None
    )

    @property
    def database_url(self) -> str:
        """SQLAlchemy connection URL for MySQL/MariaDB."""
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
            f"?charset=utf8mb4"
        )


def load_config() -> Config:
    """Load config, optionally reading from a .env file first."""
    _load_dotenv()
    return Config()


def _load_dotenv() -> None:
    """Minimal .env loader — no external dependency required."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
