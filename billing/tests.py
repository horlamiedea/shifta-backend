import threading
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from accounts.models import Facility, Professional, User
from shifts.models import Shift, ShiftApplication
from . import ledger
from .models import Transaction, WalletTransfer
from .services import ReleaseFundsService, ShiftPayoutService


def _test_money(user, balance):
    """Opening balances go through the ledger, as everywhere else (accounts start at zero)."""
    if Decimal(balance):
        ledger.credit(user, balance, 'ADJUSTMENT', f'test-opening:{user.pk}', settle=False,
                      metadata={'reason': 'test fixture'})


def make_facility(balance='0.00', email=None):
    user = User.objects.create_user(email=email or f'fac-{uuid.uuid4().hex[:8]}@test.ng', password='x')
    Facility.objects.create(
        user=user, name='Test Hospital', address='1 Marina, Lagos',
        rc_number=uuid.uuid4().hex[:10], is_verified=True,
    )
    _test_money(user, balance)
    return User.objects.get(pk=user.pk)  # fresh: the cached account predates the opening credit


def make_professional(balance='0.00'):
    user = User.objects.create_user(email=f'pro-{uuid.uuid4().hex[:8]}@test.ng', password='x')
    Professional.objects.create(user=user, license_number=uuid.uuid4().hex[:10])
    _test_money(user, balance)
    return User.objects.get(pk=user.pk)


def balance_of(user):
    user = User.objects.get(pk=user.pk)
    return (user.facility if user.is_facility else user.professional).wallet_balance


class LedgerTests(TestCase):
    def test_credit_updates_balance_and_records_entry(self):
        user = make_facility('100.00')
        tx, created = ledger.credit(user, '50.10', 'FUNDING', 'k-credit')

        self.assertTrue(created)
        self.assertEqual(balance_of(user), Decimal('150.10'))
        self.assertEqual(tx.direction, Transaction.CREDIT)
        self.assertEqual(tx.balance_after, Decimal('150.10'))
        self.assertEqual(tx.status, 'SUCCESS')

    def test_debit_rejects_overdraft_and_leaves_balance(self):
        user = make_facility('100.00')
        with self.assertRaises(ledger.InsufficientFunds):
            ledger.debit(user, '100.01', 'CHARGE', 'k-overdraft')
        self.assertEqual(balance_of(user), Decimal('100.00'))
        self.assertFalse(Transaction.objects.filter(idempotency_key='k-overdraft').exists())

    def test_same_key_is_applied_once(self):
        user = make_professional()
        first, created_first = ledger.credit(user, '500', 'PAYOUT', 'shift-payout:abc')
        second, created_second = ledger.credit(user, '500', 'PAYOUT', 'shift-payout:abc')

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(balance_of(user), Decimal('500.00'))

    def test_key_reused_for_different_movement_is_rejected(self):
        user = make_professional()
        ledger.credit(user, '500', 'PAYOUT', 'k-reuse')
        with self.assertRaises(ledger.IdempotencyConflict):
            ledger.credit(user, '400', 'PAYOUT', 'k-reuse')

    def test_rejects_invalid_amounts(self):
        user = make_facility('100')
        for bad in ('0', '-5', 'abc', 'NaN', 'Infinity', None):
            with self.subTest(amount=bad), self.assertRaises(ValueError):
                ledger.credit(user, bad, 'FUNDING', f'k-bad-{bad}')

    def test_professional_movements_settle_on_embedly_facility_ones_do_not(self):
        facility = make_facility('1000')
        pro = make_professional()

        debit_tx, _ = ledger.debit(facility, '300', 'CHARGE', 'k-charge')
        credit_tx, _ = ledger.credit(pro, '200', 'PAYOUT', 'k-pay')

        # A facility's money already sits in the platform wallet: nothing to move.
        self.assertFalse(WalletTransfer.objects.filter(ledger_transaction=debit_tx).exists())
        payout = WalletTransfer.objects.get(ledger_transaction=credit_tx)
        self.assertEqual((payout.source_user_id, payout.destination_user_id), (None, pro.pk))
        self.assertEqual(payout.status, WalletTransfer.QUEUED)

    def test_settle_false_queues_nothing(self):
        user = make_facility()
        tx, _ = ledger.credit(user, '10', 'FUNDING', 'k-inflow', settle=False)
        self.assertFalse(WalletTransfer.objects.filter(ledger_transaction=tx).exists())

    def test_stale_full_save_cannot_overwrite_balance(self):
        user = make_professional('0')
        stale = Professional.objects.get(user=user)  # loaded before the payout
        ledger.credit(user, '750', 'PAYOUT', 'k-stale')

        stale.specialties = ['ICU']
        stale.save()  # e.g. a profile update holding the old balance

        self.assertEqual(balance_of(user), Decimal('750.00'))
        self.assertEqual(Professional.objects.get(user=user).specialties, ['ICU'])


class ShiftPayoutTests(TestCase):
    def setUp(self):
        self.facility = make_facility('20000')
        self.pro = make_professional()
        start = timezone.now() - timedelta(hours=10)
        self.shift = Shift.objects.create(
            facility=self.facility.facility, role='Nurse', specialty='ICU', quantity_needed=1,
            start_time=start, end_time=start + timedelta(hours=8), rate=Decimal('2500'),
        )
        # The facility paid for the shift up front, as ShiftCreateService does.
        ledger.debit(self.facility, '20000', 'CHARGE', f'shift-charge:{self.shift.id}', shift=self.shift)
        self.app = ShiftApplication.objects.create(
            shift=self.shift, professional=self.pro.professional,
            status='COMPLETED', clock_out_time=timezone.now(),
        )

    def test_release_then_scheduled_payout_pays_once(self):
        # Release Funds runs now; the 24h task clock-out scheduled runs later.
        ReleaseFundsService()(self.facility, self.app.id)
        ShiftPayoutService()(self.app.id)

        self.assertEqual(balance_of(self.pro), Decimal('20000.00'))
        self.assertEqual(Transaction.objects.filter(user=self.pro, transaction_type='PAYOUT').count(), 1)

    def test_other_facility_cannot_release_funds(self):
        intruder = make_facility('0')
        with self.assertRaises(PermissionError):
            ReleaseFundsService()(intruder, self.app.id)
        self.assertEqual(balance_of(self.pro), Decimal('0.00'))

    def test_unfunded_shift_cannot_pay_out(self):
        # A shift nobody paid for (e.g. created by the seed command): no money to pay from.
        start = timezone.now() - timedelta(hours=10)
        unfunded = Shift.objects.create(
            facility=self.facility.facility, role='Nurse', specialty='ICU', quantity_needed=1,
            start_time=start, end_time=start + timedelta(hours=8), rate=Decimal('2500'),
        )
        app = ShiftApplication.objects.create(shift=unfunded, professional=make_professional().professional,
                                              status='COMPLETED', clock_out_time=timezone.now())
        with self.assertRaises(ledger.EscrowShortfall):
            ShiftPayoutService()(app.id)
        self.assertFalse(Transaction.objects.filter(shift=unfunded, transaction_type='PAYOUT').exists())

    def test_escrow_cannot_pay_out_more_than_was_charged(self):
        ShiftPayoutService()(self.app.id)  # pays the full 20,000
        with self.assertRaises(ledger.EscrowShortfall):
            ledger.credit(self.facility, '1', 'REFUND', 'k-extra-refund', shift=self.shift)
        self.assertEqual(ledger.shift_escrow(self.shift.pk), Decimal('0.00'))

    def test_not_paid_before_clock_out(self):
        self.app.clock_out_time = None
        self.app.status = 'IN_PROGRESS'
        self.app.save()
        self.assertIsNone(ShiftPayoutService()(self.app.id))
        self.assertEqual(balance_of(self.pro), Decimal('0.00'))


def run_concurrently(fn, count):
    """Run fn(i) in `count` threads released at the same instant; return results."""
    barrier = threading.Barrier(count)
    results = [None] * count

    def worker(i):
        try:
            barrier.wait()
            results[i] = fn(i)
        except Exception as exc:  # collected for assertions
            results[i] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


