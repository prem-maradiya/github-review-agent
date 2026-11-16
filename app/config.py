"""Application settings loaded from environment variables / .env."""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- GitHub ---
    github_webhook_secret: str = Field(..., description="Secret configured on the GitHub webhook")
    # Option A: GitHub App (recommended)
    github_app_id: str | None = None
    github_app_private_key: str | None = None  # PEM contents (\n escaped is fine)
    github_app_private_key_path: str | None = None
    # Option B: Personal access token (simpler for a single repo)
    github_token: str | None = None
    github_api_url: str = "https://api.github.com"

    # --- LLM ---
    gemini_api_key: str | None = None  # SDK also reads GEMINI_API_KEY itself
    llm_model: str = "gemini-3.5-flash"
    llm_fallback_models: list[str] = Field(default_factory=lambda: ["gemini-2.5-flash", "gemini-3.5-flash-lite"])
    llm_max_tokens: int = 16000
    llm_enabled: bool = True

    # --- Infra ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Review behaviour ---
    max_files: int = 50
    max_patch_chars_per_file: int = 20_000
    max_total_patch_chars: int = 150_000
    max_comments: int = 25
    min_severity: str = "low"  # low | medium | high | critical
    fail_on: str = "critical"  # PR check fails at/above this severity; "never" to always pass
    review_instructions: str = ""  # extra guidance for the LLM (set per repo via .ai-review.yml)
    review_command: str = "/review"
    ignored_paths: list[str] = Field(
        default_factory=lambda: [
            "*.lock", "package-lock.json", "yarn.lock", "poetry.lock",
            "*.min.js", "*.min.css", "*.svg", "*.png", "*.jpg", "dist/*", "vendor/*",
        ]
    )

    def private_key(self) -> str | None:
        if self.github_app_private_key:
            return self.github_app_private_key.replace("\\n", "\n")
        if self.github_app_private_key_path:
            with open(self.github_app_private_key_path, encoding="utf-8") as f:
                return f.read()
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
