"""
Advanced Speechmatics configuration and features for improved accuracy.
This module contains implementations for custom vocabulary, domain models,
audio enhancement, confidence scoring, and alternative transcriptions.

NOTE: Domain models are not yet supported in the current LiveKit Speechmatics plugin.
This module shows how they would be implemented when support is added.
The UI and database already support storing domain preferences for future use.
"""
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from livekit.plugins.speechmatics.types import TranscriptionConfig

logger = logging.getLogger("transcriber.speechmatics_advanced")


@dataclass
class AdvancedSpeechmaticsConfig:
    """Extended Speechmatics configuration with all accuracy features."""
    
    # Basic settings (inherited from main config)
    language: str = "ar"
    operating_point: str = "enhanced"
    enable_partials: bool = False
    max_delay: float = 3.5
    punctuation_sensitivity: float = 0.5
    diarization: str = "speaker"
    
    # Custom vocabulary settings
    enable_custom_vocab: bool = True
    custom_vocab_items: List[Dict[str, Any]] = None
    custom_dictionary_id: Optional[str] = None
    
    # Domain-specific settings
    domain: str = "broadcast"  # Best for sermons/lectures
    output_locale: str = "ar-SA"  # Saudi Arabic for religious content
    
    # Audio enhancement settings
    enable_automatic_audio_enhancement: bool = True
    sample_rate: int = 16000  # Optimal for speech
    
    # Advanced features
    max_alternatives: int = 3  # Get top 3 alternatives
    enable_word_level_confidence: bool = True
    enable_entities: bool = True  # Detect names, places
    
    def __post_init__(self):
        if self.custom_vocab_items is None:
            self.custom_vocab_items = self._get_default_islamic_vocabulary()
    
    def _get_default_islamic_vocabulary(self) -> List[Dict[str, Any]]:
        """Get default Islamic/Arabic religious vocabulary."""
        return [
            # Common Islamic phrases
            {"content": "بسم الله الرحمن الرحيم", "sounds_like": ["bismillahir rahmanir raheem"]},
            {"content": "الحمد لله رب العالمين", "sounds_like": ["alhamdulillahi rabbil alameen"]},
            {"content": "صلى الله عليه وسلم", "sounds_like": ["sallallahu alayhi wasallam"]},
            {"content": "السلام عليكم ورحمة الله وبركاته", "sounds_like": ["assalamu alaikum wa rahmatullahi wa barakatuh"]},
            
            # Prayer-related terms
            {"content": "صلاة الفجر", "sounds_like": ["salatul fajr", "fajr prayer"]},
            {"content": "صلاة الظهر", "sounds_like": ["salatul dhuhr", "dhuhr prayer"]},
            {"content": "صلاة العصر", "sounds_like": ["salatul asr", "asr prayer"]},
            {"content": "صلاة المغرب", "sounds_like": ["salatul maghrib", "maghrib prayer"]},
            {"content": "صلاة العشاء", "sounds_like": ["salatul isha", "isha prayer"]},
            {"content": "صلاة التراويح", "sounds_like": ["salatul tarawih", "tarawih prayer"]},
            {"content": "صلاة الجمعة", "sounds_like": ["salatul jumuah", "friday prayer"]},
            
            # Quranic terms
            {"content": "القرآن الكريم", "sounds_like": ["al quran al kareem", "the holy quran"]},
            {"content": "سورة الفاتحة", "sounds_like": ["surat al fatihah"]},
            {"content": "آية الكرسي", "sounds_like": ["ayatul kursi"]},
            
            # Titles and honorifics
            {"content": "الشيخ", "sounds_like": ["sheikh", "shaykh"]},
            {"content": "الإمام", "sounds_like": ["imam", "al imam"]},
            {"content": "الحاج", "sounds_like": ["hajj", "al hajj"]},
            {"content": "الأستاذ", "sounds_like": ["ustadh", "al ustadh"]},
        ]
    
    def build_transcription_config(self, room_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Build complete TranscriptionConfig with all advanced features."""
        config = {
            "language": self.language,
            "operating_point": self.operating_point,
            "enable_partials": self.enable_partials,
            "max_delay": self.max_delay,
            "punctuation_overrides": {"sensitivity": self.punctuation_sensitivity},
            "diarization": self.diarization,
        }
        
        # Add custom vocabulary if enabled
        if self.enable_custom_vocab:
            if self.custom_dictionary_id:
                config["custom_dictionary"] = {
                    "language": self.language,
                    "dictionary_id": self.custom_dictionary_id,
                }
            elif self.custom_vocab_items:
                config["additional_vocab"] = self.custom_vocab_items[:1000]  # Max 1000 items
        
        # Add domain configuration
        if self.domain:
            config["domain"] = self.domain
        
        # Add output locale for regional variations
        if self.output_locale:
            config["output_locale"] = self.output_locale
        
        # Audio enhancement
        if self.enable_automatic_audio_enhancement:
            config["enable_automatic_audio_enhancement"] = True
            
        # Alternative transcriptions
        if self.max_alternatives > 1:
            config["max_alternatives"] = self.max_alternatives
            
        # Entity detection
        if self.enable_entities:
            config["enable_entities"] = True
            
        # Room-specific overrides
        if room_config:
            # Override domain based on content type
            content_type = room_config.get('content_type')
            if content_type:
                config["domain"] = self._get_domain_for_content_type(content_type)
            
            # Add room-specific vocabulary
            room_vocab = room_config.get('custom_vocabulary', [])
            if room_vocab and self.enable_custom_vocab:
                config["additional_vocab"].extend(room_vocab)
        
        return config
    
    def _get_domain_for_content_type(self, content_type: str) -> str:
        """Map content type to Speechmatics domain."""
        domain_mapping = {
            'sermon': 'broadcast',
            'lecture': 'broadcast',
            'announcement': 'contact_centre',
            'discussion': 'conversational',
            'recitation': 'broadcast',
            'interview': 'conversational',
        }
        return domain_mapping.get(content_type, 'general')


class TranscriptionProcessor:
    """Process transcriptions with confidence scoring and alternatives."""
    
    def __init__(self, confidence_threshold: float = 0.7):
        self.confidence_threshold = confidence_threshold
        self.low_confidence_words = []
        self.alternative_selections = []
    
    async def process_transcription_event(self, ev) -> Dict[str, Any]:
        """Process a transcription event with confidence analysis."""
        if not hasattr(ev, 'alternatives') or not ev.alternatives:
            return None
            
        # Get primary transcription
        primary = ev.alternatives[0]
        result = {
            "text": primary.text,
            "confidence": getattr(primary, 'confidence', 1.0),
            "is_final": ev.type == "FINAL_TRANSCRIPT",
            "alternatives": [],
            "low_confidence_words": [],
            "requires_review": False
        }
        
        # Check overall confidence
        if result["confidence"] < self.confidence_threshold:
            result["requires_review"] = True
            logger.warning(f"Low confidence transcript: {result['confidence']:.2f}")
        
        # Process alternatives
        for i, alt in enumerate(ev.alternatives[1:], 1):
            result["alternatives"].append({
                "rank": i + 1,
                "text": alt.text,
                "confidence": getattr(alt, 'confidence', 0.0),
                "diff_from_primary": self._calculate_diff(primary.text, alt.text)
            })
        
        # Process word-level confidence if available
        if hasattr(primary, 'words'):
            for word in primary.words:
                if hasattr(word, 'confidence') and word.confidence < 0.6:
                    result["low_confidence_words"].append({
                        "text": word.text,
                        "confidence": word.confidence,
                        "position": getattr(word, 'start_time', 0)
                    })
        
        # Log significant alternatives
        if result["alternatives"] and result["alternatives"][0]["confidence"] > result["confidence"] - 0.1:
            logger.info(f"Close alternatives detected: Primary vs Alt1 confidence diff < 0.1")
        
        return result
    
    def _calculate_diff(self, text1: str, text2: str) -> float:
        """Calculate difference ratio between two texts."""
        # Simple character-based difference
        if not text1 or not text2:
            return 1.0
        
        common = sum(1 for a, b in zip(text1, text2) if a == b)
        return 1.0 - (common / max(len(text1), len(text2)))
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get transcription quality statistics."""
        return {
            "total_low_confidence_words": len(self.low_confidence_words),
            "alternative_selection_count": len(self.alternative_selections),
            "average_confidence": sum(w["confidence"] for w in self.low_confidence_words) / max(1, len(self.low_confidence_words))
        }


class AudioEnhancer:
    """Audio enhancement for better transcription accuracy."""
    
    @staticmethod
    def get_audio_format_config() -> Dict[str, Any]:
        """Get optimal audio format configuration."""
        return {
            "type": "raw",
            "encoding": "pcm_s16le",  # 16-bit PCM
            "sample_rate": 16000,      # 16kHz optimal for speech
            "channels": 1,             # Mono for single speaker
        }
    
    @staticmethod
    def get_audio_events_config(content_type: str = "sermon") -> Dict[str, Any]:
        """Get audio event detection configuration based on content type."""
        configs = {
            "sermon": {
                "enable_music": False,
                "enable_applause": True,
                "enable_laughter": False,
                "enable_speech": True,
            },
            "lecture": {
                "enable_music": False,
                "enable_applause": True,
                "enable_laughter": True,
                "enable_speech": True,
            },
            "announcement": {
                "enable_music": False,
                "enable_applause": False,
                "enable_laughter": False,
                "enable_speech": True,
            }
        }
        return configs.get(content_type, configs["sermon"])


# Usage example in main.py:
"""
# Import advanced config
from speechmatics_advanced import AdvancedSpeechmaticsConfig, TranscriptionProcessor

# Create advanced config
advanced_config = AdvancedSpeechmaticsConfig(
    language=stt_config.language,
    max_delay=stt_config.max_delay,
    punctuation_sensitivity=stt_config.punctuation_sensitivity,
)

# Build transcription config with all features
transcription_config_dict = advanced_config.build_transcription_config(room_config)

# Initialize STT with advanced config
stt_provider = speechmatics.STT(
    transcription_config=TranscriptionConfig(**transcription_config_dict)
)

# Create processor for handling confidence scores
processor = TranscriptionProcessor(confidence_threshold=0.75)

# In transcription loop:
result = await processor.process_transcription_event(ev)
if result and result["requires_review"]:
    # Flag for human review or use alternative
    pass
"""