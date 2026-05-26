import os

import pandas as pd

from src.logger import setup_logger

logger = setup_logger(__name__)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")


class DataExporter:
    """Exports a DataFrame to CSV or JSON files."""

    def __init__(self, df: pd.DataFrame):
        self.df = df
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    def export_to_csv(self, filepath: str) -> str:
        """Export the DataFrame to a CSV file without the index."""
        full_path = os.path.join(OUTPUT_DIR, filepath)
        self.df.to_csv(full_path, index=False)
        logger.info("Data exported to CSV: %s (%d rows)", full_path, len(self.df))
        return full_path

    def export_to_json(self, filepath: str) -> str:
        """Export the DataFrame to a JSON file as a list of records."""
        full_path = os.path.join(OUTPUT_DIR, filepath)
        self.df.to_json(full_path, orient="records", indent=2, force_ascii=False)
        logger.info("Data exported to JSON: %s (%d rows)", full_path, len(self.df))
        return full_path
