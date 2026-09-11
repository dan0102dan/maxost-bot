import sys
import time
from pathlib import Path


def main():
    try:
        age=time.time()-float(Path('/tmp/maxost.heartbeat').read_text())
        sys.exit(0 if 0<=age<180 else 1)
    except (OSError,ValueError):
        sys.exit(1)


if __name__=='__main__':
    main()
