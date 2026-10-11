import hashlib
import hmac
import time

from fastapi import HTTPException, Request

from . import config

COOKIE = "sg_session"
TTL = 365 * 24 * 3600  # 登录一次记住一年


def _secret():
    """从环境变量派生(而非本地文件):Render 无持久盘,随机文件会导致每次重启登录失效。"""
    seed = config.PASSWORD + "|" + config.DATABASE_URL
    return hashlib.sha256(("sg-secret:" + seed).encode()).digest()


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
