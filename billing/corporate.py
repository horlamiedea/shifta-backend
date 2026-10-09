"""
Corporate (business) onboarding on Embedly — used for facility wallets and for
Shifta's own platform wallet.

Embedly opens a corporate wallet only after its back office approves the
business. For approval it needs (all verified on staging, 2026-10-08):
  1. the corporate customer: RC number, TIN, name, address, city, email
  2. at least one director: name, email, phone, date of birth, address, BVN,
     NIN, electricity meter number and disco
  3. six documents — CAC, TIN certificate, board resolution, utility bill,
     MEMART and SCUML — uploaded to Embedly first, because the API takes URLs
     (not the base64 its docs describe). PDF, JPEG or PNG. Resubmitting
     replaces them.
Approval itself is a manual review; billing.tasks.provision_missing_wallets
keeps trying to open the wallet until Embedly allows it.
"""
from dataclasses import dataclass
from datetime import date

from django.utils import timezone

from .embedly_client import embedly_client

# (our field, Embedly's field, label shown to users)
DOCUMENTS = (
    ('cac', 'cac', 'CAC certificate'),
    ('tin_document', 'tin', 'TIN certificate'),
    ('board_resolution', 'boardResolution', 'board resolution'),
    ('utility_bill', 'utilityBill', 'utility bill'),
    ('memart', 'memart', 'MEMART'),
    ('scuml', 'scuml', 'SCUML certificate'),
)
DOCUMENT_TYPES = {'application/pdf', 'image/jpeg', 'image/png'}
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024

DISCO_CODES = (
    'ABUJA', 'EKO', 'IKEJA', 'IBADAN', 'ENUGU', 'PH', 'JOS', 'KADUNA', 'KANO', 'BH', 'PROTOGY',
    'PHISBOND', 'ACCESSPOWER', 'YOLA', 'ABIA', 'ADAMAWA', 'AKWA IBOM', 'ANAMBRA', 'BAUCHI',
    'BAYELSA', 'BENUE', 'BORNO', 'CROSS RIVER', 'DELTA', 'EBONYI', 'EDO', 'EKITI', 'GOMBE', 'IMO',
    'JIGAWA', 'KATSINA', 'KEBBI', 'KOGI', 'KWARA', 'LAGOS', 'NASSARAWA', 'NIGER', 'OGUN', 'ONDO',
    'OSUN', 'OYO', 'PLATEAU', 'RIVERS', 'SOKOTO', 'TARABA', 'YOBE', 'ZAMFARA', 'FCT',
)


@dataclass
class Business:
    name: str
    rc_number: str
    tin: str
    address: str
    city: str
    email: str


@dataclass
class Director:
    first_name: str
    last_name: str
    email: str
    phone: str
    dob: date
    address: str
    bvn: str
    nin: str
    meter_number: str
    disco_code: str
    middle_name: str = ''

    @property
    def identity(self):
        return f"{self.first_name} {self.last_name}".strip().lower(), self.bvn[-4:]


@dataclass
class Document:
    filename: str
    content: bytes
    content_type: str


def submit(record, *, business, director=None, documents=None):
    """
    Send Embedly whatever is new and record progress on `record` (an
    EmbedlyWallet or PlatformWallet). Call again to add another director or to
    replace documents. Progress is saved after each step, so a failure part-way
    never repeats a step that already succeeded. Raises ValueError with a
    message fit to show the user.
    """
    from .wallet_service import _embedly_phone  # avoid a circular import

    meta = dict(record.meta or {})
    directors = list(meta.get('directors') or [])

    # Validate everything before calling Embedly.
    if not record.customer_id and not (business.tin or '').strip():
        raise ValueError("Your company TIN is required.")
    if not directors and director is None:
        raise ValueError("Add at least one company director.")
    if documents is not None or not meta.get('documents_submitted_at'):
        _validate_documents(documents or {})

    def save(**fields):
        record.meta = meta
        for name, value in fields.items():
            setattr(record, name, value)
        record.save(update_fields=['meta', 'updated_at', *fields])

    if not record.customer_id:
        r = embedly_client.create_corporate_customer(
            rc_number=business.rc_number, tin=business.tin.strip(), business_name=business.name,
            address=business.address, city=business.city, email=business.email,
            wallet_name=business.name[:60],
        )
        if not r.ok or not (r.data or {}).get('id'):
            raise ValueError(f"Wallet provider rejected the business details: {r.message or 'unknown error'}")
        save(customer_id=r.data['id'])

    if director is not None and not any(
            (d['name'].lower(), d['bvn_last4']) == director.identity for d in directors):
        r = embedly_client.add_corporate_director(
            record.customer_id, first_name=director.first_name, last_name=director.last_name,
            middle_name=director.middle_name, email=director.email, phone=_embedly_phone(director.phone),
            dob=director.dob, address=director.address, bvn=director.bvn, nin=director.nin,
            meter_number=director.meter_number, disco_code=director.disco_code,
        )
        if not r.ok:
            raise ValueError(f"The director could not be added: {r.message or 'unknown error'}")
        # The director's BVN/NIN are never stored, only enough to avoid re-adding them.
        directors.append({
            'id': (r.data or {}).get('directorId'),
            'name': f"{director.first_name} {director.last_name}".strip(),
            'bvn_last4': director.bvn[-4:],
            'added_at': timezone.now().isoformat(),
        })
        meta['directors'] = directors
        save()

    if documents:
        urls = {}
        for field, embedly_name, label in DOCUMENTS:
            doc = documents[field]
            r = embedly_client.upload_file(doc.filename, doc.content, doc.content_type)
            if not r.ok or not isinstance(r.data, str):
                raise ValueError(f"Could not upload the {label}. Please try again.")
            urls[embedly_name] = r.data
        replace = bool(meta.get('documents_submitted_at'))
        r = embedly_client.submit_corporate_documents(record.customer_id, urls, replace=replace)
        if not r.ok and r.code == '409' and not replace:
            # Embedly already holds documents (we lost track): replace them.
            r = embedly_client.submit_corporate_documents(record.customer_id, urls, replace=True)
        if not r.ok:
            raise ValueError(f"The documents could not be submitted: {r.message or 'unknown error'}")
        meta['documents_submitted_at'] = timezone.now().isoformat()
        save()

    from .models import EmbedlyWallet
    meta['kyc'] = {'methods': ['CORPORATE'], 'submitted_at': timezone.now().isoformat()}
    meta['last_error'] = ''
    save(status=EmbedlyWallet.PENDING_TIER)
    return record


def sniff_content_type(content):
    """The real type from the file's first bytes; the client's claim isn't trusted."""
    if content.startswith(b'%PDF-'):
        return 'application/pdf'
    if content.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if content.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    return ''


def make_document(filename, content):
    return Document(filename=filename, content=content, content_type=sniff_content_type(content))


def _validate_documents(documents):
    missing = [label for field, _, label in DOCUMENTS if field not in documents]
    if missing:
        raise ValueError(f"Upload the company documents. Missing: {', '.join(missing)}.")
    for field, _, label in DOCUMENTS:
        doc = documents[field]
        if doc.content_type not in DOCUMENT_TYPES:
            raise ValueError(f"The {label} must be a PDF, JPEG or PNG.")
        if len(doc.content) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"The {label} must be 5 MB or smaller.")


def approval_state(customer_id):
    """Embedly's back-office decision on the business, e.g. "Pending"."""
    r = embedly_client.get_corporate_customer(customer_id)
    return str((r.data or {}).get('isCorporateVerified') or '') if r.ok else ''
