import csv
import io
import re
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator, validate_email
from django.db import transaction

from tasks.services import ensure_discovery_call
from vendors.activity import record_activity
from vendors.models import ActivityEvent, LeadIntakeEvent, Vendor
from vendors.normalization import normalize_email, normalize_instagram, normalize_phone
from vendors.options import CATEGORIES, LEAD_SOURCES, canonical
from vendors.services import find_matches

from .models import ImportBatch

MAX_CSV_BYTES = 2 * 1024 * 1024

HEADER_ALIASES = {
    'brand': 'brand_name', 'brand name': 'brand_name', 'business name': 'brand_name',
    'first name': 'first_name', 'last name': 'last_name',
    'email': 'email', 'phone': 'phone', 'instagram': 'instagram', 'ig': 'instagram',
    'website': 'website', 'category': 'category',
    'lead source': 'lead_source', 'source': 'lead_source',
    'notes': 'notes', 'tags': 'tags',
    'already contacted': 'already_contacted', 'contacted': 'already_contacted',
    'inactive': 'inactive',
    'exclude from outreach': 'exclude_from_outreach', 'do not contact': 'exclude_from_outreach',
}
FLAG_FIELDS = ('already_contacted', 'inactive', 'exclude_from_outreach')
TRUE_VALUES = {'yes', 'y', 'true', '1', 'x'}


class Classification:
    NEW = 'new'
    MATCHED = 'matched'
    POSSIBLE_DUPLICATE = 'possible_duplicate'
    INVALID = 'invalid'
    SKIPPED = 'skipped'


@dataclass
class Row:
    line: int
    classification: str
    data: dict
    flags: dict
    reasons: list[str] = field(default_factory=list)
    matches: list[dict] = field(default_factory=list)

    def as_dict(self):
        return {
            'line': self.line, 'classification': self.classification, 'brand_name': self.data.get('brand_name') or '',
            'data': self.data, 'flags': self.flags, 'reasons': self.reasons, 'matches': self.matches,
        }


class CsvError(Exception):
    pass


def _normalize_header(header: str) -> str:
    return re.sub(r'[\s_-]+', ' ', (header or '').strip().lower())


def _row_to_vendor_data(record: dict) -> tuple[dict, dict]:
    value = lambda key: (record.get(key) or '').strip()  # noqa: E731
    data = {
        'brand_name': value('brand_name'), 'first_name': value('first_name'), 'last_name': value('last_name'),
        'email': value('email'), 'phone': value('phone'), 'instagram': value('instagram'),
        'website': value('website'), 'notes': value('notes'),
        'lead_source': canonical(LEAD_SOURCES, value('lead_source')) or value('lead_source') or 'CSV import',
        'tags': [tag.strip() for tag in value('tags').split(';') if tag.strip()],
        'category': '',
    }
    raw_category = value('category')
    category = canonical(CATEGORIES, raw_category)
    if category:
        data['category'] = category
    elif raw_category:
        line = f'Imported category: {raw_category}'
        data['notes'] = f"{data['notes']}\n{line}" if data['notes'] else line
    flags = {flag: value(flag).lower() in TRUE_VALUES for flag in FLAG_FIELDS}
    return data, flags


def _validation_errors(data: dict) -> list[str]:
    reasons = []
    if not data['brand_name']:
        reasons.append('Brand name is required.')
    if not (data['email'] or data['phone'] or data['instagram']):
        reasons.append('Add at least one of email, phone or Instagram.')
    if data['email']:
        try:
            validate_email(data['email'])
        except DjangoValidationError:
            reasons.append('Email is not valid.')
    if data['phone'] and len(normalize_phone(data['phone'])) < 7:
        reasons.append('Phone needs at least 7 digits.')
    if data['website']:
        try:
            URLValidator(schemes=['http', 'https'])(data['website'])
        except DjangoValidationError:
            reasons.append('Website must be a full http(s) URL.')
    return reasons


def _summary(vendor: Vendor) -> dict:
    return {'id': str(vendor.pk), 'brand_name': vendor.brand_name, 'status': vendor.display_status}


def read_csv(raw: bytes) -> tuple[list[dict], list[str]]:
    if len(raw) > MAX_CSV_BYTES:
        raise CsvError('The file is larger than 2 MB.')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as error:
        raise CsvError('The file must be UTF-8 encoded CSV.') from error
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvError('The file has no header row.')
    mapping, ignored = {}, []
    for header in reader.fieldnames:
        key = HEADER_ALIASES.get(_normalize_header(header))
        if key and key not in mapping.values():
            mapping[header] = key
        elif header and header.strip():
            ignored.append(header.strip())
    records = []
    for raw_record in reader:
        records.append({mapping[h]: v for h, v in raw_record.items() if h in mapping and isinstance(v, str)})
    return records, ignored


