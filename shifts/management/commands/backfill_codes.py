from django.core.management.base import BaseCommand
from shifts.models import ShiftApplication


class Command(BaseCommand):
    help = 'Generate check-in/check-out codes for applications that are missing them'

    def handle(self, *args, **options):
        apps = ShiftApplication.objects.filter(
            status__in=['CONFIRMED', 'IN_PROGRESS', 'ATTENDANCE_PENDING'],
        ).filter(
            # Missing either code
            check_in_code__isnull=True,
        ) | ShiftApplication.objects.filter(
            status__in=['CONFIRMED', 'IN_PROGRESS', 'ATTENDANCE_PENDING'],
            check_in_code='',
        )

        # Also catch missing check_out_code
        apps_out = ShiftApplication.objects.filter(
            status__in=['CONFIRMED', 'IN_PROGRESS', 'ATTENDANCE_PENDING'],
        ).filter(
            check_out_code__isnull=True,
        ) | ShiftApplication.objects.filter(
            status__in=['CONFIRMED', 'IN_PROGRESS', 'ATTENDANCE_PENDING'],
            check_out_code='',
        )

        all_ids = set(apps.values_list('id', flat=True)) | set(apps_out.values_list('id', flat=True))
        to_fix = ShiftApplication.objects.filter(id__in=all_ids)

        count = 0
        for app in to_fix:
            updated = False
            if not app.check_in_code:
                app.check_in_code = ShiftApplication.generate_code()
                updated = True
            if not app.check_out_code:
                app.check_out_code = ShiftApplication.generate_code()
                updated = True
            if updated:
                app.save(update_fields=['check_in_code', 'check_out_code', 'updated_at'])
                count += 1
                self.stdout.write(
                    f"  {app.professional} -> "
                    f"check-in: {app.check_in_code}, check-out: {app.check_out_code}"
                )

        self.stdout.write(self.style.SUCCESS(f'\nDone. Generated codes for {count} application(s).'))
