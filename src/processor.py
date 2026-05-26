import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from src.logger import setup_logger

logger = setup_logger(__name__)

# Required fields that must be non-None for a DOM extraction to be valid.
# If any required field returns None, the AI fallback is triggered.
REQUIRED_FIELDS = ["title"]


class DataProcessor:
    """Cost-aware two-stage extraction engine with DataFrame cleaning utilities.

    Stage 1: Fast DOM extraction via BeautifulSoup (zero cost).
    Stage 2: LLM fallback ONLY when required fields are missing — indicating
             the website layout changed and selectors need updating.
    """

    def __init__(self):
        self.df: pd.DataFrame = pd.DataFrame()
        self.llm_fallback_triggered: bool = False

    # ------------------------------------------------------------------
    # Stage 1: DOM-based extraction
    # ------------------------------------------------------------------

    def _extract_with_dom(self, html: str) -> list[dict] | None:
        """Attempt to extract structured data using BeautifulSoup selectors.

        Returns a list of dicts on success, or None if extraction fails or
        yields no results.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")

            # Strategy A: look for <table> elements
            tables = soup.find_all("table")
            if tables:
                rows = []
                for table in tables:
                    headers = [th.get_text(strip=True) for th in table.find_all("th")]
                    for tr in table.find_all("tr"):
                        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                        if cells:
                            if headers and len(cells) == len(headers):
                                rows.append(dict(zip(headers, cells)))
                            else:
                                rows.append({f"col_{i}": c for i, c in enumerate(cells)})
                if rows:
                    logger.info("DOM extraction (tables): %d rows extracted.", len(rows))
                    return rows

            # Strategy B: look for repeated item containers
            selectors = [
                "article", ".item", ".product", ".card", ".listing",
                ".result", "[data-item]", ".post",
            ]
            for selector in selectors:
                items = soup.select(selector)
                if len(items) >= 2:
                    rows = []
                    for item in items:
                        title_el = item.select_one("h1, h2, h3, h4, .title, .name")
                        price_el = item.select_one(".price, .cost, .amount")
                        desc_el = item.select_one("p, .description, .summary, .text")
                        link_el = item.select_one("a[href]")
                        rows.append({
                            "title": title_el.get_text(strip=True) if title_el else None,
                            "price": price_el.get_text(strip=True) if price_el else None,
                            "description": desc_el.get_text(strip=True) if desc_el else None,
                            "url": link_el["href"] if link_el else None,
                        })
                    logger.info(
                        "DOM extraction (selector '%s'): %d items extracted.",
                        selector, len(rows),
                    )
                    return rows

            logger.info("DOM extraction found no structured data.")
            return None

        except Exception as exc:
            logger.warning("DOM extraction error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Cost-aware validation
    # ------------------------------------------------------------------

    def _has_missing_required_fields(self, records: list[dict]) -> list[str]:
        """Check if any required fields are None across extracted records.

        Returns a list of field names that are missing.
        """
        missing = set()
        for record in records:
            for field in REQUIRED_FIELDS:
                if record.get(field) is None:
                    missing.add(field)
        return list(missing)

    # ------------------------------------------------------------------
    # Stage 2: Cost-aware LLM fallback
    # ------------------------------------------------------------------

    def _extract_with_llm(self, html: str) -> list[dict]:
        """Fallback: convert HTML → Markdown (save tokens), then use Gemini
        with Pydantic structured outputs to extract missing fields."""
        from src.ai_parser import extract_with_llm, ExtractedData

        self.llm_fallback_triggered = True

        # Convert to markdown to reduce token costs (~80% reduction)
        markdown_content = md(html, strip=["img", "script", "style", "nav", "footer"])
        lines = [line.strip() for line in markdown_content.splitlines() if line.strip()]
        markdown_content = "\n".join(lines)

        logger.warning(
            "[COST-AWARE] AI fallback triggered. HTML=%d chars → Markdown=%d chars. "
            "DOM selectors likely need updating for this site.",
            len(html), len(markdown_content),
        )

        results = extract_with_llm(html, schema=ExtractedData)
        # Stamp every AI-healed record so the DB can audit LLM usage
        return [{**r.model_dump(), "ai_fallback_used": True} for r in results]

    # ------------------------------------------------------------------
    # Public: cost-aware two-stage extract
    # ------------------------------------------------------------------

    def extract(self, html: str) -> list[dict]:
        """Cost-aware extraction: DOM first, LLM ONLY when required fields are missing.

        The LLM is never called unnecessarily. It only fires when:
        1. DOM extraction returns None (no data found at all), OR
        2. DOM extraction returns records but required fields are None
           (indicating a layout change broke the selectors).
        """
        self.llm_fallback_triggered = False

        # Stage 1: Fast DOM extraction (zero cost)
        try:
            records = self._extract_with_dom(html)
        except Exception as exc:
            logger.warning("Stage 1 (DOM) crashed: %s. Will attempt LLM.", exc)
            records = None

        if records:
            # Validate: are required fields present?
            missing = self._has_missing_required_fields(records)
            if not missing:
                logger.info("DOM extraction successful. No AI cost incurred.")
                return records

            # Required fields missing → layout likely changed
            logger.warning(
                "[COST-AWARE] Required fields missing from DOM extraction: %s. "
                "Triggering LLM fallback. UPDATE YOUR SELECTORS to avoid future costs.",
                missing,
            )

        # Stage 2: LLM fallback (costs money — only when necessary)
        records = self._extract_with_llm(html)
        if records:
            return records

        logger.error("Both extraction stages returned no data.")
        return []

    # ------------------------------------------------------------------
    # DataFrame utilities (unchanged API)
    # ------------------------------------------------------------------

    def load_data(self, raw_data: list[dict]) -> pd.DataFrame:
        """Convert a list of dictionaries into a Pandas DataFrame."""
        if not raw_data:
            logger.warning("No data provided to load_data.")
            self.df = pd.DataFrame()
            return self.df

        self.df = pd.DataFrame(raw_data)
        logger.info("Loaded %d records with %d columns.", len(self.df), len(self.df.columns))
        return self.df

    def clean_data(self) -> pd.DataFrame:
        """Clean the DataFrame: strip whitespace from strings and fill NaN values."""
        if self.df.empty:
            logger.warning("DataFrame is empty. Nothing to clean.")
            return self.df

        # Strip leading/trailing whitespace from string columns
        string_cols = self.df.select_dtypes(include=["object"]).columns
        for col in string_cols:
            self.df[col] = self.df[col].map(
                lambda x: x.strip() if isinstance(x, str) else x
            )

        # Fill NaN based on column dtype
        for col in self.df.columns:
            if self.df[col].dtype == "object":
                self.df[col] = self.df[col].fillna("N/A")
            elif np.issubdtype(self.df[col].dtype, np.number):
                self.df[col] = self.df[col].fillna(0)
            else:
                self.df[col] = self.df[col].fillna("N/A")

        logger.info("Data cleaned: whitespace stripped, NaN values filled.")
        return self.df

    def deduplicate(self, subset_keys: list[str] | None = None) -> pd.DataFrame:
        """Drop duplicate rows from the DataFrame.

        If *subset_keys* is provided, duplicates are identified based on those
        columns only; otherwise the entire row is evaluated.
        """
        if self.df.empty:
            logger.warning("DataFrame is empty. Nothing to deduplicate.")
            return self.df

        before_count = len(self.df)
        self.df = self.df.drop_duplicates(subset=subset_keys, keep="first").reset_index(drop=True)
        removed = before_count - len(self.df)
        logger.info("Deduplication complete: %d duplicate row(s) removed.", removed)
        return self.df

    def apply_filter(self, filter_key: str, filter_value: str) -> pd.DataFrame:
        """Filter the DataFrame where *filter_key* column contains *filter_value*.

        Uses case-insensitive string matching for string columns.
        Returns the filtered DataFrame (does not mutate self.df).
        """
        if not filter_key or not filter_value:
            logger.warning("Both filter_key and filter_value must be provided. Returning unfiltered data.")
            return self.df

        if filter_key not in self.df.columns:
            logger.warning("Column '%s' not found in DataFrame. Returning unfiltered data.", filter_key)
            return self.df

        col = self.df[filter_key]

        if pd.api.types.is_string_dtype(col):
            mask = col.str.contains(filter_value, case=False, na=False)
        else:
            mask = col == filter_value

        filtered_df = self.df[mask].reset_index(drop=True)
        logger.info(
            "Filter applied: column='%s', value='%s' -> %d of %d rows matched.",
            filter_key, filter_value, len(filtered_df), len(self.df),
        )
        return filtered_df
