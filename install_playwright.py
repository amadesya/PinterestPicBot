import asyncio
from playwright.async_api import async_playwright

async def main():
    playwright = await async_playwright().start()
    await playwright.chromium.launch()
    await playwright.stop()

asyncio.run(main())
