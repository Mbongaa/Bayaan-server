#!/usr/bin/env python3
"""
Webhook handler for Supabase integration
Receives notifications about room creation and management from the dashboard
"""

import asyncio
import json
import logging
import os
from aiohttp import web
from typing import Dict, Any

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Webhook secret for validation (should match Supabase webhook secret)
WEBHOOK_SECRET = os.environ.get("SUPABASE_WEBHOOK_SECRET", "")

class WebhookHandler:
    def __init__(self):
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        
    async def handle_room_created(self, payload: dict):
        """Handle room creation webhook from Supabase"""
        try:
            # Extract room information (aligning with your Supabase schema)
            room_data = payload.get("record", {})
            room_name = room_data.get("Livekit_room_name")  # Note: Capital L in your schema
            mosque_id = room_data.get("mosque_id")
            room_id = room_data.get("id")
            room_title = room_data.get("Title")
            transcription_language = room_data.get("transcription_language")
            translation_language = room_data.get("translation__language")  # Note: double underscore
            
            if not room_name or not mosque_id:
                logger.error(f"Missing required fields in room creation webhook: {payload}")
                return {"status": "error", "message": "Missing Livekit_room_name or mosque_id"}
            
            # Store session information
            self.active_sessions[room_name] = {
                "room_id": room_id,
                "mosque_id": mosque_id,
                "room_title": room_title,
                "transcription_language": transcription_language or "ar",  # Default to Arabic
                "translation_language": translation_language or "nl",     # Default to Dutch
                "created_at": room_data.get("created_at"),
                "status": "active"
            }
            
            logger.info(f"🏛️ Room created for mosque {mosque_id}: {room_name} (ID: {room_id})")
            logger.info(f"🗣️ Transcription: {transcription_language}, Translation: {translation_language}")
            logger.info(f"📊 Active sessions: {len(self.active_sessions)}")
            
            return {"status": "success", "room_name": room_name, "room_id": room_id}
            
        except Exception as e:
            logger.error(f"Error handling room creation webhook: {e}")
            return {"status": "error", "message": str(e)}
    
    async def handle_room_deleted(self, payload: dict):
        """Handle room deletion webhook from Supabase"""
        try:
            # Extract room information
            room_data = payload.get("old_record", {})
            room_name = room_data.get("livekit_room_name")
            
            if room_name and room_name in self.active_sessions:
                del self.active_sessions[room_name]
                logger.info(f"🗑️ Room deleted: {room_name}")
                logger.info(f"📊 Active sessions: {len(self.active_sessions)}")
            
            return {"status": "success", "room_name": room_name}
            
        except Exception as e:
            logger.error(f"Error handling room deletion webhook: {e}")
            return {"status": "error", "message": str(e)}
    
    async def handle_session_started(self, payload: dict):
        """Handle session start webhook from Supabase"""
        try:
            session_data = payload.get("record", {})
            room_id = session_data.get("room_id")
            session_id = session_data.get("id")
            mosque_id = session_data.get("mosque_id")
            logging_enabled = session_data.get("logging_enabled", False)
            
            logger.info(f"🎤 Session started: {session_id} for room {room_id}, mosque {mosque_id}")
            logger.info(f"📝 Logging enabled: {logging_enabled}")
            
            # Find matching room by room_id and update with session info
            room_found = False
            for room_name, room_info in self.active_sessions.items():
                if room_info.get("room_id") == room_id:
                    room_info["session_id"] = session_id
                    room_info["session_started_at"] = session_data.get("started_at")
                    room_info["logging_enabled"] = logging_enabled
                    room_info["status"] = "recording" if logging_enabled else "active"
                    logger.info(f"🏛️ Updated room {room_name} with session {session_id}")
                    room_found = True
                    break
            
            if not room_found:
                # Create temporary session entry if room not found
                logger.warning(f"⚠️ Room not found for session {session_id}, creating temporary entry")
                temp_room_name = f"session_{session_id[:8]}"
                self.active_sessions[temp_room_name] = {
                    "room_id": room_id,
                    "mosque_id": mosque_id,
                    "session_id": session_id,
                    "session_started_at": session_data.get("started_at"),
                    "logging_enabled": logging_enabled,
                    "status": "recording" if logging_enabled else "active",
                    "transcription_language": "ar",  # Default
                    "translation_language": "nl"    # Default
                }
                    
            return {"status": "success", "session_id": session_id, "logging_enabled": logging_enabled}
            
        except Exception as e:
            logger.error(f"Error handling session start webhook: {e}")
            return {"status": "error", "message": str(e)}
    
    async def handle_session_ended(self, payload: dict):
        """Handle session end webhook from Supabase"""
        try:
            session_data = payload.get("record", {})
            session_id = session_data.get("id")
            
            # Update session status
            for room_name, room_info in self.active_sessions.items():
                if room_info.get("session_id") == session_id:
                    room_info["session_ended_at"] = session_data.get("ended_at")
                    room_info["status"] = "ended"
                    logger.info(f"🛑 Session ended for room {room_name}: {session_id}")
                    break
                    
            return {"status": "success", "session_id": session_id}
            
        except Exception as e:
            logger.error(f"Error handling session end webhook: {e}")
            return {"status": "error", "message": str(e)}
    
    def get_room_context(self, room_name: str) -> Dict[str, Any]:
        """Get tenant context for a specific room"""
        return self.active_sessions.get(room_name, {})

