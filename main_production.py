#!/usr/bin/env python3
"""
Production-ready LiveKit Agent for Bayaan Translation Service
Optimized for Render deployment as a background worker
"""

import asyncio
import logging
import os
import sys
import signal
from datetime import datetime

# Setup production logging
def setup_production_logging():
    """Configure logging for production."""
    log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
    
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )

def health_check() -> bool:
    """Health check endpoint for Render."""
    try:
        # Basic health check - verify environment variables are set
        required_vars = [
            'LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET',
            'OPENAI_API_KEY', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY'
        ]
        
        for var in required_vars:
            if not os.getenv(var):
                print(f"Missing required environment variable: {var}")
                return False
        
        print("Health check passed - all required environment variables are set")
        return True
        
    except Exception as e:
        print(f"Health check failed: {e}")
        return False

def main():
    """Main production entry point."""
    # Setup production logging
    setup_production_logging()
    
    logger = logging.getLogger(__name__)
    logger.info("Starting Bayaan LiveKit Agent for production deployment")
    
    # Set production environment
    os.environ['ENVIRONMENT'] = 'production'
    
    try:
        # Import and run the agent directly
        from livekit.agents import WorkerOptions, cli
        from main import entrypoint, prewarm, request_fnc
        
        # Production worker configuration
        worker_opts = WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            request_fnc=request_fnc
        )
        
        logger.info("Starting LiveKit CLI with production configuration")
        
        # Run the agent - this handles its own event loop
        cli.run_app(worker_opts)
        
    except Exception as e:
        logger.error(f"Fatal error in production agent: {e}")
        sys.exit(1)

if __name__ == "__main__":
    # Handle different command line arguments
    if len(sys.argv) > 1:
        if sys.argv[1] == "health":
            # Health check endpoint
            is_healthy = health_check()
            sys.exit(0 if is_healthy else 1)
        elif sys.argv[1] == "start":
            # Start the agent
            main()
        else:
            # Default to original main.py behavior
            from main import *
            # Run with original CLI
            from livekit.agents import cli, WorkerOptions
            cli.run_app(WorkerOptions(
                entrypoint_fnc=entrypoint,
                prewarm_fnc=prewarm,
                request_fnc=request_fnc
            ))
    else:
        # Run the main agent
        main() 