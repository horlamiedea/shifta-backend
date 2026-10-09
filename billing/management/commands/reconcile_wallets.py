"""
Check that every Embedly wallet tallies with the app wallet.

    python manage.py reconcile_wallets
    python manage.py reconcile_wallets --queue-opening-balances

For each active wallet it prints the app balance, what Embedly should hold
(app balance adjusted for transfers still in flight), what Embedly actually
holds, and the drift.

--queue-opening-balances backs app balances that predate the user's Embedly
wallet (seeded or pre-integration balances) with a one-off transfer from the
platform wallet. It runs at most once per user and needs the platform wallet
to hold enough money.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from billing.models import EmbedlyWallet, WalletTransfer
from billing.platform import platform_account_number
from billing.reconciliation import (
    _owner_balance, provider_balance, queue_opening_balance, reconcile_wallet,
)


class Command(BaseCommand):
    help = "Compare app wallet balances with Embedly wallet balances."

    def add_arguments(self, parser):
        parser.add_argument('--queue-opening-balances', action='store_true',
                            help='Fund pre-existing app balances from the platform wallet (once per user).')

    def handle(self, *args, **options):
        if options['queue_opening_balances']:
            for wallet in EmbedlyWallet.objects.filter(status=EmbedlyWallet.ACTIVE).select_related('user'):
                with transaction.atomic():
                    _, note = queue_opening_balance(wallet)
                self.stdout.write(f"opening balance {wallet.user.email}: {note}")
            self.stdout.write('')

        header = f"{'user':<40} {'app':>14} {'expected':>14} {'embedly':>14} {'drift':>12}"
        self.stdout.write(header)
        self.stdout.write('-' * len(header))
        drifted = 0
        for wallet in EmbedlyWallet.objects.filter(status=EmbedlyWallet.ACTIVE).select_related('user'):
            drift = reconcile_wallet(wallet)
            wallet.refresh_from_db()
            app = _owner_balance(wallet.user)
            if drift is None:
                self.stdout.write(self.style.ERROR(f"{wallet.user.email:<40} {app:>14} {'':>14} {'unreachable':>14}"))
                continue
            line = (f"{wallet.user.email:<40} {app:>14} {wallet.expected_provider_balance:>14} "
                    f"{wallet.provider_balance:>14} {drift:>12}")
            if drift:
                drifted += 1
                self.stdout.write(self.style.WARNING(line))
            else:
                self.stdout.write(self.style.SUCCESS(line))

        pending = EmbedlyWallet.objects.exclude(status=EmbedlyWallet.ACTIVE).count()
        review = WalletTransfer.objects.filter(status=WalletTransfer.NEEDS_REVIEW).count()
        open_count = WalletTransfer.objects.filter(status__in=(WalletTransfer.QUEUED, WalletTransfer.PROCESSING)).count()
        platform = platform_account_number()
        platform_balance = provider_balance(platform) if platform else None

        self.stdout.write('')
        self.stdout.write(f"Platform wallet {platform or '(not configured)'}: {platform_balance}")
        self.stdout.write(f"Wallets drifting: {drifted} | not yet active: {pending} | "
                          f"transfers in flight: {open_count} | needing review: {review}")
