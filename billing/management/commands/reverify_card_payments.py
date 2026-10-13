"""
Re-check Paystack card/bank-transfer fundings that never got credited.

    python manage.py reverify_card_payments            # last 30 days, dry run
    python manage.py reverify_card_payments --apply    # credit the valid ones
    python manage.py reverify_card_payments --days 90 --apply

Use after an outage or a verification bug (e.g. the release that rejected
payments where Paystack added its fee on top). Each FAILED/PENDING funding is
verified with Paystack again; only payments Paystack confirms are credited, and
the ledger's idempotency key means none can be credited twice.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from billing.models import Transaction
from billing.wallet_service import WalletFundingVerifyService


class Command(BaseCommand):
    help = "Re-verify uncredited Paystack fundings and credit the ones Paystack confirms."

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30, help='How far back to look (default 30).')
        parser.add_argument('--apply', action='store_true', help='Credit valid payments (default: dry run).')

    def handle(self, *args, **options):
        since = timezone.now() - timedelta(days=options['days'])
        candidates = Transaction.objects.filter(
            transaction_type='FUNDING', status__in=('FAILED', 'PENDING'),
            idempotency_key__isnull=True,  # not yet credited through the ledger
            created_at__gte=since,
        ).select_related('user').order_by('created_at')

        credited = skipped = 0
        for tx in candidates:
            label = f"{tx.created_at:%Y-%m-%d %H:%M} {tx.user.email:<38} {tx.amount:>12} {tx.reference}"
            if not options['apply']:
                self.stdout.write(f"would check  {label}")
                continue
            try:
                result = WalletFundingVerifyService()(tx.reference)
            except ValueError as exc:
                skipped += 1
                self.stdout.write(self.style.WARNING(f"not paid     {label}  ({exc})"))
                continue
            credited += 1
            self.stdout.write(self.style.SUCCESS(f"credited     {label}  ({result['status']})"))

        if not options['apply']:
            self.stdout.write(f"\n{candidates.count()} payment(s) to check. Re-run with --apply to credit valid ones.")
        else:
            self.stdout.write(f"\nCredited {credited}; {skipped} not confirmed by Paystack (abandoned or unpaid).")
