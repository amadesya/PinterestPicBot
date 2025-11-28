from playwright.sync_api import sync_playwright
import subprocess

subprocess.run(["playwright", "install"], check=True)

print("Playwright browsers installed successfully!")
