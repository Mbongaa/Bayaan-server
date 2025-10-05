"""
Configuration management for LiveKit AI Translation Server.
Centralizes all configuration values and environment variables.
"""
import os
from dataclasses import dataclass
from typing import Optional, Dict
# Try to load dotenv if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # dotenv not available, will use system environment variables
    pass


@dataclass
class SupabaseConfig:
    """Supabase database configuration."""
    url: str
    service_role_key: str
    anon_key: Optional[str] = None
    
    # Timeouts
    http_timeout: float = 5.0  # General HTTP request timeout
    broadcast_timeout: float = 2.0  # Broadcast API timeout
    
    @classmethod
    def from_env(cls) -> 'SupabaseConfig':
        """Load configuration from environment variables."""
        url = os.getenv('SUPABASE_URL')
        service_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
        anon_key = os.getenv('SUPABASE_ANON_KEY')
        
        if not url:
            raise ValueError("SUPABASE_URL environment variable is required")
        if not service_key:
            raise ValueError("SUPABASE_SERVICE_ROLE_KEY environment variable is required")
            
        return cls(
            url=url,
            service_role_key=service_key,
            anon_key=anon_key
        )


@dataclass
class TranslationConfig:
    """Translation-related configuration."""
    # Language settings
    default_source_language: str = "ar"  # Arabic
    default_target_language: str = "nl"  # Dutch
    
    # Context window settings
    use_context: bool = True
    max_context_pairs: int = 12  # Increased from 6 for better accuracy in sermons/lectures
    
    # Timing settings
    translation_delay: float = 10.0  # Delay before translating incomplete sentences
    
    # Supported languages
    supported_languages: Dict[str, Dict[str, str]] = None
    
    def __post_init__(self):
        if self.supported_languages is None:
            self.supported_languages = {
                "ar": {"name": "Arabic", "flag": "🇸🇦"},
                "en": {"name": "English", "flag": "🇬🇧"},
                "es": {"name": "Spanish", "flag": "🇪🇸"},
                "fr": {"name": "French", "flag": "🇫🇷"},
                "de": {"name": "German", "flag": "🇩🇪"},
                "ja": {"name": "Japanese", "flag": "🇯🇵"},
                "nl": {"name": "Dutch", "flag": "🇳🇱"},
                "tr": {"name": "Turkish", "flag": "🇹🇷"},
                "ba": {"name": "Bashkir", "flag": "🇷🇺"},
                "eu": {"name": "Basque", "flag": "🇪🇸"},
                "be": {"name": "Belarusian", "flag": "🇧🇾"},
                "bn": {"name": "Bengali", "flag": "🇧🇩"},
                "bg": {"name": "Bulgarian", "flag": "🇧🇬"},
                "yue": {"name": "Cantonese", "flag": "🇭🇰"},
                "ca": {"name": "Catalan", "flag": "🇪🇸"},
                "hr": {"name": "Croatian", "flag": "🇭🇷"},
                "cs": {"name": "Czech", "flag": "🇨🇿"},
                "da": {"name": "Danish", "flag": "🇩🇰"},
                "eo": {"name": "Esperanto", "flag": "🌍"},
                "et": {"name": "Estonian", "flag": "🇪🇪"},
                "fi": {"name": "Finnish", "flag": "🇫🇮"},
                "gl": {"name": "Galician", "flag": "🇪🇸"},
                "el": {"name": "Greek", "flag": "🇬🇷"},
                "he": {"name": "Hebrew", "flag": "🇮🇱"},
                "hi": {"name": "Hindi", "flag": "🇮🇳"},
                "hu": {"name": "Hungarian", "flag": "🇭🇺"},
                "id": {"name": "Indonesian", "flag": "🇮🇩"},
                "ia": {"name": "Interlingua", "flag": "🌍"},
                "ga": {"name": "Irish", "flag": "🇮🇪"},
                "it": {"name": "Italian", "flag": "🇮🇹"},
                "ko": {"name": "Korean", "flag": "🇰🇷"},
                "lv": {"name": "Latvian", "flag": "🇱🇻"},
                "lt": {"name": "Lithuanian", "flag": "🇱🇹"},
                "ms": {"name": "Malay", "flag": "🇲🇾"},
                "mt": {"name": "Maltese", "flag": "🇲🇹"},
                "cmn": {"name": "Mandarin", "flag": "🇨🇳"},
                "mr": {"name": "Marathi", "flag": "🇮🇳"},
                "mn": {"name": "Mongolian", "flag": "🇲🇳"},
                "no": {"name": "Norwegian", "flag": "🇳🇴"},
                "fa": {"name": "Persian", "flag": "🇮🇷"},
                "pl": {"name": "Polish", "flag": "🇵🇱"},
                "pt": {"name": "Portuguese", "flag": "🇵🇹"},
                "ro": {"name": "Romanian", "flag": "🇷🇴"},
                "ru": {"name": "Russian", "flag": "🇷🇺"},
                "sk": {"name": "Slovakian", "flag": "🇸🇰"},
                "sl": {"name": "Slovenian", "flag": "🇸🇮"},
                "sw": {"name": "Swahili", "flag": "🇰🇪"},
                "sv": {"name": "Swedish", "flag": "🇸🇪"},
                "tl": {"name": "Tagalog", "flag": "🇵🇭"},
                "ta": {"name": "Tamil", "flag": "🇮🇳"},
                "th": {"name": "Thai", "flag": "🇹🇭"},
                "uk": {"name": "Ukrainian", "flag": "🇺🇦"},
                "ur": {"name": "Urdu", "flag": "🇵🇰"},
                "ug": {"name": "Uyghur", "flag": "🇨🇳"},
                "vi": {"name": "Vietnamese", "flag": "🇻🇳"},
                "cy": {"name": "Welsh", "flag": "🏴"},
            }
    
    def get_target_language(self, room_config: Optional[Dict[str, any]] = None) -> str:
        """Get target language from room config or use default."""
        if room_config:
            # Check both possible field names (translation_language and translation__language)
            if 'translation_language' in room_config and room_config['translation_language']:
                return room_config['translation_language']
            elif 'translation__language' in room_config and room_config['translation__language']:
                return room_config['translation__language']
        return self.default_target_language
    
    def get_source_language(self, room_config: Optional[Dict[str, any]] = None) -> str:
        """Get source language from room config or use default."""
        if room_config and 'transcription_language' in room_config and room_config['transcription_language']:
            return room_config['transcription_language']
        return self.default_source_language
    
    def get_context_window_size(self, room_config: Optional[Dict[str, any]] = None) -> int:
        """Get context window size from room config or use default."""
        if room_config and 'context_window_size' in room_config and room_config['context_window_size']:
            # Ensure the value is within valid range (3-20)
            size = int(room_config['context_window_size'])
            return max(3, min(20, size))
        return self.max_context_pairs