def preview(raw: bytes) -> dict:
    records, ignored = read_csv(raw)
    rows: list[Row] = []
    seen: dict[tuple[str, str], int] = {}

    for index, record in enumerate(records):
        line = index + 2
        data, flags = _row_to_vendor_data(record)
        if not any(data[key] for key in ('brand_name', 'email', 'phone', 'instagram', 'first_name', 'last_name')):
            rows.append(Row(line, Classification.SKIPPED, data, flags, ['Empty row.']))
            continue
        reasons = _validation_errors(data)
        if reasons:
            rows.append(Row(line, Classification.INVALID, data, flags, reasons))
            continue

        keys = [
            ('email', normalize_email(data['email'])),
            ('phone', normalize_phone(data['phone'])),
            ('instagram', normalize_instagram(data['instagram'])),
        ]
        earlier = next((seen[key] for key in keys if key[1] and key in seen), None)
        if earlier:
            rows.append(Row(line, Classification.SKIPPED, data, flags, [f'Same contact details as row {earlier} in this file.']))
            continue
        for key in keys:
            if key[1]:
                seen[key] = line

        result = find_matches(
            email=data['email'], phone=data['phone'], instagram=data['instagram'], brand_name=data['brand_name'],
        )
        matches = [{'vendor': _summary(m.vendor), 'matched_on': m.matched_on} for m in result.reliable]
        if len(result.reliable) == 1:
            rows.append(Row(line, Classification.MATCHED, data, flags,
                            [f"Matches existing vendor on {', '.join(result.reliable[0].matched_on)}."], matches))
        elif len(result.reliable) > 1:
            rows.append(Row(line, Classification.POSSIBLE_DUPLICATE, data, flags,
                            ['Contact details match more than one existing vendor.'], matches))
        elif result.name_only:
            rows.append(Row(line, Classification.POSSIBLE_DUPLICATE, data, flags,
                            ['Same business name as an existing vendor, but different contact details.'],
                            [{'vendor': _summary(v), 'matched_on': ['brand_name']} for v in result.name_only]))
        else:
            rows.append(Row(line, Classification.NEW, data, flags))

    counts = {name: 0 for name in ('new', 'matched', 'possible_duplicate', 'invalid', 'skipped')}
    for row in rows:
        counts[row.classification] += 1
    return {'rows': [row.as_dict() for row in rows], 'counts': counts, 'ignored_headers': ignored}


def confirm(raw: bytes, *, actor, file_name='', include_lines: set[int] | None = None) -> dict:
    """Re-classifies on the server (the preview is never trusted) and imports in one transaction.
    `include_lines` lists possible-duplicate rows the user chose to import as new vendors."""
    include_lines = include_lines or set()
    result = preview(raw)
    Event = ActivityEvent.EventType
    summary = {'created': 0, 'matched': 0, 'skipped': 0, 'invalid': 0, 'call_tasks_created': 0, 'vendor_ids': []}

    with transaction.atomic():
        batch = ImportBatch.objects.create(file_name=file_name[:255], created_by=actor)
        for row in result['rows']:
            kind = row['classification']
            if kind == Classification.POSSIBLE_DUPLICATE and row['line'] in include_lines:
                kind = Classification.NEW
            if kind == Classification.INVALID:
                summary['invalid'] += 1
                continue
            if kind in (Classification.SKIPPED, Classification.POSSIBLE_DUPLICATE):
                summary['skipped'] += 1
                continue

            flags = row['flags']
            metadata = {'line': row['line'], 'flags': flags}
            if kind == Classification.MATCHED:
                vendor = Vendor.objects.get(pk=row['matches'][0]['vendor']['id'])
                LeadIntakeEvent.objects.create(
                    vendor=vendor, source_type=LeadIntakeEvent.SourceType.CSV_IMPORT, source_detail=file_name,
                    import_batch=batch, captured_by=actor, metadata={**metadata, 'matched': True, 'row': row['data']},
                )
                record_activity(vendor, Event.LEAD_IMPORTED, f'Matched existing vendor in CSV import ({file_name or "CSV"})',
                                actor=actor, batch=str(batch.pk), matched=True)
                summary['matched'] += 1
                continue

            vendor = Vendor.objects.create(
                **row['data'], created_by=actor,
                is_active=not flags['inactive'], outreach_opt_out=flags['exclude_from_outreach'],
            )
            LeadIntakeEvent.objects.create(
                vendor=vendor, source_type=LeadIntakeEvent.SourceType.CSV_IMPORT, source_detail=file_name,
                import_batch=batch, captured_by=actor, metadata=metadata,
            )
            record_activity(vendor, Event.LEAD_IMPORTED, f'Imported from CSV ({file_name or "CSV"})',
                            actor=actor, batch=str(batch.pk))
            summary['created'] += 1
            summary['vendor_ids'].append(str(vendor.pk))
            if not flags['already_contacted']:
                _, created = ensure_discovery_call(vendor, actor=actor)
                summary['call_tasks_created'] += int(created)

        batch.summary = {k: v for k, v in summary.items() if k != 'vendor_ids'}
        batch.save(update_fields=['summary'])
    return {**summary, 'batch_id': str(batch.pk)}
