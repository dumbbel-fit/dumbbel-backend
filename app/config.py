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
    twofactor_tpl_staff_invite: str | None = None
    twofactor_tpl_member_invite: str | None = None
    twofactor_tpl_gym_status: str | None = None

    # Which phone login the apps use: "firebase" (SMS sent by the Firebase SDK in the app) or "2factor"
    auth_provider: str = "firebase"
    # Firebase Phone Auth (alternative to 2Factor; Google sends the SMS, no DLT needed)
    firebase_project_id: str | None = None
    firebase_web_api_key: str | None = None  # Web app apiKey; only used by the /dev/firebase-test page
    firebase_credentials_file: str | None = None  # service-account JSON path (optional for token verification)

    # Object storage (S3/R2). Without bucket config, local disk storage is used.
    s3_bucket: str | None = None
    s3_private_bucket: str | None = None
    s3_region: str = "ap-south-1"
    s3_endpoint_url: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    local_storage_dir: str = "./storage"

    # Push
    expo_access_token: str | None = None

    # App config
    min_admin_version: str = "1.0.0"
    min_member_version: str = "1.0.0"
    support_phone: str = "+914048552210"
    maintenance: bool = False
    admin_app_link: str = "https://dumbbel.fit/admin"
    member_app_link: str = "https://dumbbel.fit/app"

    # Jobs
    run_scheduler: bool = True

    cors_origins: str = "*"

    @property
    def is_prod(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
