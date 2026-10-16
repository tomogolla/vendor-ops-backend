"""Strict placeholder rendering for database-stored email templates.

Templates are written by staff and stored in the database, so they are never run through Django's
template engine. The only syntax is `{variable_name}` for a whitelisted variable, and `{{` / `}}`
for literal braces. Anything else is a syntax error.
"""

import html
import re
from dataclasses import dataclass

# The only variables any template may use, with a description shown to template authors.
VARIABLES: dict[str, str] = {
    'vendor_name': "The vendor's brand name.",
    'application_reference': 'Short reference for the application, e.g. APP-1A2B3C4D.',
    'market_name': 'The market weekend label, e.g. "Oct 10 & 11".',
    'market_dates': 'The market weekend dates, e.g. "Oct 10 – 11, 2026".',
    'booth_name': 'The booth (or add-on) a decision is about, e.g. "8ft x 8ft".',
    'owner_name': 'The team member who looks after the vendor, or "The Good Flea team".',
    'payment_link': 'A payment link (https). Only available once invoicing exists.',
}

_NAME = re.compile(r'[a-z][a-z0-9_]*\Z')


class TemplateSyntaxError(ValueError):
    pass


class MissingVariableError(ValueError):
    def __init__(self, names: list[str]):
        super().__init__(f'No value for: {", ".join(names)}')
        self.names = names


@dataclass(frozen=True)
class _Literal:
    text: str


@dataclass(frozen=True)
class _Placeholder:
    name: str


def _tokens(text: str) -> list[_Literal | _Placeholder]:
    tokens: list[_Literal | _Placeholder] = []
    buffer: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        pair = text[i:i + 2]
        if pair in ('{{', '}}'):
            buffer.append(char)
            i += 2
        elif char == '{':
            end = text.find('}', i + 1)
            name = text[i + 1:end] if end != -1 else ''
            if end == -1 or not _NAME.match(name):
                raise TemplateSyntaxError(
                    f'Invalid placeholder near "{text[i:i + 20]}". Use {{variable_name}}, or {{{{ for a literal brace.'
                )
            if buffer:
                tokens.append(_Literal(''.join(buffer)))
                buffer = []
            tokens.append(_Placeholder(name))
            i = end + 1
        elif char == '}':
            raise TemplateSyntaxError('Unmatched "}". Use }} for a literal brace.')
        else:
            buffer.append(char)
            i += 1
    if buffer:
        tokens.append(_Literal(''.join(buffer)))
    return tokens


def placeholders(text: str) -> list[str]:
    """Variable names used in `text`, in order of first use. Raises TemplateSyntaxError."""
    seen: list[str] = []
    for token in _tokens(text):
        if isinstance(token, _Placeholder) and token.name not in seen:
            seen.append(token.name)
    return seen


def template_errors(parts: dict[str, str], allowed_variables: list[str]) -> dict[str, str]:
    """Validation errors per field: syntax, variables outside the whitelist, or variables the
    template has not declared in `allowed_variables`."""
    errors: dict[str, str] = {}
    unknown_allowed = sorted(set(allowed_variables) - VARIABLES.keys())
    if unknown_allowed:
        errors['allowed_variables'] = f'Not available to templates: {", ".join(unknown_allowed)}.'
    for field, text in parts.items():
        try:
            used = placeholders(text)
        except TemplateSyntaxError as error:
            errors[field] = str(error)
            continue
        unknown = [name for name in used if name not in VARIABLES]
        undeclared = [name for name in used if name in VARIABLES and name not in allowed_variables]
        if unknown:
            errors[field] = f'Unknown placeholder(s): {", ".join("{" + name + "}" for name in unknown)}.'
        elif undeclared:
            errors[field] = (
                f'Placeholder(s) not allowed for this template: {", ".join("{" + name + "}" for name in undeclared)}. '
                'Add them to the allowed variables first.'
            )
    return errors


def render(text: str, values: dict[str, str], *, escape_html: bool) -> str:
    """Fills placeholders from `values` (whitelisted names only). Values are HTML-escaped for
    HTML bodies. Every placeholder must have a non-empty value."""
    tokens = _tokens(text)
    missing = [t.name for t in tokens if isinstance(t, _Placeholder) and not str(values.get(t.name, '') or '').strip()]
    if missing:
        raise MissingVariableError(sorted(set(missing)))
    out: list[str] = []
    for token in tokens:
        if isinstance(token, _Literal):
            out.append(token.text)
        else:
            if token.name not in VARIABLES:
                raise TemplateSyntaxError(f'Unknown placeholder {{{token.name}}}.')
            value = str(values[token.name])
            out.append(html.escape(value, quote=True) if escape_html else value)
    return ''.join(out)