class LedgerConcurrencyTests(TransactionTestCase):
    """Real threads against real Postgres — these fail without the row lock."""

    def test_parallel_debits_never_overdraw(self):
        user = make_facility('500.00')
        results = run_concurrently(
            lambda i: ledger.debit(user, '100', 'CHARGE', f'race-debit-{i}'), 10,
        )

        succeeded = [r for r in results if isinstance(r, tuple)]
        rejected = [r for r in results if isinstance(r, ledger.InsufficientFunds)]
        self.assertEqual((len(succeeded), len(rejected)), (5, 5), results)
        self.assertEqual(balance_of(user), Decimal('0.00'))

    def test_parallel_credits_lose_no_updates(self):
        user = make_professional('0')
        run_concurrently(lambda i: ledger.credit(user, '10', 'PAYOUT', f'race-credit-{i}'), 20)
        self.assertEqual(balance_of(user), Decimal('200.00'))

    def test_parallel_duplicates_apply_once(self):
        user = make_professional('0')
        results = run_concurrently(lambda i: ledger.credit(user, '1000', 'PAYOUT', 'race-same-key'), 8)

        self.assertTrue(all(isinstance(r, tuple) for r in results), results)
        self.assertEqual(sum(1 for _, created in results if created), 1)
        self.assertEqual(balance_of(user), Decimal('1000.00'))


# =============================================================================
# Embedly integration (Embedly is always mocked — see _block_network)
# =============================================================================

import hashlib
import hmac
import json
from unittest import mock

from django.test import override_settings
from rest_framework.test import APITestCase

from .embedly_client import EmbedlyResponse, _normalise, embedly_client, verify_webhook_signature
from .models import EmbedlyWallet, WebhookEvent
from .reconciliation import expected_provider_balance, queue_opening_balance, reconcile_wallet
from .transfers import process_transfer
from .wallet_service import WalletKycService, WalletWithdrawalService
from .webhooks import process_event

PLATFORM = '9000000001'
EMBEDLY_TEST_SETTINGS = dict(
    EMBEDLY_API_KEY='test-key',
    EMBEDLY_WEBHOOK_SECRET='',
    EMBEDLY_PLATFORM_ACCOUNT_NUMBER=PLATFORM,
    EMBEDLY_MAX_TRANSFER_AMOUNT=Decimal('100000'),
    EMBEDLY_BASE_URL='https://waas-staging.embedly.ng/api/v1/',
    EMBEDLY_PAYOUT_URL='https://payout-staging.embedly.ng/api/',
    WITHDRAWAL_FEE_PERCENT=Decimal('2.5'),
    WITHDRAWAL_MIN_AMOUNT=Decimal('500'),
    WITHDRAWAL_MAX_AMOUNT=Decimal('1000000'),
    # Rate limiting needs a cache; keep tests independent of Redis.
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
)


def _block_network(*args, **kwargs):
    raise RuntimeError("Tests must not call the real Embedly API — mock embedly_client.")


_module_patches = [
    mock.patch('billing.embedly_client.requests.request', side_effect=_block_network),
    # Committed ledger entries enqueue Celery tasks; keep them off any real broker.
    mock.patch('billing.tasks.dispatch_transfer.delay'),
]


def setUpModule():
    for patcher in _module_patches:
        patcher.start()


def tearDownModule():
    for patcher in _module_patches:
        patcher.stop()


def ok(data=None, raw=None):
    return EmbedlyResponse(ok=True, http_status=200, code='00', data=data, raw=raw or {'data': data})


def rejected(message, code='400', http_status=400):
    return EmbedlyResponse(ok=False, http_status=http_status, code=code, message=message, raw={'message': message})


TRANSPORT_ERROR = EmbedlyResponse(ok=False, http_status=0, message='timed out', transport_error=True)


def activate(user, account):
    return EmbedlyWallet.objects.update_or_create(user=user, defaults=dict(
        status=EmbedlyWallet.ACTIVE, is_active=True, account_number=account,
        account_name=user.email, customer_id='cust-' + account,
    ))[0]


class EmbedlyResponseShapeTests(TestCase):
    """Shapes captured from Embedly staging on 2026-10-08."""

    def test_wallet_transfer_not_found_is_http_400_with_body_code_404(self):
        r = _normalise(400, {"code": "404", "success": False, "message": "Transaction not found", "data": None})
        self.assertFalse(r.ok)
        self.assertTrue(r.not_found)
        self.assertFalse(r.ambiguous)

    def test_payout_not_found_is_http_200_with_status_code_404(self):
        r = _normalise(200, {"data": None, "statusCode": 404, "code": None,
                             "message": "Transaction not found", "succeeded": False})
        self.assertFalse(r.ok)
        self.assertTrue(r.not_found)

    def test_flat_create_wallet_success(self):
        body = {"message": "Wallet created successfully.", "walletId": "w1",
                "virtualAccount": {"accountNumber": "9710000498", "bankCode": "232", "bankName": "Sterling"}}
        r = _normalise(200, body)
        self.assertTrue(r.ok)
        self.assertEqual(r.data['virtualAccount']['accountNumber'], '9710000498')

    def test_core_and_validation_errors(self):
        self.assertTrue(_normalise(200, {"code": "00", "success": True, "message": "ok", "data": []}).ok)
        self.assertFalse(_normalise(400, {"code": "400", "success": False,
                                          "message": "Customer must have a tier", "data": None}).ok)
        v2 = _normalise(400, {"data": None, "status": 400, "message": "passportUrl is required"})
        self.assertFalse(v2.ok)
        self.assertEqual(v2.message, "passportUrl is required")
        aspnet = _normalise(400, {"errors": {"FirstName": ["required"]}, "title": "One or more validation errors occurred.", "status": 400})
        self.assertFalse(aspnet.ok)

    def test_insufficient_funds_is_a_rejection_not_ambiguous(self):
        r = _normalise(400, {"code": "-999", "success": False, "message": "Insufficient funds in the from wallet", "data": None})
        self.assertFalse(r.ok)
        self.assertFalse(r.ambiguous)

    def test_server_errors_and_timeouts_are_ambiguous(self):
        self.assertTrue(_normalise(502, {"message": "bad gateway"}).ambiguous)
        self.assertTrue(TRANSPORT_ERROR.ambiguous)


@override_settings(**EMBEDLY_TEST_SETTINGS)
class WebhookSignatureTests(TestCase):
    body = b'{"event":"nip","data":{"amount":100}}'

    def sign(self, key, body=None):
        return hmac.new(key.encode(), body or self.body, hashlib.sha512).hexdigest()

    def test_valid_signature_with_api_key(self):
        self.assertTrue(verify_webhook_signature(self.body, self.sign('test-key')))
        self.assertTrue(verify_webhook_signature(self.body, self.sign('test-key').upper()))

    def test_rejects_wrong_key_tampered_body_and_missing_header(self):
        self.assertFalse(verify_webhook_signature(self.body, self.sign('other-key')))
        self.assertFalse(verify_webhook_signature(self.body + b' ', self.sign('test-key')))
        self.assertFalse(verify_webhook_signature(self.body, ''))

    @override_settings(EMBEDLY_WEBHOOK_SECRET='whsec')
    def test_dedicated_webhook_secret_takes_precedence(self):
        self.assertTrue(verify_webhook_signature(self.body, self.sign('whsec')))
        self.assertFalse(verify_webhook_signature(self.body, self.sign('test-key')))


