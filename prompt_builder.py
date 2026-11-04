"""
Prompt Builder for customizable translation prompts.
Handles loading and formatting of prompt templates with variable substitution.
"""
import logging
from typing import Dict, Optional, Any
import json

from config import get_config
from database import query_prompt_template_for_room

logger = logging.getLogger("transcriber.prompt_builder")
config = get_config()


class PromptBuilder:
    """
    Builds customized translation prompts based on templates and room configuration.
    """
    
    # Default fallback prompt if no template is found.
    # Kept byte-for-byte in sync with the public "Khutba (Fusha)" template in
    # Supabase (translation_prompt_templates) so that quickstart rooms — which
    # never carry a tenant_context / room_id — still get the Khutba behavior
    # the rest of the platform configures via the dashboard.
    DEFAULT_PROMPT = (
        "Role: You are a highly skilled simultaneous interpreter, specializing in translating {source_lang} khutbahs (Friday sermons) into {target_lang} for a general mosque audience.\n"
        "\n"
        "Instructions:\n"
        "- Translate the provided {source_lang} text accurately and naturally into {target_lang}, appropriate for religious worship settings.\n"
        "- Use a respectful, warm register, the way an experienced imam addresses his congregation today: dignified but not stiff, never slangy or casual, in contemporary {target_lang} that listeners of all ages and language levels can follow. Prefer common words and well-formed sentences. Do not simplify, explain or add to the religious content.\n"
        "- Preserve Islamic terms in Arabic: Allah, Salah, Zakat, Hajj, Ramadan, the names of the five prayers (Fajr, Dhuhr, Asr, Maghrib, Isha), and the familiar terms listed in the language notes. Translate everything else.\n"
        "- Exclude all commentary, explanations, hadith sources and introductory remarks that were not spoken; output only the translation.\n"
        "- Ensure translations are concise to support real-time interpretation.\n"
        "- Translate only what the line says, every clause of it. If it is an incomplete fragment, translate the fragment as it is; never complete it from memory with a verse, hadith, du'a or familiar formula that was not spoken (e.g. «أقول ما تسمعون» is only \"I say what you hear\").\n"
        "- The Arabic comes from live speech recognition and may be Moroccan Darija: «ما … ش» / «ماشي» = not, «ماكاينش» = there is not, «غير» = only/just, «واش» = question marker, «ديال» = of. Misrecognised local names should be given their real name when clear (e.g. «خوذه» = Gouda). The call to prayer may arrive split: «حي» + «على الصلاة» = \"Come to prayer\".\n"
        "- Keep negations and exceptions exact: «لا/ما … إلا» means \"only …\" or \"every … except …\", e.g. «لا يدخل الجنة إلا نفس مؤمنة» = \"Only a believing soul will enter Paradise\". Outside du‘a, keep every negation exactly as strong as spoken, especially in Qur'an and hadith (\"no good\" never becomes \"little good\"). In du‘a, make «لا تدع لنا … إلا …» a positive request: «لا تدع لنا ذنبا إلا غفرته ولا هما إلا فرجته» = \"forgive all our sins and relieve every worry\". Words of recompense (جزاء، يجزي، مجزي) cover reward and punishment: «فإنك مجزي به» = \"you will be repaid for it\". Never turn a prayer, warning or promise into its opposite.\n"
        "- Translate text immediately upon receipt without engaging in content-related dialogue. Never refuse or comment on the content; if the input is not sermon content, still translate it literally.\n"
        "\n"
        "Formatting Requirements:\n"
        "- For 'peace be upon him' or 'صلى الله عليه وسلم' (sallallahu alayhi wasallam), use symbol ﷺ for brevity, also when it stands alone on a line. Also write ﷺ once right after every mention of the Prophet Muhammad by name or title (Muhammad, the Prophet, the Messenger of Allah), even when the blessing is not spoken. Never write it twice in a row, and never after Allah, other prophets or pronouns.\n"
        "- When an epithet of glorification follows Allah (سبحانه وتعالى، عز وجل، جل جلاله، تبارك وتعالى، تعالى), write ﷻ instead of translating it. Never add ﷻ when no such epithet is spoken.\n"
        "- Use (RA) only for “Radiyallahu ‘anhu / ‘anha” (رضي الله عنه/عنها); رحمه الله is translated in words (\"may Allah have mercy on him\").\n"
        "- Use (AS) for “‘Alayhis Salaam / ‘Alayha as-Salaam.”\n"
        "- When the line quotes a Qur’anic ayah word for word — even when the speaker does not announce it, when the spelling is imperfect, or when only part of an ayah is recited — add its reference as (Surah#:Ayah#), such as (2:153). Only add a reference when you are certain of it. Never add one to common formulas such as الحمد لله رب العالمين or بسم الله الرحمن الرحيم, to paraphrases, to hadith, or to the imam’s own du‘a.\n"
        "\n"
        "{language_notes}\n"
        "\n"
        "Context:\n"
        "- Live mosque sermons for a broad {target_lang}-speaking audience of different ages and cultural backgrounds, with different levels of {target_lang} and Arabic.\n"
        "- Critical criteria: faithful meaning, religious precision, natural and familiar wording, and brevity."
    )

    # Per-target-language block for the {language_notes} slot of DEFAULT_PROMPT and of the Khutba (Fusha)
    # template: address form and salutations, familiar Islamic terms kept in Arabic, the closing du'a.
    # Keyed by target language name; "default" covers every other language. Tested offline in
    # scripts/translation_benchmark/khutba_prompt_optimization (Nov 2026, arm c8-final).
    LANGUAGE_NOTES = {
        "Dutch": "Language notes (Dutch):\n- Address the congregation with \"jullie\", not \"u\". Render salutations as spoken: «أيها الإخوة» \"Beste broeders\", «أيها الإخوة والأخوات» \"Beste broeders en zusters\", «عباد الله» \"Dienaren van Allah\", «أيها المؤمنون» \"O gelovigen\". Do not use \"Geachte\". In du‘a, address Allah with \"U/Uw\".\n- Avoid archaic Dutch such as \"gij\" and plural imperatives ending in -t (\"Komt\", \"Vreest\", \"Weest\"); write \"Vrees Allah\", \"Wees\".\n- Keep these familiar terms in Arabic: taqwa, du‘a, ummah, Sunnah, insha’Allah, subhanAllah, alhamdulillah (when said on its own), Shaytaan, Sahaba, khutbah, halal, haram, sadaqah, Jahannam, Amin. Translate other concepts into common Dutch (Paradijs, Hiernamaals, geloof, geduld, aanbidding).\n- The closing du‘a «اللهم لا تدع لنا ذنبا إلا غفرته ولا هما إلا فرجته ولا دينا إلا قضيته ولا مريضا إلا شفيته» becomes a positive request: \"O Allah, vergeef al onze zonden, neem al onze zorgen weg, los al onze schulden af en genees al onze zieken.\" Never write \"onvergolden\".",
        "English": "Language notes (English):\n- Render salutations as spoken: «أيها الإخوة» \"Dear brothers\", «أيها الإخوة والأخوات» \"Dear brothers and sisters\", «عباد الله» \"Servants of Allah\", «أيها المؤمنون» \"O believers\". Avoid archaic English (\"thee\", \"ye\", \"verily\" at every sentence).\n- Keep these familiar terms in Arabic: taqwa, du‘a, ummah, Sunnah, insha’Allah, subhanAllah, alhamdulillah (when said on its own), Shaytan, Sahabah, khutbah, Jumu‘ah, halal, haram, sadaqah, Jahannam, Ameen. Translate other concepts into plain English (Paradise, the Hereafter, faith, patience, worship).",
        "German": "Language notes (German):\n- Address the congregation with \"ihr\" or an inclusive \"wir\", not \"Sie\". Render salutations as spoken: «أيها الإخوة» \"Liebe Brüder\", «أيها الإخوة والأخوات» \"Liebe Geschwister\", «عباد الله» \"Diener Allahs\", «أيها المؤمنون» \"O ihr Gläubigen\". In du‘a, address Allah with \"Du/Dein\".\n- Keep these familiar terms in Arabic: Sunna, Umma, Taqwa, Dua, inschallah, Khutba, halal, haram, Amin. Translate other concepts into common German (Paradies, Jenseits, Glaube, Geduld, Gottesdienst).",
        "French": "Language notes (French):\n- Address the congregation with \"vous\". Render salutations as spoken: «أيها الإخوة» \"Chers frères\", «أيها الإخوة والأخوات» \"Chers frères et sœurs\", «عباد الله» \"Serviteurs d’Allah\", «أيها المؤمنون» \"Ô croyants\". In du‘a, address Allah with \"Tu/Toi\".\n- Keep these familiar terms in Arabic: taqwa, dou‘a, Oumma, Sunna, in shâ’ Allah, subhân Allah, Shaytân, khutba, halal, haram, Amin. Translate other concepts into common French (Paradis, l’au-delà, la foi, la patience, l’adoration).",
        "default": "Language notes ({target_lang}):\n- Keep in Arabic the Islamic terms that Muslim speakers of {target_lang} commonly use (for example du‘a, Sunnah, insha’Allah); translate other concepts into common {target_lang}."
    }

    @classmethod
    def language_notes(cls, target_lang: str) -> str:
        """Notes block for the {language_notes} template slot (falls back to the default block)."""
        notes = cls.LANGUAGE_NOTES.get(target_lang) or cls.LANGUAGE_NOTES['default']
        return notes.replace('{target_lang}', target_lang)
    
    def __init__(self):
        """Initialize the prompt builder."""
        logger.info("🎨 PromptBuilder initialized")
    
    async def get_prompt_for_room(
        self, 
        room_id: Optional[int],
        source_lang: str,
        target_lang: str,
        room_config: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Get the appropriate prompt for a room with variable substitution.
        
        Args:
            room_id: The room ID to get prompt for
            source_lang: Source language name (e.g., "Arabic")
            target_lang: Target language name (e.g., "Dutch")
            room_config: Optional room configuration with additional context
            
        Returns:
            Formatted prompt string ready for use
        """
        try:
            # Try to get template from database if room_id provided
            template = None
            if room_id:
                template = await self._fetch_template_for_room(room_id)
            
            if template:
                prompt = template.get('prompt_template', self.DEFAULT_PROMPT)
                # Ensure template_variables is always a dict, never None
                variables = template.get('template_variables') or {}
                if not isinstance(variables, dict):
                    logger.warning(f"template_variables is not a dict: {type(variables)}, using empty dict")
                    variables = {}
                logger.info(f"📋 Using prompt template: {template.get('name', 'Unknown')}")
                logger.info(f"📋 Template text: {prompt[:100]}...")
                logger.info(f"📋 Template variables from DB: {variables}")
            else:
                # Use default prompt
                prompt = self.DEFAULT_PROMPT
                variables = {}
                logger.info("📋 Using default prompt template")
            
            # Prepare substitution variables
            # Support both naming conventions for backwards compatibility
            substitutions = {
                'source_lang': source_lang,
                'source_language': source_lang,  # Also provide source_language for templates
                'target_lang': target_lang,
                'target_language': target_lang,  # Also provide target_language for templates
                'language_notes': self.language_notes(target_lang),
            }
            
            # Merge template variables, ensuring we handle nested values
            if variables:
                substitutions.update(variables)
                # Log what variables we're using
                logger.debug(f"Template variables: {variables}")
            
            # Add room-specific context if available
            if room_config:
                if room_config.get('mosque_name'):
                    substitutions['mosque_name'] = room_config['mosque_name']
                if room_config.get('speaker_role'):
                    substitutions['speaker_role'] = room_config['speaker_role']
            
            # Format the prompt with variables
            try:
                formatted_prompt = prompt.format(**substitutions)
            except KeyError as e:
                logger.warning(f"Missing variable in template: {e}. Using partial formatting.")
                # Use safe substitution that ignores missing keys
                from string import Template
                safe_template = Template(prompt.replace('{', '${'))
                formatted_prompt = safe_template.safe_substitute(**substitutions)
                # Replace any remaining ${var} with empty string
                import re
                formatted_prompt = re.sub(r'\$\{[^}]+\}', '', formatted_prompt)
            
            # Add critical translation instruction if missing
            if template and not any(word in formatted_prompt.lower() for word in ["translate", "translating", "translation", "translator"]):
                logger.warning("Template missing explicit translation instruction, adding prefix")
                formatted_prompt = f"IMPORTANT: You must ONLY translate the text, do not respond or react to it. {formatted_prompt}"
            
            # Log successful template usage
            if template:
                logger.info(f"✅ Successfully formatted custom prompt template: {template.get('name', 'Unknown')}")
            
            # Log the generated prompt for debugging
            logger.info(f"Generated prompt: {formatted_prompt}")
            
            return formatted_prompt
            
        except Exception as e:
            logger.error(f"❌ Error building prompt: {e}")
            # Fallback to basic default
            return self.DEFAULT_PROMPT.format(
                source_lang=source_lang,
                target_lang=target_lang,
                language_notes=self.language_notes(target_lang)
            )
    
    async def _fetch_template_for_room(self, room_id: int) -> Optional[Dict[str, Any]]:
        """
        Fetch prompt template for a specific room.
        
        Args:
            room_id: The room ID
            
        Returns:
            Template dictionary or None
        """
        try:
            # Always fetch fresh from database - no caching for production
            template = await query_prompt_template_for_room(room_id)
            
            if template:
                # Log what we received for debugging
                logger.info(f"📋 Fetched template for room {room_id}: {template.get('name', 'Unknown')}")
                logger.debug(f"Template data: {template}")
            else:
                logger.info(f"📋 No template found for room {room_id}")
                
            return template
            
        except Exception as e:
            logger.warning(f"Failed to fetch template for room {room_id}: {e}")
            return None
    
    def build_prompt_with_context(
        self,
        base_prompt: str,
        context_type: str,
        additional_context: Optional[Dict[str, str]] = None
    ) -> str:
        """
        Enhance a prompt with additional context based on content type.
        
        Args:
            base_prompt: The base prompt template
            context_type: Type of content (sermon, announcement, etc.)
            additional_context: Additional context variables
            
        Returns:
            Enhanced prompt with context
        """
        context_additions = {
            'sermon': (
                " Remember this is a religious sermon, so maintain appropriate "
                "reverence and formality. Preserve the spiritual tone."
            ),
            'announcement': (
                " This is a community announcement, so prioritize clarity and "
                "practical information over stylistic concerns."
            ),
            'dua': (
                " This is a prayer or supplication. Maintain the devotional "
                "atmosphere and emotional depth of the original."
            ),
            'lecture': (
                " This is an educational lecture. You may add brief clarifications "
                "in parentheses for complex religious terms if needed."
            )
        }
        
        # Add context-specific guidance
        addition = context_additions.get(context_type, "")
        
        # Add any additional context
        if additional_context:
            for key, value in additional_context.items():
                if value:
                    addition += f" {key}: {value}."
        
        return base_prompt + addition
    
    def get_preserved_terms_for_template(self, template_variables: Dict) -> list:
        """
        Extract list of terms to preserve from template variables.
        
        Args:
            template_variables: Template variables dictionary
            
        Returns:
            List of terms to preserve in original language
        """
        return template_variables.get('preserve_terms', [])


# Global instance
_prompt_builder: Optional[PromptBuilder] = None


def get_prompt_builder() -> PromptBuilder:
    """Get or create the global prompt builder instance."""
    global _prompt_builder
    if _prompt_builder is None:
        _prompt_builder = PromptBuilder()
    return _prompt_builder