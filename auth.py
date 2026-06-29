"""
Authentication via an external OIDC identity provider (Google, Auth0, Supabase,
Keycloak, …). We never store passwords — the provider authenticates the user and
hands us a verified identity (the 'sub' claim), which we map to a local user row.

  ┌─────────┐  login   ┌────────────┐  callback  ┌──────────┐
  │ browser │ ───────▶ │  provider  │ ─────────▶ │  webapp  │
  └─────────┘          └────────────┘            └──────────┘
        session cookie (signed) holds only our local user id.

Configure with environment variables (see .env.example):
  OIDC_ISSUER          e.g. https://accounts.google.com
  OIDC_CLIENT_ID
  OIDC_CLIENT_SECRET
  OIDC_SCOPES          default "openid email profile"
  SECRET_KEY           Flask session signing key (REQUIRED, random, secret)
  APP_BASE_URL         e.g. https://prices.example.com  (for the redirect URI)

==============================================================================
SECURITY REVIEW NOTE — READ BEFORE EXPOSING TO THE INTERNET
This module wires a standard OIDC Authorization Code flow via Authlib, which is
a vetted library. It is a sound starting point, but YOU must still:
  * Run behind HTTPS (the session cookie is marked Secure; without TLS, login
    will not work — that's intentional).
  * Set a strong random SECRET_KEY and keep it secret (rotate if leaked).
  * Register the exact redirect URI (APP_BASE_URL + /auth/callback) with your
    provider and restrict it.
  * Decide who may sign in. By default ANY account at the provider can. To
    restrict (e.g. to specific emails or a Google Workspace domain), set
    OIDC_ALLOWED_EMAILS or OIDC_ALLOWED_DOMAIN.
Treat the access-control decisions here as configuration you own, not defaults
to trust blindly.
==============================================================================
"""

import os
import functools

from flask import session, redirect, url_for, request, jsonify

try:
    from authlib.integrations.flask_client import OAuth
    _HAVE_AUTHLIB = True
except ImportError:
    # authlib not installed (e.g. stale image). Degrade to single-user/local
    # mode rather than crashing the whole app on import.
    OAuth = None
    _HAVE_AUTHLIB = False


oauth = OAuth() if _HAVE_AUTHLIB else None
_provider = None


def init_auth(app):
    """Wire OIDC into the Flask app. Call once at startup."""
    global _provider
    issuer = os.environ.get("OIDC_ISSUER")

    if not issuer or not _HAVE_AUTHLIB:
        # Auth disabled (single-user/local mode), or authlib missing. No login,
        # so a stable signing key isn't important; use a provided one if present,
        # else a random ephemeral key so the app still boots without env setup.
        app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)
        if issuer and not _HAVE_AUTHLIB:
            app.logger.error("OIDC_ISSUER set but authlib is not installed — "
                             "rebuild the image (pip install authlib). Running "
                             "in single-user mode until then.")
        else:
            app.logger.warning("OIDC_ISSUER not set — auth disabled (single-user mode).")
        return None

    # Auth enabled: a strong, STABLE secret is required (sessions must survive
    # restarts and be unguessable). Fail loudly if it's missing.
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        raise RuntimeError(
            "SECRET_KEY must be set when OIDC_ISSUER is configured. "
            "Generate one: python3 -c \"import secrets; print(secrets.token_urlsafe(48))\"")
    app.secret_key = secret

    # Harden the session cookie. HTTPS is required in production.
    secure = os.environ.get("COOKIE_SECURE", "1") != "0"
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure,
    )

    oauth.init_app(app)
    _provider = oauth.register(
        name="oidc",
        client_id=os.environ["OIDC_CLIENT_ID"],
        client_secret=os.environ["OIDC_CLIENT_SECRET"],
        server_metadata_url=issuer.rstrip("/") + "/.well-known/openid-configuration",
        client_kwargs={"scope": os.environ.get("OIDC_SCOPES", "openid email profile")},
    )
    return _provider


def auth_enabled():
    return _provider is not None


def _allowed(email):
    """Optional allow-list. If neither var is set, anyone at the provider may log in."""
    allow_emails = os.environ.get("OIDC_ALLOWED_EMAILS", "").strip()
    allow_domain = os.environ.get("OIDC_ALLOWED_DOMAIN", "").strip()
    if not allow_emails and not allow_domain:
        return True
    email = (email or "").lower()
    if allow_emails and email in {e.strip().lower() for e in allow_emails.split(",")}:
        return True
    if allow_domain and email.endswith("@" + allow_domain.lower()):
        return True
    return False


def register_routes(app, get_store):
    """Add /auth/login, /auth/callback, /auth/logout, /auth/me."""

    @app.route("/auth/login")
    def auth_login():
        if not auth_enabled():
            return redirect("/")
        redirect_uri = os.environ.get("APP_BASE_URL", request.host_url.rstrip("/")) + "/auth/callback"
        return _provider.authorize_redirect(redirect_uri)

    @app.route("/auth/callback")
    def auth_callback():
        if not auth_enabled():
            return redirect("/")
        token = _provider.authorize_access_token()   # verifies signature + nonce
        info = token.get("userinfo") or _provider.userinfo()
        subject = info.get("sub")
        email = info.get("email")
        name = info.get("name") or email
        if not subject or not _allowed(email):
            return jsonify({"error": "account not permitted"}), 403
        s = get_store()
        try:
            uid = s.upsert_user(subject, email=email, name=name)
        finally:
            s.close()
        session.clear()
        session["uid"] = uid
        session["email"] = email
        return redirect("/")

    @app.route("/auth/logout")
    def auth_logout():
        session.clear()
        return redirect("/")

    @app.route("/auth/me")
    def auth_me():
        if not auth_enabled():
            return jsonify({"authenticated": False, "auth_required": False})
        if "uid" in session:
            return jsonify({"authenticated": True, "auth_required": True,
                            "email": session.get("email")})
        return jsonify({"authenticated": False, "auth_required": True})


def current_user_id():
    """The logged-in user's local id, or None. When auth is disabled, None means
    the shared/legacy bucket (single-user mode keeps working)."""
    return session.get("uid")


def login_required(fn):
    """Protect an endpoint. When auth is disabled, allows through (local mode)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if auth_enabled() and "uid" not in session:
            return jsonify({"error": "authentication required"}), 401
        return fn(*args, **kwargs)
    return wrapper
