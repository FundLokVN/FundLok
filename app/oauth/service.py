from abc import ABC, abstractmethod

import httpx
from fastapi import BackgroundTasks, HTTPException, status
import jwt
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import totp
from app.auth.service import create_token_pair, is_unseen_device
from app.auth.user_agent import describe_browser, describe_device
from app.utils.audit import append_audit
from app.utils.email import send_new_device_signin_alert
from app.core.config import settings
from app.oauth.schema import OAuthProvider, OAuthUserInfo
from app.users.models import (
    USER_STATUS_SUSPENDED,
    RefreshToken,
    TotpRecoveryCode,
    User,
    WebAuthnChallenge,
    WebAuthnCredential,
)


class OAuthProviderBase(ABC):
    provider: OAuthProvider
    issuer: str
    discovery_url: str
    client_id: str | None

    def __init__(self, client_id: str | None):
        self.client_id = client_id

    async def login(
        self,
        db: AsyncSession,
        token: str,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
        background_tasks: BackgroundTasks | None = None,
    ) -> tuple[User, dict, bool]:
        """Sign in with a provider ID token.

        Returns (user, tokens, is_new_device). The extra two exist because a
        social sign-in is a sign-in: it has to record the device on the session
        and be visible in the account's security history, exactly like the
        password path. Before this, Google sign-ins produced sessions with no
        user agent ("Unknown device" on the security screen), never appeared in
        recent activity, and never triggered a new-device alert.
        """
        claims = self.verify_token(token)
        user_info = self.map_claims(claims)
        user = await self.get_or_create_user(db, user_info)

        if user.status == USER_STATUS_SUSPENDED:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account has been suspended",
            )
        if user.totp_enabled:
            return (
                user,
                {"totp_required": True, "challenge_token": totp.create_challenge_token(user.id)},
                False,
            )

        # Asked before the new session row exists, or the sign-in being
        # recorded would count as its own prior history.
        is_new_device = await is_unseen_device(db, user.id, user_agent)

        tokens = await create_token_pair(
            db, user.id, user_agent=user_agent, ip_address=ip_address
        )
        await db.commit()

        if is_new_device and user.signin_alerts_enabled and background_tasks is not None:
            background_tasks.add_task(
                send_new_device_signin_alert,
                user.email,
                device=describe_device(user_agent),
                browser=describe_browser(user_agent),
                ip_address=ip_address,
            )

        return user, tokens, is_new_device

    def verify_token(self, token: str) -> dict:
        return self.verify_id_token(token)

    def verify_id_token(self, id_token: str) -> dict:
        if not self.client_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"{self.provider.value} OAuth client ID is not configured",
            )

        try:
            discovery = self._fetch_json(self.discovery_url)
            jwks = self._fetch_json(discovery["jwks_uri"])
            header = jwt.get_unverified_header(id_token)
            jwk = self._find_key(jwks["keys"], header.get("kid"))
            # PyJWT does not accept a raw JWK dict as the key: PyJWK parses it
            # into a cryptography public key (kty RSA/EC -> RS/ES family).
            key = jwt.PyJWK(jwk).key
            claims = jwt.decode(
                id_token,
                key,
                algorithms=self._signing_algorithms(discovery, jwk),
                audience=self.client_id,
                options={"verify_iss": False},
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid OAuth token",
            ) from exc

        if not self._issuer_is_valid(claims.get("iss")):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid OAuth issuer",
            )

        if claims.get("email_verified") is not True and self.provider == OAuthProvider.GOOGLE:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Google email is not verified",
            )

        return claims

    @abstractmethod
    def map_claims(self, claims: dict) -> OAuthUserInfo:
        raise NotImplementedError

    @abstractmethod
    def _issuer_is_valid(self, issuer: str | None) -> bool:
        raise NotImplementedError

    async def get_or_create_user(self, db: AsyncSession, user_info: OAuthUserInfo) -> User:
        # Accounts are found by email, so an address the provider has not
        # verified would sign its holder into whoever's account carries it (or
        # create a verified-looking one in the owner's name). Refuse it here,
        # whatever the provider, rather than trusting each map_claims to.
        if not user_info.email_verified:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Your sign-in provider has not verified this email address",
            )
        result = await db.execute(select(User).where(User.email == user_info.email))
        user = result.scalar_one_or_none()
        if user:
            # The provider has already confirmed ownership of this email, so an
            # existing account that signed up via email/password should be
            # considered verified once they log in through OAuth.
            if user_info.email_verified and not user.email_verified:
                await self._reclaim_unverified_account(db, user)
            return user

        user = User(
            email=user_info.email,
            phone=None,
            full_name=user_info.full_name,
            # No password: this account signs in through the provider. The
            # column is nullable (migration d5a8c31f6b02) precisely so this
            # state is representable.
            #
            # It used to hold token_urlsafe(32) -- a random string, not a hash.
            # passlib can't identify that format, so verify_password() raised
            # UnknownHashError instead of returning False: any password-login
            # attempt on an OAuth account was a 500, not a 401. It also made
            # has_password report True, hiding the "Set password" flow from the
            # only users who need it.
            password_hash=None,
            role=None,  # chosen later via /select-role, same as email/password signup
            status="ACTIVE",
            email_verified=True,
        )
        db.add(user)
        await db.flush()
        return user

    async def _reclaim_unverified_account(self, db: AsyncSession, user: User) -> None:
        """Hand an unverified account to the person who just proved the email.

        /auth/register creates a password account for any address without
        proving the registrant owns it. Nothing stops a stranger registering
        the victim's email first and waiting: when the real owner later signs
        in with Google or Microsoft, this lookup finds that account by email.
        Before this, it was only marked verified, and the stranger's password
        (and sessions, and any 2FA they enrolled) still worked, i.e. an
        account takeover of whatever the owner then put in it.

        This is the first moment anyone has proven ownership of the address,
        so every sign-in credential set before it belongs to someone unproven
        and is removed:

          * the password (the owner can set their own via "Set password")
          * every refresh session, so the stranger is signed out
          * TOTP: the secret, the enabled flag and all recovery codes; left in
            place it would lock the real owner out behind the stranger's
            authenticator
          * passkeys and pending passkey challenges

        Deliberately a no-op for an account already verified: then its
        credentials were set by someone who proved the email, i.e. the owner.
        """
        now = datetime.now(timezone.utc)
        had = {
            "password": user.password_hash is not None,
            "totp": bool(user.totp_enabled or user.totp_secret),
        }

        user.email_verified = True
        user.password_hash = None
        user.totp_enabled = False
        user.totp_secret = None
        user.totp_confirmed_at = None

        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await db.execute(delete(TotpRecoveryCode).where(TotpRecoveryCode.user_id == user.id))
        await db.execute(delete(WebAuthnCredential).where(WebAuthnCredential.user_id == user.id))
        await db.execute(delete(WebAuthnChallenge).where(WebAuthnChallenge.user_id == user.id))

        # On the admin audit trail (entity USER, which the user's own security
        # screen doesn't list), so an operator can see why a password or
        # authenticator the account used to have is gone.
        append_audit(
            db,
            entity_type="USER",
            entity_id=user.id,
            action="UNVERIFIED_ACCOUNT_RECLAIMED",
            actor_id=user.id,
            before_state=had,
            after_state={"provider": self.provider.value, "email_verified": True},
        )
        await db.flush()

    def _fetch_json(self, url: str) -> dict:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.json()

    def _signing_algorithms(self, discovery: dict, key: dict) -> list[str]:
        """Algorithms we accept for an ID token -- never read from the token itself.

        The JWT header is supplied by whoever is logging in, so taking `alg` from
        it lets the caller choose how their own token gets verified. The key here
        is a *public* JWK, which makes an HMAC alg the dangerous case: it would
        hand the caller a verification "secret" they already know.

        Trust the provider instead, in descending order of specificity: the alg
        published on the JWK itself, then the discovery document, then RS256 --
        the only value Google, Apple and Microsoft advertise. The RS/ES/PS filter
        keeps a permissive discovery document from reopening the same hole.
        """
        if key.get("alg"):
            candidates = [key["alg"]]
        else:
            candidates = list(discovery.get("id_token_signing_alg_values_supported") or [])

        return [alg for alg in candidates if alg[:2] in ("RS", "ES", "PS")] or ["RS256"]

    def _find_key(self, keys: list[dict], kid: str | None) -> dict:
        if not kid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="OAuth token header is missing key id",
            )

        for key in keys:
            if key.get("kid") == kid:
                return key

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OAuth signing key not found",
        )


