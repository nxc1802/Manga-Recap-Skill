from supabase import create_client, Client
from src.core.config import config
from src.core.logger import logger

def get_supabase_client(use_admin: bool = True) -> Client:
    """
    Initializes and returns a Supabase client.
    By default, uses the service_role key (admin) to bypass RLS policies
    for backend automated processing.
    """
    url = config.supabase_url
    # Use service_role key for admin actions, fallback to anon key
    key = config.supabase_service_role_key if use_admin else config.supabase_anon_key
    
    if not url or not key:
        logger.error("Supabase URL or Key is missing from configuration!")
        raise ValueError("Supabase URL or Key is missing")
        
    try:
        client = create_client(url, key)
        return client
    except Exception as e:
        logger.exception("Failed to connect to Supabase: %s", e)
        raise

# Global client instances
supabase = get_supabase_client(use_admin=True)
