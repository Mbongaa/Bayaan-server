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

class ProductionAgent:
    """Production-ready agent wrapper with health checks and graceful shutdown."""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.is_running = False
        self.shutdown_event = asyncio.Event()
        
    async def health_check(self) -> bool:
        """Health check endpoint for Render."""
        try:
            # Basic health check - verify environment variables are set
            required_vars = [
                'LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET',
                'OPENAI_API_KEY', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY'
            ]
            
            for var in required_vars:
                if not os.getenv(var):
                    self.logger.error(f"Missing required environment variable: {var}")
                    return False
            
            # Check if agent is running
            return self.is_running
            
        except Exception as e:
            self.logger.error(f"Health check failed: {e}")
            return False
    
    async def start_agent(self):
        """Start the LiveKit agent in production mode."""
        self.logger.info("Starting Bayaan LiveKit Agent in production mode")
        
        try:
            # Set production environment
            os.environ['ENVIRONMENT'] = 'production'
            
            # Import and configure the agent
            from livekit.agents import WorkerOptions, cli
            from main import entrypoint, prewarm, request_fnc
            
            # Production worker configuration
            worker_opts = WorkerOptions(
                entrypoint_fnc=entrypoint,
                prewarm_fnc=prewarm,
                request_fnc=request_fnc
            )
            
            self.is_running = True
            
            # Start the agent with production configuration
            cli.run_app(worker_opts)
            
        except Exception as e:
            self.logger.error(f"Failed to start agent: {e}")
            self.is_running = False
            raise
    
    async def graceful_shutdown(self):
        """Handle graceful shutdown for production."""
        self.logger.info("Initiating graceful shutdown...")
        
        try:
            # Stop accepting new jobs
            self.is_running = False
            
            # Wait for current jobs to complete (max 30 seconds)
            await asyncio.wait_for(self.shutdown_event.wait(), timeout=30.0)
            
            # Close database connections
            try:
                from database import close_database_connections
                await close_database_connections()
            except Exception as e:
                self.logger.warning(f"Error closing database connections: {e}")
            
            self.logger.info("Graceful shutdown completed")
            
        except asyncio.TimeoutError:
            self.logger.warning("Shutdown timeout reached, forcing exit")
        except Exception as e:
            self.logger.error(f"Error during shutdown: {e}")
    
    def setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            self.logger.info(f"Received signal {signum}, initiating shutdown...")
            self.shutdown_event.set()
        
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

async def main():
    """Main production entry point."""
    # Setup production logging
    setup_production_logging()
    
    logger = logging.getLogger(__name__)
    logger.info("Starting Bayaan LiveKit Agent for production deployment")
    
    # Create production agent
    agent = ProductionAgent()
    
    # Setup signal handlers
    agent.setup_signal_handlers()
    
    try:
        # Start the agent
        await agent.start_agent()
        
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error in production agent: {e}")
        sys.exit(1)
    finally:
        # Graceful shutdown
        await agent.graceful_shutdown()

if __name__ == "__main__":
    # Handle different command line arguments
    if len(sys.argv) > 1:
        if sys.argv[1] == "health":
            # Health check endpoint
            agent = ProductionAgent()
            is_healthy = asyncio.run(agent.health_check())
            sys.exit(0 if is_healthy else 1)
        elif sys.argv[1] == "start":
            # Start the agent
            asyncio.run(main())
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
        asyncio.run(main()) 