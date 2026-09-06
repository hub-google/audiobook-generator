"""Website adapters: all site-specific parsing lives in this package."""
from .registry import resolve_source
from .base import SourceParseError, SourceAccessError
