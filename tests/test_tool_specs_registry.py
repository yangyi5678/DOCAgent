from tools.doc_specs import DOC_TOOL_SPECS
from tools.doc_tools import register_doc_tools
from tools.tool_registry import ToolRegistry
from tools.tool_spec import ToolCategory, ToolSource


REQUIRED = {
    "doc.identify_file",
    "doc.parse",
    "doc.extract_text",
    "doc.extract_tables",
    "doc.extract_images",
    "doc.extract_formulas",
    "doc.chunk",
    "doc.evaluate_quality",
    "doc.export_artifacts",
}


def test_doc_specs_complete_and_local_python():
    names = {s.name for s in DOC_TOOL_SPECS}
    assert REQUIRED.issubset(names)
    for s in DOC_TOOL_SPECS:
        assert s.source == ToolSource.LOCAL_PYTHON
        assert s.category == ToolCategory.DOCUMENT
        assert s.tool_obj is not None
        assert isinstance(s.permissions, list)


def test_register_doc_tools_and_resolve():
    reg = ToolRegistry()
    register_doc_tools(reg)
    names = {s.name for s in reg.all_specs()}
    assert REQUIRED.issubset(names)
    resolved = reg.resolve_tools()
    assert len(resolved) >= len(REQUIRED)
