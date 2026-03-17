# integration_utils.py
import requests
import time
import logging
import config

logger = logging.getLogger(__name__)

def call_ad_admin(endpoint, method="POST", data=None, timeout=5):
    """
    Makes an HTTP call to the Ad Admin API with retry and exponential backoff.
    """
    url = f"{config.AD_ADMIN_URL}{endpoint}"
    backoff = 1
    max_retries = 5
    
    for i in range(max_retries):
        try:
            if method == "POST":
                response = requests.post(url, json=data, timeout=timeout)
            elif method == "PATCH":
                response = requests.patch(url, json=data, timeout=timeout)
            elif method == "DELETE":
                response = requests.delete(url, timeout=timeout)
            else:
                response = requests.get(url, timeout=timeout)
            
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Ad Admin API call failed ({url}): {e}. Retry {i+1}/{max_retries} in {backoff}s...")
            if i < max_retries - 1:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
    
    logger.error(f"Ad Admin API call failed after {max_retries} retries.")
    return None
