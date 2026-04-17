"""LLM provider abstractions — Gemini and OpenAI-compatible (e.g. DeepSeek)."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from google import genai
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class GeminiProvider:
    """Thin abstraction over Google GenAI SDK.

    LLM calls go through the native SDK (proxied via HTTPS_PROXY env var).
    """

    def __init__(self, api_key: str, vertexai_api_key: str = "", **kwargs):
        if vertexai_api_key:
            self.client = genai.Client(vertexai=True, api_key=vertexai_api_key)
        else:
            self.client = genai.Client(api_key=api_key)

    async def generate_json(
        self,
        model: str,
        system_prompt: str,
        user_parts: List[Dict[str, Any]],
        response_schema: Type[BaseModel],
        temperature: float = 0.7,
    ) -> Dict[str, Any]:
        contents = self._build_contents(user_parts)
        response = await self.client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=response_schema,
                temperature=temperature,
            ),
        )
        if response.text:
            result = json.loads(response.text)
            validated = response_schema(**result)
            return validated.model_dump()
        raise ValueError("Empty response from Gemini")

    async def generate_text(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.7,
    ) -> str:
        response = await self.client.aio.models.generate_content(
            model=model,
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
            ),
        )
        if response.text:
            return response.text.strip()
        raise ValueError("Empty response from Gemini")

    @staticmethod
    def _build_contents(parts: List[Dict[str, Any]]) -> list:
        content_parts = []
        for part in parts:
            if part["type"] == "text":
                content_parts.append(types.Part(text=part["data"]))
            elif part["type"] == "image":
                image_data = part["data"]
                if isinstance(image_data, str):
                    content_parts.append(types.Part.from_uri(
                        file_uri=image_data,
                        mime_type=part.get("mime_type", "image/png"),
                    ))
                else:
                    content_parts.append(types.Part.from_bytes(
                        data=image_data,
                        mime_type=part.get("mime_type", "image/png"),
                    ))
            elif part["type"] == "image_file":
                path = Path(part["data"])
                if path.exists():
                    content_parts.append(types.Part.from_bytes(
                        data=path.read_bytes(),
                        mime_type=part.get("mime_type", "image/png"),
                    ))
        return [types.Content(role="user", parts=content_parts)]


class OpenAICompatibleProvider:
    """Provider for any OpenAI-compatible API (DeepSeek, etc.).

    Uses JSON mode + Pydantic validation to produce structured output.
    Proxy is picked up automatically from HTTPS_PROXY env var via httpx.
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    async def generate_json(
        self,
        system_prompt: str,
        user_message: str,
        response_schema: Type[BaseModel],
        temperature: float = 0.7,
    ) -> Dict[str, Any]:
        """Generate structured JSON using OpenAI-compatible JSON mode."""
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            temperature=temperature,
        )
        raw = response.choices[0].message.content or ""
        result = json.loads(raw)
        validated = response_schema(**result)
        return validated.model_dump()
