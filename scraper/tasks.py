# scraper/tasks.py
import asyncio
import os
import platform
import re
import subprocess

from asgiref.sync import async_to_sync  # For calling sync DB from async
from asgiref.sync import sync_to_async
from celery import group, shared_task
from celery.utils.log import get_task_logger
from django.utils import timezone
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from .models import ISSNQuery  # Assuming models.py is in the same app
from .models import ScrapeBatch

logger = get_task_logger(__name__)

# --- VPN Configuration (Placeholders/Constants) ---
VPN_NAME = "Behdani" # Used by rasdial
VPN_SERVER = "vpn.birjand.ac.ir"
VPN_USERNAME = "4011312071"
VPN_PASSWORD = "0640663427"
# --- End VPN Configuration ---

# --- Gigalib and Scopus Details (Keep these configurable if possible) ---
GIGALIB_IP_URL = "http://gigalib.org/ip/"
GIGALIB_EMAIL_PAGE_URL_FRAGMENT = "gigalib.org/getemail.aspx"
GIGALIB_PD_URL_FRAGMENT = "pd.gigalib.org/search-basic.aspx"
USER_EMAIL = "alibehdani.elt@gmail.com"
GIGALIB_SCOPUS_LINK_TEXT = "سرور1"
# --- End Details ---

# Helper to run sync DB operations from async code
@sync_to_async
def update_issn_query_status(issn_query_id, status, **kwargs):
    try:
        query = ISSNQuery.objects.get(id=issn_query_id)
        query.status = status
        for key, value in kwargs.items():
            if hasattr(query, key):
                setattr(query, key, value)
        if status in [ISSNQuery.Status.PROCESSING]:
            query.processing_started_at = timezone.now()
        if status not in [ISSNQuery.Status.PENDING, ISSNQuery.Status.PROCESSING]:
            query.processing_finished_at = timezone.now()
        query.save()
        logger.info(f"ISSNQuery {issn_query_id} status updated to {status} with kwargs: {kwargs}")
    except ISSNQuery.DoesNotExist:
        logger.error(f"ISSNQuery {issn_query_id} not found for status update.")
    except Exception as e:
        logger.error(f"Error updating ISSNQuery {issn_query_id}: {e}")


@sync_to_async
def get_issn_value(issn_query_id):
    try:
        return ISSNQuery.objects.get(id=issn_query_id).issn
    except ISSNQuery.DoesNotExist:
        logger.error(f"ISSNQuery {issn_query_id} not found when getting ISSN value.")
        return None

def connect_vpn_windows_task():
    logger.info(f"Attempting to connect to VPN: {VPN_NAME}...")
    # This is Windows-specific and problematic in Docker on Linux hosts
    if platform.system() != "Windows":
        logger.warning("VPN connection via rasdial is Windows-specific. This will likely fail on non-Windows workers or in typical Docker setups.")
        # In a real Dockerized Linux worker, you'd use a Linux PPTP client here.
        # For now, we'll let it try and fail or succeed if somehow on Windows.
        # return False # Or raise an exception if VPN is critical and platform is wrong

    try:
        subprocess.run(['rasdial', VPN_NAME, '/disconnect'], check=False, timeout=30, capture_output=True)
        logger.info(f"Attempted disconnect for any existing session for {VPN_NAME}.")
    except Exception as e:
        logger.warning(f"Error during pre-disconnect for {VPN_NAME}: {e}")

    try:
        process = subprocess.run(
            ['rasdial', VPN_NAME, VPN_USERNAME, VPN_PASSWORD],
            check=True, capture_output=True, text=True, timeout=60
        )
        if "Command completed successfully" in process.stdout:
            logger.info(f"VPN '{VPN_NAME}' command executed successfully.")
            # Add a small delay for the connection to establish fully
            asyncio.sleep(8) # This sleep needs to be handled by the async caller
            return True
        else:
            logger.error(f"Failed to execute VPN connect command for '{VPN_NAME}'. Stdout: {process.stdout}, Stderr: {process.stderr}")
            return False
    except subprocess.CalledProcessError as e:
        logger.error(f"CalledProcessError connecting to VPN '{VPN_NAME}': {e}\nStdout: {e.stdout}\nStderr: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout during VPN connection attempt for '{VPN_NAME}'.")
        return False
    except FileNotFoundError:
        logger.error("`rasdial` command not found. This VPN connection method is for Windows only.")
        return False
    except Exception as e:
        logger.error(f"Generic exception connecting to VPN: {e}")
        return False