@override_settings(**EMBEDLY_TEST_SETTINGS)
class EmbedlyWebhookTests(APITestCase):
    url = '/api/v1/billing/embedly/webhook/'

    def setUp(self):
        self.facility = make_facility('0')
        self.wallet = activate(self.facility, '9710000111')

    def post(self, payload, key='test-key'):
        body = json.dumps(payload).encode()
        sig = hmac.new(key.encode(), body, hashlib.sha512).hexdigest()
        return self.client.post(self.url, data=body, content_type='application/json',
                                HTTP_X_EMBEDLY_SIGNATURE=sig)

    def inflow(self, reference='NIP-778899', amount=25000, account='9710000111', sender='0123456789'):
        return {'event': 'nip', 'data': {
            'accountNumber': account, 'reference': reference, 'amount': amount, 'fee': 0,
            'senderName': 'Lagos General', 'senderAccountNumber': sender,
            'dateOfTransaction': '2026-10-08T09:27:22Z',
        }}

    def history(self, reference='NIP-778899', amount=25000):
        return ok({'walletHistories': [{
            'transactionReference': reference, 'amount': amount, 'debitCreditIndicator': 'C',
        }], 'totalPages': 1})

    def test_bad_signature_is_rejected_and_not_stored(self):
        response = self.post(self.inflow(), key='forged')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(WebhookEvent.objects.count(), 0)
        self.assertEqual(balance_of(self.facility), Decimal('0'))

    def test_confirmed_inflow_credits_once_even_when_redelivered(self):
        with mock.patch.object(embedly_client, 'get_account_history', return_value=self.history()):
            self.assertEqual(self.post(self.inflow()).status_code, 200)
            self.assertEqual(self.post(self.inflow()).status_code, 200)  # Embedly retry

        self.assertEqual(balance_of(self.facility), Decimal('25000.00'))
        self.assertEqual(Transaction.objects.filter(user=self.facility, transaction_type='FUNDING').count(), 1)
        self.assertEqual(WebhookEvent.objects.get().status, WebhookEvent.PROCESSED)
        # The money is already in the Embedly wallet: nothing to transfer.
        self.assertFalse(WalletTransfer.objects.exists())

    def test_inflow_not_in_embedly_history_is_not_credited_until_it_is(self):
        empty = ok({'walletHistories': [], 'totalPages': 1})
        with mock.patch.object(embedly_client, 'get_account_history', return_value=empty):
            self.assertEqual(self.post(self.inflow()).status_code, 200)
        event = WebhookEvent.objects.get()
        self.assertEqual(event.status, WebhookEvent.FAILED)
        self.assertEqual(balance_of(self.facility), Decimal('0'))

        # History catches up; the sweeper reprocesses the stored event.
        with mock.patch.object(embedly_client, 'get_account_history', return_value=self.history()):
            process_event(event.pk)
        self.assertEqual(balance_of(self.facility), Decimal('25000.00'))

    def test_amount_mismatch_with_history_is_not_credited(self):
        with mock.patch.object(embedly_client, 'get_account_history', return_value=self.history(amount=250)):
            self.post(self.inflow(amount=25000))
        self.assertEqual(balance_of(self.facility), Decimal('0'))

    def test_internal_transfers_are_not_counted_twice(self):
        with mock.patch.object(embedly_client, 'get_account_history') as history:
            self.post(self.inflow(reference='SHF-abc123'))
            self.post(self.inflow(reference='NIP-1', sender=PLATFORM))
        history.assert_not_called()
        self.assertEqual(balance_of(self.facility), Decimal('0'))
        self.assertTrue(all(e.status == WebhookEvent.IGNORED for e in WebhookEvent.objects.all()))

    def test_unknown_account_is_ignored(self):
        self.post(self.inflow(account='9999999999'))
        self.assertEqual(WebhookEvent.objects.get().status, WebhookEvent.IGNORED)


@override_settings(**EMBEDLY_TEST_SETTINGS)
class TransferDispatchTests(TestCase):
    def setUp(self):
        self.pro = make_professional('0')
        activate(self.pro, '9710000111')

    def charge(self, amount='30000', key='k-dispatch'):
        """A shift payout: the platform wallet pays the professional's wallet."""
        tx, _ = ledger.credit(self.pro, amount, 'PAYOUT', key)
        return list(WalletTransfer.objects.filter(ledger_transaction=tx).order_by('reference'))

    def test_successful_send_moves_platform_money_to_professional(self):
        (t,) = self.charge()
        with mock.patch.object(embedly_client, 'wallet_to_wallet', return_value=ok(0)) as send:
            t = process_transfer(t.pk)
        self.assertEqual(t.status, WalletTransfer.SUCCESS)
        kwargs = send.call_args.kwargs
        self.assertEqual((kwargs['from_account'], kwargs['to_account']), (PLATFORM, '9710000111'))
        self.assertEqual(kwargs['amount'], Decimal('30000.00'))

    def test_waits_for_wallet_activation(self):
        pro = make_professional('0')  # no Embedly wallet yet
        tx, _ = ledger.credit(pro, '5000', 'PAYOUT', 'k-wait')
        t = WalletTransfer.objects.get(ledger_transaction=tx)
        with mock.patch.object(embedly_client, 'wallet_to_wallet') as send:
            t = process_transfer(t.pk)
        send.assert_not_called()
        self.assertEqual(t.status, WalletTransfer.QUEUED)
        self.assertIn('wallet activation', t.last_error)

    def test_timeout_then_requery_not_found_resends_same_reference(self):
        (t,) = self.charge()
        with mock.patch.object(embedly_client, 'wallet_to_wallet', return_value=TRANSPORT_ERROR) as send:
            t = process_transfer(t.pk)
        self.assertEqual(t.status, WalletTransfer.PROCESSING)
        first_reference = send.call_args.kwargs['reference']

        WalletTransfer.objects.filter(pk=t.pk).update(next_attempt_at=None)
        not_found = rejected('Transaction not found', code='404')
        with mock.patch.object(embedly_client, 'wallet_to_wallet_status', return_value=not_found), \
                mock.patch.object(embedly_client, 'wallet_to_wallet', return_value=ok(0)) as resend:
            t = process_transfer(t.pk)
        self.assertEqual(t.status, WalletTransfer.SUCCESS)
        self.assertEqual(resend.call_args.kwargs['reference'], first_reference)

    def test_timeout_that_actually_succeeded_is_not_sent_again(self):
        (t,) = self.charge()
        with mock.patch.object(embedly_client, 'wallet_to_wallet', return_value=TRANSPORT_ERROR):
            process_transfer(t.pk)
        WalletTransfer.objects.filter(pk=t.pk).update(next_attempt_at=None)
        with mock.patch.object(embedly_client, 'wallet_to_wallet_status', return_value=ok({'status': 'Success'})), \
                mock.patch.object(embedly_client, 'wallet_to_wallet') as send:
            t = process_transfer(t.pk)
        send.assert_not_called()
        self.assertEqual(t.status, WalletTransfer.SUCCESS)

    def test_duplicate_reference_on_resend_means_the_first_attempt_landed(self):
        (t,) = self.charge()
        with mock.patch.object(embedly_client, 'wallet_to_wallet', return_value=TRANSPORT_ERROR):
            process_transfer(t.pk)
        WalletTransfer.objects.filter(pk=t.pk).update(next_attempt_at=None)
        # Requery raced Embedly and saw nothing; the resend is then refused as a duplicate.
        duplicate = rejected('Duplicate Reference detected', code='-18')
        with mock.patch.object(embedly_client, 'wallet_to_wallet_status',
                               side_effect=[rejected('Transaction not found', code='404'), ok({'status': 'Success'})]), \
                mock.patch.object(embedly_client, 'wallet_to_wallet', return_value=duplicate):
            t = process_transfer(t.pk)
        self.assertEqual(t.status, WalletTransfer.SUCCESS)

    def test_insufficient_funds_backs_off_and_retries_with_fresh_reference(self):
        (t,) = self.charge()
        no_funds = rejected('Insufficient funds in the from wallet', code='-999')
        with mock.patch.object(embedly_client, 'wallet_to_wallet', return_value=no_funds) as send:
            t = process_transfer(t.pk)
        self.assertEqual((t.status, t.rejections), (WalletTransfer.QUEUED, 1))
        self.assertIsNotNone(t.next_attempt_at)
        first = send.call_args.kwargs['reference']

        WalletTransfer.objects.filter(pk=t.pk).update(next_attempt_at=None)
        with mock.patch.object(embedly_client, 'wallet_to_wallet', return_value=ok(0)) as send:
            t = process_transfer(t.pk)
        self.assertEqual(send.call_args.kwargs['reference'], f"{first}-R1")
        self.assertEqual(t.status, WalletTransfer.SUCCESS)

    def test_amounts_over_the_single_debit_cap_are_chunked(self):
        transfers = self.charge('250000', key='k-big')
        self.assertEqual([t.amount for t in transfers], [Decimal('100000'), Decimal('100000'), Decimal('50000')])
        self.assertEqual(len({t.reference for t in transfers}), 3)


