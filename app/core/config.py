from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Local development switch. Declared here because `extra="ignore"` above
    # means an undeclared DEBUG in .env is silently dropped — it was, until
    # now. Defaults to FALSE so a deployment that forgets to set it is the
    # closed one: it gates the interactive API docs, and failing open there
    # publishes every route and schema in the service.
    DEBUG: bool = False

    # Sets the Secure attribute on the auth cookies, so the browser only ever
    # sends them over HTTPS. Defaults to TRUE: a deployment that forgets this
    # variable must get the safe behaviour, because the failure mode is a
    # 14-day refresh token travelling in clear text on any downgraded or
    # mixed-content request.
    #
    # Local development sets it False in .env. Chrome and Firefox do accept
    # Secure cookies on localhost/127.0.0.1, but Safari historically does not,
    # and "works in my browser" is a poor reason for a dev to lose their
    # session.
    COOKIE_SECURE: bool = True

    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14
    # Hard ceiling on one sign-in, however often it is refreshed. Every
    # rotation restarts REFRESH_TOKEN_EXPIRE_DAYS, so without this a session
    # used once a fortnight never ends and a stolen one can be kept alive
    # forever. Counted from the original sign-in, not the last refresh.
    SESSION_MAX_LIFETIME_DAYS: int = 30
    GOOGLE_CLIENT_ID: str | None = None
    MICROSOFT_CLIENT_ID: str | None = None
    MICROSOFT_TENANT_ID: str = "common"
    MOCK_UPLOAD_BASE_URL: str = "https://mock-storage.fundlok.local/upload"
    FRONTEND_URL: str = "http://localhost:3000"
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000,https://fundlok-front-end.vercel.app"
    CLOUDFLARE_TURNSTILE_SECRET_KEY: str | None = None

    # How many reverse proxies sit in front of the app. X-Forwarded-For is
    # APPENDED to by each hop, so any entry a proxy did not write itself was
    # supplied by the client and is a free-text field. Reading the leftmost
    # entry — the obvious implementation — reads exactly the part the caller
    # controls, which turns a stored client IP into a value the caller chooses.
    #
    # 0 (the default) means trust nothing and use the socket peer. Set it to
    # the real number of hops in each deployment: 1 behind Cloud Run, 2 behind
    # Cloudflare in front of Cloud Run. The Nth entry from the RIGHT is then
    # the address the innermost trusted proxy observed, and is the first one
    # the client could not have forged.
    #
    # This is only the single control point because uvicorn is started with
    # --no-proxy-headers (docker/entrypoint.sh, package.json). Uvicorn's
    # ProxyHeadersMiddleware is ON by default and rewrites request.client from
    # X-Forwarded-For itself, which silently overrode a 0 here — verified: with
    # it enabled, a request carrying a forged XFF stored the forged address
    # despite this setting being 0. If that flag is ever dropped, this setting
    # stops meaning anything.
    TRUSTED_PROXY_HOPS: int = 0

    # The frontend's shared secret: the SAME value as BACKEND_SECRET_KEY on
    # Vercel, which every Next → FastAPI call sends as X-Backend-Secret. A
    # request carrying it came from our own Next server, so the X-Client-IP it
    # also carries is trusted as the visitor's address and TRUSTED_PROXY_HOPS
    # is not consulted (app/utils/client_ip.py).
    #
    # Needed because the hop rule cannot see visitors at all: the API's caller
    # is Vercel, so XFF ends in a Vercel egress address, and the server-side
    # session checks Next makes on every page load carry no visitor address in
    # it. Unset (the default) disables the header entirely and leaves the hop
    # rule as the only one.
    BACKEND_SECRET_KEY: str | None = None

    # --- Abuse controls (compliance audit SEC-08) ---
    # Captcha is required unless turned off here. Required with no
    # CLOUDFLARE_TURNSTILE_SECRET_KEY means the protected endpoints refuse
    # (503) rather than accept every request. The safe value is the default,
    # as with COOKIE_SECURE; local dev and the tests opt out.
    CAPTCHA_REQUIRED: bool = True
    # Per-IP limits, app/core/rate_limit.py. Off until the deployment sees
    # real visitor addresses: BACKEND_SECRET_KEY set and the frontend sending
    # X-Client-IP, checked in the audit log (docs/deployment/
    # ci-cd-pipeline.md, "Client IP"). Keyed on a proxy's address, a per-IP
    # limit throttles every visitor as one caller, so with neither
    # BACKEND_SECRET_KEY nor TRUSTED_PROXY_HOPS set it stays off regardless.
    RATE_LIMIT_ENABLED: bool = False
    RATE_LIMIT_PER_MINUTE: int = 600
    RATE_LIMIT_SENSITIVE_PER_MINUTE: int = 20


    # --- Passkeys / WebAuthn ---
    # The Relying Party ID is the domain a credential is bound to, and a
    # passkey registered against one RP ID cannot be used against another —
    # get it wrong and every existing passkey silently stops working. It must
    # be the site's registrable domain (no scheme, no port): "localhost" in
    # development, "fundlok.com" in production. Derived from FRONTEND_URL when
    # unset so local development needs no configuration at all.
    WEBAUTHN_RP_ID: str | None = None
    WEBAUTHN_RP_NAME: str = "FundLok"
    # Exact origin(s) the browser will report, scheme and port included.
    # Comma-separated: a staging domain and production can share a build.
    WEBAUTHN_ORIGINS: str | None = None

    # Encrypts users.totp_secret at rest (app/auth/totp_crypto.py). A SEPARATE
    # secret from SECRET_KEY on purpose: sharing them would mean rotating the
    # JWT key locks every 2FA user out of their authenticator. Unset means
    # enrolment is refused — the feature fails closed rather than storing
    # password-equivalent secrets in plaintext.
    TOTP_ENCRYPTION_KEY: str | None = None

    # Encrypts the Google OAuth refresh token an admin's Gmail connection holds
    # under Admin > Email (app/admin/internal_email/crypto.py). Its own key,
    # separate from both SECRET_KEY and TOTP_ENCRYPTION_KEY, so each can rotate
    # alone. Unset means no account can be connected: fail closed, never
    # plaintext.
    EMAIL_SETTINGS_ENCRYPTION_KEY: str | None = None

    # Admin > Email: each admin connects their Gmail account with Google OAuth
    # 2.0 (authorization code flow, scope gmail.send) and mail goes out through
    # the Gmail API. A "Web application" OAuth client. GOOGLE_OAUTH_CLIENT_ID
    # unset means the sign-in client (GOOGLE_CLIENT_ID) is used, so reusing it
    # needs only that client's secret here. The redirect URI must be
    # registered on the client EXACTLY, and defaults to the frontend's /api
    # proxy: {FRONTEND_URL}/api/admin/email/oauth/callback. No client id or no
    # secret means the Connect button answers 503 (GMAIL_OAUTH_NOT_CONFIGURED).
    GOOGLE_OAUTH_CLIENT_ID: str | None = None
    GOOGLE_OAUTH_CLIENT_SECRET: str | None = None
    GOOGLE_OAUTH_REDIRECT_URI: str | None = None
    # Comma-separated domains a connected Gmail account must be on, e.g.
    # "fundlok.com". Empty means any Google account.
    GMAIL_CONNECT_ALLOWED_DOMAINS: str = ""

    # PartyKit realtime push for the notification bell (app/notifications/
    # realtime.py). All three unset = feature off and polling is the only path.
    # PARTYKIT_HOST is e.g. "fundlok-notifications.<user>.partykit.dev" (no
    # scheme). The two secrets are DIFFERENT on purpose: one signs the short
    # browser tokens, the other authorises us to publish; leaking one must not
    # grant the other. Both must match `partykit env` on the PartyKit project.
    PARTYKIT_HOST: str | None = None
    PARTYKIT_JWT_SECRET: str | None = None
    PARTYKIT_PUBLISH_SECRET: str | None = None

    # Cloudflare R2 (S3-compatible). Locally, point at the minio container.
    R2_ENDPOINT_URL: str | None = None
    R2_ACCESS_KEY_ID: str | None = None
    R2_SECRET_ACCESS_KEY: str | None = None
    R2_BUCKET: str = "fundlok-uploads"
    # Bucket for retained KYC/KYB verification documents (falls back to
    # R2_BUCKET when unset; keys are prefixed verification/KYC|KYB/<id>/).
    R2_VERIFICATION_BUCKET: str | None = None
    R2_REGION: str = "auto"
    R2_PRESIGN_EXPIRE_SECONDS: int = 15 * 60

    # SMTP Configuration
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 1025
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_SECURE: bool = False
    EMAILS_FROM_EMAIL: str = "noreply@fundlok.com"
    EMAILS_FROM_NAME: str = "FundLok"
    # Where /contact submissions are forwarded. The submitter's address goes on
    # Reply-To, so replying from this inbox answers them directly.
    CONTACT_INBOX_EMAIL: str = "support@fundlok.com"

    # Didit KYC / identity verification (https://docs.didit.me)
    # The verification API authenticates with a static secret on x-api-key.
    DIDIT_API_KEY: str | None = None
    DIDIT_WORKFLOW_ID: str | None = None  # KYC workflow UUID (investors), passed per session
    DIDIT_KYB_WORKFLOW_ID: str | None = None  # KYB workflow UUID (SMEs / business verification)
    # Webhook destination secret_shared_key (X-Signature-V2 HMAC). Accepts the
    # legacy DIDIT_WEBHOOK_SECRET_KEY name as a fallback alias.
    DIDIT_WEBHOOK_SECRET: str | None = None
    DIDIT_WEBHOOK_SECRET_KEY: str | None = None
    DIDIT_BASE_URL: str = "https://verification.didit.me"
    DIDIT_CALLBACK_URL: str | None = None  # FE return URL; defaults to FRONTEND_URL/kyc/callback
    DIDIT_LANGUAGE: str = "vi"  # ISO 639-1 UI language for the hosted flow (Vietnamese)

    # GVerify / GHub eKYC (Datatrust, API v2.5.1) — direct-API KYC provider,
    # parallel to Didit. See docs/specs/gverify/ekyc-kyc-verification.md.
    GVERIFY_BASE_URL: str | None = None  # partner-provided {api-base-url}
    GVERIFY_API_KEY: str | None = None  # x-api-key header value
    GVERIFY_PARTNER_CODE: str | None = None  # partner/subpartner "code" sent per request
    GVERIFY_OS_TYPE: str = "fundlok-backend"  # os-type header
    GVERIFY_MIN_OCR_CONFIDENCE: float = 0.85  # min person_number/full_name confidence
    # Face similarity (the provider's `matching` %, 0-100) at or above which the
    # portrait is taken to be the card's own photo resubmitted, not a capture of
    # the person, and the attempt is parked for a human (spec §6 rule 11).
    # A stopgap for the missing liveness step (spec Open Question 2).
    GVERIFY_CARD_PHOTO_SIMILARITY: float = 98.0
    # KYB (business verification) — docs/specs/gverify/ekyb-kyb-verification.md
    GVERIFY_MIN_KYB_OCR_CONFIDENCE: float = 0.85  # min name/tax_code confidence
    # Require the SME's KYC-verified person_number to appear among the
    # certificate's legal representatives (spec Rule 7, default off pending review).
    GVERIFY_KYB_REQUIRE_REP_MATCH: bool = False
    # Submissions allowed per user per rolling 24 hours, counted separately for
    # KYC and KYB. Every automatic attempt is billable provider calls (OCR plus
    # face match, or OCR plus the tax registry), and a rejection invites a retry.
    GVERIFY_MAX_ATTEMPTS_PER_DAY: int = 5

    # --- Phone verification (Zalo ZBS) ---
    # docs/specs/users/phone-verification.md. A new account (password or
    # social sign-in) must prove a Vietnamese mobile number by one-time code
    # before it may choose a role. Required by default, like CAPTCHA_REQUIRED:
    # with Zalo unconfigured the send endpoint answers 503 rather than letting
    # accounts through unverified. The tests opt out.
    PHONE_VERIFICATION_REQUIRED: bool = True
    # How the code reaches the phone:
    #   zalo — a Zalo ZBS template message (needs the ZALO_* settings below)
    #   log  — writes the code to the server log; only honoured with DEBUG=True
    PHONE_OTP_CHANNEL: Literal["zalo", "log"] = "zalo"

    # Zalo Official Account app (developers.zalo.me > app > Official Account).
    # The secret goes in the `secret_key` header of token refreshes only.
    ZALO_APP_ID: str | None = None
    ZALO_APP_SECRET: str | None = None
    # The refresh token from authorising the app on the OA. Read once to start
    # the chain: Zalo replaces the refresh token on every use, so the current
    # one lives encrypted in zalo_oa_tokens. Putting a different value here
    # (after re-authorising the OA) starts a new chain.
    ZALO_OA_REFRESH_TOKEN: str | None = None
    # Encrypts the stored OA tokens (app/phone_verification/zalo.py). Its own
    # key, like TOTP_ENCRYPTION_KEY: unset means Zalo is refused, never that
    # tokens are stored in plaintext.
    ZALO_TOKEN_ENCRYPTION_KEY: str | None = None
    # The approved ZBS template of type OTP, and the name of its code parameter.
    ZALO_ZBS_OTP_TEMPLATE_ID: str | None = None
    ZALO_ZBS_OTP_PARAM: str = "otp"
    # Sends with "mode": "development": Zalo delivers only to admins of the
    # app or the OA. For trying the template out; never on in production.
    ZALO_ZBS_DEVELOPMENT_MODE: bool = False

    # ARQ worker (T2 -- HANDOFF-03). The daily repayment batch and ML scoring
    # run through ARQ, never BackgroundTasks (CLAUDE.md). Points at the
    # docker-compose `redis` service locally; a managed Redis URL in prod.
    REDIS_URL: str = "redis://localhost:6379/0"
    @property
    def webauthn_rp_id(self) -> str:
        """Registrable domain for passkeys, derived from FRONTEND_URL if unset."""
        if self.WEBAUTHN_RP_ID:
            return self.WEBAUTHN_RP_ID.strip()
        from urllib.parse import urlparse

        host = urlparse(self.FRONTEND_URL).hostname or "localhost"
        return host

    @property
    def webauthn_origins(self) -> list[str]:
        """Origins an assertion may legitimately come from."""
        if self.WEBAUTHN_ORIGINS:
            return [o.strip().rstrip("/") for o in self.WEBAUTHN_ORIGINS.split(",") if o.strip()]
        return [self.FRONTEND_URL.strip().rstrip("/")]

    @property
    def gmail_oauth_client_id(self) -> str | None:
        """The OAuth client for Admin > Email: its own, else the sign-in one."""
        client_id = self.GOOGLE_OAUTH_CLIENT_ID or self.GOOGLE_CLIENT_ID
        return client_id.strip() if client_id else None

    @property
    def gmail_oauth_redirect_uri(self) -> str:
        """Where Google sends the admin back after the Gmail consent screen."""
        if self.GOOGLE_OAUTH_REDIRECT_URI:
            return self.GOOGLE_OAUTH_REDIRECT_URI.strip()
        return self.FRONTEND_URL.strip().rstrip("/") + "/api/admin/email/oauth/callback"

    @property
    def gmail_connect_allowed_domains(self) -> list[str]:
        return [
            d.strip().lower().lstrip("@")
            for d in self.GMAIL_CONNECT_ALLOWED_DOMAINS.split(",")
            if d.strip()
        ]

    @property
    def cors_origins(self) -> list[str]:
        origins = [o.strip().rstrip("/") for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]
        frontend = self.FRONTEND_URL.strip().rstrip("/") if self.FRONTEND_URL else None
        if frontend and frontend not in origins:
            origins.append(frontend)
        return origins



settings = Settings()