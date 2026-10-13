import logging

from django.core.cache import cache
from django.http import Http404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from decimal import Decimal
from core.router import route
from .models import Invoice, Transaction, EmbedlyWallet
from .serializers import TransactionSerializer, WalletKycSerializer, WithdrawalSerializer, AmountSerializer
from .services import ReleaseFundsService
from .wallet_service import (
    WalletFundingService, WalletFundingVerifyService, WalletWithdrawalService, WalletKycService,
    ensure_wallet_record,
)
from .embedly_client import embedly_client, is_staging, verify_webhook_signature
from .idempotency import idempotent
from . import webhooks
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, inline_serializer
from rest_framework import serializers

logger = logging.getLogger(__name__)


class MoneyWriteThrottleMixin:
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'wallet_write'


def _first_error(serializer):
    field, errors = next(iter(serializer.errors.items()))
    return f"{field}: {errors[0]}" if field != 'non_field_errors' else str(errors[0])

@extend_schema(
    responses={
        200: inline_serializer(
            name='InvoiceListResponse',
            many=True,
            fields={
                'id': serializers.UUIDField(),
                'month': serializers.CharField(),
                'amount': serializers.DecimalField(max_digits=12, decimal_places=2),
                'status': serializers.CharField(),
                'pdf_url': serializers.URLField()
            }
        ),
        403: inline_serializer(name='InvoicePermissionError', fields={'error': serializers.CharField()})
    }
)

@route("billing/invoices/", name="invoice-list")
class InvoiceListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_facility:
             return Response({"error": "Only facilities have invoices"}, status=403)
             
        invoices = Invoice.objects.filter(facility=request.user.facility).order_by('-created_at')
        data = [{
            "id": i.id,
            "month": i.month,
            "amount": i.amount,
            "status": i.status,
            "pdf_url": i.pdf_url
        } for i in invoices]
        return Response(data)

@extend_schema(responses={200: TransactionSerializer(many=True)})
@route("billing/transactions/", name="transaction-list")
class TransactionListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        transactions = (
            Transaction.objects
            .filter(user=request.user)
            .select_related('shift__facility')
            .order_by('-created_at')
        )
        serializer = TransactionSerializer(
            transactions, many=True, context={'request': request},
        )
        return Response(serializer.data)

@extend_schema(
    responses={
        200: inline_serializer(
            name='ReleaseFundsResponse',
            fields={'status': serializers.CharField()}
        )
    }
)
@route("billing/release-funds/<uuid:application_id>/", name="release-funds")
class ReleaseFundsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, application_id):
        try:
            result = ReleaseFundsService()(user=request.user, application_id=application_id)
        except PermissionError as e:
            return Response({'error': str(e)}, status=403)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)
        return Response(result)


# ============================================
# WALLET ENDPOINTS
# ============================================

def _wallet_payload(wallet):
    return {
        'status': wallet.status,
        'is_active': wallet.status == EmbedlyWallet.ACTIVE,
        # Fund the wallet by bank transfer to this account.
        'account_number': wallet.account_number,
        'account_name': wallet.account_name,
        'bank_name': wallet.bank_name,
        'kyc_methods': (wallet.meta.get('kyc') or {}).get('methods', []),
        # Open in a browser/webview so the user can take the KYC selfie.
        'liveness_url': wallet.meta.get('liveness_url') if wallet.status == EmbedlyWallet.PENDING_LIVENESS else None,
        'last_error': wallet.meta.get('last_error') or None,
        'message': {
            EmbedlyWallet.PENDING_KYC: 'Verify your identity to open your wallet.',
            EmbedlyWallet.PENDING_LIVENESS: 'Take a quick selfie to finish verifying your identity.',
            EmbedlyWallet.PENDING_TIER: 'Verification received. Your wallet will open once the provider approves it.',
            EmbedlyWallet.ACTIVE: 'Fund your wallet by bank transfer to the account above.',
            EmbedlyWallet.FAILED: 'We could not open your wallet. Contact support.',
        }.get(wallet.status, ''),
    }


