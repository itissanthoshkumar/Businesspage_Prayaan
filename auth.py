"""Authentication for the /admin back-office.

Standard library only — no new dependency on the one internet-facing process.

Three deliberate choices, each buying a specific property:

  1. The session cookie is scoped to Path=/admin. A browser then never attaches
     it to a public page request, so the credential that can read the whole
     customer and lead database is simply absent from the traffic that
     strangers generate. It also means a stolen public-page log can never
     contain a session.

  2. Passwords are PBKDF2-HMAC-SHA256 at 240k iterations with a per-user salt.
     Slow by design; hashlib gives this for free.

  3. Every state-changing admin request carries a CSRF token bound to the
     session. Without it, a page on another site could POST a page deletion
     using the admin's own cookie. The public lead form is deliberately NOT
     CSRF-protected — it is anonymous by design and defended instead by the
     honeypot, the time floor and the quotas.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

# One switch decides whether this process is a review build. Every preview-only
# affordance rides it — the design switcher, the gallery, the dev SECRET
# fallback below and the one-click sign-in — so a production deployment cannot
# end up with some of them on and some off. Read once at import: a build does
# not change what it is halfway through its life.
PREVIEW_BUILD = os.getenv("PBN_SHOW_SWITCHER", "true").lower() in ("1", "true", "yes")

# The identity the one-click shortcut signs in as. Deliberately NOT a real
# account: every page it creates is stamped with this name in the audit trail,
# so a row made by the shortcut can never be mistaken for the work of a person
# who typed a password.
DEV_USERNAME = "preview-admin"
DEV_ROLE = "admin"

# In production this must be set. Refusing to boot without it is the point: a
# service that silently generates a key on start invalidates every session on
# every restart, and two workers would sign with different keys.
SECRET = os.getenv("PBN_SECRET_KEY", "")
_DEV_FALLBACK = "dev-only-not-for-production"
if not SECRET:
    if PREVIEW_BUILD:
        SECRET = _DEV_FALLBACK          # review/preview builds only
    else:
        raise RuntimeError(
            "PBN_SECRET_KEY is not set. Refusing to start: sessions would be "
            "unsignable and every restart would silently log everyone out.")

SESSION_COOKIE = "pbn_admin"
SESSION_PATH = "/admin"
SESSION_MAX_AGE = 8 * 3600              # a working day, then sign in again
PBKDF2_ROUNDS = 240_000

# Login throttling, per username. Held in memory: a single process today, and a
# lockout that resets on restart is still far better than none. Move to the
# store if this ever runs multi-worker.
_ATTEMPTS = {}
MAX_ATTEMPTS = 8
LOCKOUT_SECONDS = 900


def hash_password(password: str, salt: bytes = None) -> str:
    """-> 'pbkdf2$<rounds>$<salt_b64>$<hash_b64>'"""
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode())


# A fixed valid hash of a random throwaway password. login() verifies against
# this when the username is unknown or inactive, so the PBKDF2 cost is paid on
# every attempt and response time no longer reveals whether a username exists.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt_b64, hash_b64 = (stored or "").split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 base64.b64decode(salt_b64), int(rounds))
        # constant time: a timing difference here leaks the hash a byte at a time
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


def _sign(payload: bytes) -> str:
    mac = hmac.new(SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
    return "{}.{}".format(
        base64.urlsafe_b64encode(payload).decode().rstrip("="),
        base64.urlsafe_b64encode(mac).decode().rstrip("="))


def _unsign(token: str):
    try:
        body, mac = token.split(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        given = base64.urlsafe_b64decode(mac + "=" * (-len(mac) % 4))
        expected = hmac.new(SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(given, expected):
            return None
        return json.loads(payload.decode("utf-8"))
    except Exception:                                   # noqa: BLE001
        return None


def make_session(username: str, role: str) -> str:
    payload = json.dumps({
        "u": username, "r": role, "t": int(time.time()),
        # a random nonce so two sessions for the same user in the same second
        # are still distinct tokens, and so the CSRF token differs per login
        "n": secrets.token_urlsafe(9),
    }, separators=(",", ":")).encode("utf-8")
    return _sign(payload)


def read_session(token: str):
    data = _unsign(token or "")
    if not data:
        return None
    if int(time.time()) - int(data.get("t", 0)) > SESSION_MAX_AGE:
        return None                                     # expired
    return data


def csrf_token(session: dict) -> str:
    """Bound to the session, so it cannot be lifted from one user to another."""
    if not session:
        return ""
    msg = "{}|{}".format(session.get("u", ""), session.get("n", "")).encode("utf-8")
    return hmac.new(SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:40]


def csrf_ok(session: dict, given: str) -> bool:
    want = csrf_token(session)
    return bool(want) and hmac.compare_digest(want, given or "")


def note_failure(username: str):
    now = time.time()
    count, _ = _ATTEMPTS.get(username, (0, now))
    _ATTEMPTS[username] = (count + 1, now)


def clear_failures(username: str):
    _ATTEMPTS.pop(username, None)


def locked_out(username: str) -> int:
    """Seconds remaining, 0 if not locked."""
    count, last = _ATTEMPTS.get(username, (0, 0))
    if count < MAX_ATTEMPTS:
        return 0
    remaining = int(LOCKOUT_SECONDS - (time.time() - last))
    if remaining <= 0:
        _ATTEMPTS.pop(username, None)
        return 0
    return remaining
