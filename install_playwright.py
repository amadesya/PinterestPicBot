import subprocess

try:
    subprocess.run(["playwright", "install", "--with-deps"], check=True)
    print("Playwright browsers installed")
except Exception as e:
    print("Playwright setup failed:", e)