@route("billing/wallet/", name="wallet-balance")
class WalletBalanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        balance = Decimal('0.00')

        if user.is_professional:
            balance = user.professional.wallet_balance
        elif user.is_facility:
            balance = user.facility.wallet_balance

        # Only professionals have an Embedly wallet; a facility's money is held
        # in the platform wallet and funded through Paystack.
        wallet_info = _wallet_payload(ensure_wallet_record(user)) if user.is_professional else None

        return Response({
            'balance': str(balance),
            'currency': 'NGN',
            'wallet': wallet_info,
            # Staging only: POST billing/wallet/simulate-inflow/ adds test money.
            'sandbox': is_staging(),
        })


@route("billing/wallet/kyc/", name="wallet-kyc")
class WalletKycView(MoneyWriteThrottleMixin, APIView):
    """
    Open a professional's Embedly wallet:
    {bvn and/or nin, dob: YYYY-MM-DD, address, city, middle_name?}
    -> may answer status PENDING_LIVENESS with `liveness_url`; open it for the
       selfie. POST again (empty body) for a fresh link.
    Facilities don't have wallets (they fund by card or bank transfer).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = WalletKycSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'error': _first_error(serializer)}, status=400)
        data = serializer.validated_data
        try:
            wallet = WalletKycService()(
                request.user, bvn=data.get('bvn') or None, nin=data.get('nin') or None,
                dob=data.get('dob'), address=data.get('address'), city=data.get('city'),
                middle_name=data.get('middle_name', ''),
            )
        except PermissionError as e:
            return Response({'error': str(e)}, status=403)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)
        return Response({'wallet': _wallet_payload(wallet)})


@route("billing/wallet/fund/", name="wallet-fund")
class WalletFundView(MoneyWriteThrottleMixin, APIView):
    """Card funding via Paystack. Bank transfer to the wallet's account needs no call."""
    permission_classes = [IsAuthenticated]

    @idempotent('wallet-fund')
    def post(self, request):
        serializer = AmountSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'error': _first_error(serializer)}, status=400)

        try:
            service = WalletFundingService()
            result = service(user=request.user, amount=serializer.validated_data['amount'])
            return Response(result)
        except PermissionError as e:
            return Response({'error': str(e)}, status=403)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)


@route("billing/wallet/fund/verify/", name="wallet-fund-verify")
class WalletFundVerifyView(MoneyWriteThrottleMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        reference = request.data.get('reference')
        if not reference:
            return Response({'error': 'Reference is required.'}, status=400)

        try:
            service = WalletFundingVerifyService()
            result = service(reference=reference, user=request.user)
            return Response(result)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)


@route("billing/withdraw/", name="withdraw")
class WithdrawalView(MoneyWriteThrottleMixin, APIView):
    """
    Withdraw to a bank account. Send an `Idempotency-Key` header (a UUID per
    withdrawal attempt) so retries and double-taps can't withdraw twice.
    """
    permission_classes = [IsAuthenticated]

    @idempotent('withdraw')
    def post(self, request):
        serializer = WithdrawalSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'error': _first_error(serializer)}, status=400)
        data = serializer.validated_data

        try:
            service = WalletWithdrawalService()
            result = service(
                user=request.user,
                amount=data['amount'],
                bank_code=data['bank_code'],
                account_number=data['account_number'],
            )
            return Response(result)
        except PermissionError as e:
            return Response({'error': str(e)}, status=403)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)


@route("billing/banks/", name="bank-list")
class BankListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        banks = cache.get('embedly:banks')
        if banks is None:
            result = embedly_client.get_banks()
            if not result.ok:
                return Response({'error': 'Could not fetch banks.'}, status=502)
            banks = [
                {'bank_name': b.get('bankname'), 'bank_code': b.get('bankcode')}
                for b in (result.data or [])
            ]
            cache.set('embedly:banks', banks, 6 * 60 * 60)
        return Response(banks)


