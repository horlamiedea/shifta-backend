from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from decimal import Decimal
from core.router import route
from .models import Invoice, Transaction
from .services import WithdrawalService, ReleaseFundsService

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

@route("billing/withdraw/", name="withdraw")
class WithdrawalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        amount = request.data.get("amount")
        if not amount:
            return Response({"error": "Amount is required"}, status=400)
            
        service = WithdrawalService()
        result = service(user=request.user, amount=Decimal(amount))
        return Response(result)

@route("billing/release-funds/<uuid:application_id>/", name="release-funds")
class ReleaseFundsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, application_id):
        service = ReleaseFundsService()
        result = service(user=request.user, application_id=application_id)
        return Response(result)

