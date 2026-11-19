"""Importable implementation for the parser-conformance vector validator."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_PATH = Path(__file__).with_name("validate-parser-conformance.py")
_SPEC = spec_from_file_location("validate_parser_conformance", _PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"cannot load {_PATH}")
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
load_vectors = _MODULE.load_vectors
