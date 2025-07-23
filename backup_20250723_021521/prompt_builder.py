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
    
    # Default fallback prompt if no template is found
    DEFAULT_PROMPT = (
        "You are an expert simultaneous interpreter. Your task is to translate from {source_lang} to {target_lang}. "
        "Provide a direct and accurate translation of the user's input. Be concise and use natural-sounding language. "
        "Do not add any additional commentary, explanations, or introductory phrases."
    )
    
    def __init__(self):
        """Initialize the prompt builder."""
        self.cached_templates = {}
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
                prompt = template['prompt_template']
                variables = template.get('template_variables', {})
                logger.info(f"📋 Using prompt template: {template.get('name', 'Unknown')}")
            else:
                # Use default prompt
                prompt = self.DEFAULT_PROMPT
                variables = {}
                logger.info("📋 Using default prompt template")
            
            # Prepare substitution variables
            substitutions = {
                'source_lang': source_lang,
                'target_lang': target_lang,
                **variables  # Include template-specific variables
            }
            
            # Add room-specific context if available
            if room_config:
                if room_config.get('mosque_name'):
                    substitutions['mosque_name'] = room_config['mosque_name']
                if room_config.get('speaker_role'):
                    substitutions['speaker_role'] = room_config['speaker_role']
            
            # Format the prompt with variables
            formatted_prompt = prompt.format(**substitutions)
            
            # Log the generated prompt for debugging
            logger.debug(f"Generated prompt: {formatted_prompt[:100]}...")
            
            return formatted_prompt
            
        except Exception as e:
            logger.error(f"❌ Error building prompt: {e}")
            # Fallback to basic default
            return self.DEFAULT_PROMPT.format(
                source_lang=source_lang,
                target_lang=target_lang
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
            # Check cache first
            cache_key = f"room_{room_id}"
            if cache_key in self.cached_templates:
                return self.cached_templates[cache_key]
            
            # Fetch from database
            template = await query_prompt_template_for_room(room_id)
            
            if template:
                # Cache for future use (5 minute cache)
                self.cached_templates[cache_key] = template
                
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