import pytest
import pandas as pd

from src.processor import DataProcessor


@pytest.fixture
def sample_data():
    """A sample list of dicts simulating raw API data."""
    return [
        {"name": "  Alice  ", "city": "New York", "age": 30},
        {"name": "Bob", "city": "  Los Angeles  ", "age": 25},
        {"name": "Charlie", "city": "Chicago", "age": None},
        {"name": "  Alice  ", "city": "New York", "age": 30},
        {"name": "Diana", "city": "New York", "age": 28},
        {"name": "Eve", "city": None, "age": 35},
    ]


@pytest.fixture
def processor(sample_data):
    """Return a DataProcessor loaded with sample data."""
    proc = DataProcessor()
    proc.load_data(sample_data)
    return proc


class TestLoadData:
    def test_load_data_creates_dataframe(self, sample_data):
        proc = DataProcessor()
        df = proc.load_data(sample_data)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 6
        assert list(df.columns) == ["name", "city", "age"]

    def test_load_data_empty_list(self):
        proc = DataProcessor()
        df = proc.load_data([])
        assert df.empty


class TestCleanData:
    def test_strips_whitespace(self, processor):
        processor.clean_data()
        assert processor.df["name"].iloc[0] == "Alice"
        assert processor.df["city"].iloc[1] == "Los Angeles"

    def test_fills_nan_strings_with_na(self, processor):
        processor.clean_data()
        assert processor.df["city"].iloc[5] == "N/A"

    def test_fills_nan_numbers_with_zero(self, processor):
        processor.clean_data()
        assert processor.df["age"].iloc[2] == 0

    def test_clean_empty_dataframe(self):
        proc = DataProcessor()
        proc.load_data([])
        df = proc.clean_data()
        assert df.empty


class TestDeduplicate:
    def test_removes_full_row_duplicates(self, processor):
        processor.clean_data()
        df = processor.deduplicate()
        # The duplicate "Alice / New York / 30" row should be removed
        assert len(df) == 5

    def test_deduplicate_with_subset_keys(self, processor):
        processor.clean_data()
        # Deduplicate on "city" only — "New York" appears for Alice and Diana
        df = processor.deduplicate(subset_keys=["city"])
        cities = df["city"].tolist()
        assert cities.count("New York") == 1

    def test_deduplicate_no_duplicates(self):
        proc = DataProcessor()
        proc.load_data([
            {"a": 1, "b": 2},
            {"a": 3, "b": 4},
        ])
        df = proc.deduplicate()
        assert len(df) == 2

    def test_deduplicate_empty_dataframe(self):
        proc = DataProcessor()
        proc.load_data([])
        df = proc.deduplicate()
        assert df.empty


class TestApplyFilter:
    def test_filter_string_case_insensitive(self, processor):
        processor.clean_data()
        df = processor.apply_filter("city", "new york")
        assert len(df) >= 1
        assert all("New York" in val or "new york" in val.lower() for val in df["city"])

    def test_filter_partial_match(self, processor):
        processor.clean_data()
        df = processor.apply_filter("name", "ali")
        assert len(df) >= 1
        assert all("Ali" in val or "ali" in val.lower() for val in df["name"])

    def test_filter_no_match(self, processor):
        processor.clean_data()
        df = processor.apply_filter("city", "Nonexistent")
        assert len(df) == 0

    def test_filter_missing_column(self, processor):
        processor.clean_data()
        df = processor.apply_filter("nonexistent_col", "value")
        # Returns unfiltered data when column doesn't exist
        assert len(df) == len(processor.df)

    def test_filter_empty_key(self, processor):
        processor.clean_data()
        df = processor.apply_filter("", "value")
        assert len(df) == len(processor.df)

    def test_filter_empty_value(self, processor):
        processor.clean_data()
        df = processor.apply_filter("city", "")
        assert len(df) == len(processor.df)
