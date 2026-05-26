import os
from typing import Literal, Type, TypeVar

import instructor
from google import genai
from markdownify import markdownify as md
from pydantic import BaseModel, Field, ValidationError, field_validator

from src.logger import setup_logger

logger = setup_logger(__name__)

T = TypeVar("T", bound=BaseModel)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


class TravelBookingExtraction(BaseModel):
    """Validated schema for travel booking price and availability extraction."""

    total_price: float = Field(
        ...,
        description=(
            "Absolute bottom-line price inclusive of all mandatory taxes, "
            "resort fees, and service charges."
        ),
    )
    currency: str = Field(
        ...,
        description="Three-letter ISO 4217 currency code (e.g. USD, EUR, GBP).",
    )
    is_exact_match: bool = Field(
        ...,
        description=(
            "True only when the page displays rates for the exact room or ticket "
            "class specified in the extraction context."
        ),
    )
    inventory_status: Literal["available", "sold_out", "limited"] = Field(
        ...,
        description=(
            "Current availability: 'available' (bookable, no scarcity warning), "
            "'limited' (last few units / high-demand language), or 'sold_out'."
        ),
    )

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError(
                f"currency must be a 3-letter ISO 4217 code, got: {v!r}"
            )
        return v

    @field_validator("total_price")
    @classmethod
    def _validate_price(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"total_price cannot be negative: {v}")
        return round(v, 2)


_TRAVEL_EXTRACTION_PROMPT = """\
You are a specialist travel booking data extractor.
Extract pricing and availability from the booking page content below.

TARGET CLASS CONTEXT:
Room or ticket class to match: "{room_or_ticket_class}"

EXTRACTION RULES:

1. total_price (float)
   - The TOTAL charge a guest pays at checkout, inclusive of ALL mandatory taxes,
     resort fees, cleaning fees, and booking surcharges.
   - Do NOT return the nightly rate, base rate, or any pre-tax subtotal.
   - If multiple totals are displayed, use the final checkout total.
   - If no clear price is found, return 0.0. Never hallucinate a price.

2. currency (string)
   - Three-letter ISO 4217 code. Infer from symbols when the code is not explicit:
       $ → USD | € → EUR | £ → GBP | ¥ → JPY | A$ → AUD | AED stays AED
   - If currency cannot be determined with confidence, return "USD".

3. is_exact_match (boolean)
   - Return true ONLY if the page is displaying the rate for EXACTLY the class
     specified in TARGET CLASS CONTEXT above.
   - Minor wording variation is acceptable (e.g. "Deluxe King" ≈ "King Deluxe Room").
   - Return false if the page shows a different room category, an upgrade,
     a substitute, or if TARGET CLASS CONTEXT is "not specified".

4. inventory_status (string — MUST be one of exactly three values)
   - "available"  → Room/ticket is bookable; no scarcity signals present.
   - "limited"    → Page contains scarcity language such as: "only X left",
                    "last room", "high demand", "selling fast", "X rooms remaining",
                    "hurry", "just booked", "almost gone".
   - "sold_out"   → No units available; booking is not possible; waitlist only.

OUTPUT FORMAT:
Return a single JSON object with EXACTLY these four keys and no others:
  total_price, currency, is_exact_match, inventory_status

Do NOT return a JSON array. Do NOT add extra fields.

PAGE CONTENT:
{markdown_content}"""


def _html_to_markdown(html: str) -> str:
    """Convert raw HTML to Markdown to drastically reduce token usage."""
    markdown = md(html, strip=["img", "script", "style", "nav", "footer", "header"])
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    return "\n".join(lines)


def _validate_extractions(
    results: list[TravelBookingExtraction],
) -> list[TravelBookingExtraction]:
    """Re-validate each extraction result against TravelBookingExtraction.

    Defense-in-depth guard: instructor validates at parse time, but field
    coercions or model version drift can silently produce structurally broken
    objects.  This layer catches any such cases before they enter the pipeline.
    """
    valid: list[TravelBookingExtraction] = []
    for item in results:
        try:
            validated = TravelBookingExtraction.model_validate(
                item.model_dump(), strict=False
            )
            valid.append(validated)
        except ValidationError as exc:
            logger.error(
                "Post-LLM validation rejected extraction result %s | errors: %s",
                item,
                exc.errors(),
            )
    if len(valid) < len(results):
        logger.warning(
            "_validate_extractions: %d of %d result(s) passed validation.",
            len(valid),
            len(results),
        )
    return valid


def extract_with_llm(
    html_content: str,
    room_or_ticket_class: str = "",
    schema: Type[T] = TravelBookingExtraction,
    model: str = "gemini-2.0-flash",
) -> list[T]:
    """Extract travel booking data from HTML using Gemini Flash with instructor.

    1. Converts HTML → Markdown (saves ~80% tokens).
    2. Injects room_or_ticket_class context so the model can answer is_exact_match.
    3. Sends to Gemini via instructor for a single structured Pydantic object.
    4. Runs _validate_extractions as a post-parse safety gate before returning.
    """
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not set. Cannot use LLM extraction.")
        return []

    markdown_content = _html_to_markdown(html_content)
    logger.info(
        "LLM extraction: HTML=%d chars → Markdown=%d chars (room_class=%r)",
        len(html_content),
        len(markdown_content),
        room_or_ticket_class,
    )

    max_chars = 60_000
    if len(markdown_content) > max_chars:
        markdown_content = markdown_content[:max_chars]
        logger.warning("Markdown truncated to %d chars for LLM input.", max_chars)

    prompt = _TRAVEL_EXTRACTION_PROMPT.format(
        room_or_ticket_class=room_or_ticket_class.strip() or "not specified",
        markdown_content=markdown_content,
    )

    client = genai.Client(api_key=GEMINI_API_KEY)
    instructor_client = instructor.from_genai(client, mode=instructor.Mode.GENAI_JSON)

    try:
        result: T = instructor_client.chat.completions.create(
            model=model,
            response_model=schema,
            messages=[{"role": "user", "content": prompt}],
        )
        validated = _validate_extractions([result])
        logger.info(
            "LLM extraction complete: %d item(s) passed validation.", len(validated)
        )
        return validated
    except Exception as exc:
        logger.error("LLM extraction failed: %s", exc)
        return []
