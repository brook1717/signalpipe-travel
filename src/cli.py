import argparse


def parse_arguments() -> argparse.Namespace:
    """Parse and return command-line arguments for the scraper."""
    parser = argparse.ArgumentParser(
        description="Multi-Source Data Scraper and API Client",
    )

    parser.add_argument(
        "--source",
        type=str,
        required=True,
        help="URL or API endpoint to scrape/fetch",
    )
    parser.add_argument(
        "--search",
        type=str,
        default=None,
        help="Optional search term",
    )
    parser.add_argument(
        "--format",
        type=str,
        choices=["csv", "json"],
        default="csv",
        help="Output format (default: csv)",
    )
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="Output filename",
    )
    parser.add_argument(
        "--filter-key",
        type=str,
        default=None,
        help="Optional key to filter data by",
    )
    parser.add_argument(
        "--filter-value",
        type=str,
        default=None,
        help="Optional value for the filter key",
    )
    parser.add_argument(
        "--use-browser",
        action="store_true",
        default=False,
        help="Use Playwright stealth browser instead of standard requests",
    )
    parser.add_argument(
        "--proxies",
        type=str,
        default=None,
        help="Path to a text file containing proxy list (one per line)",
    )
    parser.add_argument(
        "--queue",
        action="store_true",
        default=False,
        help="Push tasks to AWS SQS queue instead of executing locally",
    )
    parser.add_argument(
        "--webhook",
        type=str,
        default=None,
        help="Webhook URL to deliver cleaned data to (e.g. Zapier, Make.com, custom API)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=50,
        metavar="N",
        help=(
            "Safety ceiling: maximum number of pages to fetch per domain "
            "(default: 50). If the site returns data at page N the fetcher "
            "stops, logs a [SAFETY CEILING] warning, and saves collected data. "
            "Prevents infinite pagination from draining proxy bandwidth or "
            "generating unexpected cloud costs."
        ),
    )

    return parser.parse_args()
