from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.authentication import TokenAuthentication
from decimal import Decimal
from core.router import route
from .models import Invoice, Transaction, EmbedlyWallet
from .services import WithdrawalService, ReleaseFundsService
from .wallet_service import WalletFundingService, WalletFundingVerifyService, WalletWithdrawalService, WalletCreateService
from .embedly_client import embedly_client
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, inline_serializer
from rest_framework import serializers

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

@extend_schema(
    responses={
        200: inline_serializer(
            name='TransactionListResponse',
            many=True,
            fields={
                'id': serializers.UUIDField(),
                'type': serializers.CharField(),
                'amount': serializers.DecimalField(max_digits=12, decimal_places=2),
                'status': serializers.CharField(),
                'created_at': serializers.DateTimeField()
            }
        )
    }
)
@route("billing/transactions/", name="transaction-list")
class TransactionListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        transactions = Transaction.objects.filter(user=request.user).order_by('-created_at')
        data = [{
            "id": t.id,
            "type": t.transaction_type,
            "amount": t.amount,
            "status": t.status,
            "created_at": t.created_at
        } for t in transactions]
        return Response(data)

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
        service = ReleaseFundsService()
        result = service(user=request.user, application_id=application_id)
        return Response(result)


# ============================================
# WALLET ENDPOINTS
# ============================================

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

        # Get Embedly wallet info
        wallet_info = {}
        try:
            embedly_wallet = EmbedlyWallet.objects.get(user=user)
            wallet_info = {
                'account_number': embedly_wallet.account_number,
                'account_name': embedly_wallet.account_name,
                'bank_name': embedly_wallet.bank_name,
                'is_active': embedly_wallet.is_active,
            }
        except EmbedlyWallet.DoesNotExist:
            pass

        return Response({
            'balance': str(balance),
            'currency': 'NGN',
            'wallet': wallet_info,
        })


@route("billing/wallet/fund/", name="wallet-fund")
class WalletFundView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        amount = request.data.get('amount')
        if not amount:
            return Response({'error': 'Amount is required.'}, status=400)

        try:
            service = WalletFundingService()
            result = service(user=request.user, amount=amount)
            return Response(result)
        except PermissionError as e:
            return Response({'error': str(e)}, status=403)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)


@route("billing/wallet/fund/verify/", name="wallet-fund-verify")
class WalletFundVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        reference = request.data.get('reference')
        if not reference:
            return Response({'error': 'Reference is required.'}, status=400)

        try:
            service = WalletFundingVerifyService()
            result = service(reference=reference)
            return Response(result)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)


@route("billing/withdraw/", name="withdraw")
class WithdrawalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        amount = request.data.get('amount')
        if not amount:
            return Response({'error': 'Amount is required.'}, status=400)

        bank_code = request.data.get('bank_code')
        account_number = request.data.get('account_number')
        account_name = request.data.get('account_name')

        try:
            service = WalletWithdrawalService()
            result = service(
                user=request.user,
                amount=amount,
                bank_code=bank_code,
                account_number=account_number,
                account_name=account_name,
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
        result = embedly_client.get_banks()
        if result['success']:
            return Response(result['data'])
        return Response({'error': 'Could not fetch banks.'}, status=502)


@route("billing/bank-enquiry/", name="bank-enquiry")
class BankEnquiryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        account_number = request.data.get('account_number')
        bank_code = request.data.get('bank_code')
        if not account_number or not bank_code:
            return Response({'error': 'account_number and bank_code are required.'}, status=400)

        result = embedly_client.bank_enquiry(account_number, bank_code)
        if result['success']:
            return Response(result['data'])
        return Response({'error': 'Bank enquiry failed.'}, status=400)


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
            settings.PAYSTACK_SECRET_KEY.encode('utf-8'),
            payload,
            hashlib.sha512
        ).hexdigest()

        if not hmac.compare_digest(expected, signature):
            return Response({'error': 'Invalid signature'}, status=400)

        data = request.data
        event = data.get('event')

        if event == 'charge.success':
            reference = data.get('data', {}).get('reference')
            if reference:
                try:
                    service = WalletFundingVerifyService()
                    service(reference=reference)
                except Exception:
                    pass

        return Response({'status': 'ok'})

