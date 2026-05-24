from .doc_tools import (
    chunk_tool,
    evaluate_quality_tool,
    export_artifacts_tool,
    extract_formulas_tool,
    extract_images_tool,
    extract_tables_tool,
    extract_text_tool,
    identify_file_tool,
    parse_tool,
)
from .tool_spec import MainAgentToolSpec, ToolCategory, ToolSource

TAGS = ["doc", "document", "parse", "pdf", "docx", "xlsx", "csv", "markdown", "html", "text", "chunk", "quality", "artifact", "table", "image", "formula"]
WRAP_SCHEMA = {"type": "object", "properties": {"success": {"type": "boolean"}, "tool": {"type": "string"}, "data": {}, "error": {"type": ["string", "null"]}}, "required": ["success", "tool", "data", "error"]}

DOC_TOOL_SPECS = [
    MainAgentToolSpec(name="doc.identify_file", description="Identify document type, mime, hash, size and counters.", source=ToolSource.LOCAL_PYTHON, category=ToolCategory.DOCUMENT, tags=TAGS, input_schema={"type": "object", "required": ["file_path"]}, output_schema=WRAP_SCHEMA, permissions=["filesystem"], tool_obj=identify_file_tool),
    MainAgentToolSpec(name="doc.parse", description="Parse document to structured model/chunks/quality/artifacts.", source=ToolSource.LOCAL_PYTHON, category=ToolCategory.DOCUMENT, tags=TAGS, input_schema={"type": "object", "required": ["file_path"]}, output_schema=WRAP_SCHEMA, permissions=["filesystem"], tool_obj=parse_tool),
    MainAgentToolSpec(name="doc.extract_text", description="Extract location-aware textual blocks.", source=ToolSource.LOCAL_PYTHON, category=ToolCategory.DOCUMENT, tags=TAGS, input_schema={"type": "object", "required": ["file_path"]}, output_schema=WRAP_SCHEMA, permissions=["filesystem"], tool_obj=extract_text_tool),
    MainAgentToolSpec(name="doc.extract_tables", description="Extract structured tables with confidence.", source=ToolSource.LOCAL_PYTHON, category=ToolCategory.DOCUMENT, tags=TAGS, input_schema={"type": "object", "required": ["file_path"]}, output_schema=WRAP_SCHEMA, permissions=["filesystem"], tool_obj=extract_tables_tool),
    MainAgentToolSpec(name="doc.extract_images", description="Extract figures/images to artifacts with sanitized names.", source=ToolSource.LOCAL_PYTHON, category=ToolCategory.DOCUMENT, tags=TAGS, input_schema={"type": "object", "required": ["file_path"]}, output_schema=WRAP_SCHEMA, permissions=["filesystem"], tool_obj=extract_images_tool),
    MainAgentToolSpec(name="doc.extract_formulas", description="Extract formulas with low-confidence warnings where partial.", source=ToolSource.LOCAL_PYTHON, category=ToolCategory.DOCUMENT, tags=TAGS, input_schema={"type": "object", "required": ["file_path"]}, output_schema=WRAP_SCHEMA, permissions=["filesystem"], tool_obj=extract_formulas_tool),
    MainAgentToolSpec(name="doc.chunk", description="Create traceable section-aware chunks.", source=ToolSource.LOCAL_PYTHON, category=ToolCategory.DOCUMENT, tags=TAGS, input_schema={"type": "object", "required": ["parsed_document"]}, output_schema=WRAP_SCHEMA, permissions=["none"], tool_obj=chunk_tool),
    MainAgentToolSpec(name="doc.evaluate_quality", description="Evaluate quality and produce warnings/confidence.", source=ToolSource.LOCAL_PYTHON, category=ToolCategory.DOCUMENT, tags=TAGS, input_schema={"type": "object", "required": ["parsed_document"]}, output_schema=WRAP_SCHEMA, permissions=["none"], tool_obj=evaluate_quality_tool),
    MainAgentToolSpec(name="doc.export_artifacts", description="Export JSON/Markdown/JSONL/CSV/report artifacts safely.", source=ToolSource.LOCAL_PYTHON, category=ToolCategory.DOCUMENT, tags=TAGS, input_schema={"type": "object", "required": ["parsed_document", "export_dir"]}, output_schema=WRAP_SCHEMA, permissions=["filesystem"], tool_obj=export_artifacts_tool),
]
