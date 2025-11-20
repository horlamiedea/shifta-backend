import random
from datetime import date, timedelta

class AIVerificationService:
    def verify_certificate(self, image_url):
        """
        Mock implementation of AI verification.
        In real life, this would:
        1. Download image from Azure Blob Storage (image_url).
        2. Send to Gemini/OpenAI Vision API.
        3. Extract expiry date.
        4. Delete image (as per requirement).
        """
        print(f"AI Service: Verifying certificate at {image_url}...")
        
        # Simulate processing time
        # ...
        
        # Mock Logic:
        # If URL contains "expired", simulate expired certificate.
        # If URL contains "invalid", simulate unreadable.
        # Otherwise, valid.
        
        if "expired" in image_url:
            expiry_date = date.today() - timedelta(days=30)
            return {
                "is_valid": False,
                "expiry_date": expiry_date,
                "reason": "Certificate expired on " + str(expiry_date)
            }
        elif "invalid" in image_url:
             return {
                "is_valid": False,
                "expiry_date": None,
                "reason": "Could not read expiry date."
            }
        else:
            expiry_date = date.today() + timedelta(days=365)
            return {
                "is_valid": True,
                "expiry_date": expiry_date,
                "reason": None
            }
