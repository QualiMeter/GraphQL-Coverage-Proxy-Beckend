"""Fail-closed AST copies of actual wire documents; never retain source locations.

Names/aliases/type conditions and container shape are structural. Every scalar
literal is replaced, including enums, booleans, null and variable defaults.
No schema validation is implied: placeholders are deliberately not executable.
"""

from dataclasses import dataclass
from typing import Literal

from graphql import GraphQLError, parse, print_ast
from graphql.language import Node, Visitor, visit
from graphql.language.ast import (
    BooleanValueNode, EnumValueNode, FloatValueNode, IntValueNode,
    NullValueNode, StringValueNode,
)

from app.diagnostics import internal_failure

DocumentAvailability = Literal["available", "redacted", "unavailable"]
MAX_SAFE_DOCUMENT_BYTES = 65_536
MAX_DOCUMENT_INPUT_BYTES = 262_144
MAX_DOCUMENT_TOKENS = 10_000
MAX_AST_NODES = 20_000
MAX_AST_DEPTH = 64


@dataclass(frozen=True)
class SafeDocument:
    text: str | None = None
    availability: DocumentAvailability = "unavailable"


_ALLOWED_KINDS = frozenset({
    "document", "operation_definition", "variable_definition", "selection_set", "field",
    "argument", "fragment_spread", "inline_fragment", "fragment_definition", "variable",
    "name", "directive", "named_type", "list_type", "non_null_type", "int_value",
    "float_value", "string_value", "boolean_value", "null_value", "enum_value",
    "list_value", "object_value", "object_field",
})
_REPLACEMENTS = {
    "string_value": lambda: StringValueNode(value="[REDACTED]", block=False),
    "int_value": lambda: IntValueNode(value="0"),
    "float_value": lambda: FloatValueNode(value="0.0"),
    "enum_value": lambda: EnumValueNode(value="REDACTED"),
    "boolean_value": lambda: BooleanValueNode(value=False),
    "null_value": NullValueNode,
}


class _UnsupportedDocument(ValueError):
    pass


def _bounded_tree(root: Node):
    stack = [(root, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if node.kind not in _ALLOWED_KINDS or count > MAX_AST_NODES or depth > MAX_AST_DEPTH:
            raise _UnsupportedDocument("Unsupported or excessive AST")
        for key in node.keys:
            if key == "loc":
                continue
            value = getattr(node, key, None)
            if isinstance(value, Node):
                stack.append((value, depth + 1))
            elif isinstance(value, (tuple, list)):
                stack.extend((child, depth + 1) for child in value if isinstance(child, Node))


class _Mask(Visitor):
    redacted = False

    def enter(self, node, *_args):
        replacement = _REPLACEMENTS.get(node.kind)
        if replacement is not None:
            self.redacted = True
            return replacement()
        return None


def mask_value_ast(node: Node) -> SafeDocument:
    """Also used for schema argument defaults; never print an unmasked value."""
    try:
        _bounded_tree(node)
        masker = _Mask()
        text = print_ast(visit(node, masker))
        if not text or len(text.encode("utf-8")) > MAX_SAFE_DOCUMENT_BYTES:
            return SafeDocument()
        return SafeDocument(text, "redacted" if masker.redacted else "available")
    except (_UnsupportedDocument, UnicodeError, RecursionError):
        return SafeDocument()
    except Exception as exc:
        internal_failure("safe_document_mask", exc)
        return SafeDocument()


def normalize_document(source: str) -> SafeDocument:
    try:
        if len(source.encode("utf-8")) > MAX_DOCUMENT_INPUT_BYTES:
            return SafeDocument()
        document = parse(source, no_location=True, max_tokens=MAX_DOCUMENT_TOKENS)
        if not document.definitions or any(d.kind not in {
            "operation_definition", "fragment_definition"
        } for d in document.definitions):
            return SafeDocument()
        return mask_value_ast(document)
    except (GraphQLError, UnicodeError, RecursionError):
        return SafeDocument()
    except Exception as exc:
        internal_failure("safe_document_parse", exc)
        return SafeDocument()
