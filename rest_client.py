#!/usr/bin/env python3
"""
Simplified REST Client for Locations-BM

This is a simplified version of the REST client used for API calls.
Based on the rest_modified.py from ll-lps_cli repo.
"""

import asyncio
import aiohttp
import json
import time
import os
import logging

# --------------- Logging setup ---------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)

TOKEN_REFRESH = 20 * 60  # Token refresh interval in seconds

class Config:
    """Configuration class for REST API settings."""
    def __init__(self, url, username, password, token_file='token.txt'):
        self.REST_API = url
        self.REST_USER = username
        self.REST_PASSWORD = password
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.REST_TOKEN_FILE = os.path.join(current_dir, token_file)
        self.REST_AUTH = None
        self.SERVICE_KEY = None

class Rest:
    """REST API client for interacting with external services."""
    
    class ClientError(Exception):
        def __init__(self, status, reason, content):
            super().__init__(f'{status}: {reason}')
            self.status = status
            self.reason = reason
            self.content = content

    def __init__(self, config):
        self.config = Config(config.REST_API, config.REST_USER, config.REST_PASSWORD, getattr(config, "REST_TOKEN_FILE", "token.txt"))
        self._token = None
        self._evToken = None
        self._tokenTime = 0

    # ------------------------------------ TOKEN MANAGEMENT ------------------------------------ #

    async def get_token(self):
        """Retrieve or refresh the API token."""
        if not self.config.REST_TOKEN_FILE:
            return self._token

        if time.time() - self._tokenTime >= TOKEN_REFRESH:
            self._evToken = None

        if self._evToken is not None:
            await self._evToken.wait()
        else:
            self._evToken = asyncio.Event()
            self._tokenTime = time.time()
            await self._refresh_token()
            self._evToken.set()

        return self._token

    async def _refresh_token(self):
        """Refresh the API token by authenticating with the server."""
        token = None

        # Check for valid token in file
        if self.config.REST_TOKEN_FILE:
            try:
                st = os.stat(self.config.REST_TOKEN_FILE)
                if time.time() - st.st_mtime < TOKEN_REFRESH:
                    with open(self.config.REST_TOKEN_FILE, 'r') as f:
                        token = f.read()
                    self._tokenTime = st.st_mtime
            except Exception as e:
                logging.warning(f"Error reading token file: {e}")

        # Authenticate and fetch a new token if necessary
        if not token:
            logging.info("Authenticating to fetch a new token...")
            data = await self._request(
                method='POST',
                svc='authenticate',
                body={'username': self.config.REST_USER, 'password': self.config.REST_PASSWORD},
                no_auth=True
            )
            token = data.get('token', '') if isinstance(data, dict) else ''
            self._tokenTime = time.time()

            # Save the new token to the token file
            if token and self.config.REST_TOKEN_FILE:
                with open(self.config.REST_TOKEN_FILE, 'w') as f:
                    f.write(token)

        self._token = token

    # ------------------------------------ REQUEST HANDLING ------------------------------------ #

    async def _authorize(self, headers):
        if self.config.REST_AUTH:
            headers.update({'Authorization': self.config.REST_AUTH})
        elif self.config.SERVICE_KEY:
            headers.update({'Authorization': self.config.SERVICE_KEY})
        else:
            token = await self.get_token()
            if token:
                headers.update({'Authorization': f'Bearer {token}'})

    async def _request(self, method, svc, body=None, verbose=0, no_auth=False):
        # Ensure proper URL construction without double slashes
        base_url = self.config.REST_API.rstrip('/')
        url = f"{base_url}/{svc.lstrip('/')}"
        headers = {}

        if body is not None:
            headers.update({'Content-Type': 'application/json'})
        if not no_auth:
            await self._authorize(headers)

        if verbose > 0:
            logging.info(f"{method}: {url}")
        if verbose > 2:
            logging.debug(json.dumps(headers, indent=2))
        if body and verbose > 1:
            logging.debug(json.dumps(body, indent=2))
        
        async def on_response(response):
            status = response.status
            content_type = response.content_type
            url_str = str(response.url)
            try:
                if content_type == 'application/json':
                    data = await response.json()
                    return data
                else:
                    text = await response.text()
                    return text
            except Exception as e:
                text = await response.text()
                return text

        async with aiohttp.ClientSession() as session:
            try:
                if method == 'POST':
                    return await on_response(await session.post(url, headers=headers, json=body))
                if method == 'PUT':
                    return await on_response(await session.put(url, headers=headers, json=body))
                if method == 'GET':
                    return await on_response(await session.get(url, headers=headers, json=body))
                if method == 'DELETE':
                    return await on_response(await session.delete(url, headers=headers))
            except aiohttp.ClientError as e:
                logging.error(f"HTTP request failed for {url}: {e}")
                raise

    # ------------------------------------ PUBLIC API METHODS ------------------------------------ #

    async def post(self, svc, body, verbose=0):
        return await self._request('POST', svc, body, verbose=verbose)

    async def put(self, svc, body, verbose=0):
        return await self._request('PUT', svc, body, verbose=verbose)

    async def get(self, svc, body=None, verbose=0):
        return await self._request('GET', svc, body, verbose=verbose)

    async def delete(self, svc, verbose=0):
        return await self._request('DELETE', svc, verbose=verbose)
