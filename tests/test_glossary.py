from pathlib import Path

from planner.glossary import load_glossary

GLOSSARY_DIR = Path(__file__).parent.parent / "glossary"


def test_load_glossary_concatenates_both_files():
    text = load_glossary(GLOSSARY_DIR)
    assert "region_name" in text  # from terms.md
    assert "count_distinct(order_id)" in text  # from metrics_catalog.md


def test_load_glossary_is_deterministically_ordered():
    # metrics_catalog.md sorts before terms.md alphabetically -- pin this down
    # explicitly so a future file addition can't silently make prompt content
    # order-dependent on filesystem iteration order.
    text = load_glossary(GLOSSARY_DIR)
    assert text.index("Standard Metric Definitions") < text.index("Business Terminology")


def test_load_glossary_empty_dir_returns_empty_string(tmp_path):
    assert load_glossary(tmp_path) == ""
