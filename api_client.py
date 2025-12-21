"""
API Client for SOMEWHERE Game Engine
Provides a simple interface to call engine functions via API or directly

Usage in bot.py:
    from api_client import api
    
    # These calls automatically route to API or direct engine based on USE_API flag
    result = api.generate_intro_turn()
    result = api.advance_turn_image_fast(choice, fate)
    state = api.get_state()
"""
import os
import requests
import engine
from typing import Optional, Dict, Any, List

# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

# Feature flag: Set to True to use API, False to use direct engine calls
USE_API = os.getenv("USE_API_MODE", "false").lower() == "true"

# API base URL
API_BASE = os.getenv("API_BASE_URL", "http://localhost:5001/api")

# Request timeout (seconds)
TIMEOUT = 120  # Long timeout for image generation

print(f"[API_CLIENT] USE_API_MODE: {USE_API}")
if USE_API:
    print(f"[API_CLIENT] API_BASE_URL: {API_BASE}")


# ═══════════════════════════════════════════════════════════════════════════
# API CLIENT CLASS
# ═══════════════════════════════════════════════════════════════════════════

class GameEngineClient:
    """
    Unified interface for game engine calls
    Routes to API or direct engine based on USE_API flag
    """
    
    def __init__(self, use_api: bool = USE_API, api_base: str = API_BASE, session_id: str = 'default'):
        self.use_api = use_api
        self.api_base = api_base
        self.session_id = session_id  # Session ID for this client
    
    def _api_call(self, method: str, endpoint: str, json_data: Optional[Dict] = None) -> Any:
        """Make an API call and return the data"""
        url = f"{self.api_base}{endpoint}"
        
        try:
            if method.upper() == 'GET':
                response = requests.get(url, timeout=TIMEOUT)
            elif method.upper() == 'POST':
                response = requests.post(url, json=json_data, timeout=TIMEOUT)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            response.raise_for_status()
            result = response.json()
            
            if result.get('success'):
                return result.get('data')
            else:
                error_msg = result.get('error', 'Unknown error')
                details = result.get('details', '')
                raise Exception(f"API Error: {error_msg}. {details}")
        
        except requests.exceptions.RequestException as e:
            print(f"[API_CLIENT] Request failed: {e}")
            raise Exception(f"API request failed: {e}")
    
    # ═══════════════════════════════════════════════════════════════════════
    # STATE MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════
    
    def get_state(self) -> Dict:
        """Get current game state"""
        if self.use_api:
            return self._api_call('GET', f'/state?session_id={self.session_id}')
        else:
            return engine.get_state(self.session_id)
    
    def reload_state(self) -> Dict:
        """Force reload state from disk"""
        if self.use_api:
            return self._api_call('POST', '/state/reload')
        else:
            engine.state = engine._load_state()
            return engine.get_state()
    
    def reset_state(self):
        """Reset game state"""
        if self.use_api:
            self._api_call('POST', '/state/reset', {'session_id': self.session_id})
        else:
            engine.reset_state(self.session_id)
    
    def save_state(self, state: Dict):
        """Save game state to disk"""
        if self.use_api:
            # API mode: send state to server
            self._api_call('POST', '/state/save', {'session_id': self.session_id, 'state': state})
        else:
            # Direct mode: save via engine module
            engine._save_state(state, self.session_id)
    
    # ═══════════════════════════════════════════════════════════════════════
    # GAME FLOW
    # ═══════════════════════════════════════════════════════════════════════
    
    def generate_intro_turn(self) -> Dict:
        """Generate full intro turn"""
        if self.use_api:
            return self._api_call('POST', '/game/intro', {'session_id': self.session_id})
        else:
            return engine.generate_intro_turn(self.session_id)
    
    def generate_intro_image_fast(self) -> Dict:
        """Generate intro image (Phase 1)"""
        if self.use_api:
            return self._api_call('POST', '/game/intro/image')
        else:
            return engine.generate_intro_image_fast()
    
    def generate_intro_choices_deferred(
        self, 
        image_url: str, 
        prologue: str, 
        vision_dispatch: str, 
        dispatch: Optional[str] = None
    ) -> Dict:
        """Generate intro choices (Phase 2)"""
        if self.use_api:
            return self._api_call('POST', '/game/intro/choices', {
                'image_url': image_url,
                'prologue': prologue,
                'vision_dispatch': vision_dispatch,
                'dispatch': dispatch,
                'session_id': self.session_id
            })
        else:
            return engine.generate_intro_choices_deferred(
                image_url, prologue, vision_dispatch, dispatch, self.session_id
            )
    
    def advance_turn_image_fast(
        self, 
        choice: str, 
        fate: str = "NORMAL", 
        is_timeout_penalty: bool = False
    ) -> Dict:
        """Process action - Phase 1: Generate image"""
        if self.use_api:
            return self._api_call('POST', '/game/action/image', {
                'choice': choice,
                'fate': fate,
                'is_timeout_penalty': is_timeout_penalty,
                'session_id': self.session_id
            })
        else:
            return engine.advance_turn_image_fast(choice, fate, is_timeout_penalty, self.session_id)
    
    def advance_turn_choices_deferred(
        self,
        consequence_img_url: str,
        dispatch: str,
        vision_dispatch: str,
        choice: str,
        consequence_img_prompt: str = "",
        hard_transition: bool = False
    ) -> Dict:
        """Process action - Phase 2: Generate choices"""
        if self.use_api:
            return self._api_call('POST', '/game/action/choices', {
                'consequence_img_url': consequence_img_url,
                'dispatch': dispatch,
                'vision_dispatch': vision_dispatch,
                'choice': choice,
                'consequence_img_prompt': consequence_img_prompt,
                'hard_transition': hard_transition,
                'session_id': self.session_id
            })
        else:
            return engine.advance_turn_choices_deferred(
                consequence_img_url,
                dispatch,
                vision_dispatch,
                choice,
                consequence_img_prompt,
                hard_transition,
                self.session_id
            )
    
    # ═══════════════════════════════════════════════════════════════════════
    # UTILITIES
    # ═══════════════════════════════════════════════════════════════════════
    
    def get_last_movement_type(self) -> Optional[str]:
        """Get last detected movement type"""
        if self.use_api:
            result = self._api_call('GET', '/movement')
            return result.get('movement_type')
        else:
            return engine.get_last_movement_type()
    
    def get_history(self) -> List[Dict]:
        """Get game history"""
        if self.use_api:
            result = self._api_call('GET', '/history')
            return result.get('history', [])
        else:
            return engine.history if hasattr(engine, 'history') else []
    
    def get_config(self) -> Dict:
        """Get engine configuration"""
        if self.use_api:
            return self._api_call('GET', '/config')
        else:
            return {
                "IMAGE_ENABLED": getattr(engine, 'IMAGE_ENABLED', True),
                "WORLD_IMAGE_ENABLED": getattr(engine, 'WORLD_IMAGE_ENABLED', True),
                "VEO_MODE_ENABLED": getattr(engine, 'VEO_MODE_ENABLED', False),
                "QUALITY_MODE": getattr(engine, 'QUALITY_MODE', True),
            }
    
    def set_config(self, **kwargs):
        """Update engine configuration"""
        if self.use_api:
            self._api_call('POST', '/config', kwargs)
        else:
            for key, value in kwargs.items():
                if hasattr(engine, key):
                    setattr(engine, key, value)
    
    def get_prompt(self, prompt_key: str) -> Optional[str]:
        """Get a specific prompt"""
        if self.use_api:
            result = self._api_call('GET', f'/prompts/{prompt_key}')
            return result.get('value')
        else:
            prompts = getattr(engine, 'PROMPTS', {})
            return prompts.get(prompt_key)
    
    # ═══════════════════════════════════════════════════════════════════════
    # DIRECT ENGINE ACCESS (for backwards compatibility)
    # ═══════════════════════════════════════════════════════════════════════
    
    @property
    def PROMPTS(self):
        """Access PROMPTS dictionary (direct engine only)"""
        if self.use_api:
            print("[API_CLIENT] Warning: PROMPTS access not available in API mode")
            return {}
        return engine.PROMPTS
    
    @property
    def history(self):
        """Access history (tries to maintain compatibility)"""
        if self.use_api:
            # In API mode, fetch history when accessed
            return self.get_history()
        return engine.history
    
    @property
    def state(self):
        """Access state (tries to maintain compatibility)"""
        if self.use_api:
            # In API mode, fetch state when accessed
            return self.get_state()
        return engine.state
    
    @state.setter
    def state(self, value):
        """Set state (direct engine only)"""
        if self.use_api:
            print("[API_CLIENT] Warning: Direct state setting not available in API mode")
        else:
            engine.state = value
    
    @property
    def QUALITY_MODE(self):
        """Access QUALITY_MODE"""
        config = self.get_config()
        return config.get('QUALITY_MODE', True)
    
    @QUALITY_MODE.setter
    def QUALITY_MODE(self, value: bool):
        """Set QUALITY_MODE"""
        self.set_config(QUALITY_MODE=value)
    
    @property
    def IMAGE_ENABLED(self):
        """Access IMAGE_ENABLED"""
        config = self.get_config()
        return config.get('IMAGE_ENABLED', True)
    
    @IMAGE_ENABLED.setter
    def IMAGE_ENABLED(self, value: bool):
        """Set IMAGE_ENABLED"""
        self.set_config(IMAGE_ENABLED=value)
    
    @property
    def WORLD_IMAGE_ENABLED(self):
        """Access WORLD_IMAGE_ENABLED"""
        config = self.get_config()
        return config.get('WORLD_IMAGE_ENABLED', True)
    
    @WORLD_IMAGE_ENABLED.setter
    def WORLD_IMAGE_ENABLED(self, value: bool):
        """Set WORLD_IMAGE_ENABLED"""
        self.set_config(WORLD_IMAGE_ENABLED=value)
    
    @property
    def VEO_MODE_ENABLED(self):
        """Access VEO_MODE_ENABLED"""
        config = self.get_config()
        return config.get('VEO_MODE_ENABLED', False)
    
    @VEO_MODE_ENABLED.setter
    def VEO_MODE_ENABLED(self, value: bool):
        """Set VEO_MODE_ENABLED"""
        self.set_config(VEO_MODE_ENABLED=value)
    
    def _ask(self, *args, **kwargs):
        """
        Direct access to _ask function (not available in API mode)
        Falls back to direct engine call
        """
        if self.use_api:
            print("[API_CLIENT] Warning: _ask() not available in API mode, using direct engine")
        return engine._ask(*args, **kwargs)
    
    @property
    def client(self):
        """Access AI client (direct engine only)"""
        if self.use_api:
            print("[API_CLIENT] Warning: client access not available in API mode")
            return None
        return engine.client
    
    @property
    def choice_tmpl(self):
        """Access choice template (direct engine only)"""
        if self.use_api:
            print("[API_CLIENT] Warning: choice_tmpl access not available in API mode")
            return ""
        return engine.choice_tmpl


# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL INSTANCE
# ═══════════════════════════════════════════════════════════════════════════

# Create a global instance that can be imported
api = GameEngineClient()

# For backwards compatibility, you can also access engine directly
# This will be removed once API mode is fully tested
def get_engine():
    """Get direct engine reference (for backwards compatibility)"""
    return engine


