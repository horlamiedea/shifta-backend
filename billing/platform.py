"""
Shifta's platform wallet on Embedly.

Shift charges flow into it; refunds, earnings and fee refunds flow out of it.
Until it is open, wallet transfers wait in the outbox (nothing is lost). Set it
up once with `python manage.py setup_platform_wallet`:

  --config platform.json   production: Shifta onboarded as a business (director
                           + six documents); opens once Embedly approves it
  --adopt <account>        use an existing wallet in this Embedly organisation
  --staging-individual     staging only: an instant Tier-1 individual wallet

EMBEDLY_PLATFORM_ACCOUNT_NUMBER, when set, overrides the stored wallet.
"""
import hashlib
import logging
from datetime import date

from django.conf import settings
from django.db import transaction

from . import corporate
from .embedly_client import embedly_client, is_staging
from .models import EmbedlyWallet, PlatformWallet

logger = logging.getLogger(__name__)

PLATFORM_NAME = 'Shifta Platform'


def platform_account_number():
    if settings.EMBEDLY_PLATFORM_ACCOUNT_NUMBER:
        return settings.EMBEDLY_PLATFORM_ACCOUNT_NUMBER
    return (
        PlatformWallet.objects.filter(status=EmbedlyWallet.ACTIVE)
        .values_list('account_number', flat=True).first() or ''
    )


def get_platform_wallet():
    return PlatformWallet.objects.first()


def _record(kind):
    record, _ = PlatformWallet.objects.get_or_create(singleton=True, defaults={'kind': kind})
    if record.status == EmbedlyWallet.ACTIVE:
        raise ValueError(f"The platform wallet is already open ({record.account_number}).")
    if record.customer_id and record.kind != kind:
        raise ValueError(f"Platform onboarding already started as {record.kind}; continue that instead.")
    record.kind = kind
    record.save(update_fields=['kind', 'updated_at'])
    return record


def setup_corporate(business, director=None, documents=None):
    """Onboard Shifta as a business. The wallet opens after Embedly's approval."""
    record = corporate.submit(_record(PlatformWallet.CORPORATE), business=business,
                              director=director, documents=documents)
    return activate_platform_wallet(record.pk)


def adopt(account_number):
    """Use a wallet that already exists in this Embedly organisation."""
    r = embedly_client.get_wallet_by_account(account_number)
    if not r.ok or not isinstance(r.data, dict):
        raise ValueError(f"Embedly has no wallet {account_number} in this organisation: {r.message}")
    record, _ = PlatformWallet.objects.get_or_create(singleton=True)
    account = r.data.get('virtualAccount') or {}
    record.kind = PlatformWallet.ADOPTED
    record.status = EmbedlyWallet.ACTIVE
    record.customer_id = r.data.get('customerId')
    record.wallet_id = r.data.get('id')
    record.account_number = account_number
    record.account_name = r.data.get('name') or PLATFORM_NAME
    record.bank_name = account.get('bankName', '')
    record.bank_code = account.get('bankCode', '')
    record.save()
    return record


def setup_staging_individual(email):
    """
    Staging only. Opens a Tier-1 individual wallet immediately (test BVN +
    mocked liveness) so payments can be tested before corporate approval.
    Tier-1 limits (₦300k balance, ₦50k a day) make it unfit for production.
    """
    if not is_staging():
        raise ValueError("--staging-individual only works against Embedly staging.")
    record = _record(PlatformWallet.INDIVIDUAL)
    if not record.customer_id:
        # Embedly requires a unique mobile number; derive a placeholder from the email.
        mobile = '080' + str(int(hashlib.sha256(email.encode()).hexdigest(), 16))[:8]
        r = embedly_client.create_individual_customer(
            first_name='Shifta', last_name='Platform', middle_name='', dob=date(1990, 1, 1),
            address='1 Test Street', city='Lagos', email=email, mobile_number=mobile,
        )
        if not r.ok or not (r.data or {}).get('id'):
            raise ValueError(f"Embedly rejected the platform customer: {r.message}")
        record.customer_id = r.data['id']
        record.save(update_fields=['customer_id', 'updated_at'])

    # Staging accepts any 11 digits but links each BVN to one customer.
    test_bvn = '22' + str(int(hashlib.sha256(record.customer_id.encode()).hexdigest(), 16))[:9]
    r = embedly_client.verify_bvn(record.customer_id, test_bvn)
    if not r.ok and r.code != '-904':
        raise ValueError(f"Test BVN was rejected: {r.message}")
    r = embedly_client.initiate_liveness(record.customer_id)
    if not ((r.ok and str((r.data or {}).get('status', '')).lower() == 'completed') or r.code == '409'):
        raise ValueError(f"Mock liveness did not complete: {r.message}")

    record.status = EmbedlyWallet.PENDING_TIER
    record.save(update_fields=['status', 'updated_at'])
    return activate_platform_wallet(record.pk)


def activate_platform_wallet(record_id=None):
    """Open the wallet once Embedly allows it. Safe to call repeatedly."""
    from .wallet_service import apply_created_wallet, record_wallet_refusal

    with transaction.atomic():
        qs = PlatformWallet.objects.select_for_update()
        record = qs.get(pk=record_id) if record_id else qs.first()
        if record is None or record.status == EmbedlyWallet.ACTIVE or not record.customer_id:
            return record

        is_corporate = record.kind == PlatformWallet.CORPORATE
        if is_corporate:
            r = embedly_client.create_corporate_wallet(record.customer_id, PLATFORM_NAME)
        else:
            r = embedly_client.create_individual_wallet(record.customer_id, PLATFORM_NAME)

        if r.ok and apply_created_wallet(record, r, PLATFORM_NAME):
            record.save()
            logger.info("Platform wallet opened: %s", record.account_number)
            return record
        if not r.ok:
            record_wallet_refusal(record, r, corporate_customer=is_corporate)
            record.save(update_fields=['status', 'meta', 'updated_at'])
        return record
