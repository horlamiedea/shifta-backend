from django.db import migrations

CREDIT_TYPES = ('FUNDING', 'REFUND', 'PAYOUT', 'REVERSAL')
DEBIT_TYPES = ('CHARGE', 'WITHDRAWAL')


def forwards(apps, schema_editor):
    EmbedlyWallet = apps.get_model('billing', 'EmbedlyWallet')
    Transaction = apps.get_model('billing', 'Transaction')

    # Wallets that were fully created keep working.
    EmbedlyWallet.objects.filter(is_active=True).exclude(account_number__isnull=True) \
        .exclude(account_number='').update(status='ACTIVE')

    # Half-created records predate KYC support: send the user back through KYC,
    # keeping the old Embedly ids for reference.
    for wallet in EmbedlyWallet.objects.exclude(status='ACTIVE').exclude(customer_id__isnull=True):
        wallet.meta = {**(wallet.meta or {}), 'legacy_customer_id': wallet.customer_id}
        wallet.customer_id = None
        wallet.status = 'PENDING_KYC'
        wallet.is_active = False
        wallet.save(update_fields=['meta', 'customer_id', 'status', 'is_active'])

    Transaction.objects.filter(direction='', transaction_type__in=CREDIT_TYPES).update(direction='CREDIT')
    Transaction.objects.filter(direction='', transaction_type__in=DEBIT_TYPES).update(direction='DEBIT')


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0004_embedly_ledger'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
