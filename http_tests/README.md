# Shifta API - HTTP Test Files

This folder contains HTTP test files for testing the Shifta API endpoints using VS Code's REST Client extension or any HTTP client that supports `.http` files.

## Prerequisites

1. **VS Code REST Client Extension** (recommended)
   - Install: `humao.rest-client`
   - Or use any HTTP client like Postman, Insomnia, or curl

2. **Backend Server Running**
   ```bash
   cd /Users/olamideajayi/Developers/shifta-backend
   python manage.py runserver
   ```

3. **Database with Mock Data**
   ```bash
   python manage.py seed_data
   ```

## Quick Start

### Step 1: Seed the Database
```bash
# Navigate to backend directory
cd /Users/olamideajayi/Developers/shifta-backend

# Activate virtual environment
source env/bin/activate

# Run migrations (if needed)
python manage.py migrate

# Seed with mock data
python manage.py seed_data
```

### Step 2: Start the Server
```bash
python manage.py runserver
```

### Step 3: Test the API
1. Open `auth.http` in VS Code
2. Click "Send Request" on the facility login request
3. Copy the token from the response
4. Use the token in other `.http` files

## Test Files

| File | Description |
|------|-------------|
| `auth.http` | Authentication (login, register, profile) |
| `shifts.http` | Shift management (create, apply, manage, clock-in/out) |
| `billing.http` | Billing & wallet (transactions, invoices, withdrawals) |
| `communications.http` | Notifications & chat |

## Test Credentials

### Facilities
| Email | Password | Name |
|-------|----------|------|
| `facility1@shifta.com` | `password123` | Lagos General Hospital |
| `facility2@shifta.com` | `password123` | Lekki Medical Center |

### Professionals
| Email | Password | Name | Specialty |
|-------|----------|------|-----------|
| `nurse1@shifta.com` | `password123` | Adaeze Okonkwo | ICU Nurse |
| `doctor1@shifta.com` | `password123` | Chukwuemeka Eze | Emergency Physician |
| `pharmacist1@shifta.com` | `password123` | Funke Adeyemi | Pharmacist |
| `physio1@shifta.com` | `password123` | Tunde Bakare | Physiotherapist |
| `labtech1@shifta.com` | `password123` | Ngozi Nnamdi | Lab Scientist (Unverified) |

## Using with cURL

### Login (Facility)
```bash
curl -X POST http://localhost:8000/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{"email": "facility1@shifta.com", "password": "password123"}'
```

### Get Profile
```bash
curl -X GET http://localhost:8000/api/v1/auth/profile/ \
  -H "Authorization: Token YOUR_TOKEN_HERE"
```

### List Shifts
```bash
curl -X GET http://localhost:8000/api/v1/shifts/ \
  -H "Authorization: Token YOUR_TOKEN_HERE"
```

### Create Shift
```bash
curl -X POST http://localhost:8000/api/v1/shifts/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Token YOUR_TOKEN_HERE" \
  -d '{
    "role": "Night Nurse",
    "specialty": "Registered Nurse (General)",
    "quantity_needed": 2,
    "start_time": "2026-01-25T20:00:00Z",
    "end_time": "2026-01-26T08:00:00Z",
    "rate": 25000
  }'
```

### Get Dashboard Stats
```bash
curl -X GET http://localhost:8000/api/v1/facility/dashboard/stats/ \
  -H "Authorization: Token YOUR_TOKEN_HERE"
```

### Get Notifications
```bash
curl -X GET http://localhost:8000/api/v1/notifications/ \
  -H "Authorization: Token YOUR_TOKEN_HERE"
```

### Get Transactions
```bash
curl -X GET http://localhost:8000/api/v1/billing/transactions/ \
  -H "Authorization: Token YOUR_TOKEN_HERE"
```

## API Response Format

All API responses follow this standardized format:

```json
{
  "success": true,
  "status_code": 200,
  "message": "Request successful",
  "data": { ... }
}
```

Error responses:
```json
{
  "success": false,
  "status_code": 400,
  "message": "Validation failed",
  "errors": { ... },
  "data": null
}
```

## Troubleshooting

### "Token not found" error
- Make sure you've logged in first and copied the token correctly
- Token format: `Authorization: Token abc123...`

### "Permission denied" error
- Check if you're using the correct user type (facility vs professional)
- Some endpoints are restricted to specific user types

### Database errors
- Run `python manage.py migrate` to apply migrations
- Run `python manage.py seed_data` to reset and seed data

### Server not responding
- Make sure the server is running: `python manage.py runserver`
- Check if port 8000 is available
