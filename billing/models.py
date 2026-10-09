from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from accounts.models import Facility, Professional, User
from shifts.models import Shift
from core.models import BaseModel


class EmbedlyWallet(BaseModel):
    """Stores Embedly virtual wallet details for each user."""
    # Embedly only opens a wallet for a KYC-verified customer, so provisioning
    # moves through these states; the periodic task retries PENDING_TIER.
    PENDING_KYC = 'PENDING_KYC'            # waiting for the user to submit BVN/NIN (or TIN)
    PENDING_LIVENESS = 'PENDING_LIVENESS'  # BVN/NIN verified; user must complete the selfie check
    PENDING_TIER = 'PENDING_TIER'          # KYC done; Embedly hasn't tiered/approved it yet
    ACTIVE = 'ACTIVE'
    FAILED = 'FAILED'                      # Embedly rejected the details; see meta['last_error']
    STATUSES = (
        (PENDING_KYC, 'Pending KYC'), (PENDING_LIVENESS, 'Pending liveness check'),
        (PENDING_TIER, 'Pending Embedly approval'), (ACTIVE, 'Active'), (FAILED, 'Failed'),
    )

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='embedly_wallet')
    status = models.CharField(max_length=20, choices=STATUSES, default=PENDING_KYC, db_index=True)
    customer_id = models.CharField(max_length=255, blank=True, null=True)
    wallet_id = models.CharField(max_length=255, blank=True, null=True)
    account_number = models.CharField(max_length=50, blank=True, null=True, db_index=True)
    account_name = models.CharField(max_length=255, blank=True, null=True)
    bank_name = models.CharField(max_length=255, blank=True, null=True)
    bank_code = models.CharField(max_length=50, blank=True, null=True)
    provider = models.CharField(max_length=50, default='EMBEDLY')
    is_active = models.BooleanField(default=True)
    meta = models.JSONField(default=dict, blank=True)

    # Reconciliation: last balance Embedly reported vs what the app ledger expects.
    provider_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    expected_provider_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    drift = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    last_reconciled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # One app user per Embedly account number; blanks are failed/incomplete creations.
            models.UniqueConstraint(
                fields=['account_number'],
                condition=Q(account_number__isnull=False) & ~Q(account_number=''),
                name='uniq_embedly_wallet_account_number',
            ),
        ]

    def __str__(self):
        return f"Wallet: {self.account_number} ({self.user.email})"


class PlatformWallet(BaseModel):
    """
    Shifta's own Embedly wallet: receives shift charges and fees, pays refunds
    and earnings. A single row, managed by `manage.py setup_platform_wallet`;
    see billing.platform.
    """
    CORPORATE = 'CORPORATE'    # Shifta as an approved business (production)
    INDIVIDUAL = 'INDIVIDUAL'  # staging only: an instant Tier-1 test wallet
    ADOPTED = 'ADOPTED'        # an existing wallet in the organisation
    KINDS = ((CORPORATE, 'Corporate'), (INDIVIDUAL, 'Individual (staging)'), (ADOPTED, 'Adopted'))

    singleton = models.BooleanField(default=True, unique=True, editable=False)
    kind = models.CharField(max_length=20, choices=KINDS, default=CORPORATE)
    status = models.CharField(
        max_length=20, choices=EmbedlyWallet.STATUSES, default=EmbedlyWallet.PENDING_KYC,
    )
    customer_id = models.CharField(max_length=255, blank=True, null=True)
    wallet_id = models.CharField(max_length=255, blank=True, null=True)
    account_number = models.CharField(max_length=50, blank=True, null=True)
    account_name = models.CharField(max_length=255, blank=True, null=True)
    bank_name = models.CharField(max_length=255, blank=True, null=True)
    bank_code = models.CharField(max_length=50, blank=True, null=True)
    meta = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"Platform wallet {self.account_number or '(not open)'} [{self.status}]"


class Transaction(BaseModel):
    """
    User-facing history and the app wallet ledger.

    Rows written through billing.ledger carry `direction`, `balance_after` and
    a unique `idempotency_key`, which together make every balance change
    auditable and impossible to apply twice. Legacy rows predate the ledger
    and may have these blank.
    """
    TRANSACTION_TYPES = (
        ('PAYOUT', 'Payout'),
        ('CHARGE', 'Charge'),
        ('REFUND', 'Refund'),
        ('FUNDING', 'Funding'),
        ('WITHDRAWAL', 'Withdrawal'),
        ('REVERSAL', 'Reversal'),
        ('ADJUSTMENT', 'Adjustment'),
    )
    CREDIT = 'CREDIT'
    DEBIT = 'DEBIT'
    DIRECTIONS = ((CREDIT, 'Credit'), (DEBIT, 'Debit'))

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='transactions')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    reference = models.CharField(max_length=100, unique=True) # Paystack reference
    status = models.CharField(max_length=20, default='PENDING') # PENDING, PROCESSING, SUCCESS, FAILED
    # created_at in BaseModel
    shift = models.ForeignKey(Shift, on_delete=models.SET_NULL, null=True, blank=True)

    direction = models.CharField(max_length=6, choices=DIRECTIONS, blank=True, default='')
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    idempotency_key = models.CharField(max_length=191, unique=True, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)

    def __str__(self):
        return f"{self.transaction_type} - {self.amount} - {self.status}"


