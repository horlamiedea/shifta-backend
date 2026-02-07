"""
Django Management Command to seed the database with mock data for testing.

Usage:
    python manage.py seed_data

This will create:
- 2 Facility users with facilities
- 5 Professional users with profiles
- 10 Shifts (various statuses)
- 15 Shift Applications
- Sample Transactions
- Sample Notifications
- Sample Chat Rooms and Messages
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
import uuid
import random

from accounts.models import User, Professional, Facility
from shifts.models import Shift, ShiftApplication
from billing.models import Transaction, Invoice
from communications.models import ChatRoom, Message
from core.models import Notification


class Command(BaseCommand):
    help = 'Seeds the database with mock data for testing'

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING('Seeding database with mock data...'))
        
        # Clear existing data (optional - comment out if you want to keep existing data)
        self.stdout.write('Clearing existing data...')
        Message.objects.all().delete()
        ChatRoom.objects.all().delete()
        Notification.objects.all().delete()
        Transaction.objects.all().delete()
        Invoice.objects.all().delete()
        ShiftApplication.objects.all().delete()
        Shift.objects.all().delete()
        Professional.objects.all().delete()
        Facility.objects.all().delete()
        User.objects.filter(is_superuser=False).delete()
        
        # Create Facilities
        self.stdout.write('Creating facilities...')
        facilities = self.create_facilities()
        
        # Create Professionals
        self.stdout.write('Creating professionals...')
        professionals = self.create_professionals()
        
        # Create Shifts
        self.stdout.write('Creating shifts...')
        shifts = self.create_shifts(facilities)
        
        # Create Applications
        self.stdout.write('Creating shift applications...')
        applications = self.create_applications(shifts, professionals)
        
        # Create Transactions
        self.stdout.write('Creating transactions...')
        self.create_transactions(facilities, professionals)
        
        # Create Invoices
        self.stdout.write('Creating invoices...')
        self.create_invoices(facilities)
        
        # Create Notifications
        self.stdout.write('Creating notifications...')
        self.create_notifications(facilities, professionals)
        
        # Create Chat Rooms and Messages
        self.stdout.write('Creating chat rooms and messages...')
        self.create_chat_data(applications)
        
        self.stdout.write(self.style.SUCCESS('✅ Database seeded successfully!'))
        self.print_credentials()

    def create_facilities(self):
        facilities = []
        facility_data = [
            {
                'email': 'facility1@shifta.com',
                'password': 'password123',
                'name': 'Lagos General Hospital',
                'address': '123 Marina Road, Lagos Island, Lagos',
                'rc_number': 'RC123456',
                'is_verified': True,
                'wallet_balance': Decimal('500000.00'),
                'location_lat': 6.4541,
                'location_lng': 3.3947,
            },
            {
                'email': 'facility2@shifta.com',
                'password': 'password123',
                'name': 'Lekki Medical Center',
                'address': '45 Admiralty Way, Lekki Phase 1, Lagos',
                'rc_number': 'RC789012',
                'is_verified': True,
                'wallet_balance': Decimal('250000.00'),
                'location_lat': 6.4281,
                'location_lng': 3.4219,
            },
        ]
        
        for data in facility_data:
            # Create user without is_facility flag (it's a property based on related Facility)
            user = User.objects.create_user(
                email=data['email'],
                password=data['password'],
            )
            # Create the Facility which makes user.is_facility return True
            facility = Facility.objects.create(
                user=user,
                name=data['name'],
                address=data['address'],
                rc_number=data['rc_number'],
                is_verified=data['is_verified'],
                wallet_balance=data['wallet_balance'],
                location_lat=data['location_lat'],
                location_lng=data['location_lng'],
            )
            facilities.append(facility)
        
        return facilities

    def create_professionals(self):
        professionals = []
        specialties_list = [
            ['Registered Nurse (General)', 'ICU Nurse'],
            ['Emergency Physician', 'General Practitioner (GP)'],
            ['Pharmacist'],
            ['Physiotherapist'],
            ['Medical Laboratory Scientist'],
        ]
        
        professional_data = [
            {
                'email': 'nurse1@shifta.com',
                'password': 'password123',
                'first_name': 'Adaeze',
                'last_name': 'Okonkwo',
                'license_number': 'NMC/RN/2020/12345',
                'specialties': specialties_list[0],
                'is_verified': True,
                'wallet_balance': Decimal('75000.00'),
                'current_location_lat': 6.4600,
                'current_location_lng': 3.3800,
            },
            {
                'email': 'doctor1@shifta.com',
                'password': 'password123',
                'first_name': 'Chukwuemeka',
                'last_name': 'Eze',
                'license_number': 'MDCN/2019/54321',
                'specialties': specialties_list[1],
                'is_verified': True,
                'wallet_balance': Decimal('150000.00'),
                'current_location_lat': 6.4350,
                'current_location_lng': 3.4100,
            },
            {
                'email': 'pharmacist1@shifta.com',
                'password': 'password123',
                'first_name': 'Funke',
                'last_name': 'Adeyemi',
                'license_number': 'PCN/2021/98765',
                'specialties': specialties_list[2],
                'is_verified': True,
                'wallet_balance': Decimal('50000.00'),
                'current_location_lat': 6.4450,
                'current_location_lng': 3.4000,
            },
            {
                'email': 'physio1@shifta.com',
                'password': 'password123',
                'first_name': 'Tunde',
                'last_name': 'Bakare',
                'license_number': 'MRTB/PT/2020/11111',
                'specialties': specialties_list[3],
                'is_verified': True,
                'wallet_balance': Decimal('35000.00'),
                'current_location_lat': 6.4200,
                'current_location_lng': 3.4300,
            },
            {
                'email': 'labtech1@shifta.com',
                'password': 'password123',
                'first_name': 'Ngozi',
                'last_name': 'Nnamdi',
                'license_number': 'MLSCN/2022/22222',
                'specialties': specialties_list[4],
                'is_verified': False,  # Unverified for testing
                'wallet_balance': Decimal('0.00'),
                'current_location_lat': 6.4500,
                'current_location_lng': 3.3900,
            },
        ]
        
        for data in professional_data:
            # Create user without is_professional flag (it's a property based on related Professional)
            user = User.objects.create_user(
                email=data['email'],
                password=data['password'],
                first_name=data['first_name'],
                last_name=data['last_name'],
            )
            # Create the Professional which makes user.is_professional return True
            professional = Professional.objects.create(
                user=user,
                license_number=data['license_number'],
                specialties=data['specialties'],
                is_verified=data['is_verified'],
                wallet_balance=data['wallet_balance'],
                current_location_lat=data['current_location_lat'],
                current_location_lng=data['current_location_lng'],
            )
            professionals.append(professional)
        
        return professionals

    def create_shifts(self, facilities):
        shifts = []
        now = timezone.now()
        
        shift_templates = [
            # Upcoming shifts
            {'role': 'ICU Nurse', 'specialty': 'ICU Nurse', 'status': 'OPEN', 'days_offset': 1, 'hours': 12, 'rate': 25000, 'qty': 3},
            {'role': 'ER Nurse', 'specialty': 'Emergency Room (ER) Nurse', 'status': 'OPEN', 'days_offset': 2, 'hours': 9, 'rate': 20000, 'qty': 2},
            {'role': 'General Nurse', 'specialty': 'Registered Nurse (General)', 'status': 'OPEN', 'days_offset': 3, 'hours': 12, 'rate': 18000, 'qty': 4},
            {'role': 'Night Nurse', 'specialty': 'Registered Nurse (General)', 'status': 'OPEN', 'days_offset': 1, 'hours': 12, 'rate': 22000, 'qty': 2},
            # In progress
            {'role': 'Day Nurse', 'specialty': 'Registered Nurse (General)', 'status': 'FILLED', 'days_offset': 0, 'hours': 9, 'rate': 18000, 'qty': 1},
            # Past shifts
            {'role': 'ICU Nurse', 'specialty': 'ICU Nurse', 'status': 'COMPLETED', 'days_offset': -2, 'hours': 12, 'rate': 25000, 'qty': 2},
            {'role': 'Pharmacist', 'specialty': 'Pharmacist', 'status': 'COMPLETED', 'days_offset': -3, 'hours': 9, 'rate': 15000, 'qty': 1},
            # More upcoming
            {'role': 'Physiotherapist', 'specialty': 'Physiotherapist', 'status': 'OPEN', 'days_offset': 5, 'hours': 6, 'rate': 12000, 'qty': 1},
            {'role': 'Lab Technician', 'specialty': 'Medical Laboratory Scientist', 'status': 'OPEN', 'days_offset': 4, 'hours': 8, 'rate': 14000, 'qty': 2},
            {'role': 'Emergency Doctor', 'specialty': 'Emergency Physician', 'status': 'OPEN', 'days_offset': 2, 'hours': 12, 'rate': 50000, 'qty': 1},
        ]
        
        for i, template in enumerate(shift_templates):
            facility = facilities[i % len(facilities)]
            start_time = now + timedelta(days=template['days_offset'], hours=8)
            end_time = start_time + timedelta(hours=template['hours'])
            
            shift = Shift.objects.create(
                facility=facility,
                role=template['role'],
                specialty=template['specialty'],
                quantity_needed=template['qty'],
                quantity_filled=template['qty'] if template['status'] in ['FILLED', 'COMPLETED'] else 0,
                start_time=start_time,
                end_time=end_time,
                rate=Decimal(str(template['rate'])),
                status=template['status'],
                is_negotiable=random.choice([True, False]),
                min_rate=Decimal(str(template['rate'] * 0.8)) if random.choice([True, False]) else None,
                address=facility.address,
                latitude=facility.location_lat,
                longitude=facility.location_lng,
            )
            shifts.append(shift)
        
        return shifts

    def create_applications(self, shifts, professionals):
        applications = []
        statuses = ['PENDING', 'CONFIRMED', 'REJECTED', 'CANCELLED', 'IN_PROGRESS', 'COMPLETED']
        
        # Create applications for various shifts
        application_configs = [
            # Pending applications for open shifts
            {'shift_idx': 0, 'pro_idx': 0, 'status': 'PENDING'},
            {'shift_idx': 0, 'pro_idx': 1, 'status': 'PENDING'},
            {'shift_idx': 1, 'pro_idx': 0, 'status': 'CONFIRMED'},
            {'shift_idx': 2, 'pro_idx': 0, 'status': 'PENDING'},
            {'shift_idx': 2, 'pro_idx': 1, 'status': 'REJECTED'},
            # In progress
            {'shift_idx': 4, 'pro_idx': 0, 'status': 'IN_PROGRESS', 'clocked_in': True},
            # Completed
            {'shift_idx': 5, 'pro_idx': 0, 'status': 'COMPLETED', 'clocked_in': True, 'clocked_out': True},
            {'shift_idx': 5, 'pro_idx': 1, 'status': 'COMPLETED', 'clocked_in': True, 'clocked_out': True},
            {'shift_idx': 6, 'pro_idx': 2, 'status': 'COMPLETED', 'clocked_in': True, 'clocked_out': True},
            # More pending
            {'shift_idx': 7, 'pro_idx': 3, 'status': 'PENDING'},
            {'shift_idx': 8, 'pro_idx': 4, 'status': 'PENDING'},
            {'shift_idx': 9, 'pro_idx': 1, 'status': 'CONFIRMED'},
            # Cancelled
            {'shift_idx': 3, 'pro_idx': 0, 'status': 'CANCELLED'},
        ]
        
        now = timezone.now()
        
        for config in application_configs:
            shift = shifts[config['shift_idx']]
            professional = professionals[config['pro_idx']]
            
            clock_in = None
            clock_out = None
            
            if config.get('clocked_in'):
                clock_in = shift.start_time
            if config.get('clocked_out'):
                clock_out = shift.end_time
            
            app = ShiftApplication.objects.create(
                shift=shift,
                professional=professional,
                status=config['status'],
                clock_in_time=clock_in,
                clock_out_time=clock_out,
            )
            applications.append(app)
        
        return applications

    def create_transactions(self, facilities, professionals):
        now = timezone.now()
        
        # Facility transactions (charges)
        for facility in facilities:
            for i in range(3):
                Transaction.objects.create(
                    user=facility.user,
                    amount=Decimal(str(random.randint(15000, 50000))),
                    transaction_type='CHARGE',
                    reference=str(uuid.uuid4()),
                    status='SUCCESS',
                )
        
        # Professional transactions (payouts)
        for professional in professionals[:3]:  # Only verified ones
            for i in range(2):
                Transaction.objects.create(
                    user=professional.user,
                    amount=Decimal(str(random.randint(10000, 30000))),
                    transaction_type='PAYOUT',
                    reference=str(uuid.uuid4()),
                    status='SUCCESS',
                )

    def create_invoices(self, facilities):
        from datetime import date
        now = timezone.now()
        
        for facility in facilities:
            # Last month invoice - use first day of month as DateField
            last_month = now.month - 1 if now.month > 1 else 12
            last_year = now.year if now.month > 1 else now.year - 1
            Invoice.objects.create(
                facility=facility,
                month=date(last_year, last_month, 1),  # First day of last month
                amount=Decimal(str(random.randint(100000, 300000))),
                status='PAID',
            )
            # Current month invoice
            Invoice.objects.create(
                facility=facility,
                month=date(now.year, now.month, 1),  # First day of current month
                amount=Decimal(str(random.randint(50000, 150000))),
                status='PENDING',
            )

    def create_notifications(self, facilities, professionals):
        notification_types = [
            ('SHIFT_POSTED', 'New Shift Available', 'A new ICU Nurse shift is available near you.'),
            ('BOOKED', 'Shift Confirmed', 'Your application for the ICU Nurse shift has been confirmed.'),
            ('REMINDER', 'Shift Reminder', 'Your shift at Lagos General Hospital starts in 2 hours.'),
            ('BROADCAST', 'Message from Facility', 'Please arrive 15 minutes early for your shift.'),
            ('INVOICE_GENERATED', 'Invoice Ready', 'Your monthly invoice for January 2026 is ready.'),
        ]
        
        # Notifications for facilities
        for facility in facilities:
            for ntype, title, message in notification_types[:2]:
                Notification.objects.create(
                    user=facility.user,
                    notification_type=ntype,
                    title=title,
                    message=message,
                    is_read=random.choice([True, False]),
                )
        
        # Notifications for professionals
        for professional in professionals:
            for ntype, title, message in notification_types:
                Notification.objects.create(
                    user=professional.user,
                    notification_type=ntype,
                    title=title,
                    message=message,
                    is_read=random.choice([True, False]),
                )

    def create_chat_data(self, applications):
        # Create chat rooms for confirmed/in-progress applications
        for app in applications:
            if app.status in ['CONFIRMED', 'IN_PROGRESS', 'COMPLETED']:
                room = ChatRoom.objects.create(application=app)
                
                # Add some messages
                messages_data = [
                    (app.shift.facility.user, "Hello! Looking forward to having you on the shift."),
                    (app.professional.user, "Thank you! I'll be there on time."),
                    (app.shift.facility.user, "Great! Please report to the reception desk when you arrive."),
                ]
                
                for sender, content in messages_data:
                    Message.objects.create(
                        room=room,
                        sender=sender,
                        content=content,
                    )

    def print_credentials(self):
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=' * 60))
        self.stdout.write(self.style.SUCCESS('TEST CREDENTIALS'))
        self.stdout.write(self.style.SUCCESS('=' * 60))
        self.stdout.write('')
        self.stdout.write(self.style.WARNING('FACILITIES:'))
        self.stdout.write('  Email: facility1@shifta.com')
        self.stdout.write('  Password: password123')
        self.stdout.write('  Name: Lagos General Hospital')
        self.stdout.write('')
        self.stdout.write('  Email: facility2@shifta.com')
        self.stdout.write('  Password: password123')
        self.stdout.write('  Name: Lekki Medical Center')
        self.stdout.write('')
        self.stdout.write(self.style.WARNING('PROFESSIONALS:'))
        self.stdout.write('  Email: nurse1@shifta.com')
        self.stdout.write('  Password: password123')
        self.stdout.write('  Name: Adaeze Okonkwo (ICU Nurse)')
        self.stdout.write('')
        self.stdout.write('  Email: doctor1@shifta.com')
        self.stdout.write('  Password: password123')
        self.stdout.write('  Name: Chukwuemeka Eze (Emergency Physician)')
        self.stdout.write('')
        self.stdout.write('  Email: pharmacist1@shifta.com')
        self.stdout.write('  Password: password123')
        self.stdout.write('  Name: Funke Adeyemi (Pharmacist)')
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=' * 60))
