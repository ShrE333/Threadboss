from __future__ import annotations

import asyncio
import base64
import logging
from copy import deepcopy
from typing import Any
from urllib.parse import urljoin

import httpx

from .config import Settings
from .ids import normalize_jid

logger = logging.getLogger(__name__)


class WahaError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class WahaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.waha_base_url.rstrip('/') + '/'

    @property
    def headers(self) -> dict[str, str]:
        headers = {'Accept': 'application/json'}
        if self.settings.waha_api_key:
            headers['X-Api-Key'] = self.settings.waha_api_key
        return headers

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = urljoin(self.base_url, path.lstrip('/'))
        headers = dict(self.headers)
        headers.update(kwargs.pop('headers', {}))

        attempts = max(1, int(self.settings.waha_request_retries))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.request(method, url, headers=headers, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError) as exc:
                last_error = exc
                if attempt < attempts:
                    delay = self.settings.waha_retry_base_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        'Transient WAHA network error (%s/%s) for %s %s: %s; retrying in %.2fs',
                        attempt, attempts, method, path, exc, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise WahaError(f'WAHA {method} {path} request failed after {attempts} attempts: {exc}') from exc
            except httpx.HTTPError as exc:
                raise WahaError(f'WAHA {method} {path} request failed: {exc}') from exc

            # Retry gateway/service transient failures as well. Do not retry 4xx.
            if response.status_code in {502, 503, 504} and attempt < attempts:
                delay = self.settings.waha_retry_base_seconds * (2 ** (attempt - 1))
                logger.warning(
                    'Transient WAHA HTTP %s (%s/%s) for %s %s; retrying in %.2fs',
                    response.status_code, attempt, attempts, method, path, delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.is_error:
                raise WahaError(
                    f'WAHA {method} {path} failed: {response.status_code} {response.text[:500]}',
                    status_code=response.status_code,
                )
            return response

        raise WahaError(f'WAHA {method} {path} request failed: {last_error}')

    async def get_me(self, session: str) -> dict[str, Any]:
        response = await self._request('GET', f'/api/sessions/{session}/me')
        data = response.json()
        if not data or not data.get('id'):
            raise WahaError(f"WAHA session '{session}' is not authenticated/working")

        data['id'] = normalize_jid(str(data['id']))
        if data.get('lid'):
            data['lid'] = normalize_jid(str(data['lid']))
        return data

    async def get_lid_by_phone_number(self, session: str, phone_id: str) -> str | None:
        """Resolve the owner's @c.us / phone-number JID to its WhatsApp @lid alias.

        WAHA exposes GET /api/{session}/lids/pn/{phoneNumber}.  GOWS can emit
        message.any events with @lid chat IDs, so ThreadBoss needs both aliases
        to recognize Message Yourself correctly.
        """
        phone_id = normalize_jid(phone_id)
        phone_number = phone_id.split('@', 1)[0]
        if not phone_number:
            return None

        try:
            response = await self._request(
                'GET',
                f'/api/{session}/lids/pn/{phone_number}',
            )
        except WahaError as exc:
            # Missing mapping is not fatal. WAHA can return a not-found response
            # until it knows the LID mapping for the account/contact.
            if exc.status_code in {404}:
                return None
            raise

        data = response.json()
        lid = data.get('lid') if isinstance(data, dict) else None
        if not lid:
            return None
        return normalize_jid(str(lid))

    async def get_session(self, session: str) -> dict[str, Any]:
        response = await self._request('GET', f'/api/sessions/{session}')
        return response.json()

    async def get_messages_since(
        self,
        session: str,
        since_timestamp: int,
        *,
        limit: int = 100,
        offset: int = 0,
        download_media: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch history across all chats. GOWS supports chatId=all."""
        response = await self._request(
            'GET',
            f'/api/{session}/chats/all/messages',
            params={
                'limit': int(limit),
                'offset': int(offset),
                'filter.timestamp.gte': int(since_timestamp),
                'downloadMedia': str(bool(download_media)).lower(),
            },
        )
        data = response.json()
        return data if isinstance(data, list) else []

    async def send_text(self, session: str, chat_id: str, text: str) -> dict[str, Any]:
        response = await self._request(
            'POST',
            '/api/sendText',
            json={
                'session': session,
                'chatId': normalize_jid(chat_id),
                'text': text,
            },
            headers={'Content-Type': 'application/json'},
        )
        try:
            return response.json()
        except Exception:
            return {'ok': True}

    async def send_list(
        self,
        session: str,
        chat_id: str,
        *,
        title: str,
        description: str,
        button: str,
        sections: list[dict[str, Any]],
        footer: str = '',
    ) -> dict[str, Any]:
        response = await self._request(
            'POST',
            '/api/sendList',
            json={
                'session': session,
                'chatId': normalize_jid(chat_id),
                'reply_to': None,
                'message': {
                    'title': title,
                    'description': description,
                    'footer': footer,
                    'button': button,
                    'sections': sections,
                },
            },
            headers={'Content-Type': 'application/json'},
        )
        try:
            return response.json()
        except Exception:
            return {'ok': True}

    async def send_poll(
        self,
        session: str,
        chat_id: str,
        *,
        name: str,
        options: list[str],
        multiple_answers: bool = False,
    ) -> dict[str, Any]:
        response = await self._request(
            'POST',
            '/api/sendPoll',
            json={
                'session': session,
                'chatId': normalize_jid(chat_id),
                'poll': {
                    'name': name,
                    'options': options,
                    'multipleAnswers': multiple_answers,
                },
            },
            headers={'Content-Type': 'application/json'},
        )
        try:
            return response.json()
        except Exception:
            return {'ok': True}

    async def send_file_bytes(
        self,
        session: str,
        chat_id: str,
        data: bytes,
        *,
        filename: str,
        mimetype: str,
        caption: str = '',
    ) -> dict[str, Any]:
        response = await self._request(
            'POST',
            '/api/sendFile',
            json={
                'session': session,
                'chatId': normalize_jid(chat_id),
                'caption': caption,
                'file': {
                    'mimetype': mimetype,
                    'filename': filename,
                    'data': base64.b64encode(data).decode('ascii'),
                },
            },
            headers={'Content-Type': 'application/json'},
        )
        try:
            return response.json()
        except Exception:
            return {'ok': True}

    async def configure_message_any_webhook(self, session: str, webhook_url: str) -> None:
        """Merge the ThreadBoss message.any webhook into the current WAHA session config."""
        current = await self.get_session(session)
        config = deepcopy(current.get('config') or {})
        webhooks = list(config.get('webhooks') or [])

        # Remove an older ThreadBoss entry for the exact same URL.
        webhooks = [wh for wh in webhooks if wh.get('url') != webhook_url]

        webhook: dict[str, Any] = {
            'url': webhook_url,
            'events': ['message.any', 'poll.vote', 'poll.vote.failed'],
            'retries': {
                'policy': 'exponential',
                'delaySeconds': 2,
                'attempts': 8,
            },
        }
        if self.settings.waha_webhook_hmac_key:
            webhook['hmac'] = {'key': self.settings.waha_webhook_hmac_key}

        webhooks.append(webhook)
        config['webhooks'] = webhooks

        await self._request(
            'PUT',
            f'/api/sessions/{session}',
            json={
                'name': session,
                'config': config,
            },
            headers={'Content-Type': 'application/json'},
        )
