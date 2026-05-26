"""Apify Actor wrapper for the Multi-Source Data Scraper.

Reads input configuration, routes fetching through BrowserFetcher,
processes results, and pushes cleaned data to the Apify dataset.
"""

import asyncio

from apify import Actor

from src.fetcher import BrowserFetcher
from src.processor import DataProcessor


async def main():
    async with Actor:
        Actor.log.info("Starting Multi-Source Data Scraper Actor.")

        # Read input configuration
        actor_input = await Actor.get_input() or {}
        urls = actor_input.get("urls", [])
        search_term = actor_input.get("search", None)
        use_proxy = actor_input.get("use_proxy", False)
        proxy_config = actor_input.get("proxy_config", None)

        if not urls:
            Actor.log.warning("No URLs provided in input. Exiting.")
            return

        Actor.log.info("Processing %d URLs (search=%s, proxy=%s).", len(urls), search_term, use_proxy)

        # Resolve proxy
        proxy = None
        if use_proxy and proxy_config:
            proxy = proxy_config.get("proxy")
        elif use_proxy:
            proxy_configuration = await Actor.create_proxy_configuration()
            proxy_url = await proxy_configuration.new_url()
            proxy = proxy_url

        processor = DataProcessor()

        for url in urls:
            Actor.log.info("Fetching: %s", url)

            try:
                fetcher = BrowserFetcher(proxy=proxy)
                html = fetcher.fetch_html(url)
                Actor.log.info("Fetched %d chars from %s.", len(html), url)

                # Two-stage extraction
                records = processor.extract(html)

                if records:
                    # Clean via DataFrame pipeline
                    processor.load_data(records)
                    processor.clean_data()
                    processor.deduplicate()
                    cleaned = processor.df.to_dict(orient="records")

                    await Actor.push_data(cleaned)
                    Actor.log.info("Pushed %d records for %s.", len(cleaned), url)
                else:
                    Actor.log.warning("No data extracted from %s.", url)

            except Exception as exc:
                Actor.log.error("Failed to process %s: %s", url, exc)
                continue

        Actor.log.info("Actor run complete.")


if __name__ == "__main__":
    asyncio.run(main())
