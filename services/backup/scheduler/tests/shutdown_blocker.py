import os
import signal
import sys
import time
from pathlib import Path

if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[2] == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path(sys.argv[1]).write_text(str(os.getpid()))
    time.sleep(600)
