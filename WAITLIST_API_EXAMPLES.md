# Waitlist API

Shifta has separate early-access lists for healthcare professionals and facilities. Both endpoints accept JSON and are public.

## Professional waitlist

`POST /api/v1/auth/waitlist/`

```json
{
  "email": "amara@example.com",
  "full_name": "Amara Okafor",
  "phone_number": "+2348012345678",
  "medical_type": "Registered Nurse (General)",
  "location": "Ikeja, Lagos",
  "years_of_experience": 4
}
```

`years_of_experience` is optional. CV and licence files are deliberately not collected at the waitlist stage; credential verification happens during onboarding.

## Facility waitlist

`POST /api/v1/auth/waitlist/facility/`

```json
{
  "email": "ops@lagoscare.example",
  "facility_name": "Lagos Care Hospital",
  "contact_name": "Tola Adeyemi",
  "phone_number": "+2348098765432",
  "facility_type": "HOSPITAL",
  "location": "Ikeja, Lagos",
  "staffing_need": "REGULAR",
  "monthly_shift_volume": 30
}
```

`monthly_shift_volume` is optional.

Facility types: `HOSPITAL`, `CLINIC`, `SPECIALIST`, `DIAGNOSTIC`, `OTHER`.

Staffing needs: `URGENT`, `REGULAR`, `SEASONAL`, `EXPLORING`.

## Responses

New entries return `201 Created`:

```json
{
  "status": "success",
  "message": "Added to professional waitlist"
}
```

A repeated email returns `200 OK` and does not create a duplicate:

```json
{
  "status": "existing",
  "message": "Already on waitlist"
}
```

Validation errors return `400 Bad Request`:

```json
{
  "error": "Please check the highlighted fields",
  "fields": {
    "email": ["Enter a valid email address."]
  }
}
```