def disconnect_vpn_windows_task():
    logger.info(f"Attempting to disconnect from VPN: {VPN_NAME}...")
    if platform.system() != "Windows":
        logger.warning("VPN disconnection via rasdial is Windows-specific.")
        return
    try:
        subprocess.run(['rasdial', VPN_NAME, '/disconnect'], check=True, timeout=30, capture_output=True)
        logger.info(f"VPN '{VPN_NAME}' disconnected successfully.")
    except Exception as e: # Catch broad exceptions for disconnection
        logger.error(f"Error disconnecting from VPN '{VPN_NAME}': {e}")


VPN_PEER_NAME = "Behdani" # Matches the name of your file in /etc/ppp/peers/

def connect_vpn_linux_task():
    logger.info(f"Attempting to connect to VPN peer: {VPN_PEER_NAME} using pon...")
    try:
        # Ensure no existing connection for this peer
        subprocess.run(['poff', VPN_PEER_NAME], check=False, timeout=15, capture_output=True)
        logger.info(f"Attempted 'poff {VPN_PEER_NAME}' before connecting.")
    except Exception as e:
        logger.warning(f"Error during pre-emptive 'poff {VPN_PEER_NAME}': {e}")

    try:
        # The 'pon' command usually daemonizes.
        # We need to check if the interface (e.g., ppp0) comes up.
        process = subprocess.run(
            ['pon', VPN_PEER_NAME],
            check=True, capture_output=True, text=True, timeout=30
        )
        logger.info(f"'pon {VPN_PEER_NAME}' command executed. Stdout: {process.stdout}, Stderr: {process.stderr}")
        
        # Wait a bit and check for ppp0 interface or route
        time.sleep(10) # Give time for connection to establish

        # Check for ppp0 interface (this is a basic check)
        check_ip = subprocess.run(['ip', 'addr', 'show', 'ppp0'], capture_output=True, text=True)
        if 'ppp0' in check_ip.stdout and 'inet' in check_ip.stdout:
            logger.info(f"VPN '{VPN_PEER_NAME}' seems connected (ppp0 interface found).")
            # You might also check for a default route via ppp0
            check_route = subprocess.run(['ip', 'route', 'show', 'default'], capture_output=True, text=True)
            if 'ppp0' in check_route.stdout:
                logger.info("Default route via ppp0 confirmed.")
                return True
            else:
                logger.warning("ppp0 interface up, but not the default route. Check VPN config.")
                # Depending on needs, this might still be acceptable or an error.
                return True # Or False if default route is critical
        else:
            logger.error(f"Failed to confirm VPN '{VPN_PEER_NAME}' connection (ppp0 not found or no IP).")
            # Try to get logs if connection failed
            try:
                log_process = subprocess.run(['tail', '-n', '20', '/var/log/syslog'], capture_output=True, text=True) # or /var/log/daemon.log
                logger.info(f"Last ppp logs (syslog/daemon.log might show more):\n{log_process.stdout}")
            except Exception as log_e:
                logger.warning(f"Could not fetch ppp logs: {log_e}")
            return False

    except subprocess.CalledProcessError as e:
        logger.error(f"CalledProcessError connecting to VPN '{VPN_PEER_NAME}' with pon: {e}\nStdout: {e.stdout}\nStderr: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout during VPN connection attempt for '{VPN_PEER_NAME}' with pon.")
        return False
    except FileNotFoundError:
        logger.error("`pon` command not found. Ensure pptp-linux is installed and configured in the container.")
        return False
    except Exception as e:
        logger.error(f"Generic exception connecting to VPN with pon: {e}")
        return False


