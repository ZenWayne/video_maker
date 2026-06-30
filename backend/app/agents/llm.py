"""LLM provider abstractions — Gemini and OpenAI-compatible (e.g. DeepSeek)."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from google import genai
from google.genai import types
from pydantic import BaseModel

from app import observability

logger = logging.getLogger(__name__)


def _summarize_parts(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Trace-safe summary of user parts — keep text, never dump raw image bytes."""
    summary: List[Dict[str, Any]] = []
    for part in parts:
        if part.get("type") == "text":
            summary.append({"type": "text", "data": part.get("data")})
        else:
            data = part.get("data")
            summary.append(
                {
                    "type": part.get("type"),
                    "data": data if isinstance(data, str) else "<bytes>",
                }
            )
    return summary


class GeminiProvider:
    """Thin abstraction over Google GenAI SDK.

    LLM calls go through the native SDK (proxied via HTTPS_PROXY env var).
    """

    def __init__(self, project: str = "", location: str = "", **kwargs):
        self.client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
        )

    async def generate_json(
        self,
        model: str,
        system_prompt: str,
        user_parts: List[Dict[str, Any]],
        response_schema: Type[BaseModel],
        temperature: float = 0.7,
        operation: Optional[str] = None,
    ) -> Dict[str, Any]:
        contents = self._build_contents(user_parts)
        with observability.generation(
            name=operation or "gemini-generate-json",
            model=model,
            input={"system": system_prompt, "parts": _summarize_parts(user_parts)},
            model_parameters={"temperature": temperature},
        ) as gen:
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
            if not response.text:
                raise ValueError("Empty response from Gemini")
            result = json.loads(response.text)
            validated = response_schema(**result)
            observability.update_span(gen, output=result)
            return validated.model_dump()

    async def generate_text(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.7,
        operation: Optional[str] = None,
        image_paths: Optional[List[str]] = None,
    ) -> str:
        # Multimodal when image_paths given: text part + each image as image_file.
        # Missing files are skipped by _build_contents (path.exists() check).
        contents = user_message
        if image_paths:
            parts: List[Dict[str, Any]] = [{"type": "text", "data": user_message}]
            parts += [{"type": "image_file", "data": p} for p in image_paths]
            contents = self._build_contents(parts)
        with observability.generation(
            name=operation or "gemini-generate-text",
            model=model,
            input={"system": system_prompt, "user": user_message, "num_images": len(image_paths or [])},
            model_parameters={"temperature": temperature},
        ) as gen:
            response = await self.client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=temperature,
                ),
            )
            if not response.text:
                raise ValueError("Empty response from Gemini")
            text = response.text.strip()
            observability.update_span(gen, output=text)
            return text

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
        operation: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate structured JSON using OpenAI-compatible JSON mode."""
        with observability.generation(
            name=operation or "openai-compatible-generate-json",
            model=self.model,
            input={"system": system_prompt, "user": user_message},
            model_parameters={"temperature": temperature},
        ) as gen:
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
            usage = getattr(response, "usage", None)
            observability.update_span(
                gen,
                output=result,
                usage_details=(
                    {
                        "input": usage.prompt_tokens,
                        "output": usage.completion_tokens,
                        "total": usage.total_tokens,
                    }
                    if usage
                    else None
                ),
            )
            return validated.model_dump()
