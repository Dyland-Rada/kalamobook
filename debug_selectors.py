
import asyncio
from playwright.async_api import async_playwright
import os

BASE_URL = 'https://www.casadellibro.com'

async def main():
    async with async_playwright() as p:
        # Launch with stealth args
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080}
        )
        page = await context.new_page()
        
        # Add init script to mask webdriver
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        
        # SEARCH FOR DUNE
        query = "Dune"
        print(f"Navigating to {BASE_URL}")
        await page.goto(BASE_URL, timeout=60000)
        
        # Accept cookies if present (optional, good practice)
        try:
            await page.click('#onetrust-accept-btn-handler', timeout=5000)
            print("Accepted cookies")
        except:
            print("No cookie banner found or timeout")

        # Check for popup (Country change) and close it
        print("Checking for country popup...")
        try:
            # Wait a bit, sometimes it pops up late
            try:
                await page.wait_for_selector('div.popup-cambio-pais, dialog[open]', timeout=5000)
                print("Popup selector found")
            except:
                print("Popup selector not found immediately")

            # Try to close any open dialog or popup class
            if await page.locator('div.popup-cambio-pais').count() > 0:
                 print("Found .popup-cambio-pais container")
                 # Try to click the close button inside
                 close_btn = page.locator('div.popup-cambio-pais button')
                 if await close_btn.count() > 0:
                     await close_btn.first.click()
                     print("Clicked close button in .popup-cambio-pais")
            
            # Also check for generic dialog
            if await page.locator('dialog[open]').count() > 0:
                 print("Found open dialog")
                 # Check if it is the country one (contains "Cambio de país")
                 text = await page.locator('dialog[open]').inner_text()
                 if "Cambio de país" in text:
                     print("It is the country dialog")
                     # Try to find close button inside dialog
                     await page.locator('dialog[open] button').first.click()
                     print("Clicked button in dialog")
                     
            await page.wait_for_timeout(1000)
        except Exception as e:
            print(f"Error handling popup: {e}")

        # Type in search box
        print("Typing search query...")
        try:
             # Use specific ID found in dump
             await page.fill('#empathy-search', query)
             print("Filled input")
             
             # Click search button
             await page.click('button[name="buscar"]')
             print("Clicked search button")
        except Exception as e:
             print(f"Error interacting with search: {e}")

        # Wait for results
        print("Waiting for results...")
        # Wait for url to contain 'search' (if it navigates) or 'query'
        try:
            await page.wait_for_url('**/search?*', timeout=10000)
            print("URL changed to search page")
        except:
            print("URL did not change or already there")
            
        await page.wait_for_timeout(10000)
        
        # Take screenshot
        await page.screenshot(path="search_results_interactive.png")
        print("Saved search_results_interactive.png")
        
        # Dump HTML
        content = await page.content()
        with open("search_dump_interactive.html", "w", encoding="utf-8") as f:
            f.write(content)
        print("Saved search_dump_interactive.html")
        
        # Print detected products
        # Try finding ANY link with text containing "Dune"
        links = page.locator('a:has-text("Dune")')
        count = await links.count()
        print(f"Found {count} links with text 'Dune'")
        
        for i in range(min(5, count)):
            item = links.nth(i)
            text = await item.inner_text()
            href = await item.get_attribute('href')
            print(f"--- Link {i} ---")
            print(f"Text: {text}")
            print(f"Href: {href}")
            
            # Check parent for product class
            # ...


        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