class GoogleOAuthProvider(OAuthProviderBase):
    provider = OAuthProvider.GOOGLE
    issuer = "https://accounts.google.com"
    discovery_url = "https://accounts.google.com/.well-known/openid-configuration"

    def map_claims(self, claims: dict) -> OAuthUserInfo:
        return OAuthUserInfo(
            provider=self.provider,
            provider_subject=claims["sub"],
            email=claims["email"],
            full_name=claims.get("name"),
            email_verified=bool(claims.get("email_verified")),
        )

    tokeninfo_url = "https://oauth2.googleapis.com/tokeninfo"
    userinfo_url = "https://www.googleapis.com/oauth2/v3/userinfo"

    def verify_token(self, token: str) -> dict:
        # An ID token is a JWT (header.payload.signature) and goes through the
        # signed path: signature, aud == our client ID, issuer, email_verified.
        # Anything else is treated as an access token from the frontend's
        # implicit flow, which must prove it was issued to FundLok too.
        if token.count(".") == 2:
            return self.verify_id_token(token)
        return self.verify_access_token(token)

    def verify_access_token(self, access_token: str) -> dict:
        """Accept a Google access token only if it was issued to FundLok.

        This used to call userinfo and trust whatever came back. userinfo
        answers for ANY valid Google access token, whichever app it was issued
        to, so a token a victim granted to some other site (one the attacker
        runs, or one that leaked) could be replayed here and exchanged for a
        full FundLok session on the account with that email.

        tokeninfo reports who the token was issued to. We require aud/azp to
        be our own client ID and the email to be verified before trusting it.
        """
        if not self.client_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"{self.provider.value} OAuth client ID is not configured",
            )

        invalid = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google access token",
        )

        info = self._google_get(self.tokeninfo_url, params={"access_token": access_token})
        if info is None:
            raise invalid

        # Both must name us: aud is the client the token is for, azp the one
        # that requested it. A token for another client ID is someone else's.
        if info.get("aud") != self.client_id or info.get("azp", self.client_id) != self.client_id:
            raise invalid

        # tokeninfo returns the flag as the string "true"; the ID-token path
        # gets a real boolean. Accept only those two, never a truthy guess.
        if info.get("email_verified") not in (True, "true"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Google email is not verified",
            )

        if not info.get("sub") or not info.get("email"):
            raise invalid

        claims = {
            "sub": info["sub"],
            "email": info["email"],
            "email_verified": True,
        }

        # tokeninfo carries no display name. userinfo is fetched only for the
        # name, now that the token is known to be ours, and must describe the
        # same Google account.
        profile = self._google_get(
            self.userinfo_url, headers={"Authorization": f"Bearer {access_token}"}
        )
        if profile and profile.get("sub") == info["sub"] and profile.get("name"):
            claims["name"] = profile["name"]

        return claims

    def _google_get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> dict | None:
        """GET a Google endpoint; None on any non-200 or unreadable body."""
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url, params=params, headers=headers)
            if response.status_code != 200:
                return None
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        return body if isinstance(body, dict) else None

    def _issuer_is_valid(self, issuer: str | None) -> bool:
        return issuer in {"https://accounts.google.com", "accounts.google.com"}


