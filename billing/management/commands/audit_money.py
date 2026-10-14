"""
Account for every naira: no balance may hold money no real payment backs.

    python manage.py audit_money                        # report (read-only)
    python manage.py audit_money --all                  # include clean accounts
    python manage.py audit_money --write-off            # show what would be written off
    python manage.py audit_money --write-off --confirm  # do it

For each facility and professional it shows the balance, what the ledger
explains, untracked money (balance with no ledger entry), and "over" money:
balance above what real payments entitle the account to (see
billing.accounting). Then the whole-system position: cash Shifta should hold
vs what it owes.

--write-off --confirm records untracked balances in the ledger and debits the
unbacked excess as ADJUSTMENT entries, so every remaining naira is backed and
traceable. Missing money (a balance below its own ledger) is reported, never
auto-corrected.
"""
import uuid

from django.core.management.base import BaseCommand

from billing.accounting import statements, system_position, write_off


class Command(BaseCommand):
    help = "Prove every balance is backed by real money; optionally write off unbacked money."

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='List clean accounts too.')
        parser.add_argument('--write-off', action='store_true', help='Write off unbacked money (dry run without --confirm).')
        parser.add_argument('--confirm', action='store_true', help='Actually post the write-offs.')

    def handle(self, *args, **options):
        all_statements = statements()
        header = (f"{'account':<38} {'kind':<12} {'balance':>13} {'ledger':>13} {'untracked':>12} "
                  f"{'paid in':>12} {'entitled':>12} {'OVER':>12}")
        self.stdout.write(header)
        self.stdout.write('-' * len(header))

        flagged = []
        for st in sorted(all_statements, key=lambda s: (s.kind, -s.balance)):
            problem = st.untracked != 0 or st.over > 0
            if problem:
                flagged.append(st)
            if not (problem or options['all']):
                continue
            line = (f"{st.email[:38]:<38} {st.kind:<12} {st.balance:>13} {st.ledger_sum:>13} {st.untracked:>12} "
                    f"{st.backed_in + st.legacy_in:>12} {st.entitled:>12} {st.over:>12}")
            style = self.style.ERROR if st.untracked < 0 else self.style.WARNING if problem else self.style.SUCCESS
            self.stdout.write(style(line))
            if st.legacy_in:
                self.stdout.write(f"    {st.legacy_in} of 'paid in' is pre-ledger funding (not re-verified)")
            if st.untracked < 0:
                self.stdout.write(self.style.ERROR(f"    {-st.untracked} MISSING: the balance is below its own ledger"))

        total_over = sum(s.over for s in all_statements)
        total_untracked = sum(s.untracked for s in all_statements if s.untracked > 0)
        self.stdout.write('')
        if not flagged:
            self.stdout.write(self.style.SUCCESS("Every balance is explained by its ledger and backed by real payments."))
        else:
            self.stdout.write(self.style.WARNING(
                f"{len(flagged)} account(s) need attention: {total_untracked} untracked, {total_over} unbacked (over)."
            ))

        pos = system_position(all_statements)
        self.stdout.write('')
        self.stdout.write("System position")
        for label, key in (('real money in (verified payments)', 'money_in'),
                           ('paid out to banks', 'paid_out_to_banks'),
                           ('Shifta should hold', 'should_hold'),
                           ('owed: all balances', 'balances'),
                           ('owed: escrow on open shifts', 'shift_escrow'),
                           ('owed: completed work not yet paid', 'unpaid_completed_work'),
                           ('owed in total', 'owes')):
            self.stdout.write(f"  {label:<36} {pos[key]:>14}")
        if pos['shortfall']:
            self.stdout.write(self.style.ERROR(
                f"  SHORTFALL {pos['shortfall']}: the app owes more than was ever paid in "
                f"(unbacked money has been credited or spent)."
            ))
        else:
            self.stdout.write(self.style.SUCCESS("  Covered: everything owed is backed by money received."))

        if options['write_off']:
            self._write_off([s for s in flagged if s.untracked >= 0 and (s.over > 0 or s.untracked > 0)],
                            options['confirm'])

    def _write_off(self, targets, confirm):
        self.stdout.write('')
        if not targets:
            self.stdout.write("Nothing to write off.")
            return
        if not confirm:
            for st in targets:
                self.stdout.write(f"would write off {st.over:>12} from {st.email} "
                                  f"(balance {st.balance} -> {st.balance - st.over})")
            self.stdout.write("Dry run. Re-run with --confirm to post these as ADJUSTMENT entries.")
            return
        run_id = uuid.uuid4().hex[:12]
        for st in targets:
            amount = write_off(st, run_id=run_id)
            self.stdout.write(self.style.SUCCESS(f"wrote off {amount:>12} from {st.email} (run {run_id})"))
