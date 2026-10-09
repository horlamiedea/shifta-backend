from rest_framework import status
from rest_framework.test import APITestCase

from .models import WaitlistFacility, WaitlistProfessional


class ProfessionalWaitlistTests(APITestCase):
    endpoint = '/api/v1/auth/waitlist/'

    def test_professional_can_join_without_documents(self):
        response = self.client.post(
            self.endpoint,
            {
                'email': 'nurse@example.com',
                'full_name': 'Amara Okafor',
                'phone_number': '+2348012345678',
                'medical_type': 'Registered Nurse (General)',
                'location': 'Lagos',
                'years_of_experience': 4,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        professional = WaitlistProfessional.objects.get(email='nurse@example.com')
        self.assertFalse(professional.cv_file)
        self.assertFalse(professional.license_file)

    def test_professional_fields_are_validated(self):
        response = self.client.post(self.endpoint, {'email': 'bad-email'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('fields', response.data)


class FacilityWaitlistTests(APITestCase):
    endpoint = '/api/v1/auth/waitlist/facility/'
    payload = {
        'email': 'ops@lagoscare.example',
        'facility_name': 'Lagos Care Hospital',
        'contact_name': 'Tola Adeyemi',
        'phone_number': '+2348098765432',
        'facility_type': 'HOSPITAL',
        'location': 'Ikeja, Lagos',
        'staffing_need': 'REGULAR',
        'monthly_shift_volume': 30,
    }

    def test_facility_can_join_waitlist(self):
        response = self.client.post(self.endpoint, self.payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            WaitlistFacility.objects.filter(email='ops@lagoscare.example').exists()
        )

    def test_duplicate_facility_email_is_idempotent(self):
        self.client.post(self.endpoint, self.payload, format='json')
        duplicate = {**self.payload, 'email': 'OPS@LAGOSCARE.EXAMPLE'}

        response = self.client.post(self.endpoint, duplicate, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(WaitlistFacility.objects.count(), 1)