def disconnect_vpn_linux_task():
    logger.info(f"Attempting to disconnect from VPN peer: {VPN_PEER_NAME} using poff...")
    try:
        subprocess.run(['poff', VPN_PEER_NAME], check=True, timeout=15, capture_output=True)
        logger.info(f"VPN '{VPN_PEER_NAME}' disconnected successfully via poff.")
    except Exception as e:
        logger.error(f"Error disconnecting from VPN '{VPN_PEER_NAME}' with poff: {e}")


async def run_playwright_scraper(issn_query_id, current_issn):
    # This is the core of your Playwright script, adapted
    # DOWNLOAD_PATH needs to be unique per task or managed carefully
    # For Celery, it's better to return data or paths rather than relying on fixed DOWNLOAD_PATH
    
    # Ensure the /scraped_data/batch_X/issn_Y directory exists
    # This path should be relative to a shared volume if workers are separate from web server
    # For now, let's assume a local path within the Django project media root or similar.
    query_obj = await sync_to_async(ISSNQuery.objects.get)(id=issn_query_id)
    batch_id_str = str(query_obj.batch.batch_id)
    
    # Define a unique download path for this task
    # MEDIA_ROOT is typically defined in settings.py
    from django.conf import settings

    # Ensure this path is writable by the Celery worker
    # e.g. os.path.join(settings.MEDIA_ROOT, 'scraped_data', batch_id_str, current_issn)
    # If MEDIA_ROOT isn't set or you prefer a different structure:
    base_download_dir = os.path.join(settings.BASE_DIR, 'media', 'scraped_data', batch_id_str, current_issn)
    os.makedirs(base_download_dir, exist_ok=True)
    task_download_path = os.path.join(base_download_dir, f"scopus_export_{current_issn}.csv")

    scopus_advanced_search_query = f'ISSN ( {current_issn.replace("-", "")} ) AND ( LIMIT-TO ( DOCTYPE , "ar" ) ) AND ( LIMIT-TO ( LANGUAGE , "English" ) ) AND ( LIMIT-TO ( SRCTYPE , "j" ) )'
    
    playwright_instance = None
    browser = None
    context = None
    main_page = None # Renamed from 'page' to avoid confusion with Scopus page
    scopus_page_instance = None # Renamed from 'scopus_page'

    try:
        playwright_instance = await async_playwright().start()
        # When running in Docker, you might not have Edge. Chromium is more standard.
        # Also, ensure browsers are installed in the Docker image: playwright install chromium
        try:
            browser = await playwright_instance.chromium.launch(headless=True, slow_mo=200) # RUN HEADLESS IN PRODUCTION/CELERY
        except Exception as e_browser:
            logger.error(f"Failed to launch Edge, trying Chromium for ISSN {current_issn}: {e_browser}")
            # Ensure chromium is installed in your environment/container
            # You might need `playwright install chromium`
            browser = await playwright_instance.chromium.launch(headless=True, slow_mo=200) # RUN HEADLESS

        context = await browser.new_context(
            ignore_https_errors=True,
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 Edg/91.0.864.59" # Example user agent
        )
        main_page = await context.new_page()

        logger.info(f"Navigating to {GIGALIB_IP_URL} for ISSN {current_issn}")
        await main_page.goto(GIGALIB_IP_URL, timeout=60000, wait_until="domcontentloaded")

        if "gigalib.org/getemail.aspx" not in main_page.url:
            logger.info(f"Not on getemail.aspx for ISSN {current_issn}. Checking for 'Continue to site'.")
            try:
                continue_button = main_page.get_by_role("button", name="Continue to site", exact=False)
                if await continue_button.is_visible(timeout=7000):
                    await continue_button.click()
                    await main_page.wait_for_url(f"**/{GIGALIB_EMAIL_PAGE_URL_FRAGMENT}", timeout=60000)
            except PlaywrightTimeoutError:
                logger.warning(f"No 'Continue to site' button for gigalib.org for ISSN {current_issn}.")
                await update_issn_query_status(issn_query_id, ISSNQuery.Status.GIGALIB_ERROR, error_message="Gigalib initial navigation failed (continue button).")
                return {"status": "GIGALIB_ERROR", "message": "Gigalib initial navigation failed (continue button)."}


        await main_page.wait_for_url(f"**/{GIGALIB_EMAIL_PAGE_URL_FRAGMENT}", timeout=60000)
        logger.info(f"Entering email for ISSN {current_issn}")
        await main_page.locator("#ContentPlaceHolder1_txtEmail").fill(USER_EMAIL, timeout=30000)
        await main_page.locator("#ContentPlaceHolder1_btnaaddemail").click(timeout=30000)
        await main_page.wait_for_load_state('domcontentloaded', timeout=60000)
        
        current_content = await main_page.content()
        if "pd.gigalib.org" not in main_page.url and "doesn't support a secure connection" in current_content and "Continue to site" in current_content:
            logger.info(f"On security warning for pd.gigalib.org for ISSN {current_issn}. Clicking 'Continue'.")
            try:
                await main_page.get_by_role("button", name="Continue to site").click(timeout=10000)
                await main_page.wait_for_url(f"**/{GIGALIB_PD_URL_FRAGMENT}", timeout=60000)
            except PlaywrightTimeoutError:
                logger.warning(f"Timeout clicking 'Continue' for pd.gigalib.org for ISSN {current_issn}.")
                await update_issn_query_status(issn_query_id, ISSNQuery.Status.GIGALIB_ERROR, error_message="Gigalib pd navigation failed (continue button).")
                return {"status": "GIGALIB_ERROR", "message": "Gigalib pd navigation failed (continue button)."}


        await main_page.wait_for_url(f"**/{GIGALIB_PD_URL_FRAGMENT}", timeout=60000)
        logger.info(f"Clicking 'دسترسی پایگاه های استنادی' for ISSN {current_issn}")
        await main_page.get_by_text("دسترسی پایگاه های استنادی", exact=True).click(timeout=30000)
        await main_page.wait_for_timeout(3000)

        logger.info(f"Attempting to click Scopus link '{GIGALIB_SCOPUS_LINK_TEXT}' for ISSN {current_issn}")
        scopus_link_gigalib = main_page.get_by_text(GIGALIB_SCOPUS_LINK_TEXT, exact=True).first
        if not await scopus_link_gigalib.is_visible(timeout=5000):
            scopus_link_gigalib = main_page.get_by_role("button", name=GIGALIB_SCOPUS_LINK_TEXT, exact=True).first
        
        async with main_page.context.expect_page() as new_page_info:
            await scopus_link_gigalib.click(timeout=30000)
        scopus_page_instance = await new_page_info.value
        await scopus_page_instance.bring_to_front()
        await scopus_page_instance.wait_for_load_state('domcontentloaded', timeout=120000)
        logger.info(f"Switched to Scopus page for ISSN {current_issn}: {scopus_page_instance.url}")

        if "scopus.com" not in scopus_page_instance.url: # Basic check
            logger.error(f"Failed to reach Scopus for ISSN {current_issn}. Current URL: {scopus_page_instance.url}")
            await update_issn_query_status(issn_query_id, ISSNQuery.Status.SCOPUS_LOGIN_ERROR, error_message=f"Did not land on Scopus. URL: {scopus_page_instance.url}")
            return {"status": "SCOPUS_LOGIN_ERROR", "message": f"Did not land on Scopus. URL: {scopus_page_instance.url}"}

        logger.info(f"Accessing Advanced document search in Scopus for ISSN {current_issn}")
        adv_search_link = scopus_page_instance.get_by_role("link", name="Advanced document search")
        query_input_visible = await scopus_page_instance.locator("div#searchfield[contenteditable='true']").is_visible(timeout=5000)

        if not query_input_visible: # If query input not there, try clicking advanced search
            try:
                await adv_search_link.click(timeout=30000)
                await scopus_page_instance.wait_for_load_state('domcontentloaded', timeout=60000)
            except PlaywrightTimeoutError:
                logger.warning(f"Advanced search link click timed out for ISSN {current_issn}, assuming already on page or page structure changed.")
        
        logger.info(f"Entering Scopus advanced query for ISSN {current_issn}")
        query_input_div = scopus_page_instance.locator("div#searchfield[contenteditable='true']")
        await query_input_div.wait_for(state="visible", timeout=30000)
        await query_input_div.click()
        await query_input_div.fill(scopus_advanced_search_query, timeout=30000)

        logger.info(f"Clicking Scopus search button for ISSN {current_issn}")
        await scopus_page_instance.locator("button#advSearch").click(timeout=60000)
        await scopus_page_instance.wait_for_load_state('domcontentloaded', timeout=150000)

        # Check for "No documents found"
        # This selector might need adjustment based on Scopus UI
        no_results_locator = scopus_page_instance.locator('text=/No documents were found/i, text=/No results found/i, [data-testid="no-results-message"]')
        if await no_results_locator.is_visible(timeout=5000):
            logger.info(f"No documents found for ISSN {current_issn}.")
            await update_issn_query_status(issn_query_id, ISSNQuery.Status.NO_RESULTS, article_count=0)
            return {"status": "NO_RESULTS", "issn": current_issn, "article_count": 0}
        
        logger.info(f"Search complete for ISSN {current_issn}. Initiating export.")
        export_button = scopus_page_instance.locator('button[data-testid="export-results-button"], button#export_results').first
        await export_button.click(timeout=30000)

        await scopus_page_instance.get_by_role("button", name="CSV").or_(scopus_page_instance.get_by_text("CSV", exact=True)).click(timeout=30000)
        await scopus_page_instance.wait_for_timeout(4000)

        export_dialog = scopus_page_instance.locator('section[role="document"]').last
        await export_dialog.wait_for(state="visible", timeout=30000)
        
        await export_dialog.locator('input#select-range[data-testid="radio-button-input"]').check(timeout=10000)
        
        max_docs_str = "2000" # Default Scopus limit per export often around this, adjust if needed
        try:
            dialog_title_text = await export_dialog.locator('h1').inner_text(timeout=5000) # More generic h1
            match = re.search(r'Export (\d+) documents', dialog_title_text)
            if match: max_docs_str = match.group(1)
            # Scopus may limit export to 2000 or 20000 per go regardless of results.
            # The "You can export up to X documents" is also key.
            up_to_text_element = export_dialog.locator('text=/You can export up to ([\d,]+) documents/i').first
            if await up_to_text_element.is_visible(timeout=3000):
                full_text = await up_to_text_element.inner_text()
                numbers_match = re.search(r'([\d,]+)', full_text.split("up to")[-1])
                if numbers_match:
                    max_docs_str = numbers_match.group(1).replace(',', '')
                    logger.info(f"Max docs from 'up to' text for {current_issn}: {max_docs_str}")

        except Exception as e_max: logger.warning(f"Could not parse max_docs for {current_issn}: {e_max}")

        # Safety: Don't try to export more than a reasonable limit (e.g. Scopus UI might allow 20k but time out)
        # This might need to be the actual number of results if less than Scopus's own hard limit.
        # The video showed 1064. Let's stick to a limit or parse actual found docs.
        # For now, using the parsed max_docs_str which should reflect search results up to UI limit.

        await export_dialog.locator('input[data-testid="input-range-from"]').fill("1", timeout=10000)
        await export_dialog.locator('input[data-testid="input-range-to"]').fill(max_docs_str, timeout=10000)

        categories = ["Citation information", "Bibliographical information", "Abstract & keywords", "Funding details", "Other information"]
        for cat_text in categories:
            cb_selector = export_dialog.locator(f'label:has(span:text-is("{cat_text}")) input[type="checkbox"]').first
            if await cb_selector.is_visible(timeout=3000) and not await cb_selector.is_checked():
                await cb_selector.check(timeout=5000)
        
        logger.info(f"Starting CSV download for ISSN {current_issn} to {task_download_path}")
        async with scopus_page_instance.expect_download(timeout=300000) as download_info: # 5 min
            await export_dialog.locator('button[data-testid="submit-export-button"]').click(timeout=30000)
        
        download = await download_info.value
        await download.save_as(task_download_path)
        logger.info(f"Download complete for ISSN {current_issn}: {task_download_path}")

        # Count articles (rows in CSV, minus header)
        article_count = 0
        try:
            with open(task_download_path, 'r', encoding='utf-8') as f:
                article_count = sum(1 for row in f) -1 # Subtract header
            if article_count < 0: article_count = 0
        except Exception as e_count:
            logger.warning(f"Could not count articles in CSV for {current_issn}: {e_count}")


        await update_issn_query_status(issn_query_id, ISSNQuery.Status.COMPLETED, 
                                       result_csv_path=task_download_path, 
                                       article_count=article_count)
        return {"status": "COMPLETED", "issn": current_issn, "path": task_download_path, "article_count": article_count}

    except PlaywrightTimeoutError as e:
        logger.error(f"PlaywrightTimeoutError for ISSN {current_issn} on page {getattr(scopus_page_instance or main_page, 'url', 'N/A')}: {str(e)[:500]}")
        error_page_url = getattr(scopus_page_instance or main_page, 'url', 'N/A')
        await update_issn_query_status(issn_query_id, ISSNQuery.Status.FAILED, error_message=f"Timeout on {error_page_url}: {str(e)[:250]}")
        # Try to take a screenshot
        # screenshot_path = os.path.join(settings.MEDIA_ROOT, 'error_screenshots', f"{current_issn}_{timezone.now().strftime('%Y%m%d%H%M%S')}.png")
        # os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
        # if scopus_page_instance and not scopus_page_instance.is_closed(): await scopus_page_instance.screenshot(path=screenshot_path)
        # elif main_page and not main_page.is_closed(): await main_page.screenshot(path=screenshot_path)
        return {"status": "FAILED", "issn": current_issn, "error": f"Timeout: {str(e)[:250]}"}
    except Exception as e:
        logger.error(f"General scraping error for ISSN {current_issn}: {e}", exc_info=True)
        await update_issn_query_status(issn_query_id, ISSNQuery.Status.FAILED, error_message=f"General error: {str(e)[:250]}")
        return {"status": "FAILED", "issn": current_issn, "error": f"General error: {str(e)[:250]}"}
    finally:
        if browser: await browser.close()
        if playwright_instance: await playwright_instance.stop()


