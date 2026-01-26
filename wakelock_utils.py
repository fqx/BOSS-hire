"""
Wake Lock utilities to prevent system sleep during long-running operations.
Supports macOS (required) and Windows (optional).
"""
import platform
import subprocess
import atexit
import signal
import sys
from log_utils import logger


class WakeLock:
    """
    Context manager to prevent system sleep and screen dimming.
    
    Usage:
        # As context manager
        with WakeLock():
            do_long_running_task()
        
        # Or with explicit control
        lock = WakeLock()
        lock.acquire()
        # ... do work ...
        lock.release()
    """
    
    _instance = None  # Singleton for atexit cleanup
    
    def __init__(self):
        self._process = None
        self._enabled = False
        self._original_handlers = {}
    
    def acquire(self):
        """Start preventing system sleep."""
        if self._enabled:
            logger.debug("WakeLock already acquired")
            return
        
        system = platform.system()
        
        if system == 'Darwin':  # macOS
            self._acquire_macos()
        elif system == 'Windows':
            self._acquire_windows()
        else:
            logger.warning(f"WakeLock not supported on {system}")
            return
        
        if self._enabled:
            # Store instance for atexit
            WakeLock._instance = self
            
            # Register cleanup handlers
            atexit.register(self._atexit_handler)
            self._setup_signal_handlers()
            
            logger.info("WakeLock acquired - system sleep disabled")
    
    def release(self):
        """Allow system to sleep again."""
        if not self._enabled:
            return
        
        system = platform.system()
        
        if system == 'Darwin':
            self._release_macos()
        elif system == 'Windows':
            self._release_windows()
        
        self._enabled = False
        self._restore_signal_handlers()
        
        # Try to unregister atexit (not always possible)
        try:
            atexit.unregister(self._atexit_handler)
        except Exception:
            pass
        
        logger.info("WakeLock released - system sleep enabled")
    
    def _acquire_macos(self):
        """macOS: Use caffeinate command."""
        try:
            # -d: prevent display sleep
            # -i: prevent idle sleep
            # -s: prevent system sleep (when on AC power)
            self._process = subprocess.Popen(
                ['caffeinate', '-dis'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            self._enabled = True
        except FileNotFoundError:
            logger.error("caffeinate command not found - cannot prevent sleep")
        except Exception as e:
            logger.error(f"Failed to start caffeinate: {e}")
    
    def _release_macos(self):
        """macOS: Terminate caffeinate process."""
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception as e:
                logger.warning(f"Error terminating caffeinate: {e}")
                try:
                    self._process.kill()
                except Exception:
                    pass
            finally:
                self._process = None
    
    def _acquire_windows(self):
        """Windows: Use SetThreadExecutionState API."""
        try:
            import ctypes
            
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_DISPLAY_REQUIRED = 0x00000002
            
            result = ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            )
            
            if result == 0:
                logger.error("SetThreadExecutionState failed")
            else:
                self._enabled = True
        except Exception as e:
            logger.error(f"Failed to set Windows execution state: {e}")
    
    def _release_windows(self):
        """Windows: Reset execution state."""
        try:
            import ctypes
            
            ES_CONTINUOUS = 0x80000000
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception as e:
            logger.warning(f"Error resetting Windows execution state: {e}")
    
    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful cleanup."""
        # Store original handlers
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._original_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._signal_handler)
            except (OSError, ValueError):
                # Signal not available on this platform
                pass
    
    def _restore_signal_handlers(self):
        """Restore original signal handlers."""
        for sig, handler in self._original_handlers.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass
        self._original_handlers.clear()
    
    def _signal_handler(self, signum, frame):
        """Handle termination signals."""
        logger.info(f"Received signal {signum}, releasing WakeLock...")
        self.release()
        
        # Call original handler or exit
        original = self._original_handlers.get(signum)
        if original and callable(original) and original not in (signal.SIG_IGN, signal.SIG_DFL):
            original(signum, frame)
        else:
            sys.exit(0)
    
    def _atexit_handler(self):
        """Cleanup handler for atexit."""
        self.release()
    
    def __enter__(self):
        self.acquire()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False  # Don't suppress exceptions
    
    @property
    def is_active(self):
        """Check if wake lock is currently active."""
        return self._enabled
