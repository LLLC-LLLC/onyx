# file-under-test: backend/onyx/tools/tool_implementations/search/search_tool.py
from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from onyx.configs.constants import DocumentSource
from onyx.context.search.models import InferenceChunk
from onyx.context.search.models import InferenceSection
from onyx.context.search.models import PersonaSearchInfo
from onyx.context.search.models import SearchDocsResponse
from onyx.server.features.search.api import search_response_from_tool_response
from onyx.server.query_and_chat.placement import Placement
from onyx.tools.models import SearchToolOverrideKwargs
from onyx.tools.tool_implementations.search.search_tool import SearchTool

MODULE = "onyx.tools.tool_implementations.search.search_tool"


def _chunk(document_id: str, chunk_id: int) -> InferenceChunk:
    return InferenceChunk(
        document_id=document_id,
        chunk_id=chunk_id,
        content="Duplicate content",
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
        blurb="Duplicate blurb",
    )


def _section(
    center_chunk: InferenceChunk, chunks: list[InferenceChunk]
) -> InferenceSection:
    return InferenceSection(
        center_chunk=center_chunk,
        chunks=chunks,
        combined_content="\n".join(chunk.content for chunk in chunks),
    )


def _tool() -> SearchTool:
    return SearchTool(
        tool_id=1,
        emitter=MagicMock(),
        user=MagicMock(is_anonymous=False),
        persona_search_info=PersonaSearchInfo(
            document_set_names=[],
            search_start_date=None,
            attached_document_ids=[],
            hierarchy_node_ids=[],
        ),
        llm=MagicMock(),
        document_index=MagicMock(),
        user_selected_filters=None,
        project_id_filter=None,
        enable_slack_search=False,
    )


def test_programmatic_results_use_final_merged_sections() -> None:
    a7 = _chunk("document-a", 7)
    a8 = _chunk("document-a", 8)
    a9 = _chunk("document-a", 9)
    a10 = _chunk("document-a", 10)
    b3 = _chunk("document-b", 3)
    b4 = _chunk("document-b", 4)
    b5 = _chunk("document-b", 5)
    b6 = _chunk("document-b", 6)
    selected_sections = [
        _section(a8, [a7, a8]),
        _section(a9, [a9, a10]),
        _section(b4, [b3, b4, b5, b6]),
    ]
    tool = _tool()

    with (
        patch(f"{MODULE}.get_session_with_current_tenant") as mock_session_ctx,
        patch(f"{MODULE}.build_access_filters_for_user", return_value=[]),
        patch(f"{MODULE}.get_current_search_settings", return_value=MagicMock()),
        patch(f"{MODULE}.EmbeddingModel"),
        patch(f"{MODULE}.get_federated_retrieval_functions", return_value=[]),
        patch(f"{MODULE}.fetch_unique_document_sources", return_value=[]),
        patch(f"{MODULE}.decide_search_scope", return_value=None),
        patch(f"{MODULE}.search_pipeline", return_value=[]),
        patch(f"{MODULE}.weighted_reciprocal_rank_fusion", return_value=[]),
        patch(f"{MODULE}.merge_individual_chunks", return_value=selected_sections),
        patch(f"{MODULE}.populate_file_ids_on_sections"),
        patch(f"{MODULE}.get_llm_token_counter", return_value=lambda _: 1),
        patch(f"{MODULE}._trim_sections_by_tokens", return_value=selected_sections),
        patch(
            f"{MODULE}.select_sections_for_expansion",
            return_value=(selected_sections, ["document-a", "document-b"]),
        ),
        patch(
            f"{MODULE}.expand_section_with_context",
            side_effect=lambda **kwargs: kwargs["section"],
        ),
    ):
        mock_session_ctx.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_session_ctx.return_value.__exit__ = MagicMock(return_value=False)
        response = tool.run(
            placement=Placement(turn_index=0, tab_index=0),
            override_kwargs=SearchToolOverrideKwargs(
                starting_citation_num=41,
                original_query="stable identity",
                skip_query_expansion=True,
                include_link=True,
            ),
            queries=["stable identity"],
        )

    assert isinstance(response.rich_response, SearchDocsResponse)
    typed_results = response.rich_response.programmatic_search_results
    assert typed_results is not None
    assert [
        (
            result.citation_id,
            result.document_id,
            result.section_start_chunk_id,
            result.section_end_chunk_id,
        )
        for result in typed_results
    ] == [
        (41, "document-a", 7, 10),
        (42, "document-b", 3, 6),
    ]
    duplicate_content = "\n".join(["Duplicate content"] * 4)
    assert {
        (result.title, result.content, result.link, result.source_type)
        for result in typed_results
    } == {
        (
            "Duplicate title",
            duplicate_content,
            "https://example.test/duplicate",
            "mock_connector",
        )
    }

    response.llm_facing_response = "not valid JSON"
    http_response = search_response_from_tool_response(response)
    assert [
        (
            result.citation_id,
            result.document_id,
            result.section_start_chunk_id,
            result.section_end_chunk_id,
        )
        for result in http_response.results
    ] == [
        (41, "document-a", 7, 10),
        (42, "document-b", 3, 6),
    ]
