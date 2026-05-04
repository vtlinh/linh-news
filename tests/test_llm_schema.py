from __future__ import annotations

from app.llm_schema import EDITION_SCHEMA, SECTION_REROLL_SCHEMA


def test_edition_schema_top_level_shape():
    assert EDITION_SCHEMA["type"] == "object"
    assert set(EDITION_SCHEMA["required"]) == {"sections", "stocks"}
    sec = EDITION_SCHEMA["properties"]["sections"]
    assert sec["type"] == "array"
    # Section identity is per-user now; the schema accepts any string key.
    key_schema = sec["items"]["properties"]["key"]
    assert key_schema["type"] == "string"
    assert "enum" not in key_schema


def test_subsection_required_fields():
    item_schema = EDITION_SCHEMA["properties"]["sections"]["items"]["properties"]["subsections"][
        "items"
    ]
    assert set(item_schema["required"]) == {"title", "text", "sources"}
    # Images are optional; sources are required.
    assert "images" not in item_schema["required"]


def test_stock_required_fields():
    stock = EDITION_SCHEMA["properties"]["stocks"]["items"]
    assert set(stock["required"]) == {"ticker", "price", "percent_diff", "why_it_moved"}
    why = stock["properties"]["why_it_moved"]
    assert set(why["required"]) == {"text", "sources"}


def test_reroll_schema_returns_subsections_only():
    assert SECTION_REROLL_SCHEMA["required"] == ["subsections"]
