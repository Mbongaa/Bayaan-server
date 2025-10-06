# Bayaan LiveKit Agent - Production Deployment

This directory contains the production-ready version of the Bayaan LiveKit Agent, optimized for deployment on Render as a background worker.

## 🚀 Quick Deploy to Render

### 1. Repository Setup

1. Push this directory to your GitHub repository: `https://github.com/Mbongaa/Bayaan-server.git`
2. Connect the repository to Render

### 2. Environment Variables

Set the following environment variables in your Render dashboard:

**Required:**

- `LIVEKIT_URL` - Your LiveKit server URL (e.g., `wss://your-livekit-server.com`)
- `LIVEKIT_API_KEY` - LiveKit API key
- `LIVEKIT_API_SECRET` - LiveKit API secret
- `OPENAI_API_KEY` - OpenAI API key for translation
- `SUPABASE_URL` - Supabase project URL
- `SUPABASE_SERVICE_ROLE_KEY` - Supabase service role key
- `SUPABASE_ANON_KEY` - Supabase anonymous key

**Optional:**

- `SPEECHMATICS_API_KEY` - Speechmatics API key (for enhanced STT)
- `LOG_LEVEL` - Logging level (default: INFO)
- `AGENT_NAME` - Agent name (default: bayaan-transcriber)
- `MAX_WORKERS` - Maximum worker processes (default: 2)

### 3. Deploy

1. Create a new Web Service on Render
2. Connect to your GitHub repository
3. Set the following:
   - **Environment**: Docker
   - **Dockerfile Path**: `./Dockerfile`
   - **Build Command**: (leave empty)
   - **Start Command**: `python main_production.py start`

## 📁 File Structure

```
bayaan-server-production/
├── main_production.py         # Production-ready main entry point
├── Dockerfile                 # Production Docker configuration
├── render.yaml               # Render deployment configuration
├── requirements.txt          # Python dependencies with versions
├── environment.template      # Environment variables template
├── README.md                 # This file
└── [original server files]   # All original server functionality
```

## 🔧 Production Features

### Health Checks

- Built-in health check endpoint: `python main_production.py health`
- Validates environment variables and agent status
- Integrated with Render's health monitoring

### Graceful Shutdown

- Handles SIGTERM and SIGINT signals
- Waits for current jobs to complete (max 30 seconds)
- Properly closes database connections

### Logging

- Structured logging with configurable levels
- JSON output for production monitoring
- Timestamp and context information

### Error Handling

- Robust error handling and recovery
- Automatic restart on failure
- Detailed error reporting

## 🎯 Testing Your Deployment

### 1. Verify Environment

```bash
# Check if all required environment variables are set
python main_production.py health
```

### 2. Local Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables (use environment.template)
export LIVEKIT_URL=wss://your-livekit-server.com
export LIVEKIT_API_KEY=your-api-key
# ... other variables

# Run the agent
python main_production.py start
```

### 3. Production Verification

- Check Render logs for successful startup
- Verify agent registration in LiveKit server
- Test with a room connection from your frontend

## 📊 Monitoring

### Render Dashboard

- CPU and memory usage
- Application logs
- Health check status
- Auto-scaling metrics

### LiveKit Server

- Agent registration status
- Room assignments
- Connection health

## 🔄 Scaling

The service is configured for auto-scaling:

- **Minimum instances**: 1
- **Maximum instances**: 3
- **Scaling triggers**: CPU > 80% or Memory > 80%

## 🛠️ Troubleshooting

### Common Issues

1. **Agent won't start**
   - Check all environment variables are set
   - Verify LiveKit server connectivity
   - Check logs for specific error messages

2. **Database connection errors**
   - Verify Supabase credentials
   - Check network connectivity
   - Ensure database is accessible

3. **Translation failures**
   - Check OpenAI API key and quota
   - Verify Speechmatics configuration (if used)
   - Check language configuration

### Debug Commands

```bash
# Health check
python main_production.py health

# Verbose logging
LOG_LEVEL=DEBUG python main_production.py start

# Check configuration
python -c "from config import get_config; print(get_config())"
```

## 📞 Support

For issues specific to this deployment:

1. Check Render logs first
2. Verify all environment variables
3. Test connectivity to external services
4. Contact support with relevant log excerpts

## 🔒 Security

- Non-root user in Docker container
- Environment variables for sensitive data
- Network isolation in Render
- Regular security updates

## 📈 Performance

- Optimized for background worker usage
- Efficient resource utilization
- Automatic scaling based on load
- Connection pooling for database

---

**Ready for production? Deploy to Render and start testing with your first subject!**
