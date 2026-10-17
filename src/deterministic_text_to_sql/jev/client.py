"""Client for interacting with the TypeSafe Jev System 1 API."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from deterministic_text_to_sql.jev.exceptions import (
    JevAPIConnectionError,
    JevResponseParseError,
)
from deterministic_text_to_sql.jev.models import ParsedSlots
from deterministic_text_to_sql.jev.prompts import build_jev_payload
from deterministic_text_to_sql.jev.slots import parse_jev_response
from deterministic_text_to_sql.schema.models import SchemaMetadata


class JevClient:
    """HTTP client for the ultra-low-latency TypeSafe Jev System 1 API.

    Maintains persistent connections with HTTP Keep-Alive, enforces timeouts,
    implements automatic retries with backoff on network/server errors, and provides
    context manager lifecycle management.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.typesafe.ai",
        timeout: float = 3.0,
        max_retries: int = 2,
    ) -> None:
        """Initialize the JevClient.

        Args:
            api_key: TypeSafe API key. If omitted, read from TYPESAFE_API_KEY env var.
            base_url: Base endpoint URL of the TypeSafe API.
            timeout: Request timeout in seconds (default: 3.0s).
            max_retries: Number of retries on 5xx or connection errors (default: 2).
        """
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY") or ""
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "deterministic-text-to-sql/0.1.0",
            },
        )

    def __enter__(self) -> JevClient:
        """Enter context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Exit context manager and close underlying HTTP client."""
        self.close()

    def close(self) -> None:
        """Close persistent HTTP Keep-Alive connection pool."""
        self._client.close()

    def call_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send payload to Jev API (POST /v1/systemone) with retries and timeout handling.

        Args:
            payload: Structured JSON dictionary created by build_jev_payload.

        Returns:
            Parsed JSON dictionary returned from the API.

        Raises:
            JevAPIConnectionError: If API key is missing, network fails, or 5xx persists.
            JevResponseParseError: If response payload is not valid JSON.
        """
        if not self.api_key:
            raise JevAPIConnectionError(
                "TypeSafe API key is required. "
                "Pass api_key or set TYPESAFE_API_KEY environment variable."
            )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }

        last_error: Exception | None = None

        for attempt in range(1 + self.max_retries):
            try:
                response = self._client.post(
                    "/v1/systemone",
                    json=payload,
                    headers=headers,
                )

                # Server error (5xx) -> retry
                if response.status_code >= 500:
                    last_error = JevAPIConnectionError(
                        f"Server error {response.status_code}: {response.text}"
                    )
                    if attempt < self.max_retries:
                        time.sleep(0.05 * (2**attempt))
                        continue
                    break

                # Client error (4xx) -> fail fast without retrying
                if response.status_code >= 400:
                    err_msg = (
                        f"API request failed with client error "
                        f"{response.status_code}: {response.text}"
                    )
                    raise JevAPIConnectionError(err_msg)

                try:
                    data = response.json()
                    if not isinstance(data, dict):
                        raise JevResponseParseError(
                            f"Expected JSON object in response, got {type(data).__name__}"
                        )
                    return data
                except (ValueError, TypeError) as json_err:
                    raise JevResponseParseError(
                        f"Failed to parse Jev API response as JSON: {json_err}"
                    ) from json_err

            except (httpx.TimeoutException, httpx.NetworkError) as conn_err:
                last_error = JevAPIConnectionError(f"Network error during Jev API call: {conn_err}")
                if attempt < self.max_retries:
                    time.sleep(0.05 * (2**attempt))
                    continue
                break

        raise last_error or JevAPIConnectionError("Jev API call failed after retries.")

    def extract_slots(
        self,
        query: str,
        schema: SchemaMetadata | None = None,
    ) -> ParsedSlots:
        """Extract and validate query slots end-to-end via Jev System 1 API.

        Args:
            query: User's natural language input query.
            schema: Optional semantic schema metadata for strict validation.

        Returns:
            Validated ParsedSlots instance.
        """
        payload = build_jev_payload(query, schema=schema)
        raw_response = self.call_api(payload)
        return parse_jev_response(query, raw_response, schema=schema)