class WalletTransfer(BaseModel):
    """
    Outbox of money movements to execute on Embedly.

    Created in the same DB transaction as the ledger entry it mirrors, then
    sent by a Celery task after commit. `reference` is deterministic and sent
    to Embedly, so a retry after a timeout is recognised as the same transfer
    instead of moving money twice.
    """
    WALLET_TO_WALLET = 'WALLET_TO_WALLET'
    BANK_PAYOUT = 'BANK_PAYOUT'
    KINDS = ((WALLET_TO_WALLET, 'Wallet to wallet'), (BANK_PAYOUT, 'Bank payout'))

    QUEUED = 'QUEUED'              # waiting to be sent
    PROCESSING = 'PROCESSING'      # sent; outcome not yet confirmed
    SUCCESS = 'SUCCESS'
    FAILED = 'FAILED'              # Embedly definitively rejected it
    NEEDS_REVIEW = 'NEEDS_REVIEW'  # ambiguous after retries; a human must check
    STATUSES = (
        (QUEUED, 'Queued'), (PROCESSING, 'Processing'), (SUCCESS, 'Success'),
        (FAILED, 'Failed'), (NEEDS_REVIEW, 'Needs review'),
    )
    OPEN_STATUSES = (QUEUED, PROCESSING, NEEDS_REVIEW)

    reference = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=20, choices=KINDS)
    status = models.CharField(max_length=20, choices=STATUSES, default=QUEUED, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    narration = models.CharField(max_length=255, blank=True, default='')

    # Wallet users on each side; null means the platform (escrow) wallet.
    source_user = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name='outgoing_transfers',
    )
    destination_user = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name='incoming_transfers',
    )
    # Filled from the users' wallets at send time when left blank.
    source_account = models.CharField(max_length=50, blank=True, default='')
    destination_account = models.CharField(max_length=50, blank=True, default='')
    destination_bank_code = models.CharField(max_length=20, blank=True, default='')
    destination_account_name = models.CharField(max_length=255, blank=True, default='')

    ledger_transaction = models.ForeignKey(
        Transaction, on_delete=models.PROTECT, null=True, blank=True, related_name='provider_transfers',
    )
    # Set when the transfer only aligns Embedly with an app balance that existed
    # before wallets did (e.g. seeded data); there is no ledger entry for it.
    is_opening_balance = models.BooleanField(default=False)

    attempts = models.PositiveIntegerField(default=0)
    # Times Embedly definitively rejected it (money did not move). The reference
    # sent changes only then; after an ambiguous outcome it is resent unchanged.
    rejections = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default='')
    provider_reference = models.CharField(max_length=255, blank=True, default='')
    provider_response = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    # Earliest time a worker may touch it: a retry backoff, or the lease held
    # while a send is in flight.
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    @property
    def sent_reference(self):
        return self.reference if not self.rejections else f"{self.reference}-R{self.rejections}"

    def __str__(self):
        return f"{self.kind} {self.reference} {self.amount} [{self.status}]"


class WebhookEvent(BaseModel):
    """Every webhook received, deduplicated on `event_key`."""
    RECEIVED = 'RECEIVED'
    PROCESSED = 'PROCESSED'
    IGNORED = 'IGNORED'
    FAILED = 'FAILED'
    STATUSES = ((RECEIVED, 'Received'), (PROCESSED, 'Processed'), (IGNORED, 'Ignored'), (FAILED, 'Failed'))

    provider = models.CharField(max_length=20, default='EMBEDLY')
    event_key = models.CharField(max_length=191, unique=True)
    event_type = models.CharField(max_length=100, blank=True, default='')
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=STATUSES, default=RECEIVED)
    error = models.TextField(blank=True, default='')
    attempts = models.PositiveIntegerField(default=0)
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.provider} {self.event_type} [{self.status}]"


class IdempotencyRecord(BaseModel):
    """Stored response for a client `Idempotency-Key`, replayed on retries."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='idempotency_records')
    key = models.CharField(max_length=191)
    endpoint = models.CharField(max_length=100)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'endpoint', 'key'], name='uniq_idempotency_key'),
        ]


class Invoice(BaseModel):
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='invoices')
    month = models.DateField() # First day of the month
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, default='PENDING') # PENDING, PAID
    pdf_url = models.URLField(null=True, blank=True)
    # created_at in BaseModel

    def __str__(self):
        return f"Invoice for {self.facility.name} - {self.month}"

class AdminWalletLog(BaseModel):
    admin_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='wallet_logs')
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='wallet_logs')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    comment = models.TextField()

    def __str__(self):
        return f"{self.admin_user} funded {self.facility} - {self.amount}"
