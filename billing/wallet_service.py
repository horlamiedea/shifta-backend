"""
Wallet Service - wallet provisioning, funding and withdrawals via Embedly.

Provisioning: Embedly only opens a wallet for a customer with a KYC tier.
  professional -> individual customer -> BVN and/or NIN check -> liveness
                  (selfie) -> tier assigned -> individual wallet
  facility     -> corporate customer + director + six documents (billing.corporate)
                  -> Embedly back-office approval -> corporate wallet
Liveness finishes instantly on staging; in production the user opens a link
and a customer.liveness.* webhook resumes provisioning. provision_missing_wallets
retries PENDING_TIER wallets until Embedly lets them open.

Funding: each wallet has a virtual NUBAN. A bank transfer into it fires the
`nip` webhook, which credits the app wallet (see billing.webhooks).

Withdrawal: the professional's app balance is debited at once; Embedly pays
the bank from the professional's own wallet, and the platform fee moves to
the platform wallet. If the bank payout fails the debit is reversed.
"""
import logging
import re
import uuid
from datetime import date
from decimal import Decimal, ROUND_DOWN

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.services import BaseService
from . import corporate, ledger
from .embedly_client import embedly_client
from .models import EmbedlyWallet, Transaction, WalletTransfer

logger = logging.getLogger(__name__)


def ensure_wallet_record(user):
    wallet, _ = EmbedlyWallet.objects.get_or_create(user=user)
    return wallet


def _embedly_phone(phone):
    """+2348012345678 / 2348012345678 -> 08012345678"""
    digits = re.sub(r'\D', '', phone or '')
    if digits.startswith('234') and len(digits) == 13:
        digits = '0' + digits[3:]
    return digits


def _kyc_passed(data):
    """BVN and NIN responses differ in nesting; both carry status + an *_check summary."""
    result = (data or {}).get('response', data) or {}
    state = str((result.get('status') or {}).get('status', '')).upper()
    checks = (result.get('summary') or {})
    matched = any(
        str((checks.get(k) or {}).get('status', '')).upper() == 'EXACT_MATCH'
        for k in ('bvn_check', 'nin_check')
    )
    return state in ('SUCCESS', 'VERIFIED') and matched


class WalletCreateService(BaseService):
    """
    Called at registration. Embedly needs KYC details the sign-up form doesn't
    collect, so this only records that the user needs a wallet; the user then
    completes WalletKycService.
    """

    def __call__(self, user):
        return ensure_wallet_record(user)


