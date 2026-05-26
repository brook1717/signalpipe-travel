import os
from typing import Type, TypeVar

import instructor
from google import genai
from markdownify import markdownify as md
from pydantic import BaseModel, Field

from src.logger import setup_logger

logger = setup_logger(__name__)

T = TypeVar("T", bound=BaseModel)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


class ExtractedData(BaseModel):
    """Default schema for structured data extraction from web pages."""
    title: str = Field(default="", description="Title or heading of the item")
    price: str = Field(default="", description="Price of the item")
    description: str = Field(default="", description="Description or summary text")
    category: str = Field(default="", description="Category or tag")
    url: str = Field(default="", description="Source URL or link")


def _html_to_markdown(html: str) -> str:
    """Convert raw HTML to Markdown to drastically reduce token usage."""
    markdown = md(html, strip=["img", "script", "style", "nav", "footer", "header"])
    # Collapse excessive whitespace
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    return "\n".join(lines)


def extract_with_llm(
    html_content: str,
    schema: Type[T] = ExtractedData,
    model: str = "gemini-2.0-flash",
) -> list[T]:
    """Extract structured data from HTML using an LLM with instructor.

    1. Converts HTML → Markdown (saves ~80% tokens).
    2. Sends to Gemini via the instructor library for reliable Pydantic output.
    """
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not set. Cannot use LLM extraction.")
        return []

    markdown_content = _html_to_markdown(html_content)
    logger.info(
        "LLM extraction: HTML=%d chars → Markdown=%d chars",
        len(html_content), len(markdown_content),
    )

    # Truncate to avoid hitting token limits
    max_chars = 60_000
    if len(markdown_content) > max_chars:
        markdown_content = markdown_content[:max_chars]
        logger.warning("Markdown truncated to %d chars for LLM input.", max_chars)

    client = genai.Client(api_key=GEMINI_API_KEY)
    instructor_client = instructor.from_genai(client, mode=instructor.Mode.GENAI_JSON)

    try:
        result = instructor_client.chat.completions.create(
            model=model,
            response_model=list[schema],
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Extract all structured data items from the following "
                        "web page content. Return a JSON list matching the schema.\n\n"
                        f"{markdown_content}"
                    ),
                }
            ],
        )
        logger.info("LLM extraction complete: %d items extracted.", len(result))
        return result
    except Exception as exc:
        logger.error("LLM extraction failed: %s", exc)
        return []
