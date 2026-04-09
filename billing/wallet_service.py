"""
Wallet Service - handles wallet creation, funding, and payouts via Embedly.
"""
import uuid
import logging
from decimal import Decimal
from django.db import transaction
from django.conf import settings
from core.services import BaseService
from .embedly_client import embedly_client
from .models import EmbedlyWallet, Transaction

logger = logging.getLogger(__name__)


class WalletCreateService(BaseService):
    """Creates an Embedly wallet for a user during registration."""

    def __call__(self, user):
        # Check if wallet already exists
        if EmbedlyWallet.objects.filter(user=user).exists():
            return EmbedlyWallet.objects.get(user=user)

        # 1. Create customer on Embedly
        customer_response = embedly_client.create_customer(
            first_name=user.first_name or 'User',
            last_name=user.last_name or user.email.split('@')[0],
            email=user.email,
            phone_number=user.phone_number or '',
        )

        if not customer_response['success']:
            logger.error(f"Failed to create Embedly customer for {user.email}: {customer_response['data']}")
            # Create a local-only wallet record so we can retry later
            wallet = EmbedlyWallet.objects.create(
                user=user,
                provider='EMBEDLY',
                is_active=False,
                meta={'error': 'customer_creation_failed', 'response': customer_response['data']},
            )
            return wallet

        customer_data = customer_response['data']
        customer_id = customer_data.get('data', {}).get('id') or customer_data.get('id')

        if not customer_id:
            logger.error(f"No customer ID in response for {user.email}: {customer_data}")
            wallet = EmbedlyWallet.objects.create(
                user=user,
                provider='EMBEDLY',
                is_active=False,
                meta={'error': 'no_customer_id', 'response': customer_data},
            )
            return wallet

        # 2. Create wallet on Embedly
        wallet_response = embedly_client.create_wallet(customer_id)

        if not wallet_response['success']:
            logger.error(f"Failed to create Embedly wallet for {user.email}: {wallet_response['data']}")
            wallet = EmbedlyWallet.objects.create(
                user=user,
                customer_id=customer_id,
                provider='EMBEDLY',
                is_active=False,
                meta={'error': 'wallet_creation_failed', 'response': wallet_response['data']},
            )
            return wallet

        wallet_data = wallet_response['data']
        wallet_info = wallet_data.get('data', wallet_data)

        wallet = EmbedlyWallet.objects.create(
            user=user,
            customer_id=customer_id,
            wallet_id=wallet_info.get('id', ''),
            account_number=wallet_info.get('accountNumber', ''),
            account_name=wallet_info.get('accountName', ''),
            bank_name=wallet_info.get('bankName', ''),
            bank_code=wallet_info.get('bankCode', ''),
            provider='EMBEDLY',
            is_active=True,
            meta=wallet_info,
        )

        logger.info(f"Created Embedly wallet {wallet.account_number} for {user.email}")
        return wallet


class WalletFundingService(BaseService):
    """Initialize Paystack payment to fund facility wallet."""

    @transaction.atomic
    def __call__(self, user, amount):
        if not user.is_facility:
            raise PermissionError("Only facilities can fund their wallet.")

        amount_decimal = Decimal(str(amount))
        if amount_decimal <= 0:
            raise ValueError("Amount must be greater than zero.")

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
        import requests
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
    """Verify Paystack payment and credit facility wallet."""

    @transaction.atomic
    def __call__(self, reference):
        try:
            tx = Transaction.objects.select_for_update().get(reference=reference)
        except Transaction.DoesNotExist:
            raise ValueError("Transaction not found.")

        if tx.status == 'SUCCESS':
            return {'status': 'already_verified', 'message': 'Payment already processed.'}

        # Verify with Paystack
        import requests
        verify_url = f'https://api.paystack.co/transaction/verify/{reference}'
        headers = {'Authorization': f'Bearer {settings.PAYSTACK_SECRET_KEY}'}

        try:
            response = requests.get(verify_url, headers=headers, timeout=30)
            data = response.json()
        except requests.exceptions.RequestException:
            raise ValueError("Could not verify payment. Please try again.")

        if not data.get('status') or data['data']['status'] != 'success':
            tx.status = 'FAILED'
            tx.save()
            raise ValueError("Payment verification failed.")

        # Credit the facility wallet
        user = tx.user
        if user.is_facility:
            facility = user.facility
            facility.wallet_balance += tx.amount
            facility.save()

        tx.status = 'SUCCESS'
        tx.save()

        return {
            'status': 'success',
            'message': 'Wallet funded successfully.',
            'amount': str(tx.amount),
            'new_balance': str(user.facility.wallet_balance) if user.is_facility else '0',
        }


class WalletWithdrawalService(BaseService):
    """Process professional withdrawal from org wallet via Embedly payout."""

    PLATFORM_CHARGE_PERCENT = Decimal('2.5')  # 2.5% platform charge

    @transaction.atomic
    def __call__(self, user, amount, bank_code=None, account_number=None, account_name=None):
        if not user.is_professional:
            raise PermissionError("Only professionals can withdraw.")

        professional = user.professional
        amount_decimal = Decimal(str(amount))

        if amount_decimal <= 0:
            raise ValueError("Amount must be greater than zero.")

        # Calculate charges
        platform_charge = (amount_decimal * self.PLATFORM_CHARGE_PERCENT / 100).quantize(Decimal('0.01'))
        net_amount = amount_decimal - platform_charge

        if professional.wallet_balance < amount_decimal:
            raise ValueError(
                f"Insufficient funds. Available: {professional.wallet_balance}, Requested: {amount_decimal}"
            )

        reference = str(uuid.uuid4()).replace('-', '')

        # Deduct from professional wallet
        professional.wallet_balance -= amount_decimal
        professional.save()

        # Create transaction record
        tx = Transaction.objects.create(
            user=user,
            amount=amount_decimal,
            transaction_type='WITHDRAWAL',
            reference=reference,
            status='PROCESSING',
        )

        # If bank details provided, process payout via Embedly from org wallet
        if bank_code and account_number:
            org_account = settings.EMBEDLY_DEFAULT_WALLET_ACCOUNT_NUMBER
            payout_response = embedly_client.process_payout(
                source_account=org_account,
                destination_account_number=account_number,
                destination_bank_code=bank_code,
                destination_account_name=account_name or '',
                amount=float(net_amount),
                reference=reference,
                narration=f'Shifta payout to {user.email}',
            )

            if payout_response['success']:
                tx.status = 'PROCESSING'
                tx.save()
            else:
                # Reverse the deduction
                professional.wallet_balance += amount_decimal
                professional.save()
                tx.status = 'FAILED'
                tx.save()
                raise ValueError("Payout failed. Please try again.")
        else:
            # No bank details - just mark as pending (wallet-to-wallet or future bank link)
            tx.status = 'PENDING'
            tx.save()

        return {
            'status': tx.status.lower(),
            'message': 'Withdrawal initiated successfully.',
            'reference': reference,
            'amount': str(amount_decimal),
            'charge': str(platform_charge),
            'net_amount': str(net_amount),
            'transaction_id': str(tx.id),
        }