@shared_task(bind=True, max_retries=1, default_retry_delay=60) # Add retry for transient issues
def process_single_issn_task(self, issn_query_id):
    logger.info(f"Starting Celery task for ISSNQuery ID: {issn_query_id}")
    
    # Mark as processing
    # Need to run sync DB update in a way Celery task can call
    async_to_sync(update_issn_query_status)(issn_query_id, ISSNQuery.Status.PROCESSING)
    
    current_issn = async_to_sync(get_issn_value)(issn_query_id)
    if not current_issn:
        logger.error(f"Could not retrieve ISSN for ISSNQuery ID: {issn_query_id}. Aborting task.")
        async_to_sync(update_issn_query_status)(issn_query_id, ISSNQuery.Status.FAILED, error_message="Could not retrieve ISSN value.")
        return {"status": "FAILED", "error": "ISSN value not found"}

    vpn_connected = False
    # For Docker/Linux worker:
    if platform.system().lower() == "linux": # Check if running on Linux (like in Docker)
        vpn_connected = connect_vpn_linux_task()
        if not vpn_connected:
            logger.error(f"Linux VPN connection failed for ISSN {current_issn}.")
            async_to_sync(update_issn_query_status)(issn_query_id, ISSNQuery.Status.VPN_ERROR)
            return {"status": "VPN_ERROR", "issn": current_issn}
    elif platform.system() == "Windows": # Fallback for local Windows dev if needed
            logger.warning("Running Windows VPN logic. This is for local dev only.")
            vpn_connected = connect_vpn_windows_task() # Your existing rasdial logic
            if not vpn_connected:
                logger.error(f"Linux VPN connection failed for ISSN {current_issn}.")
                async_to_sync(update_issn_query_status)(issn_query_id, ISSNQuery.Status.VPN_ERROR)
                return {"status": "VPN_ERROR", "issn": current_issn}
    else:
        logger.warning(f"Unsupported platform for automated VPN: {platform.system()}")
        # Decide behavior: fail or proceed without VPN
        # For this example, let's assume VPN is critical
        async_to_sync(update_issn_query_status)(issn_query_id, ISSNQuery.Status.VPN_ERROR, error_message="Unsupported platform for VPN.")
        return {"status": "VPN_ERROR", "issn": current_issn, "error": "Unsupported platform for VPN"}


    result = None
    if vpn_connected:
        try:
            result = asyncio.run(run_playwright_scraper(issn_query_id, current_issn))
            logger.info(f"Playwright scraper result for ISSN {current_issn}: {result}")
        except Exception as e:
            logger.error(f"Exception running playwright_scraper for {current_issn}: {e}")
            async_to_sync(update_issn_query_status)(issn_query_id, ISSNQuery.Status.FAILED, error_message=f"Async run error: {str(e)[:200]}")
            result = {"status": "FAILED", "issn": current_issn, "error": f"Async run error: {str(e)[:200]}"}
        finally:
            if platform.system() == "Windows" and vpn_connected: # Ensure VPN was actually connected by this worker
                disconnect_vpn_windows_task() # This will run synchronously
    else: # VPN not connected path (already handled above, but as a fallback)
        logger.error(f"VPN was not connected, aborting scrape for ISSN {current_issn}.")
        async_to_sync(update_issn_query_status)(issn_query_id, ISSNQuery.Status.VPN_ERROR)
        result = {"status": "VPN_ERROR", "issn": current_issn}

    return result

