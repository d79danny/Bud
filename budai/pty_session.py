"""Set a managed subprocess's controlling terminal before replacing it with Bash."""
import fcntl
import os
import sys
import termios


def main():
    fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSCTTY, 0)
    os.execv("/bin/bash", ["/bin/bash", "-c", sys.argv[1]])


if __name__ == "__main__":
    main()
