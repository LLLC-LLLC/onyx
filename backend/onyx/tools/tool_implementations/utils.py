import json
from dataclasses import dataclass
from typing import Any

from onyx.context.search.models import InferenceSection
from onyx.context.search.models import ProgrammaticSearchResult
from onyx.context.search.utils import sandbox_filename_for_document
from onyx.utils.logger import setup_logger

logger = setup_logger()


@dataclass(frozen=True)
class ProgrammaticSearchConversion:
    """Shared serialized and typed representations of final search sections."""

    llm_facing_response: str
    citation_mapping: dict[int, str]
    programmatic_search_results: list[ProgrammaticSearchResult]


def truncate_output(output: str, max_length: int, label: str = "output") -> str:
    """Truncate to ``max_length`` and append a footer noting how many chars were elided. ``label`` is only used in the debug log."""
    truncated = output[:max_length]
    if len(output) > max_length:
        truncated += (
            f"\n... [output truncated, {len(output) - max_length} characters omitted]"
        )
        logger.debug("Truncated %s: %s", label, truncated)
    return truncated


FILE_ASSOCIATED_GUIDANCE = (
    "Only a short excerpt from this document is shown below. The complete "
    'file is available in the sandbox as "{filename}" — prefer the Python '
    "code interpreter to read, parse, or analyze it\n\n"
    "Excerpt: {content}"
)


def _serialize_inference_sections(
    top_sections: list[InferenceSection],
    citation_start: int = 1,
    limit: int | None = None,
    include_source_type: bool = True,
    include_link: bool = False,
    include_document_id: bool = False,
) -> tuple[list[InferenceSection], list[dict[str, Any]], dict[int, str]]:
    """Render final sections once for both LLM and programmatic search paths."""
    # Apply limit if specified
    if limit is not None:
        top_sections = top_sections[:limit]

    # Group sections by document_id to assign same citation_id to sections from same document
    document_id_to_citation_id: dict[str, int] = {}
    citation_mapping: dict[int, str] = {}
    current_citation_id = citation_start

    # First pass: assign citation_ids to unique document_ids
    for section in top_sections:
        document_id = section.center_chunk.document_id
        if document_id not in document_id_to_citation_id:
            document_id_to_citation_id[document_id] = current_citation_id
            citation_mapping[current_citation_id] = document_id
            current_citation_id += 1

    # Second pass: build results with citation_ids assigned per document
    results = []

    for section in top_sections:
        chunk = section.center_chunk
        document_id = chunk.document_id
        citation_id = document_id_to_citation_id[document_id]

        # Combine primary and secondary owners for authors
        authors = None
        if chunk.primary_owners or chunk.secondary_owners:
            authors = []
            if chunk.primary_owners:
                authors.extend(chunk.primary_owners)
            if chunk.secondary_owners:
                authors.extend(chunk.secondary_owners)

        # Format updated_at as ISO string if available
        updated_at_str = None
        if chunk.updated_at:
            updated_at_str = chunk.updated_at.isoformat()

        # Build result dictionary in desired order, only including non-None/empty fields
        result = {
            "document": citation_id,
            "title": chunk.semantic_identifier,
        }
        if updated_at_str is not None:
            result["updated_at"] = updated_at_str
        if authors is not None:
            result["authors"] = authors  # ty: ignore[invalid-assignment]
        if include_source_type:
            result["source_type"] = chunk.source_type.value
        if include_link:
            # Get the first link from the center chunk's source_links dict
            link = None
            if chunk.source_links:
                # source_links is dict[int, str], get the first value
                link = next(iter(chunk.source_links.values()), None)
            if link:
                result["url"] = link
        if include_document_id:
            result["document_identifier"] = chunk.document_id
        if chunk.file_id is not None:
            filename = sandbox_filename_for_document(
                chunk.semantic_identifier, chunk.file_id
            )
            result["file_name"] = filename

            result["content"] = FILE_ASSOCIATED_GUIDANCE.format(
                filename=filename, content=chunk.content
            )
        else:
            result["content"] = section.combined_content
        if chunk.metadata:
            result["metadata"] = json.dumps(chunk.metadata, ensure_ascii=False)
        results.append(result)

    return top_sections, results, citation_mapping


def _llm_response_from_results(results: list[dict[str, Any]], note: str | None) -> str:
    payload: dict[str, object] = {"results": results}
    if note:
        payload["note"] = note
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _programmatic_result_from_section(
    section: InferenceSection, result: dict[str, Any]
) -> ProgrammaticSearchResult:
    chunks = section.chunks
    if not chunks:
        raise ValueError(
            "Programmatic search results require every section to contain at least one chunk."
        )

    document_id = section.center_chunk.document_id
    if not document_id or any(chunk.document_id != document_id for chunk in chunks):
        raise ValueError(
            "Programmatic search results require all section chunks to share a document identity."
        )

    center_chunk_id = section.center_chunk.chunk_id
    chunk_ids = [chunk.chunk_id for chunk in chunks]
    if center_chunk_id not in chunk_ids:
        raise ValueError(
            "Programmatic search results require the center chunk to belong to its section."
        )

    return ProgrammaticSearchResult(
        citation_id=result["document"],
        document_id=document_id,
        section_start_chunk_id=min(chunk_ids),
        section_end_chunk_id=max(chunk_ids),
        title=result["title"],
        content=result["content"],
        link=result.get("url"),
        source_type=result["source_type"],
        updated_at=result.get("updated_at"),
    )


def convert_inference_sections_to_programmatic_search_response(
    top_sections: list[InferenceSection],
    citation_start: int = 1,
    limit: int | None = None,
    include_source_type: bool = True,
    include_link: bool = False,
    include_document_id: bool = False,
    note: str | None = None,
) -> ProgrammaticSearchConversion:
    """Preserve merged-section identities alongside the LLM display response."""

    if not include_source_type:
        raise ValueError("Programmatic search results require a source type.")

    sections, results, citation_mapping = _serialize_inference_sections(
        top_sections=top_sections,
        citation_start=citation_start,
        limit=limit,
        include_source_type=include_source_type,
        include_link=include_link,
        include_document_id=include_document_id,
    )
    return ProgrammaticSearchConversion(
        llm_facing_response=_llm_response_from_results(results, note),
        citation_mapping=citation_mapping,
        programmatic_search_results=[
            _programmatic_result_from_section(section, result)
            for section, result in zip(sections, results, strict=True)
        ],
    )


def convert_inference_sections_to_llm_string(
    top_sections: list[InferenceSection],
    citation_start: int = 1,
    limit: int | None = None,
    include_source_type: bool = True,
    include_link: bool = False,
    include_document_id: bool = False,
    note: str | None = None,
) -> tuple[str, dict[int, str]]:
    """Convert InferenceSection objects to a JSON string for LLM.

    Returns a JSON string with document results and a citation mapping.
    """

    _, results, citation_mapping = _serialize_inference_sections(
        top_sections=top_sections,
        citation_start=citation_start,
        limit=limit,
        include_source_type=include_source_type,
        include_link=include_link,
        include_document_id=include_document_id,
    )

    return (
        _llm_response_from_results(results, note),
        citation_mapping,
    )
