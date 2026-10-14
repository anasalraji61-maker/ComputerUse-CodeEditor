"""نقطة دخول للتغليف (PyInstaller) والتشغيل المباشر: python launch.py"""
import sys

from assistant.app import main

if __name__ == "__main__":
    sys.exit(main())
