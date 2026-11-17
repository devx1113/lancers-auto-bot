#!/usr/bin/env python3
"""
Setup script for CrowdWorks Job Scraping Bot
"""

import subprocess
import sys
import os

def install_requirements():
    """Install required packages"""
    print("Installing required packages...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
        print("✅ All packages installed successfully!")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Error installing packages: {e}")
        return False

def main():
    print("🤖 CrowdWorks Job Scraping Bot Setup")
    print("=" * 40)
    
    if install_requirements():
        print("\n🎉 Setup completed successfully!")
        print("\nUsage:")
        print("  python main.py              # Run once")
        print("  python main.py --continuous # Run continuously")
        print("  python main.py --interval=600  # Custom interval (seconds)")
    else:
        print("\n❌ Setup failed. Please install packages manually:")
        print("  pip install -r requirements.txt")

if __name__ == "__main__":
    main()