class WalletKycService(BaseService):
    """Submit KYC to Embedly and open the user's wallet."""

    ID_PATTERN = re.compile(r'^\d{11}$')

    def __call__(self, user, *, bvn=None, nin=None, dob=None, address=None, city=None,
                 middle_name='', tin=None, director=None, documents=None):
        wallet = ensure_wallet_record(user)
        if wallet.status == EmbedlyWallet.ACTIVE:
            return wallet

        if user.is_professional:
            # Already verified IDs (e.g. returning to finish the selfie) skip straight to liveness.
            if not (wallet.meta.get('kyc') and wallet.status in (
                    EmbedlyWallet.PENDING_LIVENESS, EmbedlyWallet.PENDING_TIER)):
                self._individual(user, wallet, bvn=bvn, nin=nin, dob=dob, address=address,
                                 city=city, middle_name=middle_name)
            wallet = start_liveness(wallet)
            if wallet.status == EmbedlyWallet.PENDING_LIVENESS:
                return wallet  # the user opens meta['liveness_url']; a webhook finishes the job
        elif user.is_facility:
            self._corporate(user, wallet, tin=tin, city=city, director=director, documents=documents)
        else:
            raise PermissionError("Only facilities and professionals have wallets.")

        return activate_wallet(wallet.pk)

    def _individual(self, user, wallet, *, bvn, nin, dob, address, city, middle_name):
        ids = {kind: (number or '').strip() for kind, number in (('BVN', bvn), ('NIN', nin))}
        ids = {kind: number for kind, number in ids.items() if number}
        if not ids:
            raise ValueError("Provide your BVN or NIN (both gives higher wallet limits).")
        for kind, number in ids.items():
            if not self.ID_PATTERN.match(number):
                raise ValueError(f"{kind} must be 11 digits.")
        if not isinstance(dob, date):
            raise ValueError("Date of birth is required (YYYY-MM-DD).")
        today = timezone.now().date()
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        if age < 18:
            raise ValueError("You must be at least 18 to open a wallet.")
        if not (address and city):
            raise ValueError("Address and city are required.")
        if not (user.first_name and user.last_name):
            raise ValueError("Add your first and last name to your profile first.")
        if not wallet.customer_id and len(_embedly_phone(user.phone_number)) != 11:
            raise ValueError("Add a valid Nigerian phone number to your profile first.")

        if not wallet.customer_id:
            r = embedly_client.create_individual_customer(
                first_name=user.first_name, last_name=user.last_name, middle_name=middle_name,
                dob=dob, address=address, city=city, email=user.email,
                mobile_number=_embedly_phone(user.phone_number),
            )
            if not r.ok or not (r.data or {}).get('id'):
                raise ValueError(f"Wallet provider rejected your details: {r.message or 'unknown error'}")
            wallet.customer_id = r.data['id']
            wallet.save(update_fields=['customer_id', 'updated_at'])

        for kind, number in ids.items():
            if kind == 'BVN':
                r = embedly_client.verify_bvn(wallet.customer_id, number)
            else:
                r = embedly_client.verify_nin(
                    wallet.customer_id, number,
                    first_name=user.first_name, last_name=user.last_name, dob=dob,
                )
            # -904: Embedly already verified this customer (e.g. a retry after we
            # failed to record the first success).
            already_verified = r.code == '-904'
            if not already_verified and (not r.ok or not _kyc_passed(r.data)):
                raise ValueError(
                    f"Your {kind} could not be verified against your name "
                    f"and date of birth. Check the details and try again."
                )

        # The IDs themselves are never stored, only which ones passed.
        wallet.meta = {**wallet.meta, 'kyc': {
            'methods': sorted(ids), 'last4': {kind: number[-4:] for kind, number in ids.items()},
            'verified_at': timezone.now().isoformat(),
        }}
        wallet.status = EmbedlyWallet.PENDING_LIVENESS
        wallet.save(update_fields=['meta', 'status', 'updated_at'])

    def _corporate(self, user, wallet, *, tin, city, director, documents):
        facility = user.facility
        corporate.submit(
            wallet,
            business=corporate.Business(
                name=facility.name, rc_number=facility.rc_number, tin=tin or '',
                address=facility.address, city=city or 'Lagos', email=user.email,
            ),
            director=director, documents=documents,
        )


def start_liveness(wallet):
    """
    Start (or restart) the selfie check that Embedly requires before it assigns
    a KYC tier. Staging's mock completes immediately; in production the user
    opens the returned link and the verdict arrives by webhook.
    """
    r = embedly_client.initiate_liveness(wallet.customer_id, settings.EMBEDLY_LIVENESS_REDIRECT_URL)
    data = r.data if isinstance(r.data, dict) else {}
    state = str(data.get('status') or '').lower()
    meta = {**wallet.meta}

    if (r.ok and state == 'completed') or r.code == '409':  # 409: already completed
        wallet.status = EmbedlyWallet.PENDING_TIER
        meta.update(liveness='completed', liveness_url='', last_error='')
    elif r.ok and data.get('startKycUrl'):
        wallet.status = EmbedlyWallet.PENDING_LIVENESS
        meta.update(liveness=state or 'in_progress', liveness_url=data['startKycUrl'], last_error='')
    elif r.code == '422' and 'not enabled' in r.message.lower():
        # This organisation doesn't use liveness; the tier comes from BVN/NIN alone.
        wallet.status = EmbedlyWallet.PENDING_TIER
        meta.update(liveness='not_required', last_error='')
    else:
        meta.update(last_error=r.message or 'Could not start the identity check')
        wallet.meta = meta
        wallet.save(update_fields=['meta', 'updated_at'])
        raise ValueError("Could not start the identity check. Please try again shortly.")

    wallet.meta = meta
    wallet.save(update_fields=['meta', 'status', 'updated_at'])
    return wallet


