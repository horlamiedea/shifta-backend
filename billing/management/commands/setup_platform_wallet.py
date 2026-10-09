"""
Set up Shifta's platform wallet on Embedly (escrow for shift payments).

    python manage.py setup_platform_wallet                       # status; retry opening
    python manage.py setup_platform_wallet --config platform.json  # production: as a business
    python manage.py setup_platform_wallet --adopt 9710000000      # use an existing wallet
    python manage.py setup_platform_wallet --staging-individual    # staging: instant test wallet

--config takes a JSON file (document paths relative to the file):

    {
      "business": {"name": "Qwikshift Health Ltd", "rc_number": "RC123456",
                   "tin": "01234567-0001", "address": "...", "city": "Lagos",
                   "email": "finance@qwikshifthealth.com"},
      "director": {"first_name": "...", "last_name": "...", "middle_name": "",
                   "email": "...", "phone": "080...", "dob": "1985-01-31",
                   "address": "...", "bvn": "...", "nin": "...",
                   "meter_number": "...", "disco_code": "EKO"},
      "documents": {"cac": "docs/cac.pdf", "tin_document": "docs/tin.pdf",
                    "board_resolution": "docs/board.pdf", "utility_bill": "docs/bill.pdf",
                    "memart": "docs/memart.pdf", "scuml": "docs/scuml.pdf"}
    }

The business wallet opens once Embedly approves the business; the
provision-missing-wallets task retries every 30 minutes, or rerun this command.
"""
import json
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from billing import corporate, platform
from billing.models import EmbedlyWallet


class Command(BaseCommand):
    help = "Create or adopt Shifta's platform (escrow) wallet on Embedly."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--config', help='JSON file: business, director and document paths.')
        group.add_argument('--adopt', metavar='ACCOUNT_NUMBER', help='Use an existing wallet in this organisation.')
        group.add_argument('--staging-individual', action='store_true',
                           help='Staging only: open an instant Tier-1 test wallet.')
        parser.add_argument('--email', default='platform@shifta-test.ng',
                            help='Contact email for --staging-individual (must be unique on Embedly).')

    def handle(self, *args, **options):
        try:
            if options['adopt']:
                record = platform.adopt(options['adopt'])
            elif options['staging_individual']:
                record = platform.setup_staging_individual(options['email'])
            elif options['config']:
                record = platform.setup_corporate(**self._load_config(Path(options['config'])))
            else:
                record = platform.activate_platform_wallet()
        except ValueError as exc:
            raise CommandError(str(exc))

        self._report(record)

    def _load_config(self, path):
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise CommandError(f"Could not read {path}: {exc}")

        business = corporate.Business(**config['business'])
        director = None
        if config.get('director'):
            d = dict(config['director'])
            d['dob'] = date.fromisoformat(d['dob'])
            director = corporate.Director(**d)
        documents = None
        if config.get('documents'):
            documents = {}
            for field, relative in config['documents'].items():
                file_path = (path.parent / relative).resolve()
                try:
                    documents[field] = corporate.make_document(file_path.name, file_path.read_bytes())
                except OSError as exc:
                    raise CommandError(f"Could not read {field} at {file_path}: {exc}")
        return {'business': business, 'director': director, 'documents': documents}

    def _report(self, record):
        if record is None:
            self.stdout.write(self.style.WARNING(
                "No platform wallet yet. Run with --config, --adopt or --staging-individual."
            ))
            return
        if record.status == EmbedlyWallet.ACTIVE:
            self.stdout.write(self.style.SUCCESS(
                f"Platform wallet open: {record.account_number} ({record.bank_name}) [{record.kind}]"
            ))
            self.stdout.write("Queued transfers will now settle on the next sweep (every 5 minutes).")
            return
        self.stdout.write(self.style.WARNING(f"Platform wallet {record.status} [{record.kind}]"))
        if record.meta.get('approval'):
            self.stdout.write(f"Embedly business review: {record.meta['approval']}")
        if record.meta.get('last_error'):
            self.stdout.write(f"Embedly: {record.meta['last_error']}")
