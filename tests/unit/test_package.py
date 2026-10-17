"""Unit tests for the root package initialization and version metadata."""

import deterministic_text_to_sql


def test_package_import() -> None:
    """deterministic_text_to_sql パッケージが正常にインポートできることを確認する。"""
    assert deterministic_text_to_sql is not None


def test_package_version() -> None:
    """__version__ が 0.1.0 であることを確認する。"""
    assert deterministic_text_to_sql.__version__ == "0.1.0"
