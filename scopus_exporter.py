import asyncio
import subprocess
import platform
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import re # For parsing max documents

# --- VPN Configuration (Windows Specific) ---
VPN_NAME = "Behdani"
VPN_SERVER = "vpn.birjand.ac.ir"
VPN_USERNAME = "4011312071"
VPN_PASSWORD = "0640663427"
# --- End VPN Configuration ---

# --- Gigalib and Scopus Details ---
GIGALIB_IP_URL = "http://gigalib.org/ip/"
GIGALIB_EMAIL_PAGE_URL_FRAGMENT = "gigalib.org/getemail.aspx"
GIGALIB_PD_URL_FRAGMENT = "pd.gigalib.org/search-basic.aspx"
USER_EMAIL = "alibehdani.elt@gmail.com"
# MODIFIED: Text to find Scopus link on Gigalib
GIGALIB_SCOPUS_LINK_TEXT = "سرور1" # As requested
SCOPUS_ADVANCED_SEARCH_QUERY = 'ISSN ( 01426001 ) AND ( LIMIT-TO ( DOCTYPE , "ar" ) ) AND ( LIMIT-TO ( LANGUAGE , "English" ) ) AND ( LIMIT-TO ( SRCTYPE , "j" ) )'
# --- End Details ---

DOWNLOAD_PATH = "./scopus_export.csv"