@dataclass
class SpeechmaticsConfig:
    """Speechmatics STT configuration."""
    language: str = "ar"
    operating_point: str = "enhanced"
    enable_partials: bool = False  # Disabled to reduce API costs - frontend doesn't use partials
    max_delay: float = 3.5  # Increased from 2.0 for better context and accuracy
    punctuation_sensitivity: float = 0.5  # Default punctuation sensitivity
    diarization: str = "speaker"
    
    def with_room_settings(self, room_config: Optional[Dict[str, any]] = None) -> 'SpeechmaticsConfig':
        """Create a new config with room-specific overrides."""
        if not room_config:
            return self
            
        # Create a copy with room-specific overrides
        import copy
        new_config = copy.deepcopy(self)
        
        # Override with room settings if available
        if 'transcription_language' in room_config and room_config['transcription_language']:
            new_config.language = room_config['transcription_language']
        if 'max_delay' in room_config and room_config['max_delay'] is not None:
            new_config.max_delay = float(room_config['max_delay'])
        if 'punctuation_sensitivity' in room_config and room_config['punctuation_sensitivity'] is not None:
            new_config.punctuation_sensitivity = float(room_config['punctuation_sensitivity'])
            
        return new_config


@dataclass
class ApplicationConfig:
    """Main application configuration."""
    # Component configurations (non-default fields first)
    supabase: SupabaseConfig  # Mosque database
    translation: TranslationConfig
    speechmatics: SpeechmaticsConfig

    # Optional/default fields (must come after non-default fields)
    classroom_supabase: Optional[SupabaseConfig] = None  # Classroom database (optional)
    log_level: str = "INFO"
    default_mosque_id: int = 1
    test_mosque_id: int = 546012  # Hardcoded test mosque
    test_room_id: int = 192577    # Hardcoded test room

    @classmethod
    def load(cls) -> 'ApplicationConfig':
        """Load complete configuration from environment and defaults."""
        # Load mosque database (required)
        mosque_supabase = SupabaseConfig.from_env()

        # Load classroom database (optional)
        classroom_supabase = None
        try:
            classroom_url = os.getenv('CLASSROOM_SUPABASE_URL')
            classroom_key = os.getenv('CLASSROOM_SUPABASE_SERVICE_ROLE_KEY')
            if classroom_url and classroom_key:
                classroom_supabase = SupabaseConfig(
                    url=classroom_url,
                    service_role_key=classroom_key
                )
        except Exception as e:
            print(f"   ⚠️ Classroom database configuration error: {e}")

        return cls(
            supabase=mosque_supabase,
            translation=TranslationConfig(),
            speechmatics=SpeechmaticsConfig(),
            classroom_supabase=classroom_supabase
        )
    
    def validate(self) -> None:
        """Validate configuration at startup."""
        # Print configuration status
        print("🔧 Configuration loaded:")
        print(f"   MOSQUE SUPABASE_URL: {self.supabase.url[:50]}...")
        print(f"   MOSQUE SERVICE_KEY: {'✅ SET' if self.supabase.service_role_key else '❌ NOT SET'}")

        # Classroom database status
        if self.classroom_supabase:
            print(f"   CLASSROOM SUPABASE_URL: {self.classroom_supabase.url[:50]}...")
            print(f"   CLASSROOM SERVICE_KEY: {'✅ SET' if self.classroom_supabase.service_role_key else '❌ NOT SET'}")
        else:
            print(f"   CLASSROOM DB: ⚠️ NOT CONFIGURED (mosque-only mode)")

        print(f"   Default Languages: {self.translation.default_source_language} → {self.translation.default_target_language}")
        print(f"   Context Window: {'✅ ENABLED' if self.translation.use_context else '❌ DISABLED'} ({self.translation.max_context_pairs} pairs)")
        print(f"   STT Defaults: delay={self.speechmatics.max_delay}s, punctuation={self.speechmatics.punctuation_sensitivity}, partials={'✅' if self.speechmatics.enable_partials else '❌'}")


# Global configuration instance
_config: Optional[ApplicationConfig] = None


def get_config() -> ApplicationConfig:
    """Get or create the global configuration instance."""
    global _config
    if _config is None:
        _config = ApplicationConfig.load()
        _config.validate()
    return _config


def reset_config() -> None:
    """Reset configuration (mainly for testing)."""
    global _config
    _config = None