# Global webhook handler instance
webhook_handler = WebhookHandler()

async def handle_webhook(request):
    """Main webhook endpoint handler"""
    try:
        # Validate webhook secret if configured
        if WEBHOOK_SECRET:
            webhook_signature = request.headers.get("X-Supabase-Signature", "")
            # TODO: Implement proper signature validation
            
        # Parse webhook payload
        payload = await request.json()
        webhook_type = payload.get("type")
        table = payload.get("table")
        
        logger.info(f"📨 Received webhook: type={webhook_type}, table={table}")
        
        # Route to appropriate handler
        result = {"status": "error", "message": "Unknown webhook type"}
        
        if table == "rooms":
            if webhook_type == "INSERT":
                result = await webhook_handler.handle_room_created(payload)
            elif webhook_type == "DELETE":
                result = await webhook_handler.handle_room_deleted(payload)
                
        elif table == "room_sessions":
            if webhook_type == "INSERT":
                result = await webhook_handler.handle_session_started(payload)
            elif webhook_type == "UPDATE":
                # Check if session is ending
                if payload.get("record", {}).get("ended_at"):
                    result = await webhook_handler.handle_session_ended(payload)
                    
        return web.json_response(result)
        
    except json.JSONDecodeError:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def handle_status(request):
    """Status endpoint to check webhook handler health"""
    return web.json_response({
        "status": "healthy",
        "active_sessions": len(webhook_handler.active_sessions),
        "sessions": list(webhook_handler.active_sessions.keys())
    })

async def start_webhook_server():
    """Start the webhook server"""
    app = web.Application()
    
    # Add routes
    app.router.add_post('/webhook', handle_webhook)
    app.router.add_get('/status', handle_status)
    
    # Add CORS middleware
    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == 'OPTIONS':
            return web.Response(headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type, X-Supabase-Signature',
            })
        response = await handler(request)
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    
    app.middlewares.append(cors_middleware)
    
    # Start server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8767)
    await site.start()
    
    logger.info("🚀 Webhook server started on http://0.0.0.0:8767")
    logger.info("📨 Webhook endpoint: POST http://0.0.0.0:8767/webhook")
    logger.info("📊 Status endpoint: GET http://0.0.0.0:8767/status")
    
    try:
        await asyncio.Future()  # Run forever
    except KeyboardInterrupt:
        logger.info("Shutting down webhook server...")
        await runner.cleanup()

# Export the handler for use in main.py
def get_room_context(room_name: str) -> Dict[str, Any]:
    """Get tenant context for a room from webhook handler"""
    return webhook_handler.get_room_context(room_name)

if __name__ == "__main__":
    try:
        asyncio.run(start_webhook_server())
    except KeyboardInterrupt:
        logger.info("Webhook server stopped by user")
    except Exception as e:
        logger.error(f"Webhook server error: {e}")