def connect_vpn_windows():
    print(f"Attempting to connect to VPN: {VPN_NAME}...")
    try:
        subprocess.run(['rasdial', VPN_NAME, '/disconnect'], check=False, timeout=30, capture_output=True)
        print(f"Disconnected any existing session for {VPN_NAME}.")
    except Exception as e:
        print(f"No active session for {VPN_NAME} or error disconnecting: {e}")

    try:
        process = subprocess.run(
            ['rasdial', VPN_NAME, VPN_USERNAME, VPN_PASSWORD],
            check=True, capture_output=True, text=True, timeout=60
        )
        if "Command completed successfully" in process.stdout:
            print(f"VPN '{VPN_NAME}' command executed. Assuming connection in progress or successful.")
            return True
        else:
            print(f"VPN connection stdout: {process.stdout}")
            print(f"VPN connection stderr: {process.stderr}")
            print(f"Failed to execute VPN connect command for '{VPN_NAME}'.")
            return False
    except subprocess.CalledProcessError as e:
        print(f"Error executing VPN command for '{VPN_NAME}': {e}\nStdout: {e.stdout}\nStderr: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        print(f"Timeout during VPN connection attempt for '{VPN_NAME}'.")
        return False
    except FileNotFoundError:
        print("`rasdial` command not found. This VPN connection method is for Windows only.")
        return False

def disconnect_vpn_windows():
    print(f"Attempting to disconnect from VPN: {VPN_NAME}...")
    try:
        subprocess.run(['rasdial', VPN_NAME, '/disconnect'], check=True, timeout=30, capture_output=True)
        print(f"VPN '{VPN_NAME}' disconnected successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Error disconnecting from VPN '{VPN_NAME}': {e}")
    except subprocess.TimeoutExpired:
        print(f"Timeout during VPN disconnection for '{VPN_NAME}'.")
    except FileNotFoundError:
        print("`rasdial` command not found.")


async def main():
    if platform.system() == "Windows":
        if not connect_vpn_windows():
            print("Exiting due to VPN connection failure.")
            return
        print("Giving VPN a few seconds to establish connection...")
        await asyncio.sleep(8)
    else:
        print("VPN connection script is designed for Windows. Please ensure VPN is connected manually if not on Windows.")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=False, channel="msedge", slow_mo=700)
        except Exception:
            print("MS Edge not found, launching default Chromium.")
            browser = await p.chromium.launch(headless=False, slow_mo=700)

        context = await browser.new_context(ignore_https_errors=True, accept_downloads=True)
        page = await context.new_page()
        scopus_page = None # Initialize scopus_page

        try:
            # 1. Navigate to Gigalib
            print(f"Navigating to {GIGALIB_IP_URL}")
            await page.goto(GIGALIB_IP_URL, timeout=60000, wait_until="domcontentloaded")
            print(f"Current URL after initial nav: {page.url}")

            if "gigalib.org/getemail.aspx" not in page.url:
                 print("Did not land on getemail.aspx directly. Checking for 'Continue to site' button for gigalib.org.")
                 try:
                    continue_button = page.get_by_role("button", name="Continue to site", exact=False)
                    if await continue_button.is_visible(timeout=7000):
                        print("Found 'Continue to site' button for gigalib.org, clicking...")
                        await continue_button.click()
                        await page.wait_for_url(f"**/{GIGALIB_EMAIL_PAGE_URL_FRAGMENT}", timeout=60000)
                    # else: # No explicit else needed, if not visible, proceed
                 except PlaywrightTimeoutError:
                    print("No 'Continue to site' button found or timed out on gigalib.org warning page.")


            # 3. Gigalib Email Submission
            print(f"Ensuring we are on Gigalib email page (expected fragment: {GIGALIB_EMAIL_PAGE_URL_FRAGMENT})")
            await page.wait_for_url(f"**/{GIGALIB_EMAIL_PAGE_URL_FRAGMENT}", timeout=60000)
            print(f"Current URL: {page.url}")

            print(f"Entering email: {USER_EMAIL}")
            email_input_selector = "#ContentPlaceHolder1_txtEmail"
            await page.locator(email_input_selector).wait_for(state="visible", timeout=30000)
            await page.locator(email_input_selector).fill(USER_EMAIL, timeout=30000)

            print("Clicking submit email button (ثبت ایمیل)")
            submit_button_selector = "#ContentPlaceHolder1_btnaaddemail"
            await page.locator(submit_button_selector).wait_for(state="visible", timeout=30000)
            await page.locator(submit_button_selector).click(timeout=30000)
            await page.wait_for_load_state('domcontentloaded', timeout=60000)
            print(f"Current URL after email submission: {page.url}")

            # 4. Handle Security Warning (Second Instance) for pd.gigalib.org
            # Check if current page content suggests it's a warning page AND we are not on the target pd.gigalib.org URL yet.
            current_content = await page.content()
            if "pd.gigalib.org" not in page.url and "doesn't support a secure connection" in current_content and "Continue to site" in current_content:
                print("On a security warning page for pd.gigalib.org. Clicking 'Continue to site'.")
                try:
                    await page.get_by_role("button", name="Continue to site").click(timeout=10000)
                    await page.wait_for_url(f"**/{GIGALIB_PD_URL_FRAGMENT}", timeout=60000)
                except PlaywrightTimeoutError:
                    print("Timed out trying to click 'Continue to site' for pd.gigalib.org or waiting for next page.")
            
            # 5. Navigate to Scopus via Gigalib Portal
            print(f"Ensuring we are on Gigalib portal page (expected fragment: {GIGALIB_PD_URL_FRAGMENT})")
            await page.wait_for_url(f"**/{GIGALIB_PD_URL_FRAGMENT}", timeout=60000)
            print(f"Current URL: {page.url}")

            print("Clicking 'دسترسی پایگاه های استنادی' (Access to citation databases)")
            citation_db_link_selector = page.get_by_text("دسترسی پایگاه های استنادی", exact=True)
            await citation_db_link_selector.wait_for(state="visible", timeout=30000)
            await citation_db_link_selector.click(timeout=30000)
            await page.wait_for_timeout(3000) # Wait for dynamic content to load/update

            print(f"Attempting to click Scopus link with text: '{GIGALIB_SCOPUS_LINK_TEXT}'")
            # MODIFIED: Scopus link selection using "سرور1"
            # This assumes "سرور1" is the exact visible text of the link/button for Scopus.
            scopus_link_gigalib = page.get_by_text(GIGALIB_SCOPUS_LINK_TEXT, exact=True).first
            
            # It could also be a button if it's not a simple <a> tag
            if not await scopus_link_gigalib.is_visible(timeout=5000):
                print(f"Link with text '{GIGALIB_SCOPUS_LINK_TEXT}' not immediately visible, trying as button.")
                scopus_link_gigalib = page.get_by_role("button", name=GIGALIB_SCOPUS_LINK_TEXT, exact=True).first
            
            await scopus_link_gigalib.wait_for(state="visible", timeout=30000)

            async with page.context.expect_page() as new_scopus_page_info:
                await scopus_link_gigalib.click(timeout=30000)
            scopus_page = await new_scopus_page_info.value
            await scopus_page.bring_to_front()
            await scopus_page.wait_for_load_state('domcontentloaded', timeout=120000)
            print(f"Switched to Scopus page: {scopus_page.url}")

            # 6. Access Advanced Search in Scopus
            print("Accessing Advanced document search in Scopus...")
            advanced_search_link_selector = scopus_page.get_by_role("link", name="Advanced document search")
            try:
                await advanced_search_link_selector.wait_for(state="visible", timeout=60000)
                await advanced_search_link_selector.click(timeout=30000)
            except PlaywrightTimeoutError:
                print("Advanced document search link not immediately found. Checking if already on advanced search page.")
                query_input_div_selector_check = "div#searchfield[contenteditable='true']"
                if not await scopus_page.locator(query_input_div_selector_check).is_visible(timeout=5000):
                    try:
                        print("Attempting to click 'Documents' tab then 'Advanced document search' again.")
                        await scopus_page.get_by_role("tab", name="Documents").click(timeout=10000)
                        await advanced_search_link_selector.click(timeout=30000)
                    except Exception as e_fallback:
                        print(f"Could not navigate to Advanced Search via fallback: {e_fallback}. Will try to proceed.")
            
            await scopus_page.wait_for_load_state('domcontentloaded', timeout=60000)

            # 7. Execute Advanced Search Query
            # The selector div#searchfield[contenteditable='true'] is correct based on provided HTML.
            print(f"Entering Scopus advanced query: {SCOPUS_ADVANCED_SEARCH_QUERY}")
            query_input_div_selector = "div#searchfield"
            await scopus_page.locator(query_input_div_selector).wait_for(state="visible", timeout=30000)
            await scopus_page.locator(query_input_div_selector).fill(SCOPUS_ADVANCED_SEARCH_QUERY, timeout=30000)

            print("Clicking Scopus search button (button#advSearch)...")
            search_button_selector_scopus = "button#advSearch"
            await scopus_page.locator(search_button_selector_scopus).wait_for(state="visible", timeout=30000)
            await scopus_page.locator(search_button_selector_scopus).click(timeout=60000)
            await scopus_page.wait_for_load_state('domcontentloaded', timeout=150000) # Increased timeout


            # 8. Initiate Export of Search Results
            print("Search complete. Initiating export...")
            export_button_main_selector = 'button[data-testid="export-results-button"], button#export_results, button[aria-label*="Export"], button:has-text("Export")'
            export_button_element = scopus_page.locator(export_button_main_selector).first
            await export_button_element.wait_for(state="visible", timeout=60000)
            await export_button_element.click(timeout=30000)

            print("Selecting CSV format for export...")
            csv_option_selector = scopus_page.get_by_role("button", name="CSV").or_(scopus_page.get_by_text("CSV", exact=True))
            await csv_option_selector.wait_for(state="visible", timeout=30000)
            await csv_option_selector.click(timeout=30000)
            await scopus_page.wait_for_timeout(4000)


            # 9. Configure CSV Export Options
            print("Configuring CSV export options from new HTML structure...")
            export_dialog_selector = 'section[role="document"]' 
            dialog_element = scopus_page.locator(export_dialog_selector).last
            await dialog_element.wait_for(state="visible", timeout=30000)
            
            print("Selecting document range radio button...")
            range_radio_selector = 'input#select-range[data-testid="radio-button-input"]'
            await dialog_element.locator(range_radio_selector).check(timeout=10000)
            print("Checked 'Documents' range radio.")

            from_input_selector = 'input[data-testid="input-range-from"]'
            to_input_selector = 'input[data-testid="input-range-to"]'

            # TODO Magic number
            max_docs_str = "1064" 
            try:
                dialog_title_text = await dialog_element.locator('h1.Typography-module__mZVLC').inner_text(timeout=5000)
                match = re.search(r'Export (\d+) documents', dialog_title_text)
                if match:
                    max_docs_str = match.group(1)
                    print(f"Dynamically found max documents from title: {max_docs_str}")
                else: 
                    max_attr = await dialog_element.locator(to_input_selector).get_attribute("max", timeout=2000)
                    if max_attr and max_attr.isdigit():
                        max_docs_str = max_attr
                        print(f"Dynamically found max documents from 'to' input 'max' attribute: {max_docs_str}")
                    else: # Fallback to parsing "You can export up to X documents" text
                        up_to_text_element = dialog_element.locator('text=/You can export up to .* documents/i').first
                        if await up_to_text_element.is_visible(timeout=3000):
                            full_text = await up_to_text_element.inner_text()
                            numbers_in_text = ''.join(filter(str.isdigit, full_text.split("up to")[-1].split("documents")[0]))
                            if numbers_in_text:
                                max_docs_str = numbers_in_text
                                print(f"Dynamically found max documents from 'up to' text: {max_docs_str}")
                        else:
                             print(f"Max documents text/attr not found, using default {max_docs_str}.")
            except Exception as e_max_docs:
                print(f"Could not parse max docs, using default {max_docs_str}. Error: {e_max_docs}")

            await dialog_element.locator(from_input_selector).fill("1", timeout=10000)
            await dialog_element.locator(to_input_selector).fill(max_docs_str, timeout=10000)
            print(f"Set document range from 1 to {max_docs_str}.")

            categories_to_select = [
                "Citation information", "Bibliographical information", "Abstract & keywords",
                "Funding details", "Other information"
            ]
            print("Selecting all information categories by their main checkboxes...")
            for category_text in categories_to_select:
                category_checkbox_selector = dialog_element.locator(
                    f'label:has(span.Checkbox-module__SaBg7 > span:text-is("{category_text}")) input[type="checkbox"]'
                ).first
                try:
                    await category_checkbox_selector.wait_for(state="visible", timeout=5000)
                    if not await category_checkbox_selector.is_checked(timeout=1000):
                        await category_checkbox_selector.check(timeout=10000)
                        print(f"Checked category: {category_text}")
                    else:
                        print(f"Category '{category_text}' already selected.")
                except Exception as e_cat:
                    print(f"Could not select category '{category_text}' with specific selector: {e_cat}. Trying broader text match.")
                    broad_cat_checkbox = dialog_element.locator(f'label:has-text("{category_text}")').locator('input[type="checkbox"]').first
                    try:
                        await broad_cat_checkbox.wait_for(state="visible", timeout=3000)
                        if not await broad_cat_checkbox.is_checked(timeout=1000):
                            await broad_cat_checkbox.check(timeout=5000)
                            print(f"Checked category (broad): {category_text}")
                        else:
                            print(f"Category '{category_text}' already selected (broad).")
                    except Exception as e_broad_cat:
                         print(f"Failed to select category '{category_text}' even with broad selector: {e_broad_cat}")

            # 10. Start the actual export and download
            print("Starting export to CSV file...")
            final_export_button_selector = 'button[data-testid="submit-export-button"]'
            await dialog_element.locator(final_export_button_selector).wait_for(state="visible", timeout=30000)
            
            async with scopus_page.expect_download(timeout=300000) as download_info:
                await dialog_element.locator(final_export_button_selector).click(timeout=30000)

            download = await download_info.value
            await download.save_as(DOWNLOAD_PATH)
            file_size = await asyncio.to_thread(lambda: __import__('os').path.getsize(DOWNLOAD_PATH))
            print(f"Download complete. File saved as: {DOWNLOAD_PATH} (Size: {file_size} bytes)")

            print("Script finished successfully!")

        except PlaywrightTimeoutError as e:
            print(f"A Playwright timeout occurred: {e}")
            current_url = "N/A"; screenshot_path = "playwright_timeout_error.png"
            active_page = scopus_page if scopus_page and not scopus_page.is_closed() else page
            if active_page and not active_page.is_closed():
                current_url = active_page.url
                await active_page.screenshot(path=screenshot_path)
            print(f"Screenshot saved to {screenshot_path}. Current URL: {current_url}")

        except Exception as e:
            print(f"An error occurred: {e}")
            current_url = "N/A"; screenshot_path = "playwright_general_error.png"
            active_page = scopus_page if scopus_page and not scopus_page.is_closed() else page
            if active_page and not active_page.is_closed():
                current_url = active_page.url
                await active_page.screenshot(path=screenshot_path)
            print(f"Screenshot saved to {screenshot_path}. Current URL: {current_url}")
        finally:
            if 'browser' in locals() and browser:
                await browser.close()
            if platform.system() == "Windows":
                disconnect_vpn_windows()

if __name__ == "__main__":
    asyncio.run(main())