"""App settings (pydantic-settings).

Holds config loaded from the environment (and a local .env): the Sentry base URL,
org slug, project id, and the secret API token. The token lives only here — never
hard-coded, never committed.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    #Tells pydantic to load settings from a .env file and ignore extra env vars
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    sentry_base_url: str = "https://sentry02.aptoide.com"
    sentry_org: str = "sentry"
    sentry_project: str = "4"
    sentry_token: str  # required — comes from SENTRY_TOKEN in .env / env


# A single, shared settings instance the rest of the app imports.
settings = Settings()