@shared_task
def process_batch_task(batch_id):
    try:
        batch = ScrapeBatch.objects.get(id=batch_id)
        batch.status = ScrapeBatch.Status.PROCESSING
        batch.save(update_fields=['status'])

        issn_query_ids = list(batch.issn_queries.filter(status=ISSNQuery.Status.PENDING).values_list('id', flat=True))
        
        if not issn_query_ids:
            logger.info(f"No pending ISSNs to process for batch {batch_id}.")
            batch.status = ScrapeBatch.Status.COMPLETED # Or based on actual query statuses
            batch.save(update_fields=['status'])
            return f"No pending ISSNs for batch {batch_id}."

        # For sequential processing by one worker (simpler for VPN on Windows host)
        # You can use Celery's group for parallel execution if VPN can handle it or is containerized per worker
        # task_group = group(process_single_issn_task.s(query_id) for query_id in issn_query_ids)
        # group_result = task_group.apply_async()
        # batch.celery_group_id = group_result.id
        # batch.save(update_fields=['celery_group_id'])
        # logger.info(f"Dispatched group of tasks for batch {batch_id}, group ID: {group_result.id}")
        # return f"Processing batch {batch_id} with group ID {group_result.id}"
        
        # --- Sequential processing (safer for shared `rasdial` VPN) ---
        logger.info(f"Starting sequential processing for batch {batch_id} with {len(issn_query_ids)} ISSNs.")
        for query_id in issn_query_ids:
            logger.info(f"Dispatching task for ISSNQuery ID {query_id} in batch {batch_id} sequentially.")
            # .apply_async is good for more control, .delay is simpler
            process_single_issn_task.apply_async(args=[query_id]) 
            # Potentially add a delay between tasks if needed to be gentle on VPN/resources
            # import time
            # time.sleep(10) # 10 second delay

        # Batch status will be updated based on individual task completion or a callback.
        # For now, it remains PROCESSING. A separate monitoring task could check query statuses.
        return f"Sequentially dispatched tasks for batch {batch_id}."

    except ScrapeBatch.DoesNotExist:
        logger.error(f"ScrapeBatch with ID {batch_id} not found.")
        return f"Batch {batch_id} not found."
    except Exception as e:
        logger.error(f"Error processing batch {batch_id}: {e}", exc_info=True)
        try: # Try to mark batch as failed
            batch = ScrapeBatch.objects.get(id=batch_id)
            batch.status = ScrapeBatch.Status.FAILED
            batch.notes = f"Batch processing error: {e}"
            batch.save()
        except: pass # Ignore if batch itself cannot be saved
        return f"Failed to process batch {batch_id}: {e}"