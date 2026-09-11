from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request, status

from .config import Settings


def verify_waha_hmac(raw_body: bytes, request: Request, settings: Settings) -> None:
    key = settings.waha_webhook_hmac_key
    if not key:
        return

    algorithm = request.headers.get('X-Webhook-Hmac-Algorithm', '').lower()
    supplied = request.headers.get('X-Webhook-Hmac', '')
    if algorithm != 'sha512' or not supplied:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Missing WAHA webhook HMAC')

    expected = hmac.new(key.encode('utf-8'), raw_body, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid WAHA webhook HMAC')


def verify_admin_token(request: Request, settings: Settings) -> None:
    supplied = request.headers.get('X-ThreadBoss-Admin', '')
    if not settings.admin_token or not hmac.compare_digest(supplied, settings.admin_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid admin token')


def verify_channel_api_key(request: Request, settings: Settings) -> None:
    supplied = request.headers.get('X-ThreadBoss-Key', '')
    if not supplied:
        auth = request.headers.get('Authorization', '')
        if auth.lower().startswith('bearer '):
            supplied = auth[7:].strip()
    if not settings.channel_api_key or not hmac.compare_digest(supplied, settings.channel_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid ThreadBoss channel API key')
