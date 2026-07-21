"""
Direct engine proxy for SOMEWHERE Game Engine.

Provides a thin, per-session engine handle:

    from api_client import api as engine
    result = api.advance_turn_image_fast(choice, fate, session_id=sid)
    state  = api.get_state(sid)

Historically this class could also route calls over HTTP to api.py
(``USE_API_MODE``). That second path was never enabled in production and
had drifted out of sync with the real endpoints, so it has been removed.
GameEngineClient is now a thin, direct passthrough to the ``engine``
module. The ``use_api`` constructor argument is accepted for backward
compatibility but ignored.
"""
from typing import Optional, Dict, Any, List

import engine


class GameEngineClient:
    """Thin, direct passthrough to the engine module (per-session aware)."""

    def __init__(self, use_api: bool = False, api_base: str = None, session_id: str = 'default'):
        # use_api / api_base are retained only for signature compatibility.
        self.session_id = session_id

    # ═══════════════════════════════════════════════════════════════════════
    # STATE MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════

    def get_state(self, session_id: str = None) -> Dict:
        return engine.get_state(session_id or self.session_id)

    def reload_state(self, session_id: str = None) -> Dict:
        sid = session_id or self.session_id
        engine.state = engine._load_state(sid)
        return engine.get_state(sid)

    def reset_state(self, session_id: str = None):
        engine.reset_state(session_id or self.session_id)

    def save_state(self, state: Dict, session_id: str = None):
        engine._save_state(state, session_id or self.session_id)

    def _save_state(self, state: Dict, session_id: str = None):
        return self.save_state(state, session_id)

    def _get_state_path(self, session_id: str = None) -> str:
        return str(engine._get_state_path(session_id or self.session_id))

    # ═══════════════════════════════════════════════════════════════════════
    # GAME FLOW
    # ═══════════════════════════════════════════════════════════════════════

    def generate_intro_turn(self, session_id: str = None) -> Dict:
        return engine.generate_intro_turn(session_id or self.session_id)

    def generate_intro_image_fast(self, session_id: str = None) -> Dict:
        return engine.generate_intro_image_fast(session_id or self.session_id)

    def generate_intro_choices_deferred(
        self,
        image_url: str,
        prologue: str,
        vision_dispatch: str,
        dispatch: Optional[str] = None,
        session_id: str = None,
    ) -> Dict:
        return engine.generate_intro_choices_deferred(
            image_url, prologue, vision_dispatch, dispatch, session_id or self.session_id
        )

    def advance_turn_image_fast(
        self,
        choice: str,
        fate: str = "NORMAL",
        is_timeout_penalty: bool = False,
        session_id: str = None,
    ) -> Dict:
        return engine.advance_turn_image_fast(
            choice, fate, is_timeout_penalty, session_id or self.session_id
        )

    def advance_turn_choices_deferred(
        self,
        consequence_img_url: str,
        dispatch: str,
        vision_dispatch: str,
        choice: str,
        consequence_img_prompt: str = "",
        hard_transition: bool = False,
        session_id: str = None,
    ) -> Dict:
        return engine.advance_turn_choices_deferred(
            consequence_img_url,
            dispatch,
            vision_dispatch,
            choice,
            consequence_img_prompt,
            hard_transition,
            session_id or self.session_id,
        )

    # ═══════════════════════════════════════════════════════════════════════
    # UTILITIES
    # ═══════════════════════════════════════════════════════════════════════

    def get_last_movement_type(self) -> Optional[str]:
        return engine.get_last_movement_type()

    def get_history(self) -> List[Dict]:
        return engine.history if hasattr(engine, 'history') else []

    def get_config(self) -> Dict:
        return {
            "IMAGE_ENABLED": getattr(engine, 'IMAGE_ENABLED', True),
            "WORLD_IMAGE_ENABLED": getattr(engine, 'WORLD_IMAGE_ENABLED', True),
            "VEO_MODE_ENABLED": getattr(engine, 'VEO_MODE_ENABLED', False),
            "QUALITY_MODE": getattr(engine, 'QUALITY_MODE', True),
        }

    def set_config(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(engine, key):
                setattr(engine, key, value)

    # ═══════════════════════════════════════════════════════════════════════
    # EXPERIENCE MODE
    # ═══════════════════════════════════════════════════════════════════════

    @property
    def EXPERIENCE_MODE_NO_IMAGES(self) -> str:
        return engine.EXPERIENCE_MODE_NO_IMAGES

    @property
    def EXPERIENCE_MODE_FLIPBOOK(self) -> str:
        return engine.EXPERIENCE_MODE_FLIPBOOK

    @property
    def EXPERIENCE_MODE_FULL_FRAME(self) -> str:
        return engine.EXPERIENCE_MODE_FULL_FRAME

    @property
    def EXPERIENCE_MODES(self) -> dict:
        return engine.EXPERIENCE_MODES

    def apply_experience_mode(self, mode: str, session_id: str = None) -> bool:
        return engine.apply_experience_mode(mode, session_id or self.session_id)

    def get_prompt(self, prompt_key: str) -> Optional[str]:
        prompts = getattr(engine, 'PROMPTS', {})
        return prompts.get(prompt_key)

    # ═══════════════════════════════════════════════════════════════════════
    # DIRECT ENGINE ACCESS
    # ═══════════════════════════════════════════════════════════════════════

    @property
    def PROMPTS(self):
        return engine.PROMPTS

    @property
    def history(self):
        return engine.history

    @property
    def state(self):
        return engine.state

    @state.setter
    def state(self, value):
        engine.state = value

    @property
    def QUALITY_MODE(self):
        return getattr(engine, 'QUALITY_MODE', True)

    @QUALITY_MODE.setter
    def QUALITY_MODE(self, value: bool):
        self.set_config(QUALITY_MODE=value)

    @property
    def IMAGE_ENABLED(self):
        return getattr(engine, 'IMAGE_ENABLED', True)

    @IMAGE_ENABLED.setter
    def IMAGE_ENABLED(self, value: bool):
        self.set_config(IMAGE_ENABLED=value)

    @property
    def WORLD_IMAGE_ENABLED(self):
        return getattr(engine, 'WORLD_IMAGE_ENABLED', True)

    @WORLD_IMAGE_ENABLED.setter
    def WORLD_IMAGE_ENABLED(self, value: bool):
        self.set_config(WORLD_IMAGE_ENABLED=value)

    @property
    def VEO_MODE_ENABLED(self):
        return getattr(engine, 'VEO_MODE_ENABLED', False)

    @VEO_MODE_ENABLED.setter
    def VEO_MODE_ENABLED(self, value: bool):
        self.set_config(VEO_MODE_ENABLED=value)

    def _ask(self, *args, **kwargs):
        return engine._ask(*args, **kwargs)

    @property
    def client(self):
        return engine.client

    @property
    def choice_tmpl(self):
        return engine.choice_tmpl


# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL INSTANCE
# ═══════════════════════════════════════════════════════════════════════════

api = GameEngineClient()


def get_engine():
    """Get the direct engine module reference."""
    return engine
