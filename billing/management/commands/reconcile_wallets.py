"""
Check that money on Embedly tallies with the app.

Professionals: each Embedly wallet against its app balance. Facilities have no
Embedly wallets, so the platform wallet must cover their balances, shift
escrow and pending payouts (see billing.reconciliation.platform_cover).

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
from billing.reconciliation import (
    _owner_balance, platform_cover, professional_wallets, queue_opening_balance, reconcile_wallet,
)


class Command(BaseCommand):
    help = "Compare app wallet balances with Embedly wallet balances."

    def add_arguments(self, parser):
        parser.add_argument('--queue-opening-balances', action='store_true',
                            help='Fund pre-existing app balances from the platform wallet (once per user).')

    def handle(self, *args, **options):
        if options['queue_opening_balances']:
            for wallet in professional_wallets():
                with transaction.atomic():
                    _, note = queue_opening_balance(wallet)
                self.stdout.write(f"opening balance {wallet.user.email}: {note}")
            self.stdout.write('')

        header = f"{'user':<40} {'app':>14} {'expected':>14} {'embedly':>14} {'drift':>12}"
        self.stdout.write(header)
        self.stdout.write('-' * len(header))
        drifted = 0
        for wallet in professional_wallets():
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

        pending = EmbedlyWallet.objects.filter(user__professional__isnull=False) \
            .exclude(status=EmbedlyWallet.ACTIVE).count()
        review = WalletTransfer.objects.filter(status=WalletTransfer.NEEDS_REVIEW).count()
        open_count = WalletTransfer.objects.filter(status__in=(WalletTransfer.QUEUED, WalletTransfer.PROCESSING)).count()
        self.stdout.write('')
        self.stdout.write(f"Professional wallets drifting: {drifted} | not yet active: {pending} | "
                          f"transfers in flight: {open_count} | needing review: {review}")

        # Facilities have no Embedly wallets: the platform wallet must hold their money.
        cover = platform_cover()
        self.stdout.write('')
        self.stdout.write(f"Platform wallet {cover['platform_account'] or '(not configured)'}: "
                          f"{cover['platform_balance']}")
        for label, key in (('  facility balances', 'facility_balances'), ('  escrow on open shifts', 'shift_escrow'),
                           ('  completed work not yet paid', 'unpaid_completed_work'),
                           ('  queued to professionals', 'pending_to_professionals'), ('  must hold at least', 'required')):
            self.stdout.write(f"{label:<32} {cover[key]:>14}")
        if cover['surplus'] is None:
            self.stdout.write(self.style.ERROR("Platform wallet unreachable or not set up."))
        elif cover['surplus'] < 0:
            self.stdout.write(self.style.ERROR(
                f"SHORTFALL {-cover['surplus']}: top up the platform wallet from the Paystack settlement account."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Covered, with {cover['surplus']} of Shifta's own funds."))
