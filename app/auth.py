import hashlib
import hmac
import os
import secrets
import time

from fastapi import HTTPException, Request

from . import config

COOKIE = "sg_session"
TTL = 30 * 24 * 3600


def _secret():
    os.makedirs(os.path.dirname(config.SECRET_PATH), exist_ok=True)
    if os.path.exists(config.SECRET_PATH):
        with open(config.SECRET_PATH, "rb") as f:
            return f.read()
    s = secrets.token_bytes(32)
    with open(config.SECRET_PATH, "wb") as f:
        f.write(s)
    return s


def make_token():
    exp = str(int(time.time()) + TTL)
    sig = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return exp + "|" + sig


def check_token(token):
    try:
        exp, sig = token.split("|", 1)
        if int(exp) < time.time():
            return False
        return hmac.compare_digest(
            sig, hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest())
    except (ValueError, AttributeError):
        return False


def verify_password(password):
    return bool(config.PASSWORD) and hmac.compare_digest(password, config.PASSWORD)


def require_login(request: Request):
    token = request.cookies.get(COOKIE, "")
    if not check_token(token):
        raise HTTPException(status_code=401, detail="未登录")
