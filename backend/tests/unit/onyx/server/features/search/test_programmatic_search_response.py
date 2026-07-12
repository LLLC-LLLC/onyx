# file-under-test: backend/onyx/server/features/search/api.py
from __future__ import annotations

import pytest

from onyx.context.search.models import ProgrammaticSearchResult
from onyx.context.search.models import SearchDocsResponse
from onyx.server.features.search.api import search_response_from_tool_response
from onyx.tools.models import ToolResponse


def _tool_response(
    programmatic_search_results: list[ProgrammaticSearchResult] | None,
) -> ToolResponse:
    return ToolResponse(
        rich_response=SearchDocsResponse(
            search_docs=[],
            citation_mapping={},
            programmatic_search_results=programmatic_search_results,
        ),
        # The API must not parse this display-only value to recover identities.
        llm_facing_response="not valid JSON",
    )


def test_api_uses_typed_source_identities_not_llm_display_json() -> None:
    response = search_response_from_tool_response(
        _tool_response(
            [
                ProgrammaticSearchResult(
                    citation_id=1,
                    document_id="document-a",
                    section_start_chunk_id=7,
                    section_end_chunk_id=8,
                    title="Duplicate title",
                    content="Duplicate content",
                    link="https://example.test/duplicate",
                    source_type="mock",
                    updated_at=None,
                ),
                ProgrammaticSearchResult(
                    citation_id=2,
                    document_id="document-b",
                    section_start_chunk_id=3,
                    section_end_chunk_id=4,
                    title="Duplicate title",
                    content="Duplicate content",
                    link="https://example.test/duplicate",
                    source_type="mock",
                    updated_at=None,
                ),
            ]
        )
    )

    assert [result.document_id for result in response.results] == [
        "document-a",
        "document-b",
    ]
    assert [result.section_start_chunk_id for result in response.results] == [7, 3]
    assert [result.section_end_chunk_id for result in response.results] == [8, 4]


def test_api_rejects_a_search_tool_response_without_typed_identities() -> None:
    with pytest.raises(RuntimeError, match="typed programmatic search results"):
        search_response_from_tool_response(_tool_response(None))
