from playwright.async_api import async_playwright
import asyncio

async def main():
    async with async_playwright() as p:
        await p.chromium.launch()
    print("Playwright browsers installed successfully!")

asyncio.run(main())
