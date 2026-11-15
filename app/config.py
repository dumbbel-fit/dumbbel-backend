from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "dev"  # dev | staging | production
    api_prefix: str = "/v1"
    public_base_url: str = "http://localhost:8000"

    # Database (MySQL). Accepts the Aiven style URL: mysql://user:pass@host:port/db?ssl-mode=REQUIRED
    database_url: str = "mysql://root@localhost:3306/dumbbel"
    db_ssl_ca: str | None = None  # path to CA cert to verify the server; None = encrypt without verification
    db_pool_size: int = 10
    auto_create_tables: bool = True

    # Redis (optional). Without it an in-process cache is used (fine for a single worker / dev).
    redis_url: str | None = None

    # Auth
    jwt_secret: str = "change-me-jwt-secret"
    access_token_ttl_sec: int = 900
    refresh_token_ttl_days: int = 30
    otp_length: int = 6
    otp_ttl_sec: int = 300
    otp_dev_code: str | None = None  # dev/staging only: fixed code that always works, SMS not sent

    # QR tokens: current key id + secrets ("DBF1:secret1,DBF2:secret2"), previous key valid for 5 min
    qr_keys: str = "DBF1:change-me-qr-secret"
    qr_current_kid: str = "DBF1"
    qr_previous_kid_valid_until: int | None = None  # unix ts; previous keys accepted until then

    # 2Factor.in
    twofactor_api_key: str | None = None
    twofactor_otp_template: str = "Dumbble"
    # v1 = OTP API (2Factor falls back to a VOICE CALL if the SMS template isn't mapped)
    # r1 = transactional SMS API: SMS only, never a call; fails with a clear error if the DLT mapping is missing
    twofactor_otp_route: str = "v1"
    twofactor_sender_id: str = "DMBL"
    # Transactional (non-OTP) templates registered on DLT via 2Factor; empty = log only
    twofactor_tpl_gym_status: str | None = None

    # Which phone login the apps use: "firebase" (SMS sent by the Firebase SDK in the app) or "2factor"
    auth_provider: str = "firebase"
    # Groq LLM: nutrition coach, food estimates, daily insight, owner briefing
    groq_api_key: str | None = None
    groq_model: str | None = None  # default openai/gpt-oss-120b
    groq_fallback_model: str = "openai/gpt-oss-20b"

    # Firebase Phone Auth (alternative to 2Factor; Google sends the SMS, no DLT needed)
    firebase_project_id: str | None = None
    firebase_web_api_key: str | None = None
    firebase_web_app_id: str | None = None
    firebase_messaging_sender_id: str | None = None
    firebase_storage_bucket: str | None = None  # Web app apiKey; only used by the /dev/firebase-test page
    firebase_credentials_file: str | None = None  # service-account JSON path (optional for token verification)

    # Object storage (S3/R2). Without bucket config, local disk storage is used.
    s3_bucket: str | None = None
    s3_private_bucket: str | None = None
    s3_region: str = "ap-south-1"
    s3_endpoint_url: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    local_storage_dir: str = "./storage"

    # WhatsApp (Meta Cloud API). One Meta app for the platform; each gym connects its own number.
    meta_app_id: str | None = None
    meta_app_secret: str | None = None            # verifies webhook signatures + embedded-signup code exchange
    meta_webhook_verify_token: str | None = None  # the "Verify token" typed into the Meta webhook form
    meta_es_config_id: str | None = None          # Embedded Signup configuration id (optional)
    meta_graph_base: str = "https://graph.facebook.com"
    meta_graph_version: str = "v21.0"
    whatsapp_enc_key: str | None = None           # Fernet key for stored tokens (falls back to one derived from JWT_SECRET)
    renew_url_base: str = "https://dumbbel.fit/renew"
    renew_token_days: int = 14
    wa_send_per_sec: int = 8
    # WhatsApp via QR linking (Evolution API + Baileys) — free, no Meta setup. Leave URL empty to hide the option.
    evolution_api_url: str | None = None         # e.g. http://10.128.0.5:8080 (private) or https://wa.example.com
    evolution_api_key: str | None = None         # Evolution AUTHENTICATION_API_KEY (server-side only)
    evolution_webhook_base: str | None = None    # how Evolution reaches this API (defaults to PUBLIC_BASE_URL)
    wa_web_daily_limit: int = 200                 # max messages per gym per day on a linked number
    wa_web_min_gap_sec: int = 12                  # random pause between two messages from the same number
    wa_web_max_gap_sec: int = 35
    wa_web_quiet_start: int = 21                  # no automated sends 21:00–08:00 gym time
    wa_web_quiet_end: int = 8

    # Push
    expo_access_token: str | None = None

    # App config
    min_admin_version: str = "1.0.0"
    min_member_version: str = "1.0.0"
    support_phone: str = "+914048552210"
    maintenance: bool = False
    admin_app_link: str = "https://dumbbel.fit/admin"
    checkin_url_base: str = "https://dumbbel.fit/checkin"  # gym QR codes open this page (member app)
    member_app_link: str = "https://dumbbel.fit/app"

    # Jobs
    run_scheduler: bool = True

    # Comma-separated exact origins (e.g. https://admin.dumbbel.fit). localhost/127.0.0.1 on any port is always allowed.
    cors_origins: str = ""
    cors_allow_localhost: bool = True
    # Our own sites are always allowed: dumbbel.fit and any subdomain (www, admin, app…), https only.
    cors_origin_regex: str = r"https://([a-z0-9-]+\.)*dumbbel\.fit"

    @property
    def is_prod(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