@override_settings(**EMBEDLY_TEST_SETTINGS)
class WithdrawalTests(APITestCase):
    url = '/api/v1/billing/withdraw/'

    def setUp(self):
        self.pro = make_professional('20000')
        activate(self.pro, '9710000222')
        self.client.force_authenticate(self.pro)
        patcher = mock.patch.object(embedly_client, 'name_enquiry',
                                    return_value=ok({'accountName': 'ADAEZE OKONKWO'}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def withdraw(self, amount='10000'):
        return WalletWithdrawalService()(self.pro, amount, bank_code='000013', account_number='0123456789')

    def test_debits_and_queues_payout_and_fee(self):
        result = self.withdraw('10000')

        self.assertEqual(balance_of(self.pro), Decimal('10000.00'))
        self.assertEqual((result['net_amount'], result['charge']), ('9750', '250.00'))
        self.assertEqual(result['account_name'], 'ADAEZE OKONKWO')
        payout = WalletTransfer.objects.get(kind=WalletTransfer.BANK_PAYOUT)
        self.assertEqual((payout.amount, payout.destination_account_name), (Decimal('9750'), 'ADAEZE OKONKWO'))
        fee = WalletTransfer.objects.get(kind=WalletTransfer.WALLET_TO_WALLET)
        self.assertEqual((fee.amount, fee.source_user_id, fee.destination_user_id), (Decimal('250.00'), self.pro.pk, None))
        self.assertEqual(Transaction.objects.get(transaction_type='WITHDRAWAL').status, 'PROCESSING')

    def test_rejected_payout_reverses_withdrawal_and_cancels_unsent_fee(self):
        self.withdraw('10000')
        payout = WalletTransfer.objects.get(kind=WalletTransfer.BANK_PAYOUT)
        with mock.patch.object(embedly_client, 'bank_payout',
                               return_value=rejected('Invalid destination account')):
            payout = process_transfer(payout.pk)

        self.assertEqual(payout.status, WalletTransfer.FAILED)
        self.assertEqual(balance_of(self.pro), Decimal('20000.00'))
        self.assertEqual(Transaction.objects.get(transaction_type='WITHDRAWAL').status, 'FAILED')
        self.assertTrue(Transaction.objects.filter(transaction_type='REVERSAL', amount=Decimal('10000')).exists())
        self.assertEqual(WalletTransfer.objects.get(kind=WalletTransfer.WALLET_TO_WALLET).status, WalletTransfer.FAILED)

    def test_accepted_payout_is_confirmed_by_requery(self):
        self.withdraw('10000')
        payout = WalletTransfer.objects.get(kind=WalletTransfer.BANK_PAYOUT)
        with mock.patch.object(embedly_client, 'bank_payout', return_value=ok('EMBe66029d4f')):
            payout = process_transfer(payout.pk)
        self.assertEqual((payout.status, payout.provider_reference), (WalletTransfer.PROCESSING, 'EMBe66029d4f'))

        WalletTransfer.objects.filter(pk=payout.pk).update(next_attempt_at=None)
        with mock.patch.object(embedly_client, 'payout_status', return_value=ok({'status': 'SUCCESS'})):
            payout = process_transfer(payout.pk)
        self.assertEqual(payout.status, WalletTransfer.SUCCESS)
        self.assertEqual(Transaction.objects.get(transaction_type='WITHDRAWAL').status, 'SUCCESS')

    def test_payout_with_unknown_outcome_and_no_reference_goes_to_review_not_resend(self):
        self.withdraw('10000')
        payout = WalletTransfer.objects.get(kind=WalletTransfer.BANK_PAYOUT)
        with mock.patch.object(embedly_client, 'bank_payout', return_value=TRANSPORT_ERROR):
            process_transfer(payout.pk)
        WalletTransfer.objects.filter(pk=payout.pk).update(next_attempt_at=None)
        with mock.patch.object(embedly_client, 'bank_payout') as resend:
            payout = process_transfer(payout.pk)
        resend.assert_not_called()
        self.assertEqual(payout.status, WalletTransfer.NEEDS_REVIEW)
        self.assertEqual(balance_of(self.pro), Decimal('10000.00'))  # not reversed blindly

    def test_cannot_withdraw_more_than_balance_or_without_active_wallet(self):
        with self.assertRaises(ValueError):
            self.withdraw('20001')
        EmbedlyWallet.objects.filter(user=self.pro).update(status=EmbedlyWallet.PENDING_TIER)
        with self.assertRaises(ValueError):
            self.withdraw('1000')
        self.assertEqual(balance_of(self.pro), Decimal('20000.00'))

    def test_idempotency_key_replays_instead_of_withdrawing_twice(self):
        body = {'amount': '5000', 'bank_code': '000013', 'account_number': '0123456789'}
        first = self.client.post(self.url, body, format='json', HTTP_IDEMPOTENCY_KEY='tap-1')
        second = self.client.post(self.url, body, format='json', HTTP_IDEMPOTENCY_KEY='tap-1')

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(second['Idempotent-Replayed'], 'true')
        self.assertEqual(balance_of(self.pro), Decimal('15000.00'))
        self.assertEqual(Transaction.objects.filter(transaction_type='WITHDRAWAL').count(), 1)

    def test_idempotency_key_reused_with_different_body_is_rejected(self):
        body = {'amount': '5000', 'bank_code': '000013', 'account_number': '0123456789'}
        self.client.post(self.url, body, format='json', HTTP_IDEMPOTENCY_KEY='tap-2')
        response = self.client.post(self.url, {**body, 'amount': '6000'}, format='json', HTTP_IDEMPOTENCY_KEY='tap-2')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(balance_of(self.pro), Decimal('15000.00'))


@override_settings(**EMBEDLY_TEST_SETTINGS, EMBEDLY_LIVENESS_REDIRECT_URL='')
class WalletKycTests(APITestCase):
    BVN_OK = ok({'kycCompleted': True, 'response': {
        'status': {'state': 'COMPLETE', 'status': 'SUCCESS'},
        'summary': {'bvn_check': {'status': 'EXACT_MATCH'}},
    }})
    NIN_OK = ok({'status': {'state': 'COMPLETE', 'status': 'SUCCESS'},
                 'summary': {'nin_check': {'status': 'EXACT_MATCH'}}})
    # Captured from staging 2026-10-08: wrapped, not the flat shape the docs show.
    WALLET_CREATED = ok({
        'id': '60192771-c330-11f1-88dd-02789e000022', 'customerId': 'cust-1', 'name': 'Adaeze Okonkwo - Shifta',
        'availableBalance': 0, 'virtualAccount': {'accountNumber': '9710097303', 'bankCode': '232',
                                                   'bankName': 'Sterling Bank'},
    })
    LIVENESS_DONE = ok({'status': 'completed', 'startKycUrl': 'https://mock.embedly.ng/liveness/x'})
    LIVENESS_OPEN = ok({'status': 'in_progress', 'startKycUrl': 'https://hyperverge.co/kyc/abc?token=t'})

    def setUp(self):
        self.pro = make_professional()
        User.objects.filter(pk=self.pro.pk).update(first_name='Adaeze', last_name='Okonkwo', phone_number='+2348031234567')
        self.pro.refresh_from_db()
        self.embedly = {
            'create_individual_customer': ok({'id': 'cust-1'}),
            'verify_bvn': self.BVN_OK,
            'verify_nin': self.NIN_OK,
            'initiate_liveness': self.LIVENESS_DONE,
            'create_individual_wallet': self.WALLET_CREATED,
        }

    def kyc(self, **ids):
        mocks = {name: mock.patch.object(embedly_client, name, return_value=value) for name, value in self.embedly.items()}
        self.calls = {name: patcher.start() for name, patcher in mocks.items()}
        try:
            return WalletKycService()(self.pro, dob=date(1992, 4, 2), address='12 Allen Ave', city='Lagos',
                                      **(ids or {'bvn': '22222222222'}))
        finally:
            for patcher in mocks.values():
                patcher.stop()

    def test_kyc_with_liveness_opens_wallet_and_never_stores_ids(self):
        wallet = self.kyc(bvn='22222222222', nin='12345678901')

        self.assertEqual((wallet.status, wallet.account_number), (EmbedlyWallet.ACTIVE, '9710097303'))
        self.assertEqual(wallet.wallet_id, '60192771-c330-11f1-88dd-02789e000022')
        self.assertEqual(self.calls['create_individual_customer'].call_args.kwargs['mobile_number'], '08031234567')
        self.calls['initiate_liveness'].assert_called_once()
        stored = json.dumps(EmbedlyWallet.objects.get(pk=wallet.pk).meta)
        self.assertNotIn('22222222222', stored)
        self.assertNotIn('12345678901', stored)
        self.assertEqual(wallet.meta['kyc']['methods'], ['BVN', 'NIN'])

    def test_production_liveness_waits_for_the_selfie_then_webhook_opens_wallet(self):
        self.embedly['initiate_liveness'] = self.LIVENESS_OPEN
        wallet = self.kyc()
        self.assertEqual(wallet.status, EmbedlyWallet.PENDING_LIVENESS)
        self.assertEqual(wallet.meta['liveness_url'], 'https://hyperverge.co/kyc/abc?token=t')
        self.calls['create_individual_wallet'].assert_not_called()

        self.client.force_authenticate(self.pro)
        self.assertEqual(self.client.get('/api/v1/billing/wallet/').data['wallet']['liveness_url'],
                         'https://hyperverge.co/kyc/abc?token=t')

        payload = {'event': 'customer.liveness.approved', 'organizationId': 'org',
                   'data': {'customerId': 'cust-1', 'submissionId': 'sub-1', 'status': 'completed'}}
        body = json.dumps(payload).encode()
        sig = hmac.new(b'test-key', body, hashlib.sha512).hexdigest()
        with mock.patch.object(embedly_client, 'create_individual_wallet', return_value=self.WALLET_CREATED):
            self.client.post('/api/v1/billing/embedly/webhook/', data=body, content_type='application/json',
                             HTTP_X_EMBEDLY_SIGNATURE=sig)
        self.assertEqual(EmbedlyWallet.objects.get(pk=wallet.pk).status, EmbedlyWallet.ACTIVE)

    def test_declined_selfie_can_be_retried_without_resubmitting_ids(self):
        self.embedly['initiate_liveness'] = self.LIVENESS_OPEN
        wallet = self.kyc()
        process_event(WebhookEvent.objects.create(
            event_key='declined-1', event_type='customer.liveness.declined',
            payload={'data': {'customerId': 'cust-1', 'failureReason': {'text': 'Face match below threshold'}}},
        ).pk)
        wallet.refresh_from_db()
        self.assertEqual((wallet.status, wallet.meta['last_error']),
                         (EmbedlyWallet.PENDING_LIVENESS, 'Face match below threshold'))

        self.embedly['initiate_liveness'] = self.LIVENESS_DONE
        wallet = self.kyc()  # user taps "try again"
        self.calls['verify_bvn'].assert_not_called()
        self.assertEqual(wallet.status, EmbedlyWallet.ACTIVE)

    def test_waits_when_embedly_has_not_assigned_a_tier(self):
        self.embedly['create_individual_wallet'] = rejected('Customer must have a tier')
        wallet = self.kyc()
        self.assertEqual(wallet.status, EmbedlyWallet.PENDING_TIER)
        self.assertEqual(wallet.meta['last_error'], 'Customer must have a tier')

    def test_bvn_name_mismatch_is_rejected(self):
        self.embedly['verify_bvn'] = ok({'kycCompleted': True, 'response': {
            'status': {'state': 'COMPLETE', 'status': 'SUCCESS'},
            'summary': {'bvn_check': {'status': 'NO_MATCH'}},
        }})
        with self.assertRaises(ValueError):
            self.kyc()
        self.calls['initiate_liveness'].assert_not_called()
        self.assertEqual(EmbedlyWallet.objects.get(user=self.pro).status, EmbedlyWallet.PENDING_KYC)


@override_settings(**EMBEDLY_TEST_SETTINGS)
class ReconciliationTests(TestCase):
    def setUp(self):
        self.pro = make_professional('100000')  # predates the wallet: Embedly holds 0
        self.wallet = activate(self.pro, '9710000111')

    def embedly_balance(self, amount):
        return mock.patch.object(embedly_client, 'get_wallet_by_account',
                                 return_value=ok({'availableBalance': amount}))

    def test_pending_transfers_are_not_drift(self):
        ledger.credit(self.pro, '30000', 'PAYOUT', 'k-rec')  # platform -> pro transfer still queued
        self.assertEqual(expected_provider_balance(self.pro), Decimal('100000.00'))
        with self.embedly_balance(100000):
            self.assertEqual(reconcile_wallet(self.wallet), Decimal('0'))

    def test_unbacked_balance_shows_as_drift_and_opening_balance_backs_it_once(self):
        with self.embedly_balance(0):
            self.assertEqual(reconcile_wallet(self.wallet), Decimal('-100000.00'))
            transfers, _ = queue_opening_balance(self.wallet)
            again, note = queue_opening_balance(self.wallet)

        self.assertEqual([t.amount for t in transfers], [Decimal('100000.00')])
        self.assertEqual((transfers[0].source_user, transfers[0].destination_user_id), (None, self.pro.pk))
        self.assertIsNone(again)
        self.assertEqual(note, 'already queued')


@override_settings(**EMBEDLY_TEST_SETTINGS)
class PlatformCoverTests(TestCase):
    """Facility money lives in the platform wallet, which must cover everything owed."""

    def setUp(self):
        self.facility = make_facility('50000')  # unspent facility balance
        start = timezone.now() + timedelta(days=1)
        # An open shift with 20,000 escrowed, 5,000 of it already refunded.
        open_shift = Shift.objects.create(
            facility=self.facility.facility, role='Nurse', specialty='ICU', quantity_needed=1,
            start_time=start, end_time=start + timedelta(hours=8), rate=Decimal('2500'),
        )
        Transaction.objects.create(user=self.facility, amount=Decimal('20000'), transaction_type='CHARGE',
                                   direction='DEBIT', reference='c1', status='SUCCESS', shift=open_shift)
        Transaction.objects.create(user=self.facility, amount=Decimal('5000'), transaction_type='REFUND',
                                   direction='CREDIT', reference='r1', status='SUCCESS', shift=open_shift)
        # Completed work (8h x 2,500) not yet paid out: still in the 24h hold.
        past = timezone.now() - timedelta(hours=10)
        done = Shift.objects.create(
            facility=self.facility.facility, role='Nurse', specialty='ICU', quantity_needed=1,
            start_time=past, end_time=past + timedelta(hours=8), rate=Decimal('2500'), status='COMPLETED',
        )
        Transaction.objects.create(user=self.facility, amount=Decimal('20000'), transaction_type='CHARGE',
                                   direction='DEBIT', reference='c2', status='SUCCESS', shift=done)
        self.pro = make_professional('0')
        ShiftApplication.objects.create(shift=done, professional=self.pro.professional,
                                        status='COMPLETED', clock_out_time=timezone.now())

    def cover(self, platform_balance):
        with mock.patch.object(embedly_client, 'get_wallet_by_account',
                               return_value=ok({'availableBalance': platform_balance})):
            from .reconciliation import platform_cover
            return platform_cover()

    def test_counts_everything_owed(self):
        cover = self.cover(100000)
        self.assertEqual(cover['facility_balances'], Decimal('50000.00'))
        self.assertEqual(cover['shift_escrow'], Decimal('15000.00'))
        self.assertEqual(cover['unpaid_completed_work'], Decimal('20000.00'))
        self.assertEqual(cover['required'], Decimal('85000.00'))
        self.assertEqual(cover['surplus'], Decimal('15000.00'))

    def test_paid_work_and_queued_transfers(self):
        ledger.credit(self.pro, '20000', 'PAYOUT', 'k-cover-pay',
                      shift=ShiftApplication.objects.get().shift)  # paid; transfer queued
        cover = self.cover(80000)
        self.assertEqual(cover['unpaid_completed_work'], Decimal('0'))
        self.assertEqual(cover['pending_to_professionals'], Decimal('20000.00'))
        self.assertEqual(cover['surplus'], Decimal('-5000.00'))  # shortfall


class ShiftMoneyFlowTests(TestCase):
    """The shift services now move money only through the ledger."""

    def setUp(self):
        self.facility = make_facility('100000')
        self.start = timezone.now() + timedelta(days=2)
        patcher = mock.patch('shifts.services.notify_matching_professionals')
        patcher.start()
        self.addCleanup(patcher.stop)

    def create_shift(self, quantity=2):
        from shifts.services import ShiftCreateService
        return ShiftCreateService()(
            self.facility, role='Nurse', specialty='ICU', quantity_needed=quantity,
            start_time=self.start, end_time=self.start + timedelta(hours=8), rate=Decimal('2500'),
            address='1 Marina, Lagos', latitude=6.45, longitude=3.39,
        )

    def test_creating_a_shift_escrows_its_cost(self):
        shift = self.create_shift(quantity=2)  # 2500 x 8h x 2
        self.assertEqual(balance_of(self.facility), Decimal('60000.00'))
        charge = Transaction.objects.get(idempotency_key=f'shift-charge:{shift.id}')
        self.assertEqual((charge.amount, charge.direction), (Decimal('40000.00'), Transaction.DEBIT))
        # Escrow stays in the platform wallet, where the facility's money already is.
        self.assertFalse(WalletTransfer.objects.filter(ledger_transaction=charge).exists())

    def test_shift_beyond_balance_is_rejected_and_not_created(self):
        with self.assertRaises(ValueError):
            self.create_shift(quantity=6)  # 120000 > 100000
        self.assertEqual(balance_of(self.facility), Decimal('100000.00'))
        self.assertFalse(Shift.objects.exists())

    def test_expiry_job_refunds_unfilled_slots_once_even_if_rerun(self):
        from shifts.tasks import close_expired_shifts
        shift = self.create_shift(quantity=2)
        Shift.objects.filter(pk=shift.pk).update(
            start_time=timezone.now() - timedelta(hours=10), end_time=timezone.now() - timedelta(hours=2),
        )
        close_expired_shifts()
        Shift.objects.filter(pk=shift.pk).update(status='OPEN')  # simulate a crash-and-rerun
        close_expired_shifts()

        self.assertEqual(balance_of(self.facility), Decimal('100000.00'))
        self.assertEqual(Transaction.objects.filter(transaction_type='REFUND').count(), 1)

    def test_expiry_of_a_never_charged_shift_refunds_nothing(self):
        from shifts.tasks import close_expired_shifts
        start = timezone.now() - timedelta(hours=10)
        unfunded = Shift.objects.create(  # e.g. seeded data: no CHARGE ever made
            facility=self.facility.facility, role='Nurse', specialty='ICU', quantity_needed=3,
            start_time=start, end_time=start + timedelta(hours=8), rate=Decimal('2500'),
        )
        close_expired_shifts()
        self.assertEqual(Shift.objects.get(pk=unfunded.pk).status, 'COMPLETED')
        self.assertEqual(balance_of(self.facility), Decimal('100000.00'))

    def test_removing_a_professional_twice_refunds_once(self):
        from shifts.cancellation_services import FacilityCancelShiftService
        shift = self.create_shift(quantity=1)
        pro = make_professional()
        ShiftApplication.objects.create(shift=shift, professional=pro.professional, status='CONFIRMED')
        Shift.objects.filter(pk=shift.pk).update(quantity_filled=1)

        FacilityCancelShiftService()(self.facility, shift.id, professional_id=pro.professional.id, reason='x')
        with self.assertRaises(ValueError):
            FacilityCancelShiftService()(self.facility, shift.id, professional_id=pro.professional.id, reason='x')

        # > 4h before start: 90% refund to facility, 3% to the professional.
        self.assertEqual(balance_of(self.facility), Decimal('98000.00'))
        self.assertEqual(balance_of(pro), Decimal('600.00'))
        self.assertEqual(Shift.objects.get(pk=shift.pk).quantity_filled, 0)


# -- facilities (no wallet) and the platform wallet -------------------------

from . import corporate, platform
from .models import PlatformWallet

PDF = b'%PDF-1.4 test document'
DOC_FIELDS = ('cac', 'tin_document', 'board_resolution', 'utility_bill', 'memart', 'scuml')
UPLOADED_URL = 'https://aws-waas-assets.s3.us-west-2.amazonaws.com/WaasCore/Stg/org/doc.pdf'


@override_settings(**EMBEDLY_TEST_SETTINGS)
class FacilityNoWalletTests(APITestCase):
    """Facilities fund through Paystack; their money is held in the platform wallet."""

    def setUp(self):
        self.facility = make_facility('25000')
        self.client.force_authenticate(self.facility)

    def test_wallet_endpoint_reports_balance_without_a_wallet(self):
        data = self.client.get('/api/v1/billing/wallet/').data
        self.assertEqual(Decimal(data['balance']), Decimal('25000'))
        self.assertIsNone(data['wallet'])
        self.assertFalse(EmbedlyWallet.objects.filter(user=self.facility).exists())

    def test_facility_cannot_start_wallet_kyc(self):
        with mock.patch.object(embedly_client, 'create_corporate_customer') as corporate_call, \
                mock.patch.object(embedly_client, 'create_individual_customer') as individual_call:
            response = self.client.post('/api/v1/billing/wallet/kyc/', {'bvn': '22222222222'}, format='json')
        self.assertEqual(response.status_code, 400)
        corporate_call.assert_not_called()
        individual_call.assert_not_called()

    def test_registration_creates_no_wallet_record_for_facilities(self):
        from .wallet_service import WalletCreateService
        self.assertIsNone(WalletCreateService()(self.facility))
        self.assertFalse(EmbedlyWallet.objects.filter(user=self.facility).exists())


@override_settings(**{**EMBEDLY_TEST_SETTINGS, 'EMBEDLY_PLATFORM_ACCOUNT_NUMBER': ''})
class PlatformWalletTests(TestCase):
    def test_transfers_wait_until_a_platform_wallet_exists(self):
        pro = make_professional('0')
        activate(pro, '9710000111')
        tx, _ = ledger.credit(pro, '1000', 'PAYOUT', 'k-no-platform')
        t = WalletTransfer.objects.get(ledger_transaction=tx)
        with mock.patch.object(embedly_client, 'wallet_to_wallet') as send:
            t = process_transfer(t.pk)
        send.assert_not_called()
        self.assertEqual(t.status, WalletTransfer.QUEUED)

    def test_adopt_an_existing_wallet(self):
        found = ok({'id': 'w-1', 'customerId': 'c-1', 'name': 'Org Wallet',
                    'virtualAccount': {'accountNumber': '9710000999', 'bankName': 'Sterling Bank'}})
        with mock.patch.object(embedly_client, 'get_wallet_by_account', return_value=found):
            platform.adopt('9710000999')
        self.assertEqual(platform.platform_account_number(), '9710000999')

        with mock.patch.object(embedly_client, 'get_wallet_by_account',
                               return_value=rejected('The wallet you have specified does not exist')), \
                self.assertRaises(ValueError):
            platform.adopt('9710266834')

    @override_settings(EMBEDLY_PLATFORM_ACCOUNT_NUMBER='9719999999')
    def test_setting_overrides_the_stored_wallet(self):
        PlatformWallet.objects.create(status=EmbedlyWallet.ACTIVE, account_number='9710000999')
        self.assertEqual(platform.platform_account_number(), '9719999999')

    def test_corporate_platform_opens_once_approved(self):
        business = corporate.Business(name='Qwikshift Health Ltd', rc_number='RC1', tin='0001',
                                      address='1 Marina', city='Lagos', email='finance@shifta.ng')
        director = corporate.Director(
            first_name='Ada', last_name='Obi', email='ada@shifta.ng', phone='08030000000',
            dob=date(1980, 1, 1), address='1 Marina', bvn='22000000001', nin='33000000001',
            meter_number='450', disco_code='EKO',
        )
        docs = {field: corporate.make_document(f'{field}.pdf', PDF) for field in DOC_FIELDS}
        pending = rejected('Customer Approval is pending...', code='403')
        with mock.patch.object(embedly_client, 'create_corporate_customer', return_value=ok({'id': 'corp-p'})), \
                mock.patch.object(embedly_client, 'add_corporate_director', return_value=ok({'directorId': 'd'})), \
                mock.patch.object(embedly_client, 'upload_file', return_value=ok(UPLOADED_URL)), \
                mock.patch.object(embedly_client, 'submit_corporate_documents', return_value=ok(None)), \
                mock.patch.object(embedly_client, 'get_corporate_customer',
                                  return_value=ok({'isCorporateVerified': 'Pending'})), \
                mock.patch.object(embedly_client, 'create_corporate_wallet', return_value=pending):
            record = platform.setup_corporate(business, director, docs)
        self.assertEqual(record.status, EmbedlyWallet.PENDING_TIER)
        self.assertEqual(platform.platform_account_number(), '')

        opened = ok({'id': 'w-p', 'virtualAccount': {'accountNumber': '9710555555', 'bankName': 'Sterling Bank'}})
        with mock.patch.object(embedly_client, 'create_corporate_wallet', return_value=opened):
            platform.activate_platform_wallet()
        self.assertEqual(platform.platform_account_number(), '9710555555')


# -- Paystack card funding ------------------------------------------------------

from .wallet_service import WalletFundingVerifyService


def paystack_verify(status='success', amount=10162437, requested=10000000, currency='NGN'):
    """Shape of Paystack's verify response; defaults are a real test payment (fees passed on)."""
    response = mock.Mock()
    response.json.return_value = {'status': True, 'message': 'Verification successful', 'data': {
        'status': status, 'amount': amount, 'requested_amount': requested,
        'fees': amount - requested, 'currency': currency,
    }}
    return response


@override_settings(**EMBEDLY_TEST_SETTINGS, PAYSTACK_SECRET_KEY='sk_test_x')
class PaystackFundingVerifyTests(TestCase):
    def setUp(self):
        self.facility = make_facility('0')
        self.tx = Transaction.objects.create(
            user=self.facility, amount=Decimal('100000.00'), transaction_type='FUNDING',
            reference='2da54e5b54904b1786d544e994fc3148', status='PENDING',
        )

    def verify(self, response, user=None):
        with mock.patch('billing.wallet_service.requests.get', return_value=response):
            return WalletFundingVerifyService()(self.tx.reference, user=user or self.facility)

    def test_fee_added_on_top_still_credits_the_requested_amount(self):
        result = self.verify(paystack_verify())  # charged 101,624.37 for 100,000
        self.assertEqual(result['status'], 'success')
        self.assertEqual(balance_of(self.facility), Decimal('100000.00'))

    def test_redirect_and_webhook_both_verifying_credit_once(self):
        self.verify(paystack_verify())
        again = self.verify(paystack_verify())
        self.assertEqual(again['status'], 'already_verified')
        self.assertEqual(balance_of(self.facility), Decimal('100000.00'))

    def test_mismatched_or_unpaid_payments_are_not_credited(self):
        cases = {
            'different requested amount': paystack_verify(requested=500000, amount=500000),
            'charged less than requested': paystack_verify(amount=9000000),
            'not successful': paystack_verify(status='abandoned'),
            'wrong currency': paystack_verify(currency='USD'),
        }
        for name, response in cases.items():
            with self.subTest(name):
                Transaction.objects.filter(pk=self.tx.pk).update(status='PENDING')
                with self.assertRaises(ValueError):
                    self.verify(response)
                self.assertEqual(balance_of(self.facility), Decimal('0.00'))

    def test_abandoned_checkout_is_marked_failed_and_stays_failed(self):
        with self.assertRaisesMessage(ValueError, "wasn't completed on Paystack"):
            self.verify(paystack_verify(status='abandoned'))
        # The status change survives the error (it used to be rolled back).
        self.assertEqual(Transaction.objects.get(pk=self.tx.pk).status, 'FAILED')

    def test_payment_still_in_progress_stays_pending(self):
        with self.assertRaisesMessage(ValueError, "hasn't confirmed this payment yet"):
            self.verify(paystack_verify(status='ongoing'))
        self.assertEqual(Transaction.objects.get(pk=self.tx.pk).status, 'PENDING')

    def test_a_failed_payment_can_still_be_verified_later(self):
        Transaction.objects.filter(pk=self.tx.pk).update(status='FAILED')  # e.g. the fee bug
        self.assertEqual(self.verify(paystack_verify())['status'], 'success')
        self.assertEqual(balance_of(self.facility), Decimal('100000.00'))

    def test_another_facility_cannot_verify_this_payment(self):
        with self.assertRaises(ValueError):
            self.verify(paystack_verify(), user=make_facility('0'))
        self.assertEqual(balance_of(self.facility), Decimal('0.00'))


@override_settings(**EMBEDLY_TEST_SETTINGS, PAYSTACK_SECRET_KEY='sk_test_x')
class ReverifyCardPaymentsTests(TestCase):
    def test_recovers_payments_wrongly_marked_failed_once(self):
        from io import StringIO
        from django.core.management import call_command

        facility = make_facility('0')
        # What the 2026-10-09 release did to fee-inclusive payments.
        Transaction.objects.create(user=facility, amount=Decimal('100000.00'), transaction_type='FUNDING',
                                   reference='ref-wrongly-failed', status='FAILED')
        with mock.patch('billing.wallet_service.requests.get', return_value=paystack_verify()):
            call_command('reverify_card_payments', '--apply', stdout=StringIO())
            call_command('reverify_card_payments', '--apply', stdout=StringIO())  # rerun is harmless

        self.assertEqual(balance_of(facility), Decimal('100000.00'))
        self.assertEqual(Transaction.objects.get(reference='ref-wrongly-failed').status, 'SUCCESS')


# -- every naira accounted for -----------------------------------------------------

@override_settings(**EMBEDLY_TEST_SETTINGS, PAYSTACK_SECRET_KEY='sk_test_x')
class MoneyAuditTests(TestCase):
    def statement(self, user):
        from .accounting import statements
        return next(s for s in statements() if s.user_id == user.pk)

    def fund_by_paystack(self, user, amount, ref):
        Transaction.objects.create(user=user, amount=Decimal(amount), transaction_type='FUNDING',
                                   reference=ref, status='PENDING')
        kobo = int(Decimal(amount) * 100)
        with mock.patch('billing.wallet_service.requests.get', return_value=paystack_verify(amount=kobo, requested=kobo)):
            WalletFundingVerifyService()(ref)

    def test_paid_in_money_is_backed_and_clean(self):
        facility = make_facility('0')
        self.fund_by_paystack(facility, '100000', 'ref-clean')
        ledger.debit(facility, '30000', 'CHARGE', 'k-audit-charge')
        st = self.statement(facility)
        self.assertEqual((st.balance, st.ledger_sum, st.untracked, st.over), (Decimal('70000.00'),) * 2 + (0, 0))

    def test_new_accounts_cannot_start_with_money(self):
        user = User.objects.create_user(email='rich@test.ng', password='x')
        with self.assertRaises(ValueError):
            Facility.objects.create(user=user, name='X', address='Y', rc_number='RC-RICH', wallet_balance=Decimal('1000'))

    def test_balance_edited_outside_the_ledger_is_untracked_and_written_off(self):
        from .accounting import write_off
        facility = make_facility('0')
        self.fund_by_paystack(facility, '50000', 'ref-real')
        Facility.objects.filter(user=facility).update(wallet_balance=Decimal('2000000'))  # a direct edit

        st = self.statement(facility)
        self.assertEqual((st.untracked, st.entitled, st.over), (Decimal('1950000.00'), Decimal('50000.00'), Decimal('1950000.00')))

        self.assertEqual(write_off(st), Decimal('1950000.00'))
        after = self.statement(facility)
        self.assertEqual((after.balance, after.untracked, after.over), (Decimal('50000.00'), 0, 0))
        # The trail: the found money was recorded, then written off.
        self.assertTrue(Transaction.objects.filter(user=facility, idempotency_key__startswith='audit-recognise:').exists())
        self.assertTrue(Transaction.objects.filter(user=facility, idempotency_key__startswith='audit-writeoff:').exists())
        self.assertEqual(write_off(after), 0)  # rerun: nothing left

    def test_seed_money_and_unreferenced_admin_funding_are_unbacked(self):
        facility = make_facility('0')
        ledger.credit(facility, '500000', 'ADJUSTMENT', 'seed:x', settle=False)
        ledger.credit(facility, '10000', 'FUNDING', 'admin-fund:no-ref', metadata={'admin': 'a@b.c'})
        ledger.credit(facility, '20000', 'FUNDING', 'admin-fund:with-ref',
                      metadata={'admin': 'a@b.c', 'payment_reference': 'NIP-123'})
        st = self.statement(facility)
        self.assertEqual((st.backed_in, st.entitled, st.over), (Decimal('20000.00'), Decimal('20000.00'), Decimal('510000.00')))

    def test_missing_money_is_reported_never_auto_corrected(self):
        from .accounting import write_off
        facility = make_facility('0')
        self.fund_by_paystack(facility, '50000', 'ref-missing')
        Facility.objects.filter(user=facility).update(wallet_balance=Decimal('40000'))
        st = self.statement(facility)
        self.assertEqual(st.untracked, Decimal('-10000.00'))
        with self.assertRaises(ValueError):
            write_off(st)
        self.assertEqual(balance_of(facility), Decimal('40000'))

    def test_legacy_rows_seed_samples_ignored_real_shift_charges_counted(self):
        facility = make_facility('0')
        start = timezone.now() - timedelta(days=30)
        shift = Shift.objects.create(facility=facility.facility, role='Nurse', specialty='ICU', quantity_needed=1,
                                     quantity_filled=1, status='COMPLETED', start_time=start,
                                     end_time=start + timedelta(hours=8), rate=Decimal('2500'))
        Facility.objects.filter(user=facility).update(wallet_balance=Decimal('80000'))  # old system
        Transaction.objects.create(user=facility, amount=Decimal('100000'), transaction_type='FUNDING',
                                   reference='old-paystack', status='SUCCESS')  # pre-ledger card payment
        Transaction.objects.create(user=facility, amount=Decimal('20000'), transaction_type='CHARGE',
                                   reference='old-charge', status='SUCCESS', shift=shift)  # real
        Transaction.objects.create(user=facility, amount=Decimal('42707'), transaction_type='CHARGE',
                                   reference='seed-sample', status='SUCCESS')  # seed sample: no shift
        st = self.statement(facility)
        self.assertEqual((st.legacy_in, st.ledger_sum, st.untracked, st.over),
                         (Decimal('100000.00'), Decimal('80000.00'), 0, 0))

    def test_old_unrecorded_expiry_refunds_are_rebuilt_and_recorded(self):
        from .accounting import write_off
        facility = make_facility('0')
        self.fund_by_paystack(facility, '100000', 'ref-expiry')
        start = timezone.now() - timedelta(days=3)
        shift = Shift.objects.create(facility=facility.facility, role='Nurse', specialty='ICU', quantity_needed=2,
                                     quantity_filled=1, status='COMPLETED', start_time=start,
                                     end_time=start + timedelta(hours=8), rate=Decimal('2500'))
        ledger.debit(facility, '40000', 'CHARGE', 'k-old-shift', shift=shift)
        # The old expiry job refunded the unfilled slot (20,000) without a record.
        Facility.objects.filter(user=facility).update(wallet_balance=Decimal('80000'))

        st = self.statement(facility)
        self.assertEqual((st.inferred_refunds, st.untracked, st.over), (Decimal('20000.00'), 0, 0))
        write_off(st)  # nothing to write off, but the refund gets recorded
        refund = Transaction.objects.get(idempotency_key=f'legacy-expiry-refund:{shift.pk}')
        self.assertEqual((refund.amount, refund.shift_id), (Decimal('20000.00'), shift.pk))
        self.assertEqual(balance_of(facility), Decimal('80000.00'))
        self.assertEqual(self.statement(facility).inferred_refunds, 0)

    def test_system_shortfall_when_unbacked_money_was_spent(self):
        from .accounting import system_position
        facility = make_facility('0')
        pro = make_professional('0')
        self.fund_by_paystack(facility, '10000', 'ref-small')
        ledger.credit(facility, '90000', 'ADJUSTMENT', 'seed:y', settle=False)  # fake money
        ledger.debit(facility, '50000', 'CHARGE', 'k-spend')
        ledger.credit(pro, '50000', 'PAYOUT', 'k-pay-real')  # a real payout from fake money
        with mock.patch.object(embedly_client, 'get_wallet_by_account', return_value=ok({'availableBalance': 0})):
            pos = system_position()
        self.assertEqual(pos['money_in'], Decimal('10000.00'))
        self.assertEqual(pos['owes'], Decimal('100000.00'))  # 50,000 facility + 50,000 professional
        self.assertEqual(pos['shortfall'], Decimal('90000.00'))

    def test_audit_command_dry_run_then_confirm(self):
        from io import StringIO
        from django.core.management import call_command
        facility = make_facility('0')
        Facility.objects.filter(user=facility).update(wallet_balance=Decimal('5000'))

        out = StringIO()
        with mock.patch.object(embedly_client, 'get_wallet_by_account', return_value=ok({'availableBalance': 0})):
            call_command('audit_money', '--write-off', stdout=out)
            self.assertIn('would write off', out.getvalue())
            self.assertEqual(balance_of(facility), Decimal('5000'))
            call_command('audit_money', '--write-off', '--confirm', stdout=StringIO())
        self.assertEqual(balance_of(facility), Decimal('0.00'))

    def test_seed_command_refuses_without_confirm(self):
        from django.core.management import CommandError, call_command
        with self.assertRaises(CommandError):
            call_command('seed_data')


class OverdrawnShiftAuditTests(TestCase):
    """nurse1 on the real data: old payouts from a never-charged (seeded) shift are not real money."""

    def test_payouts_beyond_a_shifts_charges_are_unbacked(self):
        from .accounting import statements
        facility = make_facility('0')
        pro = make_professional('0')
        start = timezone.now() - timedelta(days=40)
        seeded = Shift.objects.create(facility=facility.facility, role='Day Nurse', specialty='ICU',
                                      quantity_needed=1, quantity_filled=1, status='COMPLETED', start_time=start,
                                      end_time=start + timedelta(hours=8), rate=Decimal('2500'))
        real = Shift.objects.create(facility=facility.facility, role='Nurse', specialty='ICU',
                                    quantity_needed=1, quantity_filled=1, status='COMPLETED', start_time=start,
                                    end_time=start + timedelta(hours=8), rate=Decimal('2500'))
        # Old system: real shift charged 20,000 and paid out 20,000; the seeded one paid 162,000 from nothing.
        Transaction.objects.create(user=facility, amount=Decimal('20000'), transaction_type='CHARGE',
                                   reference='l-charge', status='SUCCESS', shift=real)
        Transaction.objects.create(user=pro, amount=Decimal('20000'), transaction_type='PAYOUT',
                                   reference='l-pay-real', status='SUCCESS', shift=real)
        Transaction.objects.create(user=pro, amount=Decimal('162000'), transaction_type='PAYOUT',
                                   reference='l-pay-seeded', status='SUCCESS', shift=seeded)
        Professional.objects.filter(user=pro).update(wallet_balance=Decimal('182000'))

        st = next(s for s in statements() if s.user_id == pro.pk)
        self.assertEqual((st.untracked, st.entitled, st.over),
                         (Decimal('0.00'), Decimal('20000.00'), Decimal('162000.00')))
