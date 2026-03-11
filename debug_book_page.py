import asyncio
from playwright.async_api import async_playwright

URL = "https://www.casadellibro.com.co/libro-el-temor-de-un-hombre-sabio-saga-cronica-del-asesino-de-reyes-2/9788499899619/2042438"

async def debug():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_extra_http_headers({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        })

        await page.goto(URL, timeout=60000, wait_until='domcontentloaded')
        await page.wait_for_timeout(4000)

        # Remove cookie overlay
        await page.evaluate("""
            ['#onetrust-consent-sdk', '.onetrust-pc-dark-filter'].forEach(sel => {
                document.querySelectorAll(sel).forEach(el => el.remove());
            });
        """)

        # Debug: what's after h1?
        info = await page.evaluate("""
            () => {
                const h1 = document.querySelector('h1');
                if (!h1) return {error: 'no h1'};
                
                let result = {
                    h1_text: h1.innerText.trim(),
                    siblings: []
                };

                let el = h1.nextElementSibling;
                let i = 0;
                while (el && i < 5) {
                    result.siblings.push({
                        tag: el.tagName,
                        className: el.className,
                        text: el.innerText.trim().substring(0, 80),
                        href: el.href || ''
                    });
                    el = el.nextElementSibling;
                    i++;
                }
                return result;
            }
        """)
        print("H1 context:", info)

        # Search for any links containing "/libros-ebooks/" or "Rothfuss"
        author_info = await page.evaluate("""
            () => {
                // All links on page that look like author links
                const all_a = Array.from(document.querySelectorAll('a[href*="/libros-ebooks/"]'));
                const links = all_a.map(a => ({href: a.href, text: a.innerText.trim(), class: a.className}));
                
                // Also check for any element with class containing "author"
                const author_el = document.querySelector('[class*="author"], [class*="Author"]');
                
                return {
                    libros_ebooks_links: links.slice(0, 3),
                    author_el: author_el ? {class: author_el.className, text: author_el.innerText.trim().substring(0, 60)} : null
                };
            }
        """)
        print("Author info:", author_info)

        # Find synopsis container - search all class names on page
        synopsis_search = await page.evaluate("""
            () => {
                // Look for the element containing "Sinopsis de" text
                const allEls = document.querySelectorAll('*');
                let found = [];
                for (let el of allEls) {
                    if (el.childNodes.length > 0 && el.innerText && 
                        el.innerText.startsWith('Sinopsis de') && 
                        el.children.length <= 5) {
                        found.push({
                            tag: el.tagName,
                            class: el.className,
                            text: el.innerText.trim().substring(0, 200)
                        });
                        if (found.length >= 3) break;
                    }
                }
                return found;
            }
        """)
        print("Synopsis search:", synopsis_search)

        try:
            ver_mas = page.locator('label.like-a-link')
            n = await ver_mas.count()
            print(f"Ver mas count: {n}")
            if n > 0:
                await ver_mas.first.click(force=True)
                await page.wait_for_timeout(1000)
                print("Clicked Ver mas")
        except Exception as e:
            print(f"Ver mas error: {e}")

        # Debug synopsis
        synopsis_info = await page.evaluate("""
            () => {
                // h2.resumen is the heading. The actual synopsis text is in a sibling.
                const h2 = document.querySelector('h2.resumen');
                if (!h2) return {error: 'no h2.resumen'};
                let result = {h2_class: h2.className, siblings: []};
                // Check the parent's children for text
                const parent = h2.parentElement;
                if (parent) {
                    result.parent_class = parent.className;
                    result.parent_children = Array.from(parent.children).map(c => ({
                        tag: c.tagName, class: c.className, text: c.innerText.trim().substring(0, 200)
                    }));
                }
                return result;
            }
        """)
        print("Synopsis info:", synopsis_info)


        await browser.close()

asyncio.run(debug())
