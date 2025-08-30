
import os, re, csv, time, threading, sys
import tkinter as tk
from tkinter import ttk, messagebox

# --- Playwright ---
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except Exception:
    print("Playwright not installed. Install with: pip install playwright")
    sys.exit(1)

# Try to auto-install Chromium if missing
def ensure_playwright_browsers(status_var=None):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # Quick smoke check: launch & close (headless)
            browser = p.chromium.launch(headless=True)
            browser.close()
        return
    except Exception:
        try:
            if status_var:
                status_var.set("Installing Playwright browser (one-time)…")
            from playwright.__main__ import main as playwright_main
            # Install only Chromium to keep it small
            playwright_main(["install", "chromium"])
        except Exception as e2:
            raise RuntimeError(
                "Playwright browser not installed and auto-install failed. "
                "Run in terminal:  pip install playwright && playwright install chromium"
            ) from e2

def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\-]+", "_", name.strip())
    return (name or "flipkart").lower()

# ---------------- Core scraping ----------------
def scrape_flipkart(query: str, pages: int, engine: str, headless: bool, status_var, continue_event):
    """
    Scrapes Flipkart search results using Playwright.
    Writes CSV to <query>_products.csv
    """
    ensure_playwright_browsers(status_var)

    engine_map = {
        "Chromium": "chromium",
        "Firefox": "firefox",
        "WebKit": "webkit",
    }
    engine_key = engine_map.get(engine, "chromium")

    out_file = f"{safe_filename(query)}_products.csv"
    status_var.set("Launching browser…")

    with sync_playwright() as p:
        browser_type = getattr(p, engine_key)
        browser = browser_type.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # Helper: wait for results or detect captcha
        def wait_for_results_or_captcha(timeout_ms=20000):
            try:
                page.wait_for_selector("div[data-id]", timeout=timeout_ms)
                return "results"
            except PWTimeout:
                # Check for captcha or blocker
                url = page.url.lower()
                text = ""
                try:
                    text = page.inner_text("body")
                except Exception:
                    pass
                if "captcha" in url or "verify" in url or "unusual traffic" in text.lower():
                    return "captcha"
                return "timeout"

        # Open Flipkart
        status_var.set("Opening Flipkart…")
        page.goto("https://www.flipkart.com", wait_until="domcontentloaded", timeout=30000)
        time.sleep(1.5)

        # Close login modal if present
        try:
            close_btn = page.locator("button:has-text('✕')")
            if close_btn.count() > 0:
                close_btn.first.click()
        except Exception:
            pass

        # Search
        status_var.set(f"Searching “{query}”…")
        try:
            page.fill("input[name='q']", query, timeout=15000)
            page.keyboard.press("Enter")
        except PWTimeout:
            browser.close()
            raise RuntimeError("Search bar not found. Flipkart may have blocked or changed layout.")

        # First page: results / captcha
        state = wait_for_results_or_captcha()
        if state == "captcha":
            status_var.set("CAPTCHA detected. Solve it in the browser, then click “Continue after CAPTCHA”.")
            # Show the page to user if headless; we can't if headless True…
            if headless:
                messagebox.showwarning(
                    "CAPTCHA",
                    "CAPTCHA detected but you are in headless mode.\n"
                    "Please uncheck Headless and try again, or switch to Chromium/Firefox headful."
                )
                browser.close()
                return None
            # Wait for user to click the “Continue” button
            continued = continue_event.wait(timeout=300)  # wait up to 5 minutes
            continue_event.clear()
            if not continued:
                browser.close()
                raise RuntimeError("Timed out waiting after CAPTCHA. Try again.")
            # After user solved captcha, verify results
            status_var.set("Checking results after CAPTCHA…")
            state = wait_for_results_or_captcha(timeout_ms=20000)

        if state == "timeout":
            browser.close()
            raise RuntimeError("Product results did not load. Possibly blocked or network issue.")

        # Scrape pages
        status_var.set("Scraping page 1…")
        total_rows = 0
        with open(out_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Product Name", "Price", "Rating"])

            for i in range(1, pages + 1):
                status_var.set(f"Scraping page {i}…")
                # Ensure cards appear on each page
                try:
                    page.wait_for_selector("div[data-id]", timeout=15000)
                except PWTimeout:
                    status_var.set("No more results or blocked.")
                    break

                cards = page.locator("div[data-id]")
                count = cards.count()

                for idx in range(count):
                    card = cards.nth(idx)

                    def get_text(sel_list):
                        for sel in sel_list:
                            try:
                                loc = card.locator(sel)
                                if loc.count() > 0:
                                    t = loc.first.inner_text().strip()
                                    if t:
                                        return t
                            except Exception:
                                pass
                        return "N/A"

                    name = get_text([".KzDlHZ", "a.IRpwTa", ".wjcEIp"])
                    price = get_text([".Nx9bqj", "div:has-text('₹')"])
                    rating = get_text([".XQDdHH", "span.XQDdHH"])

                    writer.writerow([name, price, rating])
                    total_rows += 1

                # Next page
                if i < pages:
                    # Try multiple next selectors
                    clicked = False
                    for sel in [
                        "a[rel='next']",
                        "a:has-text('Next')",
                        "span:has-text('Next')",
                        "a._9QVEpD:has-text('Next')",
                    ]:
                        try:
                            if page.locator(sel).count() > 0:
                                page.locator(sel).first.click()
                                clicked = True
                                break
                        except Exception:
                            pass

                    if not clicked:
                        status_var.set("No Next button found. Stopping.")
                        break

                    # Wait for new results to load
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                    time.sleep(1.2)

        browser.close()
        status_var.set(f"Done. Rows saved: {total_rows}")
        return os.path.abspath(out_file)

# ---------------- GUI ----------------
def start_thread():
    query = entry_query.get().strip()
    pages = int(spin_pages.get())
    engine = engine_var.get()
    headless = bool(headless_var.get())

    if not query:
        messagebox.showerror("Error", "Please enter a product to search.")
        return

    btn_start.config(state="disabled")
    btn_continue.config(state="disabled")
    status.set("Starting…")

    def runner():
        try:
            path = scrape_flipkart(query, pages, engine, headless, status, continue_event)
            if path:
                messagebox.showinfo("Success", f"Data saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Error", str(e))
        finally:
            btn_start.config(state="normal")
            btn_continue.config(state="disabled")

    threading.Thread(target=runner, daemon=True).start()

def after_captcha():
    continue_event.set()
    status.set("Continuing after CAPTCHA…")
    btn_continue.config(state="disabled")

# Build GUI
root = tk.Tk()
root.title("Flipkart Product Scraper (Playwright)")
root.geometry("540x260")
root.resizable(False, False)

frm = ttk.Frame(root, padding=14)
frm.pack(fill="both", expand=True)

ttk.Label(frm, text="Product to search:").grid(row=0, column=0, sticky="w")
entry_query = ttk.Entry(frm, width=38)
entry_query.grid(row=0, column=1, columnspan=2, sticky="we", padx=6)

ttk.Label(frm, text="Pages to scrape:").grid(row=1, column=0, sticky="w", pady=(8,0))
spin_pages = ttk.Spinbox(frm, from_=1, to=20, width=6)
spin_pages.set(5)
spin_pages.grid(row=1, column=1, sticky="w", padx=6, pady=(8,0))

ttk.Label(frm, text="Engine:").grid(row=2, column=0, sticky="w", pady=(8,0))
engine_var = tk.StringVar(value="Chromium")
ttk.Combobox(frm, textvariable=engine_var, state="readonly",
             values=["Chromium", "Firefox", "WebKit"]).grid(row=2, column=1, sticky="w", padx=6, pady=(8,0))

headless_var = tk.IntVar(value=0)  # default OFF so you can see what happens
ttk.Checkbutton(frm, text="Run headless (faster)", variable=headless_var).grid(row=2, column=2, sticky="w", pady=(8,0))

btn_start = ttk.Button(frm, text="Scrape & Save CSV", command=start_thread)
btn_start.grid(row=3, column=0, columnspan=3, pady=(14,6), sticky="we")

btn_continue = ttk.Button(frm, text="Continue after CAPTCHA", command=after_captcha)
btn_continue.grid(row=4, column=0, columnspan=3, pady=(4,0), sticky="we")
btn_continue.config(state="disabled")

status = tk.StringVar(value="Ready.")
ttk.Label(frm, textvariable=status, foreground="#555").grid(row=5, column=0, columnspan=3, sticky="w", pady=(10,0))

for i in range(3):
    frm.columnconfigure(i, weight=1)

continue_event = threading.Event()

root.mainloop()