def apply_created_wallet(record, response, default_name):
    """Store a successful create-wallet response on `record`. Returns False if unusable."""
    info = response.data if isinstance(response.data, dict) else {}
    account = info.get('virtualAccount') or {}
    if not account.get('accountNumber'):
        record.meta = {**record.meta, 'last_error': 'Wallet created but no account number returned'}
        record.save(update_fields=['meta', 'updated_at'])
        return False
    record.wallet_id = info.get('walletId') or info.get('id')
    record.account_number = account['accountNumber']
    record.bank_code = account.get('bankCode', '')
    record.bank_name = account.get('bankName', '')
    record.account_name = info.get('name') or default_name
    record.status = EmbedlyWallet.ACTIVE
    record.meta = {**record.meta, 'activated_at': timezone.now().isoformat(), 'last_error': ''}
    return True


def record_wallet_refusal(record, response, *, corporate_customer):
    """Embedly wouldn't open the wallet yet: keep waiting, or mark it failed."""
    message = response.message or 'unknown error'
    waiting = (
        'tier' in message.lower() or 'pending' in message.lower()
        or response.code == '403' or response.ambiguous
    )
    meta = {**record.meta, 'last_error': message}
    if corporate_customer:
        approval = corporate.approval_state(record.customer_id)
        if approval:
            meta['approval'] = approval
        if approval.lower() in ('rejected', 'declined', 'failed'):
            waiting = False
            meta['last_error'] = f'Business verification was {approval.lower()} by the wallet provider.'
    record.status = EmbedlyWallet.PENDING_TIER if waiting else EmbedlyWallet.FAILED
    record.meta = meta


def activate_wallet(wallet_id):
    """
    Try to open the Embedly wallet for a KYC'd customer. Safe to call
    repeatedly and concurrently: the row lock stops two wallets being opened.
    """
    with transaction.atomic():
        wallet = EmbedlyWallet.objects.select_for_update().select_related('user').get(pk=wallet_id)
        if wallet.status == EmbedlyWallet.ACTIVE or not wallet.customer_id:
            return wallet

        user = wallet.user
        if user.is_facility:
            name = user.facility.name
            r = embedly_client.create_corporate_wallet(wallet.customer_id, name[:60])
        else:
            name = f"{user.first_name} {user.last_name}".strip() or user.email
            r = embedly_client.create_individual_wallet(wallet.customer_id, f"{name} - Shifta")

        if r.ok and apply_created_wallet(wallet, r, name):
            wallet.is_active = True
            wallet.save()
            logger.info("Activated Embedly wallet %s for user %s", wallet.account_number, user.pk)
            _notify_wallet_ready(user, wallet.account_number, wallet.bank_name)
            return wallet
        if r.ok:
            return wallet  # created without an account number; logged in meta

        record_wallet_refusal(wallet, r, corporate_customer=user.is_facility)
        wallet.is_active = False
        wallet.save(update_fields=['status', 'is_active', 'meta', 'updated_at'])
        return wallet


def _notify_wallet_ready(user, account_number, bank_name):
    from core.models import Notification
    transaction.on_commit(lambda: Notification.send(
        user=user, title='Your wallet is ready',
        message=f'Fund it by bank transfer to {account_number} ({bank_name}).',
        notification_type='BROADCAST', data={'type': 'wallet_ready'},
    ), robust=True)