class MicrosoftOAuthProvider(OAuthProviderBase):
    provider = OAuthProvider.MICROSOFT

    @property
    def discovery_url(self) -> str:
        return f"https://login.microsoftonline.com/{settings.MICROSOFT_TENANT_ID}/v2.0/.well-known/openid-configuration"

    def map_claims(self, claims: dict) -> OAuthUserInfo:
        # nOAuth. With MICROSOFT_TENANT_ID "common" any Entra tenant can sign
        # in, and a tenant's admin can set a user's `mail` (sent as `email`)
        # or UPN (`preferred_username`) to any address without proving it.
        # Neither is evidence of who owns the address. Microsoft's answer is
        # the `xms_edov` optional claim ("email domain owner verified"), so
        # only `email` with xms_edov true counts as verified. The optional
        # claim has to be added to the app registration before this provider
        # is switched on, or every Microsoft sign-in is refused.
        email = claims.get("email")
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Microsoft account does not provide an email address",
            )
        return OAuthUserInfo(
            provider=self.provider,
            provider_subject=claims["sub"],
            email=email,
            full_name=claims.get("name"),
            email_verified=claims.get("xms_edov") in (True, "true", "1", 1),
        )

    def _issuer_is_valid(self, issuer: str | None) -> bool:
        if not issuer:
            return False
        if settings.MICROSOFT_TENANT_ID != "common":
            return issuer == f"https://login.microsoftonline.com/{settings.MICROSOFT_TENANT_ID}/v2.0"
        return issuer.startswith("https://login.microsoftonline.com/") and issuer.endswith("/v2.0")


class OAuthService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.providers = {
            OAuthProvider.GOOGLE: GoogleOAuthProvider(settings.GOOGLE_CLIENT_ID),
            OAuthProvider.MICROSOFT: MicrosoftOAuthProvider(settings.MICROSOFT_CLIENT_ID),
        }

    async def login(
        self,
        provider: OAuthProvider,
        token: str,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
        background_tasks: BackgroundTasks | None = None,
    ) -> tuple[User, dict, bool]:
        oauth_provider = self.providers.get(provider)
        if oauth_provider is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported OAuth provider",
            )
        return await oauth_provider.login(
            self.db,
            token,
            user_agent=user_agent,
            ip_address=ip_address,
            background_tasks=background_tasks,
        )
