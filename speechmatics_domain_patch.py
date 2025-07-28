"""
Monkey patch to add domain support to LiveKit's Speechmatics plugin
This allows us to pass domain configuration to Speechmatics API
without waiting for LiveKit to add official support.
"""
import logging
from typing import Any, Dict

logger = logging.getLogger("transcriber.domain_patch")

def patch_speechmatics_for_domain_support():
    """
    Patches the LiveKit Speechmatics plugin to support domain parameter.
    Call this before importing speechmatics STT.
    """
    try:
        from livekit.plugins.speechmatics.types import TranscriptionConfig
        
        # Store original asdict method
        original_asdict = TranscriptionConfig.asdict
        
        # Create new asdict that includes domain if present
        def patched_asdict(self) -> Dict[str, Any]:
            # Get original dict
            result = original_asdict(self)
            
            # Add domain if it exists as an attribute
            if hasattr(self, '_domain') and self._domain:
                result['domain'] = self._domain
                logger.info(f"[OK] Domain '{self._domain}' added to transcription config")
            
            return result
        
        # Store original __init__
        original_init = TranscriptionConfig.__init__
        
        # Create new __init__ that accepts domain
        def patched_init(self, **kwargs):
            # Extract domain if provided
            domain = kwargs.pop('domain', None)
            
            # Call original init with remaining kwargs
            original_init(self, **kwargs)
            
            # Store domain as private attribute
            if domain:
                self._domain = domain
                logger.info(f"[INFO] TranscriptionConfig initialized with domain: {domain}")
        
        # Apply patches
        TranscriptionConfig.__init__ = patched_init
        TranscriptionConfig.asdict = patched_asdict
        
        logger.info("[OK] Speechmatics domain support patch applied successfully")
        return True
        
    except Exception as e:
        logger.error(f"[ERROR] Failed to apply domain support patch: {e}")
        return False

def test_domain_patch():
    """Test if the patch works correctly"""
    try:
        from livekit.plugins.speechmatics.types import TranscriptionConfig
        
        # Test creating config with domain
        config = TranscriptionConfig(
            language="ar",
            operating_point="enhanced",
            domain="broadcast"  # This should work now
        )
        
        # Test asdict includes domain
        config_dict = config.asdict()
        
        if 'domain' in config_dict and config_dict['domain'] == 'broadcast':
            logger.info("[OK] Domain patch test passed!")
            return True
        else:
            logger.error("[ERROR] Domain patch test failed - domain not in dict")
            return False
            
    except Exception as e:
        logger.error(f"[ERROR] Domain patch test failed with error: {e}")
        return False