class WalletFundingService(BaseService):
    """Initialize Paystack payment to fund facility wallet."""

    @transaction.atomic
    def __call__(self, user, amount):
        if not user.is_facility:
            raise PermissionError("Only facilities can fund their wallet.")

        amount_decimal = ledger.to_money(amount)

        reference = str(uuid.uuid4()).replace('-', '')

        # Create pending transaction
        tx = Transaction.objects.create(
            user=user,
            amount=amount_decimal,
            transaction_type='FUNDING',
            reference=reference,
            status='PENDING',
        )

        # Initialize Paystack payment
        paystack_url = 'https://api.paystack.co/transaction/initialize'
        payload = {
            'email': user.email,
            'amount': int(amount_decimal * 100),  # Paystack uses kobo
            'reference': reference,
            'callback_url': getattr(settings, 'PAYSTACK_CALLBACK_URL', ''),  # Frontend handles redirect
        }
        headers = {
            'Authorization': f'Bearer {settings.PAYSTACK_SECRET_KEY}',
            'Content-Type': 'application/json',
        }

        try:
            response = requests.post(paystack_url, json=payload, headers=headers, timeout=30)
            data = response.json()

            if data.get('status'):
                return {
                    'reference': reference,
                    'authorization_url': data['data']['authorization_url'],
                    'access_code': data['data']['access_code'],
                    'transaction_id': str(tx.id),
                }
            else:
                tx.status = 'FAILED'
                tx.save()
                raise ValueError(f"Paystack initialization failed: {data.get('message', 'Unknown error')}")
        except requests.exceptions.RequestException as e:
            tx.status = 'FAILED'
            tx.save()
            raise ValueError(f"Payment service unavailable: {str(e)}")


class WalletFundingVerifyService(BaseService):
    """
    Verify a Paystack payment and credit the facility wallet.

    Paystack settles to Shifta's bank account, so the Embedly side of this
    credit is a platform -> facility transfer; keep the platform wallet topped
    up from Paystack settlements.
    """

    @transaction.atomic
    def __call__(self, reference, user=None):
        try:
            tx = Transaction.objects.select_for_update().get(reference=reference, transaction_type='FUNDING')
        except Transaction.DoesNotExist:
            raise ValueError("Transaction not found.")
        if user is not None and tx.user_id != user.pk:
            raise ValueError("Transaction not found.")

        if tx.status == 'SUCCESS':
            return {'status': 'already_verified', 'message': 'Payment already processed.'}

        # Verify with Paystack
        verify_url = f'https://api.paystack.co/transaction/verify/{reference}'
        headers = {'Authorization': f'Bearer {settings.PAYSTACK_SECRET_KEY}'}

        try:
            response = requests.get(verify_url, headers=headers, timeout=30)
            data = response.json()
        except (requests.exceptions.RequestException, ValueError):
            raise ValueError("Could not verify payment. Please try again.")

        paid = data.get('status') and (data.get('data') or {}).get('status') == 'success'
        # Never trust the amount we asked for — credit what Paystack says was paid.
        paid_kobo = (data.get('data') or {}).get('amount')
        if not paid or paid_kobo is None or Decimal(paid_kobo) / 100 != tx.amount:
            tx.status = 'FAILED'
            tx.save(update_fields=['status', 'updated_at'])
            raise ValueError("Payment verification failed.")

        tx, _ = ledger.credit(
            tx.user, tx.amount, 'FUNDING', f"paystack:{reference}",
            pending=tx, narration='Card funding (Paystack)',
            metadata={'channel': 'paystack'},
        )
        facility = tx.user.facility
        facility.refresh_from_db(fields=['wallet_balance'])

        return {
            'status': 'success',
            'message': 'Wallet funded successfully.',
            'amount': str(tx.amount),
            'new_balance': str(facility.wallet_balance),
        }


