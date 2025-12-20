#!/usr/bin/env python3
"""
Start both Discord bot and API server together
Alternative to start.sh for systems where bash scripts don't work well
"""
import subprocess
import sys
import time
import signal
import os

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    print("\n\n[SHUTDOWN] Received signal, cleaning up...")
    global bot_process, api_process
    
    if api_process:
        print("[SHUTDOWN] Stopping API server...")
        api_process.terminate()
        try:
            api_process.wait(timeout=5)
        except:
            api_process.kill()
    
    if bot_process:
        print("[SHUTDOWN] Stopping Discord bot...")
        bot_process.terminate()
        try:
            bot_process.wait(timeout=5)
        except:
            bot_process.kill()
    
    print("[SHUTDOWN] Goodbye!")
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Global variables to track processes
bot_process = None
api_process = None

def main():
    global bot_process, api_process
    
    print("=" * 60)
    print("Starting SOMEWHERE Game - Combined Service")
    print("=" * 60)
    
    # Start API server FIRST to bind to port (critical for Render health checks)
    print("[1/2] Starting API server (api.py) in background...")
    port = os.getenv('PORT', '5001')
    print(f"       API will bind to port {port}")
    
    try:
        api_process = subprocess.Popen(
            [sys.executable, 'api.py'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )
        print(f"       API server started (PID: {api_process.pid})")
    except Exception as e:
        print(f"[ERROR] Failed to start API server: {e}")
        return 1
    
    # Give API a moment to bind to port
    print("       Waiting for API to bind to port...")
    time.sleep(3)
    
    # Check if API is still running
    if api_process.poll() is not None:
        print("[ERROR] API server exited immediately. Check api.py logs.")
        return 1
    
    print("       [OK] API server is running")
    
    # Now start Discord bot in background
    print("[2/2] Starting Discord bot (bot.py)...")
    try:
        bot_process = subprocess.Popen(
            [sys.executable, 'bot.py'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )
        print(f"       Discord bot started (PID: {bot_process.pid})")
    except Exception as e:
        print(f"[ERROR] Failed to start Discord bot: {e}")
        print(f"[INFO] API is still running, service will stay up")
        # Don't return error - API is running
    
    print("=" * 60)
    print("[SUCCESS] Both services started successfully!")
    print(f"   • API: http://0.0.0.0:{port}")
    print(f"   • Dashboard: http://0.0.0.0:{port}/admin")
    print("=" * 60)
    print()
    
    # Keep both processes running
    try:
        print("[MONITOR] Both services running. Monitoring...")
        while True:
            # Check API is still running
            if api_process.poll() is not None:
                print("[ERROR] API server died! Exit code:", api_process.returncode)
                return 1
            
            # Check bot (but don't fail if bot dies, API is more important)
            if bot_process and bot_process.poll() is not None:
                print("[WARN] Discord bot died! Exit code:", bot_process.returncode)
                print("[INFO] API is still running, service continues")
                bot_process = None  # Don't try to kill it later
            
            time.sleep(5)  # Check every 5 seconds
            
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Keyboard interrupt received")
        return 0
    finally:
        # Clean up processes
        print("[CLEANUP] Shutting down services...")
        
        if api_process and api_process.poll() is None:
            print("[CLEANUP] Stopping API server...")
            api_process.terminate()
            try:
                api_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                api_process.kill()
                api_process.wait()
        
        if bot_process and bot_process.poll() is None:
            print("[CLEANUP] Stopping Discord bot...")
            bot_process.terminate()
            try:
                bot_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bot_process.kill()
                bot_process.wait()
        
        print("[CLEANUP] Shutdown complete")

if __name__ == '__main__':
    sys.exit(main())

