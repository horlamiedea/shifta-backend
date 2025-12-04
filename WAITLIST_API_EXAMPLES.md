# Waitlist API - cURL Examples

## Endpoint
`POST /api/v1/auth/waitlist/`

## Example 1: JSON Request (without file uploads)

```bash
curl -X POST http://localhost:8000/api/v1/auth/waitlist/ \
  -H "Content-Type: application/json" \
  -d '{
    "email": "john.doe@example.com",
    "full_name": "John Doe",
    "phone_number": "+1234567890",
    "medical_type": "Nurse",
    "location": "Lagos, Nigeria",
    "preferred_work_address": "123 Main Street, Lagos",
    "shift_rate_9hr": "5000.00",
    "shift_rate_12hr": "6500.00",
    "shift_rate_16hr": "8500.00",
    "shift_rate_24hr": "12000.00",
    "years_of_experience": 5,
    "bio_data": "Experienced ICU nurse with 5 years of experience."
  }'
```

## Example 2: Form Data Request (with file uploads)

```bash
curl -X POST http://localhost:8000/api/v1/auth/waitlist/ \
  -F "email=john.doe@example.com" \
  -F "full_name=John Doe" \
  -F "phone_number=+1234567890" \
  -F "medical_type=Nurse" \
  -F "location=Lagos, Nigeria" \
  -F "preferred_work_address=123 Main Street, Lagos" \
  -F "shift_rate_9hr=5000.00" \
  -F "shift_rate_12hr=6500.00" \
  -F "shift_rate_16hr=8500.00" \
  -F "shift_rate_24hr=12000.00" \
  -F "years_of_experience=5" \
  -F "bio_data=Experienced ICU nurse with 5 years of experience." \
  -F "cv_file=@/path/to/cv.pdf" \
  -F "license_file=@/path/to/license.pdf"
```

## Example 3: Minimal Request (only required fields)

```bash
curl -X POST http://localhost:8000/api/v1/auth/waitlist/ \
  -H "Content-Type: application/json" \
  -d '{
    "email": "jane.smith@example.com",
    "full_name": "Jane Smith"
  }'
```

## Example 4: Request with only some shift rates

```bash
curl -X POST http://localhost:8000/api/v1/auth/waitlist/ \
  -H "Content-Type: application/json" \
  -d '{
    "email": "doctor@example.com",
    "full_name": "Dr. Sarah Johnson",
    "phone_number": "+1234567890",
    "medical_type": "Doctor",
    "shift_rate_12hr": "10000.00",
    "shift_rate_24hr": "18000.00"
  }'
```

## Field Descriptions

### Required Fields:
- `email` (string): Email address (must be unique)
- `full_name` (string): Full name of the professional

### Optional Fields:
- `phone_number` (string): Phone number
- `medical_type` (string): Type of medical professional (e.g., "Nurse", "Doctor")
- `location` (string): Current location
- `preferred_work_address` (string): Preferred work address
- `shift_rate_9hr` (decimal): Rate for 9-hour shifts
- `shift_rate_12hr` (decimal): Rate for 12-hour shifts
- `shift_rate_16hr` (decimal): Rate for 16-hour shifts
- `shift_rate_24hr` (decimal): Rate for 24-hour shifts
- `years_of_experience` (integer): Years of experience
- `bio_data` (string): Additional biographical information
- `cv_file` (file): CV/resume file (multipart/form-data only)
- `license_file` (file): License file (multipart/form-data only)

## Response Examples

### Success Response (201 Created):
```json
{
  "status": "success",
  "message": "Added to waitlist"
}
```

### Already Exists Response (200 OK):
```json
{
  "message": "Already on waitlist"
}
```

### Error Response (400 Bad Request):
```json
{
  "error": "Email and Name are required"
}
```

## Frontend Integration Notes

### Using Fetch API (JavaScript):

```javascript
// Without file uploads
const response = await fetch('http://localhost:8000/api/v1/auth/waitlist/', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    email: 'john.doe@example.com',
    full_name: 'John Doe',
    phone_number: '+1234567890',
    medical_type: 'Nurse',
    shift_rate_9hr: '5000.00',
    shift_rate_12hr: '6500.00',
    shift_rate_16hr: '8500.00',
    shift_rate_24hr: '12000.00',
    years_of_experience: 5,
    bio_data: 'Experienced ICU nurse with 5 years of experience.'
  })
});

const data = await response.json();
```

### Using FormData (with file uploads):

```javascript
const formData = new FormData();
formData.append('email', 'john.doe@example.com');
formData.append('full_name', 'John Doe');
formData.append('phone_number', '+1234567890');
formData.append('medical_type', 'Nurse');
formData.append('shift_rate_9hr', '5000.00');
formData.append('shift_rate_12hr', '6500.00');
formData.append('shift_rate_16hr', '8500.00');
formData.append('shift_rate_24hr', '12000.00');
formData.append('years_of_experience', '5');
formData.append('cv_file', cvFileInput.files[0]);
formData.append('license_file', licenseFileInput.files[0]);

const response = await fetch('http://localhost:8000/api/v1/auth/waitlist/', {
  method: 'POST',
  body: formData
});

const data = await response.json();
```

### Using Axios:

```javascript
// Without file uploads
import axios from 'axios';

const response = await axios.post('http://localhost:8000/api/v1/auth/waitlist/', {
  email: 'john.doe@example.com',
  full_name: 'John Doe',
  phone_number: '+1234567890',
  medical_type: 'Nurse',
  shift_rate_9hr: '5000.00',
  shift_rate_12hr: '6500.00',
  shift_rate_16hr: '8500.00',
  shift_rate_24hr: '12000.00',
  years_of_experience: 5,
  bio_data: 'Experienced ICU nurse with 5 years of experience.'
});

// With file uploads
const formData = new FormData();
formData.append('email', 'john.doe@example.com');
formData.append('full_name', 'John Doe');
formData.append('shift_rate_9hr', '5000.00');
formData.append('shift_rate_12hr', '6500.00');
formData.append('shift_rate_16hr', '8500.00');
formData.append('shift_rate_24hr', '12000.00');
formData.append('cv_file', cvFile);
formData.append('license_file', licenseFile);

const response = await axios.post('http://localhost:8000/api/v1/auth/waitlist/', formData, {
  headers: {
    'Content-Type': 'multipart/form-data'
  }
});
```