@route("billing/bank-enquiry/", name="bank-enquiry")
class BankEnquiryView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'bank_lookup'

    def post(self, request):
        account_number = str(request.data.get('account_number') or '')
        bank_code = str(request.data.get('bank_code') or '')
        if not account_number or not bank_code:
            return Response({'error': 'account_number and bank_code are required.'}, status=400)

        result = embedly_client.name_enquiry(account_number, bank_code)
        if result.ok and (result.data or {}).get('accountName'):
            return Response({
                'account_number': account_number,
                'bank_code': bank_code,
                'account_name': result.data['accountName'],
            })
        return Response({'error': 'Bank enquiry failed.'}, status=400)


@route("billing/embedly/webhook/", name="embedly-webhook")
class EmbedlyWebhookView(APIView):
    """
    Embedly webhook: register https://<your-domain>/api/v1/billing/embedly/webhook/
    on the Embedly dashboard (APIs & Integration). Answers 200 once the event is
    stored; processing problems are retried by the sweeper, not by Embedly.
    """
    permission_classes = []
    authentication_classes = []

    def post(self, request):
        raw_body = request.body
        signature = request.headers.get('x-embedly-signature', '')
        if not verify_webhook_signature(raw_body, signature):
            # Header names only (never values) — Embedly's docs disagree on the scheme.
            logger.warning(
                "Rejected Embedly webhook: bad or missing signature. Headers present: %s",
                sorted(k for k in request.headers.keys()),
            )
            return Response({'error': 'Invalid signature'}, status=401)

        payload = request.data if isinstance(request.data, dict) else {}
        if not payload.get('event'):
            return Response({'error': 'Malformed payload'}, status=400)

        event, _ = webhooks.record_event(payload, raw_body)
        webhooks.process_event(event.pk)
        return Response({'status': 'ok'})


@route("billing/wallet/simulate-inflow/", name="wallet-simulate-inflow")
class SimulateInflowView(APIView):
    """
    STAGING ONLY. Ask Embedly to simulate a bank transfer into your own wallet.
    Embedly then calls the webhook above, which credits the app wallet — the
    same path a real transfer takes. Returns 404 in production.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_staging():
            raise Http404
        serializer = AmountSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'error': _first_error(serializer)}, status=400)
        if request.user.is_facility:
            return Response({'error': 'Facilities fund through Paystack; use its test mode on staging.'}, status=400)
        wallet = EmbedlyWallet.objects.filter(user=request.user, status=EmbedlyWallet.ACTIVE).first()
        if wallet is None:
            return Response({'error': 'Your wallet is not active yet.'}, status=400)

        result = embedly_client.simulate_inflow(
            wallet.account_number, wallet.account_name or request.user.email,
            serializer.validated_data['amount'], narration='Shifta staging test inflow',
        )
        if not result.ok:
            return Response({'error': f'Embedly: {result.message}'}, status=502)
        return Response({'status': 'ok', 'message': 'Inflow simulated; the webhook will credit your wallet.'})


@route("billing/paystack/webhook/", name="paystack-webhook")
class PaystackWebhookView(APIView):
    """Handle Paystack webhook for payment verification."""
    permission_classes = []
    authentication_classes = []

    def post(self, request):
        import hashlib
        import hmac
        from django.conf import settings

        # Verify webhook signature
        payload = request.body
        signature = request.headers.get('x-paystack-signature', '')
        expected = hmac.new(
            (settings.PAYSTACK_SECRET_KEY or '').encode('utf-8'),
            payload,
            hashlib.sha512
        ).hexdigest()

        if not settings.PAYSTACK_SECRET_KEY or not hmac.compare_digest(expected, signature):
            return Response({'error': 'Invalid signature'}, status=400)

        data = request.data
        event = data.get('event')

        if event == 'charge.success':
            reference = data.get('data', {}).get('reference')
            if reference:
                try:
                    service = WalletFundingVerifyService()
                    service(reference=reference)
                except ValueError:
                    logger.warning("Paystack webhook for %s could not be verified", reference)

        return Response({'status': 'ok'})
