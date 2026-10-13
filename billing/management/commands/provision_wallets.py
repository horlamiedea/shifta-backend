"""
Create Embedly wallets for every professional. (Facilities have none: their
money is held in the platform wallet and they fund through Paystack.)

    python manage.py provision_wallets
    python manage.py provision_wallets --staging-test-kyc   # staging only

Embedly opens a wallet only for a customer with a KYC tier (BVN/NIN + a
liveness selfie), so for each professional this:
  - ensures a local wallet record exists,
  - retries opening the wallet for users who already passed KYC,
  - and reports who still has to submit KYC (POST /api/v1/billing/wallet/kyc/).

--staging-test-kyc fills in a staging test identity (a placeholder BVN unique
to each user — staging accepts any 11 digits but links each BVN to one
customer — plus a placeholder date of birth and address) for professionals who
haven't done KYC, and runs staging's mocked liveness check, so seeded/test
accounts can get wallets on staging. It refuses to run against production.
"""
import hashlib
from collections import Counter
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from billing.embedly_client import is_staging
from billing.models import EmbedlyWallet
from billing.wallet_service import WalletKycService, activate_wallet, ensure_wallet_record


def staging_test_bvn(user):
    """Stable per user, so a rerun reuses the same placeholder BVN."""
    digits = str(int(hashlib.sha256(str(user.pk).encode()).hexdigest(), 16))
    return '22' + digits[:9]


class Command(BaseCommand):
    help = "Create Embedly wallets for all professionals."

    def add_arguments(self, parser):
        parser.add_argument(
            '--staging-test-kyc', action='store_true',
            help="Staging only: submit Embedly's test identity for users without KYC.",
        )

    def handle(self, *args, **options):
        test_kyc = options['staging_test_kyc']
        if test_kyc and not is_staging():
            raise CommandError("--staging-test-kyc only runs against Embedly staging.")

        users = User.objects.filter(professional__isnull=False).select_related('professional').order_by('email')

        summary = Counter()
        for user in users:
            wallet = ensure_wallet_record(user)
            note = ''
            try:
                if test_kyc and wallet.status != EmbedlyWallet.ACTIVE:
                    # Verified IDs resume at the (mocked) selfie step.
                    wallet = self._test_kyc(user)
                elif wallet.status == EmbedlyWallet.PENDING_TIER:
                    wallet = activate_wallet(wallet.pk)
            except (ValueError, PermissionError) as exc:
                note = str(exc)
            wallet.refresh_from_db()
            note = note or wallet.meta.get('last_error', '')
            summary[wallet.status] += 1

            style = self.style.SUCCESS if wallet.status == EmbedlyWallet.ACTIVE else self.style.WARNING
            self.stdout.write(style(
                f"{wallet.status:<16} {user.email:<40} {wallet.account_number or '':<12} {note}"
            ))

        self.stdout.write('')
        self.stdout.write(', '.join(f"{status}: {count}" for status, count in sorted(summary.items())))
        if summary[EmbedlyWallet.PENDING_KYC]:
            self.stdout.write(
                "PENDING_KYC users must submit KYC via POST /api/v1/billing/wallet/kyc/ "
                "(or use --staging-test-kyc on staging)."
            )
        if summary[EmbedlyWallet.PENDING_LIVENESS]:
            self.stdout.write(
                "PENDING_LIVENESS users must open the selfie link returned by GET /api/v1/billing/wallet/."
            )
        if summary[EmbedlyWallet.PENDING_TIER]:
            self.stdout.write(
                "PENDING_TIER: KYC passed but Embedly hasn't tiered/approved the customer yet. "
                "The provision-missing-wallets task retries every 30 minutes."
            )

    def _test_kyc(self, user):
        service = WalletKycService()
        if not (user.first_name and user.last_name):
            raise ValueError('needs first and last name')
        return service(
            user, bvn=staging_test_bvn(user), dob=date(1990, 1, 1),
            address='1 Test Street', city='Lagos',
        )
