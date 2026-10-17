"""Cleaning for text that arrives from the public internet."""

import re
import unicodedata

_SPACES = re.compile(r'\s+')


def clean_line(value, limit: int) -> str:
    """One line of text: control and formatting characters removed (tabs and newlines become
    spaces), whitespace collapsed, trimmed and cut to `limit` characters."""
    if value is None:
        return ''
    text = str(value)
    text = ''.join(' ' if char in '\t\r\n' else char for char in text if char in '\t\r\n' or unicodedata.category(char)[0] != 'C')
    return _SPACES.sub(' ', text).strip()[:limit]
