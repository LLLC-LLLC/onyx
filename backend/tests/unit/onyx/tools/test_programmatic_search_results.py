# file-under-test: backend/onyx/tools/tool_implementations/utils.py
from __future__ import annotations

import pytest

from onyx.configs.constants import DocumentSource
from onyx.context.search.models import InferenceChunk
from onyx.context.search.models import InferenceSection
from onyx.tools.tool_implementations.utils import (
    convert_inference_sections_to_programmatic_search_response,
)


def _chunk(document_id: str, chunk_id: int) -> InferenceChunk:
    return InferenceChunk(
        document_id=document_id,
        chunk_id=chunk_id,
        content="display body",
        source_type=DocumentSource.MOCK_CONNECTOR,
        semantic_identifier="Duplicate title",
        title="Duplicate title",
        boost=1,
        score=0.5,
        hidden=False,
        metadata={},
        match_highlights=[],
        doc_summary="",
        chunk_context="",
        updated_at=None,
        image_file_id=None,
        source_links={0: "https://example.test/duplicate"},
        section_continuation=False,
        blurb="display blurb",
    )


def _section(
    center_chunk: InferenceChunk, chunks: list[InferenceChunk]
) -> InferenceSection:
    return InferenceSection(
        center_chunk=center_chunk,
        chunks=chunks,
        combined_content="display body",
    )


def test_programmatic_results_preserve_source_identities_for_ambiguous_sections() -> (
    None
):
    first_section = _section(
        _chunk("document-a", 8),
        [_chunk("document-a", 7), _chunk("document-a", 8)],
    )
    second_section = _section(
        _chunk("document-a", 21),
        [_chunk("document-a", 21), _chunk("document-a", 22)],
    )
    third_section = _section(
        _chunk("document-b", 3),
        [_chunk("document-b", 3), _chunk("document-b", 4)],
    )

    conversion = convert_inference_sections_to_programmatic_search_response(
        [first_section, second_section, third_section],
        citation_start=41,
        include_link=True,
    )

    results = conversion.programmatic_search_results
    assert [result.citation_id for result in results] == [41, 41, 42]
    assert [result.document_id for result in results] == [
        "document-a",
        "document-a",
        "document-b",
    ]
    assert [result.section_start_chunk_id for result in results] == [7, 21, 3]
    assert [result.section_end_chunk_id for result in results] == [8, 22, 4]
    assert {
        (result.title, result.content, result.link, result.source_type)
        for result in results
    } == {
        (
            "Duplicate title",
            "display body",
            "https://example.test/duplicate",
            "mock_connector",
        )
    }


def test_programmatic_results_reject_a_section_without_chunk_identity() -> None:
    center_chunk = _chunk("document-a", 8)
    section = _section(center_chunk, [])

    with pytest.raises(ValueError, match="at least one chunk"):
        convert_inference_sections_to_programmatic_search_response([section])


def test_programmatic_results_reject_a_section_without_document_identity() -> None:
    center_chunk = _chunk("", 8)
    section = _section(center_chunk, [center_chunk])

    with pytest.raises(ValueError, match="share a document identity"):
        convert_inference_sections_to_programmatic_search_response([section])