class WalletWithdrawalService(BaseService):
    """Professional withdraws from their wallet to a Nigerian bank account."""

    def __call__(self, user, amount, bank_code=None, account_number=None, account_name=None):
        if not user.is_professional:
            raise PermissionError("Only professionals can withdraw.")

        amount_decimal = ledger.to_money(amount)
        if amount_decimal < settings.WITHDRAWAL_MIN_AMOUNT:
            raise ValueError(f"Minimum withdrawal is ₦{settings.WITHDRAWAL_MIN_AMOUNT:,.2f}.")
        if amount_decimal > settings.WITHDRAWAL_MAX_AMOUNT:
            raise ValueError(f"Maximum withdrawal is ₦{settings.WITHDRAWAL_MAX_AMOUNT:,.2f}.")
        if not (bank_code and account_number):
            raise ValueError("bank_code and account_number are required.")
        if not re.match(r'^\d{10}$', str(account_number)):
            raise ValueError("Account number must be 10 digits.")

        wallet = EmbedlyWallet.objects.filter(user=user, status=EmbedlyWallet.ACTIVE).first()
        if wallet is None:
            raise ValueError("Complete wallet verification before withdrawing.")

        # Pay the name the bank returns, not whatever the client sent.
        enquiry = embedly_client.name_enquiry(str(account_number), str(bank_code))
        resolved_name = (enquiry.data or {}).get('accountName') if enquiry.ok else None
        if not resolved_name:
            raise ValueError("Could not verify that bank account. Check the details and try again.")

        # Whole naira to the bank; the platform fee absorbs the kobo.
        fee_rate = settings.WITHDRAWAL_FEE_PERCENT / 100
        net_amount = (amount_decimal * (1 - fee_rate)).quantize(Decimal('1'), rounding=ROUND_DOWN)
        platform_charge = amount_decimal - net_amount
        reference = uuid.uuid4().hex

        with transaction.atomic():
            tx, _ = ledger.debit(
                user, amount_decimal, 'WITHDRAWAL', f"withdrawal:{reference}",
                reference=reference, settle=False, status='PROCESSING',
                metadata={
                    'bank_code': str(bank_code), 'account_number': str(account_number),
                    'account_name': resolved_name, 'net_amount': str(net_amount),
                    'fee': str(platform_charge),
                },
            )
            ledger.queue_transfer(
                kind=WalletTransfer.BANK_PAYOUT, amount=net_amount, reference=f"SHF-{reference}",
                source_user=user, destination_account=str(account_number),
                destination_bank_code=str(bank_code), destination_account_name=resolved_name,
                narration='Shifta withdrawal', ledger_transaction=tx,
            )
            if platform_charge > 0:
                ledger.queue_transfer(
                    kind=WalletTransfer.WALLET_TO_WALLET, amount=platform_charge,
                    reference=f"SHF-FEE-{reference}", source_user=user,
                    narration='Withdrawal fee', ledger_transaction=tx,
                )

        return {
            'status': 'processing',
            'message': 'Withdrawal initiated successfully.',
            'reference': reference,
            'amount': str(amount_decimal),
            'charge': str(platform_charge),
            'net_amount': str(net_amount),
            'account_name': resolved_name,
            'transaction_id': str(tx.id),
        }


@transaction.atomic
def reverse_failed_withdrawal(transfer):
    """
    The bank payout failed, so the money never left the professional's Embedly
    wallet: restore the app balance and send back the fee already taken.
    """
    tx = Transaction.objects.select_for_update().get(pk=transfer.ledger_transaction_id)
    if tx.status == 'FAILED':
        return
    tx.status = 'FAILED'
    tx.save(update_fields=['status', 'updated_at'])

    ledger.credit(
        tx.user, tx.amount, 'REVERSAL', f"withdrawal-reversal:{tx.reference}",
        settle=False, metadata={'withdrawal': tx.reference, 'reason': transfer.last_error[:200]},
    )

    fee_transfers = WalletTransfer.objects.select_for_update().filter(
        ledger_transaction=tx, reference__startswith=f"SHF-FEE-{tx.reference}",
    )
    for fee in fee_transfers:
        if fee.status == WalletTransfer.QUEUED:
            # Never sent: cancel instead of refunding money that didn't move.
            fee.status = WalletTransfer.FAILED
            fee.last_error = 'Cancelled: withdrawal reversed'
            fee.completed_at = timezone.now()
            fee.save(update_fields=['status', 'last_error', 'completed_at', 'updated_at'])
        elif fee.status == WalletTransfer.SUCCESS:
            ledger.queue_transfer(
                kind=WalletTransfer.WALLET_TO_WALLET, amount=fee.amount,
                reference=f"SHF-FEEREV-{tx.reference}", destination_user=tx.user,
                narration='Withdrawal fee refund', ledger_transaction=tx,
            )
        else:
            # In flight with an unknown outcome — a human decides.
            fee.status = WalletTransfer.NEEDS_REVIEW
            fee.last_error = 'Withdrawal reversed while this fee transfer was unconfirmed'
            fee.save(update_fields=['status', 'last_error', 'updated_at'])
