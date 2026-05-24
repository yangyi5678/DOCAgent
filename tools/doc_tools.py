from __future__ import annotations

from typing import Any

from . import doc_core


def _wrap(tool: str, fn, *args, **kwargs) -> dict[str, Any]:
    try:
        return {"success": True, "tool": tool, "data": fn(*args, **kwargs), "error": None}
    except Exception as e:
        return {"success": False, "tool": tool, "data": None, "error": str(e)}


def identify_file_tool(file_path: str, base_dir: str | None = None):
    return _wrap("doc.identify_file", doc_core.core_identify_file, file_path, base_dir)


def parse_tool(file_path: str, export_dir: str | None = None, base_dir: str | None = None):
    return _wrap("doc.parse", doc_core.core_parse, file_path, export_dir, base_dir)


def extract_text_tool(file_path: str, base_dir: str | None = None):
    return _wrap("doc.extract_text", doc_core.core_extract_text, file_path, base_dir)


def extract_tables_tool(file_path: str, base_dir: str | None = None):
    return _wrap("doc.extract_tables", doc_core.core_extract_tables, file_path, base_dir)


def extract_images_tool(file_path: str, artifact_dir: str | None = None, base_dir: str | None = None):
    return _wrap("doc.extract_images", doc_core.core_extract_images, file_path, artifact_dir, base_dir)


def extract_formulas_tool(file_path: str, base_dir: str | None = None):
    return _wrap("doc.extract_formulas", doc_core.core_extract_formulas, file_path, base_dir)


def chunk_tool(parsed_document: dict[str, Any], chunk_size: int = 800, overlap: int = 100):
    return _wrap("doc.chunk", doc_core.core_chunk, parsed_document, chunk_size, overlap)


def evaluate_quality_tool(parsed_document: dict[str, Any]):
    return _wrap("doc.evaluate_quality", doc_core.core_evaluate_quality, parsed_document)


def export_artifacts_tool(parsed_document: dict[str, Any], export_dir: str, base_dir: str | None = None):
    return _wrap("doc.export_artifacts", doc_core.core_export_artifacts, parsed_document, export_dir, base_dir)


def register_doc_tools(registry):
    from .doc_specs import DOC_TOOL_SPECS

    registry.register_many(DOC_TOOL_SPECS)
