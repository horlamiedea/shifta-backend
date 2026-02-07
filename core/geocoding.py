"""
Geocoding service using Google Maps API.
"""
import requests
import os
import logging

logger = logging.getLogger(__name__)


class GeocodingService:
    """
    Service to geocode addresses using Google Maps Geocoding API.
    """
    
    GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
    
    def __init__(self):
        self.api_key = os.environ.get('GOOGLE_MAP_API_KEY', '')
    
    def geocode_address(self, address: str) -> dict:
        """
        Convert an address string to latitude/longitude coordinates.
        
        Args:
            address: The address to geocode (e.g., "123 Main St, Lagos, Nigeria")
            
        Returns:
            dict with keys: lat, lng, formatted_address, success
            If geocoding fails, returns {'success': False, 'error': 'message'}
        """
        if not self.api_key:
            logger.warning("GOOGLE_MAP_API_KEY not configured")
            return {'success': False, 'error': 'Google Maps API key not configured'}
        
        if not address:
            return {'success': False, 'error': 'Address is required'}
        
        try:
            params = {
                'address': address,
                'key': self.api_key
            }
            
            response = requests.get(self.GEOCODE_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get('status') == 'OK' and data.get('results'):
                result = data['results'][0]
                location = result['geometry']['location']
                
                return {
                    'success': True,
                    'lat': location['lat'],
                    'lng': location['lng'],
                    'formatted_address': result.get('formatted_address', address),
                    'place_id': result.get('place_id', '')
                }
            else:
                error_msg = data.get('status', 'Unknown error')
                logger.warning(f"Geocoding failed for '{address}': {error_msg}")
                return {'success': False, 'error': f'Geocoding failed: {error_msg}'}
                
        except requests.exceptions.Timeout:
            logger.error(f"Geocoding timeout for address: {address}")
            return {'success': False, 'error': 'Geocoding request timed out'}
        except requests.exceptions.RequestException as e:
            logger.error(f"Geocoding error for address '{address}': {str(e)}")
            return {'success': False, 'error': f'Geocoding request failed: {str(e)}'}
    
    def reverse_geocode(self, lat: float, lng: float) -> dict:
        """
        Convert latitude/longitude coordinates to an address.
        
        Args:
            lat: Latitude
            lng: Longitude
            
        Returns:
            dict with keys: address, formatted_address, success
            If reverse geocoding fails, returns {'success': False, 'error': 'message'}
        """
        if not self.api_key:
            logger.warning("GOOGLE_MAP_API_KEY not configured")
            return {'success': False, 'error': 'Google Maps API key not configured'}
        
        try:
            params = {
                'latlng': f'{lat},{lng}',
                'key': self.api_key
            }
            
            response = requests.get(self.GEOCODE_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get('status') == 'OK' and data.get('results'):
                result = data['results'][0]
                
                return {
                    'success': True,
                    'address': result.get('formatted_address', ''),
                    'formatted_address': result.get('formatted_address', ''),
                    'place_id': result.get('place_id', '')
                }
            else:
                error_msg = data.get('status', 'Unknown error')
                logger.warning(f"Reverse geocoding failed for ({lat}, {lng}): {error_msg}")
                return {'success': False, 'error': f'Reverse geocoding failed: {error_msg}'}
                
        except requests.exceptions.Timeout:
            logger.error(f"Reverse geocoding timeout for: ({lat}, {lng})")
            return {'success': False, 'error': 'Reverse geocoding request timed out'}
        except requests.exceptions.RequestException as e:
            logger.error(f"Reverse geocoding error for ({lat}, {lng}): {str(e)}")
            return {'success': False, 'error': f'Reverse geocoding request failed: {str(e)}'}


# Singleton instance for convenience
geocoding_service = GeocodingService()
