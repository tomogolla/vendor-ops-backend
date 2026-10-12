import re


def normalize_email(value: str | None) -> str:
    return (value or '').strip().lower()


def normalize_instagram(value: str | None) -> str:
    """'@Wayne_Ent ' and 'https://instagram.com/wayne_ent/' both become 'wayne_ent'."""
    handle = (value or '').strip().lower()
    handle = re.sub(r'^(https?://)?(www\.)?instagram\.com/', '', handle)
    handle = handle.split('?')[0].strip('/')
    return re.sub(r'[@\s]', '', handle)


def normalize_phone(value: str | None) -> str:
    """Digits only; a leading US country code is dropped so '+1 (555) 123-4567' == '555-123-4567'."""
    digits = re.sub(r'\D', '', value or '')
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    return digits
