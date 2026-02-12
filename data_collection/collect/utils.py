import hashlib
import json
import logging
import os
import random
import re
import requests
import string
import time

from bs4 import BeautifulSoup
from ghapi.core import GhApi
from fastcore.net import HTTP404NotFoundError, HTTP403ForbiddenError
from http.client import IncompleteRead, RemoteDisconnected
from typing import Optional
from tqdm import tqdm
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
import csv
from io import StringIO
from datetime import datetime
logger = logging.getLogger(__name__)

from pygments.lexers import get_lexer_for_filename
from pygments.util import ClassNotFound
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PR_KEYWORDS = {
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
}

def get_language_with_pygments(filename):
    try:
        lexer = get_lexer_for_filename(filename)
        return lexer.name.lower()
    except ClassNotFound:
        return "Unknown"


class ProxyRotator:
    """
    Manages proxy rotation based on request count.

    GitHub anonymous rate limit: 60 requests/hour per IP.
    Strategy: Rotate to a new Session ID (new IP) every N requests.

    Required environment variables:
        VORTEX_PROXY_HOST          - Proxy host address
        VORTEX_PROXY_PASSWORD      - Proxy password
        VORTEX_PROXY_HTTP_PORT     - HTTP port (default: 8080)
        VORTEX_PROXY_HTTPS_PORT    - HTTPS port (default: 18080)
        VORTEX_PROXY_COUNTRY       - Country code (default: us)
        VORTEX_PROXY_USE_SESSION   - Use session-based sticky IP (default: true)
        VORTEX_PROXY_MAX_REQUESTS_PER_IP - Max requests before rotating (default: 50)
    """

    def __init__(self, repo_full_name: str):
        self.repo_full_name = repo_full_name
        self.request_count = 0
        self.max_requests_per_ip = int(os.getenv("VORTEX_PROXY_MAX_REQUESTS_PER_IP", "50"))
        self.current_session_id = None
        self.rotation_count = 0

        self.host = os.getenv("VORTEX_PROXY_HOST")
        self.password = os.getenv("VORTEX_PROXY_PASSWORD")
        self.http_port = os.getenv("VORTEX_PROXY_HTTP_PORT", "8080")
        self.https_port = os.getenv("VORTEX_PROXY_HTTPS_PORT", "18080")
        self.country = os.getenv("VORTEX_PROXY_COUNTRY", "us")
        self.use_session = os.getenv("VORTEX_PROXY_USE_SESSION", "true").lower() == "true"

        self.enabled = bool(self.host and self.password)

        if self.enabled:
            logger.info(
                f"[{self.repo_full_name}] Proxy rotator initialized: "
                f"max_requests_per_ip={self.max_requests_per_ip}, "
                f"use_session={self.use_session}, country={self.country}"
            )

    def _generate_new_session_id(self) -> str:
        base_hash = int(hashlib.md5(self.repo_full_name.encode()).hexdigest(), 16)
        if not hasattr(self, '_random_offset'):
            self._random_offset = int(time.time()) % 1000000
        combined = (base_hash + self.rotation_count + self._random_offset) % 100000000
        return str(combined).zfill(8)

    def get_proxies(self) -> dict[str, str] | None:
        """Get current proxy config, rotating IP if request count exceeds threshold."""
        if not self.enabled:
            return None

        if self.request_count >= self.max_requests_per_ip:
            self.rotation_count += 1
            self.request_count = 0
            self.current_session_id = None
            logger.info(f"[{self.repo_full_name}] Rotating to new IP (rotation #{self.rotation_count})")

        if self.current_session_id is None:
            self.current_session_id = self._generate_new_session_id()
            logger.info(
                f"[{self.repo_full_name}] New session ID: {self.current_session_id} "
                f"(rotation #{self.rotation_count})"
            )

        if self.use_session:
            username = f"proxy-cot-{self.country}-sid-{self.current_session_id}"
        else:
            username = "proxy"

        http_proxy = f"http://{username}:{self.password}@{self.host}:{self.http_port}"
        return {
            "http": http_proxy,
            "https": http_proxy,
        }

    def increment_request_count(self):
        self.request_count += 1
        if self.request_count % 10 == 0:
            progress_pct = (self.request_count / self.max_requests_per_ip) * 100
            logger.info(
                f"[{self.repo_full_name}] Requests on current IP: "
                f"{self.request_count}/{self.max_requests_per_ip} ({progress_pct:.0f}%) "
                f"[Session: {self.current_session_id}]"
            )

    def update_env_proxies(self):
        """Update environment variables so urllib (used by GhApi) picks up the proxy."""
        proxies = self.get_proxies()
        if proxies:
            os.environ['HTTP_PROXY'] = proxies['http']
            os.environ['HTTPS_PROXY'] = proxies['https']
            os.environ['http_proxy'] = proxies['http']
            os.environ['https_proxy'] = proxies['https']
        return proxies


class Repo:
    def __init__(self, owner: str, name: str, token: Optional[str] = None,language: Optional[str] = 'python'):
        """
        Init to retrieve target repository and create ghapi tool

        Args:
            owner (str): owner of target repository
            name (str): name of target repository
            token (str): github token
        """

        self.owner = owner
        self.name = name
        self.token = token
        self.language = language
        self.repo_full_name = f"{owner}/{name}"

        # Initialize proxy rotator
        self.proxy_rotator = ProxyRotator(self.repo_full_name)
        if self.proxy_rotator.enabled:
            self.proxy_rotator.update_env_proxies()

        self.api = GhApi(token=token)
        self.repo = self.call_api(self.api.repos.get, owner=owner, repo=name)
        if self.repo is None:
            raise RuntimeError(
                f"Failed to access repository {self.repo_full_name} after multiple retries. "
                f"Check network connectivity, proxy settings, and API rate limits."
            )
    def github_api(self,url, token, max_retries=5):
        """HTTP request wrapper with proxy rotation and retry logic."""
        retries = 0
        headers = {'Authorization': f'token {token}'} if token else {}

        while retries < max_retries:
            # Rotate proxy before each request
            proxies = None
            if self.proxy_rotator.enabled:
                self.proxy_rotator.increment_request_count()
                proxies = self.proxy_rotator.update_env_proxies()

            try:
                response = requests.get(url, headers=headers, proxies=proxies, timeout=30)
                if response.status_code == 200:
                    return response
                elif response.status_code == 403 and 'X-RateLimit-Remaining' in response.headers:
                    remaining = int(response.headers['X-RateLimit-Remaining'])
                    if remaining == 0:
                        if self.proxy_rotator.enabled:
                            # Force rotate to a new IP instead of sleeping
                            logger.info(f'[{self.repo_full_name}] Rate limited, rotating proxy IP...')
                            self.proxy_rotator.rotation_count += 1
                            self.proxy_rotator.request_count = 0
                            self.proxy_rotator.current_session_id = None
                            self.proxy_rotator.update_env_proxies()
                            retries += 1
                            time.sleep(1)
                        else:
                            reset_time = int(response.headers['X-RateLimit-Reset'])
                            sleep_time = reset_time - int(time.time()) + 1
                            print(f'Rate limit exceeded. Sleeping for {sleep_time} seconds.')
                            time.sleep(sleep_time)
                            retries += 1
                    else:
                        print(f'url:{url} 403 Forbidden: {response.json()}')
                        return response
                elif response.status_code == 503 or response.status_code == 502:
                    # Server error: short backoff + rotate proxy
                    wait_time = min(3 * (retries + 1), 15)
                    logger.warning(
                        f'[{self.repo_full_name}] {response.status_code} error, '
                        f'retrying in {wait_time}s (attempt {retries+1}/{max_retries})'
                    )
                    if self.proxy_rotator.enabled:
                        self.proxy_rotator.rotation_count += 1
                        self.proxy_rotator.request_count = 0
                        self.proxy_rotator.current_session_id = None
                    time.sleep(wait_time)
                    retries += 1
                elif response.status_code == 429:
                    # Secondary rate limit
                    retry_after = int(response.headers.get('Retry-After', 60))
                    if self.proxy_rotator.enabled:
                        logger.info(f'[{self.repo_full_name}] 429 rate limited, rotating proxy...')
                        self.proxy_rotator.rotation_count += 1
                        self.proxy_rotator.request_count = 0
                        self.proxy_rotator.current_session_id = None
                        self.proxy_rotator.update_env_proxies()
                        time.sleep(2)
                    else:
                        logger.info(f'429 Too Many Requests. Sleeping for {retry_after}s')
                        time.sleep(retry_after)
                    retries += 1
                else:
                    print(f'Error: {response.status_code}, {response.text}')
                    retries += 1
                    time.sleep(min(3 * (retries), 15))
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    RemoteDisconnected, IncompleteRead, ConnectionResetError, OSError) as e:
                wait_time = min(3 * (retries + 1), 15)
                logger.warning(
                    f'[{self.repo_full_name}] Network error: {e}, '
                    f'retrying in {wait_time}s (attempt {retries+1}/{max_retries})'
                )
                if self.proxy_rotator.enabled:
                    self.proxy_rotator.rotation_count += 1
                    self.proxy_rotator.request_count = 0
                    self.proxy_rotator.current_session_id = None
                time.sleep(wait_time)
                retries += 1
            except HTTP404NotFoundError as e:
                logger.info(f"[{self.repo_full_name}] Resource not found: {url}")
                return None

        return None
    
    def call_github_api(self, **kwargs) -> dict:
        owner = kwargs['owner']
        repo = kwargs['repo']
        call_type = kwargs['call_type']
        token = kwargs['token']
        results_list =[]
        if call_type == 'get_prs':
            state = 'closed'
            url = f'https://api.github.com/repos/{owner}/{repo}/pulls?state={state}'
        elif call_type == 'get_commits':
            pull_idx = kwargs['pull_idx']
            url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pull_idx}/commits'
        elif call_type == 'get_comments':
            issue_idx = kwargs['issue_idx']
            url = f'https://api.github.com/repos/{owner}/{repo}/issues/{issue_idx}/comments'

        while url:
            response = self.github_api(url=url, token=token)
            if response is None:
                break
            response_data = response.json()
            results_list.extend(response_data)

            # check next page for more pull requests
            if 'next' in response.links:
                url = response.links['next']['url']
            else:
                url = None

        return results_list

        

    def call_api(self, func: callable, **kwargs) -> dict:
        """
        API call wrapper with rate limit handling and proxy rotation.
        """
        # Update proxy env vars before GhApi call (GhApi uses urllib which reads env vars)
        if self.proxy_rotator.enabled:
            self.proxy_rotator.increment_request_count()
            self.proxy_rotator.update_env_proxies()

        max_retries = 5
        for attempt in range(max_retries):
            try:
                values = func(**kwargs)
                return values
            except HTTP403ForbiddenError as e:
                if self.proxy_rotator.enabled:
                    # Rotate IP instead of waiting for rate limit reset
                    logger.warning(
                        f"[{self.repo_full_name}] 403 Forbidden, rotating proxy IP "
                        f"(attempt {attempt+1}/{max_retries})..."
                    )
                    self.proxy_rotator.rotation_count += 1
                    self.proxy_rotator.request_count = 0
                    self.proxy_rotator.current_session_id = None
                    self.proxy_rotator.update_env_proxies()
                    time.sleep(2)
                else:
                    while True:
                        rl = self.api.rate_limit.get()
                        logger.info(
                            f"[{self.repo_full_name}] Rate limit exceeded, waiting 5 min, "
                            f"remaining: {rl.resources.core.remaining}"
                        )
                        if rl.resources.core.remaining > 0:
                            break
                        time.sleep(60 * 5)
            except HTTP404NotFoundError as e:
                logger.info(f"[{self.repo_full_name}] Resource not found {kwargs}")
                return None
            except (RemoteDisconnected, IncompleteRead, ConnectionError,
                    ConnectionResetError, TimeoutError, OSError) as e:
                wait_time = 3 * (attempt + 1)
                logger.warning(
                    f"[{self.repo_full_name}] Network error (attempt {attempt+1}/{max_retries}): {e}. "
                    f"Retrying in {wait_time}s..."
                )
                if self.proxy_rotator.enabled:
                    self.proxy_rotator.rotation_count += 1
                    self.proxy_rotator.request_count = 0
                    self.proxy_rotator.current_session_id = None
                    self.proxy_rotator.update_env_proxies()
                if attempt < max_retries - 1:
                    time.sleep(wait_time)
                else:
                    logger.error(f"[{self.repo_full_name}] Max retries reached: {e}")
                    return None
            except Exception as e:
                wait_time = 3 * (attempt + 1)
                logger.error(
                    f"[{self.repo_full_name}] Unexpected error (attempt {attempt+1}/{max_retries}): {e}"
                )
                if attempt < max_retries - 1:
                    time.sleep(wait_time)
                else:
                    logger.error(f"[{self.repo_full_name}] Max retries reached for unexpected error: {e}")
                    return None
        return None

    def extract_resolved_issues_with_official_github_api(self, pull: dict) -> list[str]:
        """
        Extract list of issues referenced by a PR

        Args:
            pull (dict): PR dictionary object from GitHub
        Return:
            resolved_issues (list): list of issue numbers referenced by PR
        """
        kwargs = {
            'call_type' : 'get_commits',
            'owner' : self.owner,
            'repo' : self.name,
            'token' : self.token,
            'pull_idx':pull['number']

        }
        # Define 1. issue number regex pattern 2. comment regex pattern 3. keywords
        issues_pat = re.compile(r"(\w+)\s+\#(\d+)")
        comments_pat = re.compile(r"(?s)<!--.*?-->")

        # Construct text to search over for issue numbers from PR body and commit messages
        text = pull['title'] if pull['title'] else ""
        text += "\n" + (pull['body'] if pull['body'] else "")
        commits = self.call_github_api(**kwargs)
        commit_messages = [commit['commit']['message'] for commit in commits]
        commit_text = "\n".join(commit_messages) if commit_messages else ""
        text += "\n" + commit_text
        # Remove comments from text
        text = comments_pat.sub("", text)
        # Look for issue numbers in text via scraping <keyword, number> patterns
        references = issues_pat.findall(text)
        resolved_issues_set = set()
        if references:
            for word, issue_num in references:
                if word.lower() in PR_KEYWORDS:
                    resolved_issues_set.add(issue_num)
        return list(resolved_issues_set)
    
    def extract_resolved_issues(self, pull: dict) -> list[str]:
        """
        Extract list of issues referenced by a PR

        Args:
            pull (dict): PR dictionary object from GitHub
        Return:
            resolved_issues (list): list of issue numbers referenced by PR
        """
        # Define 1. issue number regex pattern 2. comment regex pattern 3. keywords
        issues_pat = re.compile(r"(\w+)\s+\#(\d+)")
        comments_pat = re.compile(r"(?s)<!--.*?-->")

        # Construct text to search over for issue numbers from PR body and commit messages
        text = pull.title if pull.title else ""
        text += "\n" + (pull.body if pull.body else "")
        commits = self.get_all_loop(
            self.api.pulls.list_commits, pull_number=pull.number, quiet=True
        )
        commit_messages = [commit.commit.message for commit in commits]
        commit_text = "\n".join(commit_messages) if commit_messages else ""
        text += "\n" + commit_text
        # Remove comments from text
        text = comments_pat.sub("", text)
        # Look for issue numbers in text via scraping <keyword, number> patterns
        references = issues_pat.findall(text)
        resolved_issues_set = set()
        if references:
            for word, issue_num in references:
                if word.lower() in PR_KEYWORDS:
                    resolved_issues_set.add(issue_num)
        return list(resolved_issues_set)

    def get_all_loop(
        self,
        func: callable,
        per_page: int = 100,
        num_pages: Optional[int] = None,
        quiet: bool = False,
        **kwargs,
    ) -> list:
        """
        Return all values from a paginated API endpoint.

        Args:
            func (callable): API function to call
            per_page (int): number of values to return per page
            num_pages (int): number of pages to return
            quiet (bool): whether to print progress
            **kwargs: keyword arguments to pass to API function
        """
        page = 1
        retry_count = 0
        max_retries = 3
        args = {
            "owner": self.owner,
            "repo": self.name,
            "per_page": per_page,
            **kwargs,
        }
        while True:
            try:
                # Update proxy before each paginated call
                if self.proxy_rotator.enabled:
                    self.proxy_rotator.increment_request_count()
                    self.proxy_rotator.update_env_proxies()

                # Get values from API call
                values = func(**args, page=page)
                yield from values
                retry_count = 0
                if len(values) == 0:
                    break
                if not quiet:
                    rl = self.api.rate_limit.get()
                    logger.info(
                        f"[{self.repo_full_name}] Processed page {page} ({per_page} values per page). Remaining calls: {rl.resources.core.remaining}"
                    )
                if num_pages is not None and page >= num_pages:
                    break
                page += 1
            except (IncompleteRead, RemoteDisconnected, ConnectionResetError, OSError) as e:
                retry_count += 1
                if retry_count <= max_retries:
                    logger.warning(
                        f"[{self.repo_full_name}] Network error on page {page} "
                        f"(attempt {retry_count}/{max_retries}): {e}, retrying..."
                    )
                    if self.proxy_rotator.enabled:
                        self.proxy_rotator.rotation_count += 1
                        self.proxy_rotator.request_count = 0
                        self.proxy_rotator.current_session_id = None
                        self.proxy_rotator.update_env_proxies()
                    time.sleep(3 * retry_count)
                else:
                    logger.error(f"[{self.repo_full_name}] Max retries on page {page}, skipping")
                    page += 1
                    retry_count = 0
            except Exception as e:
                error_str = str(e)
                logger.error(f"[{self.repo_full_name}] Error processing page {page}: {e}")
                if "404" in error_str or "Not Found" in error_str:
                    page += 1
                    continue
                if self.proxy_rotator.enabled:
                    # Rotate IP and retry
                    self.proxy_rotator.rotation_count += 1
                    self.proxy_rotator.request_count = 0
                    self.proxy_rotator.current_session_id = None
                    self.proxy_rotator.update_env_proxies()
                    time.sleep(3)
                else:
                    # Original rate limit handling
                    while True:
                        rl = self.api.rate_limit.get()
                        if rl.resources.core.remaining > 0:
                            break
                        logger.info(
                            f"[{self.repo_full_name}] Waiting for rate limit reset, checking again in 5 minutes"
                        )
                        time.sleep(60 * 5)
        if not quiet:
            logger.info(
                f"[{self.repo_full_name}] Processed {(page-1)*per_page + len(values)} values"
            )

    def get_all_issues(
        self,
        per_page: int = 100,
        num_pages: Optional[int] = None,
        direction: str = "asc",
        sort: str = "created",
        state: str = "closed",
        quiet: bool = False,
    ) -> list:
        """
        Wrapper for API call to get all issues from repo

        Args:
            per_page (int): number of issues to return per page
            num_pages (int): number of pages to return
            direction (str): direction to sort issues
            sort (str): field to sort issues by
            state (str): state of issues to look for
            quiet (bool): whether to print progress
        """
        issues = self.get_all_loop(
            self.api.issues.list_for_repo,
            num_pages=num_pages,
            per_page=per_page,
            direction=direction,
            sort=sort,
            state=state,
            quiet=quiet,
        )
        return issues

    def get_all_pulls_with_official_github_api(self, max_workers: int = 8, cache_dir: str = None) -> list:
        """
        Fetch all closed PRs with parallel pagination, page-level caching, and resume support.

        1. Fetch page 1 to discover total pages from Link header (or read from cache)
        2. Check cache for already-fetched pages (supports resume after crash)
        3. Fetch remaining pages concurrently, each thread with its own proxy IP
        4. Combine all pages and return results

        Args:
            max_workers: number of concurrent threads for parallel fetching
            cache_dir: directory for page-level cache files (enables resume). If None, no caching.
        """
        base_url = f'https://api.github.com/repos/{self.owner}/{self.name}/pulls?state=closed&per_page=100'
        headers = {'Authorization': f'token {self.token}'} if self.token else {}

        # Setup page-level cache for resume support
        page_cache_dir = None
        meta_path = None
        if cache_dir:
            page_cache_dir = os.path.join(cache_dir, f'.prcache_{self.owner}_{self.name}')
            os.makedirs(page_cache_dir, exist_ok=True)
            meta_path = os.path.join(page_cache_dir, 'meta.json')

        # Step 1: Determine total pages (from cache or fetch page 1)
        last_page = None
        page1_data = []
        if meta_path and os.path.exists(meta_path):
            try:
                with open(meta_path) as f:
                    last_page = json.load(f)['total_pages']
                logger.info(f"[{self.repo_full_name}] Resuming: {last_page} pages (from cache)")
            except Exception:
                pass

        if last_page is None:
            proxies = None
            if self.proxy_rotator.enabled:
                self.proxy_rotator.increment_request_count()
                proxies = self.proxy_rotator.update_env_proxies()

            try:
                resp = requests.get(f'{base_url}&page=1', headers=headers, proxies=proxies, timeout=30)
            except Exception as e:
                logger.warning(f"[{self.repo_full_name}] Failed to fetch page 1: {e}")
                return self._get_all_pulls_sequential()

            if resp.status_code != 200:
                logger.warning(f"[{self.repo_full_name}] Page 1 returned HTTP {resp.status_code}")
                return self._get_all_pulls_sequential()

            page1_data = resp.json()
            last_page = 1
            import re as _re
            match = _re.search(r'page=(\d+)>; rel="last"', resp.headers.get('Link', ''))
            if match:
                last_page = int(match.group(1))

            # Save page 1 and metadata to cache
            if page_cache_dir:
                with open(os.path.join(page_cache_dir, 'page_0001.json'), 'w') as f:
                    json.dump(page1_data, f)
                with open(meta_path, 'w') as f:
                    json.dump({'total_pages': last_page}, f)

            if last_page <= 1:
                logger.info(f"[{self.repo_full_name}] Only 1 page ({len(page1_data)} PRs)")
                return page1_data

        # Step 2: Find already-cached pages
        cached_pages = set()
        if page_cache_dir:
            for fname in os.listdir(page_cache_dir):
                if fname.startswith('page_') and fname.endswith('.json'):
                    try:
                        cached_pages.add(int(fname[5:-5]))
                    except ValueError:
                        pass

        pages_to_fetch = [p for p in range(1, last_page + 1) if p not in cached_pages]
        logger.info(
            f"[{self.repo_full_name}] {last_page} pages total, "
            f"{len(cached_pages)} cached, {len(pages_to_fetch)} to fetch"
        )

        # Step 3: Fetch missing pages in parallel
        fetched_data = {}
        if pages_to_fetch:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def fetch_page(page_num):
                """Fetch a single page with its own proxy session."""
                page_proxies = None
                if self.proxy_rotator.enabled:
                    sid = str((int(self.proxy_rotator.current_session_id or '0') + page_num * 7) % 100000000).zfill(8)
                    username = f"proxy-cot-{self.proxy_rotator.country}-sid-{sid}"
                    http_proxy = f"http://{username}:{self.proxy_rotator.password}@{self.proxy_rotator.host}:{self.proxy_rotator.http_port}"
                    page_proxies = {"http": http_proxy, "https": http_proxy}

                url = f'{base_url}&page={page_num}'
                for attempt in range(3):
                    try:
                        r = requests.get(url, headers=headers, proxies=page_proxies, timeout=30)
                        if r.status_code == 200:
                            data = r.json()
                            if page_cache_dir:
                                with open(os.path.join(page_cache_dir, f'page_{page_num:04d}.json'), 'w') as f:
                                    json.dump(data, f)
                            return data
                        elif r.status_code in (403, 429) and self.proxy_rotator.enabled:
                            sid = str(random.randint(10000000, 99999999))
                            username = f"proxy-cot-{self.proxy_rotator.country}-sid-{sid}"
                            http_proxy = f"http://{username}:{self.proxy_rotator.password}@{self.proxy_rotator.host}:{self.proxy_rotator.http_port}"
                            page_proxies = {"http": http_proxy, "https": http_proxy}
                            time.sleep(2)
                        else:
                            logger.warning(f"[{self.repo_full_name}] Page {page_num}: HTTP {r.status_code}")
                            time.sleep(2)
                    except Exception as e:
                        logger.warning(f"[{self.repo_full_name}] Page {page_num} attempt {attempt+1}: {e}")
                        time.sleep(2)
                return []

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(fetch_page, p): p for p in pages_to_fetch}
                for future in as_completed(futures):
                    page_num = futures[future]
                    try:
                        data = future.result()
                        if not page_cache_dir:
                            fetched_data[page_num] = data
                    except Exception as e:
                        logger.error(f"[{self.repo_full_name}] Page {page_num} failed: {e}")

        # Step 4: Combine all results
        results = []
        for page_num in range(1, last_page + 1):
            if page_cache_dir:
                cache_file = os.path.join(page_cache_dir, f'page_{page_num:04d}.json')
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file) as f:
                            results.extend(json.load(f))
                    except Exception as e:
                        logger.warning(f"[{self.repo_full_name}] Bad cache page {page_num}: {e}")
            else:
                if page_num == 1:
                    results.extend(page1_data)
                elif page_num in fetched_data:
                    results.extend(fetched_data[page_num])

        logger.info(f"[{self.repo_full_name}] Fetched {len(results)} PRs total from {last_page} pages")
        return results

    def _get_all_pulls_sequential(self) -> list:
        """Fallback: sequential pagination (original implementation)."""
        kwargs = {
            'call_type': 'get_prs',
            'owner': self.owner,
            'repo': self.name,
            'token': self.token,
        }
        return self.call_github_api(**kwargs)
      
    def get_all_pulls(
        self,
        per_page: int = 100,
        num_pages: Optional[int] = None,
        direction: str = "asc",
        sort: str = "created",
        state: str = "closed",
        quiet: str = False,
    ) -> list:
        """
        Wrapper for API call to get all PRs from repo

        Args:
            per_page (int): number of PRs to return per page
            num_pages (int): number of pages to return
            direction (str): direction to sort PRs
            sort (str): field to sort PRs by
            state (str): state of PRs to look for
            quiet (bool): whether to print progress
        """
        pulls = self.get_all_loop(
            self.api.pulls.list,
            num_pages=num_pages,
            direction=direction,
            per_page=per_page,
            sort=sort,
            state=state,
            quiet=quiet,
        )
        return pulls

def extract_problem_statement_and_hints_with_official_github_api(pull: dict, repo: Repo) -> tuple[str, str]:
    """
    Extract problem statement from issues associated with a pull request

    Args:
        pull (dict): PR dictionary object from GitHub
        repo (Repo): Repo object
    Return:
        text (str): problem statement
        hints (str): hints
    """
    if repo.name == "django":
        return extract_problem_statement_and_hints_django_with_api(pull, repo)
    text = ""
    all_hint_texts = list()
    for issue_number in pull["resolved_issues"]:
        url = f'https://api.github.com/repos/{repo.owner}/{repo.name}/issues/{issue_number}'
        try:
            issue = repo.github_api(url=url, token=repo.token).json()
        except:
            issue = None
        # logger.info('extracting statement')
        # issue = repo.call_api(
        #     repo.api.issues.get,
        #     owner=repo.owner,
        #     repo=repo.name,
        #     issue_number=issue_number,
        # )
        if issue is None:
            continue
        title = issue['title'] if issue['title'] else ""
        body = issue['body'] if issue['body'] else ""
        text += f"{title}\n{body}\n"
        issue_number = issue['number']
        hint_texts = _extract_hints_with_official_github_api(pull, repo, issue_number)
        hint_text = "\n".join(hint_texts)
        all_hint_texts.append(hint_text)
    return text, "\n".join(all_hint_texts) if all_hint_texts else ""

def _extract_hints_with_official_github_api(pull: dict, repo: Repo, issue_number: int) -> list[str]:
    """
    Extract hints from comments associated with a pull request (before first commit)

    Args:
        pull (dict): PR dictionary object from GitHub
        repo (Repo): Repo object
        issue_number (int): issue number
    Return:
        hints (list): list of hints
    """
    # Get all commits in PR
    # commits = repo.get_all_loop(
    #     repo.api.pulls.list_commits, pull_number=pull["number"], quiet=True
    # )'
    # commits = list(commits)

    commit_url =  f'https://api.github.com/repos/{repo.owner}/{repo.name}/pulls/{pull["number"]}/commits'
    commits = repo.github_api(url=commit_url, token=repo.token)
    if commits == None:
        return []
    else:
        commits =  commits.json()
    
    if len(commits) == 0:
        # If there are no comments, return no hints
        return []
    
    # Get time of first commit in PR
    commit_time = commits[0]['commit']['author']['date']  # str
    commit_time = time.mktime(time.strptime(commit_time, "%Y-%m-%dT%H:%M:%SZ"))
    
    # # Get all comments in PR
    # all_comments = repo.get_all_loop(
    #     repo.api.issues.list_comments, issue_number=issue_number, quiet=True
    # )
    # all_comments = list(all_comments)

    kwargs = {
        'call_type' : 'get_comments',
        'owner' : repo.owner,
        'repo' : repo.name,
        'token' : repo.token,
        'issue_idx':issue_number

    }
    all_comments= repo.call_github_api(**kwargs)
    # Iterate through all comments, only keep comments created before first commit
    comments = list()
    for comment in all_comments:
        comment_time = time.mktime(
            time.strptime(comment['updated_at'], "%Y-%m-%dT%H:%M:%SZ")
        )  # use updated_at instead of created_at
        if comment_time < commit_time:
            comments.append(comment)
        else:
            break
        # only include information available before the first commit was created
    # Keep text from comments
    comments = [comment['body'] for comment in comments]
    return comments


def extract_problem_statement_and_hints(pull: dict, repo: Repo) -> tuple[str, str]:
    """
    Extract problem statement from issues associated with a pull request

    Args:
        pull (dict): PR dictionary object from GitHub
        repo (Repo): Repo object
    Return:
        text (str): problem statement
        hints (str): hints
    """
    if repo.name == "django":
        return extract_problem_statement_and_hints_django(pull, repo)
    text = ""
    all_hint_texts = list()
    for issue_number in pull["resolved_issues"]:
        issue = repo.call_api(
            repo.api.issues.get,
            owner=repo.owner,
            repo=repo.name,
            issue_number=issue_number,
        )
        if issue is None:
            continue
        title = issue.title if issue.title else ""
        body = issue.body if issue.body else ""
        text += f"{title}\n{body}\n"
        issue_number = issue.number
        hint_texts = _extract_hints(pull, repo, issue_number)
        hint_text = "\n".join(hint_texts)
        all_hint_texts.append(hint_text)
    return text, "\n".join(all_hint_texts) if all_hint_texts else ""


def _extract_hints(pull: dict, repo: Repo, issue_number: int) -> list[str]:
    """
    Extract hints from comments associated with a pull request (before first commit)

    Args:
        pull (dict): PR dictionary object from GitHub
        repo (Repo): Repo object
        issue_number (int): issue number
    Return:
        hints (list): list of hints
    """
    # Get all commits in PR
    commits = repo.get_all_loop(
        repo.api.pulls.list_commits, pull_number=pull["number"], quiet=True
    )
    commits = list(commits)
    if len(commits) == 0:
        # If there are no comments, return no hints
        return []
    # Get time of first commit in PR
    commit_time = commits[0].commit.author.date  # str
    commit_time = time.mktime(time.strptime(commit_time, "%Y-%m-%dT%H:%M:%SZ"))
    # Get all comments in PR
    all_comments = repo.get_all_loop(
        repo.api.issues.list_comments, issue_number=issue_number, quiet=True
    )
    all_comments = list(all_comments)
    # Iterate through all comments, only keep comments created before first commit
    comments = list()
    for comment in all_comments:
        comment_time = time.mktime(
            time.strptime(comment.updated_at, "%Y-%m-%dT%H:%M:%SZ")
        )  # use updated_at instead of created_at
        if comment_time < commit_time:
            comments.append(comment)
        else:
            break
        # only include information available before the first commit was created
    # Keep text from comments
    comments = [comment.body for comment in comments]
    return comments

def check_token_validity(token: str) -> bool:
    url = "https://api.github.com/user"
    headers = {"Authorization": f"token {token}"}
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()  # 如果返回 401 错误，说明 token 无效
        return True
    except requests.exceptions.RequestException:
        logger.warning("Invalid or expired GitHub token.")
        return False


def get_with_retries(
    url: str,
    token: str = None,
    max_retries: int = 5,
    backoff_factor: float = 0.5,
    timeout: int = 15,
    proxy_rotator: ProxyRotator = None,
) -> str:
    if token and not check_token_validity(token):
        logger.warning("Invalid GitHub token, aborting request.")
        return ""

    session = requests.Session()
    headers = {"Authorization": f"token {token}"} if token else {}

    retries = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # Get proxy config
    proxies = None
    if proxy_rotator and proxy_rotator.enabled:
        proxy_rotator.increment_request_count()
        proxies = proxy_rotator.get_proxies()

    try:
        response = session.get(url, headers=headers, timeout=timeout, proxies=proxies)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return ""

def extract_patches(pull: dict, repo: Repo) -> tuple[str, str, bool]:
    """
    Get patch and test patch from PR

    Args:
        pull (dict): PR dictionary object from GitHub
        repo (Repo): Repo object
    Return:
        patch_change_str (str): gold patch
        patch_test_str (str): test patch
    """
    # Convert diff to patch format with "index" lines removed
    # patch = requests.get(pull["diff_url"]).text
    patch = get_with_retries(pull["diff_url"], repo.token, proxy_rotator=repo.proxy_rotator)
    if patch =='':
        return "", "", False
    if patch.endswith("\n"):
        patch = patch[:-1]
    # Create change patch and test patch
    patch_change, patch_test = [], []

    # Flag to determine if current diff block is a test or general change
    # Values: 'test', 'diff', None
    flag = None

    for line in patch.split("\n"):
        # Exclude commit specific metadata
        if line.startswith("index "):
            continue
        # Determine if current diff block is a test or general change
        if line.startswith("diff --git a/"):
            words = set(re.split(r" |_|\/|\.", line.lower()))
            flag = (
                "test"
                if ("test" in words or "tests" in words or "testing" in words)
                else "diff"
            )
            if repo.language == 'python':
                if flag != "test" and not line.strip().endswith(".py"):
                    flag = None
            elif repo.language == 'js':
                language = get_language_with_pygments(line.strip())
                is_js = (language=='javascript' or language == 'typescript')
                if  ('webpack' in repo.name or 'jest' in repo.name)  and line.strip().endswith(".json"):
                    is_js = True

                if flag != "test" and not is_js:
                    flag = None
            elif repo.language == 'java':
                file_name = line.split("/")[-1]
                if file_name.endswith(".java"):
                    file_name.replace(".java", "")
                    if(file_name.startswith("Test") or file_name.startswith("Tests") or file_name.endswith("Test") or file_name.endswith("Tests")):
                        flag = "test"

                language = get_language_with_pygments(line.strip())
                is_java = (language=='java')
                if  ( 'netty' in repo.name)  and (line.strip().endswith(".c") or line.strip().endswith("pom.xml")):
                    is_java = True

                if flag != "test" and not is_java:
                    flag = None

                
        # Append line to separate patch depending on flag status
        if flag == "test":
            patch_test.append(line)
        elif flag == "diff":
            patch_change.append(line)

    patch_change_str = "\n".join(patch_change) + "\n" if len(patch_change) > 0 else ""
    patch_test_str = "\n".join(patch_test) + "\n" if len(patch_test) > 0 else ""
    return patch_change_str, patch_test_str, True


### MARK: Repo Specific Parsing Functions ###
def extract_problem_statement_and_hints_django(
    pull: dict, repo: Repo
) -> tuple[str, str]:
    """
    Get problem statement and hints from issues associated with a pull request

    Args:
        pull (dict): PR dictionary object from GitHub
        repo (Repo): Repo object
    Return:
        text (str): problem statement
        hints (str): hints
    """
    text = ""
    all_hints_text = list()
    for issue_number in pull["resolved_issues"]:
        url = f"https://code.djangoproject.com/ticket/{issue_number}"
        resp = requests.get(url)
        if resp.status_code != 200:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")

        # Get problem statement (title + body)
        issue_desc = soup.find("div", {"id": "ticket"})
        title = issue_desc.find("h1", class_="searchable").get_text()
        title = re.sub(r"\s+", " ", title).strip()
        body = issue_desc.find("div", class_="description").get_text()
        body = re.sub(r"\n+", "\n", body)
        body = re.sub(r"    ", "\t", body)
        body = re.sub(r"[ ]{2,}", " ", body).strip()
        text += f"{title}\n{body}\n"

        # Get time of first commit in PR
        commits = repo.get_all_loop(
            repo.api.pulls.list_commits, pull_number=pull["number"], quiet=True
        )
        commits = list(commits)
        if len(commits) == 0:
            continue
        commit_time = commits[0].commit.author.date
        commit_time = time.mktime(time.strptime(commit_time, "%Y-%m-%dT%H:%M:%SZ"))

        # Get all comments before first commit
        comments_html = soup.find("div", {"id": "changelog"})
        div_blocks = comments_html.find_all("div", class_="change")
        comments = []
        # Loop through each div block
        for div_block in div_blocks:
            # Find the comment text and timestamp
            comment_resp = div_block.find("div", class_="comment")
            timestamp_resp = div_block.find("a", class_="timeline")
            if comment_resp is None or timestamp_resp is None:
                continue

            comment_text = re.sub(r"\s+", " ", comment_resp.text).strip()
            timestamp = timestamp_resp["title"]
            if timestamp.startswith("See timeline at "):
                timestamp = timestamp[len("See timeline at ") :]
            timestamp = time.mktime(time.strptime(timestamp, "%m/%d/%y %H:%M:%S"))

            # Append the comment and timestamp as a tuple to the comments list
            if timestamp < commit_time:
                all_hints_text.append((comment_text, timestamp))

    return text, all_hints_text

### MARK: Repo Specific Parsing Functions ###
def extract_problem_statement_and_hints_django_with_api(
    pull: dict, repo: Repo
) -> tuple[str, str]:
    """
    Get problem statement and hints from issues associated with a pull request

    Args:
        pull (dict): PR dictionary object from GitHub
        repo (Repo): Repo object
    Return:
        text (str): problem statement
        hints (str): hints
    """
    text = ""
    all_hints_text = list()
    try:
        for issue_number in pull["resolved_issues"]:
            logger.info(issue_number)
            try:
            
            
                # URL of the CSV data
                url = f"https://code.djangoproject.com/ticket/{issue_number}?format=csv"
                # Make a GET request to fetch the CSV data
                
                title = ''
                body = ''
                # Check if the request was successful
                max_tries =0 
                
                while True:
                    response = requests.get(url)
                    logger.info(response.status_code )
                    if max_tries>5:
                        break
                    if response.status_code == 200:
                        # Read the CSV data into a list of dictionaries
                        csv_reader = csv.DictReader(StringIO(response.text))
                        csv_reader = list(csv_reader)
                        # logger.info(csv_reader)
                        csv_data = csv_reader[0]
                        break
                    elif response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            logger.info(f"Too many requests. Retrying after {retry_after} seconds.")
                            time.sleep(int(retry_after))
                        else:
                            logger.info("Too many requests. Retrying after default 120 seconds.")
                            time.sleep(120)
                        max_tries += 1
                    else:
                        logger.info(f"Failed to retrieve data:{response.status_code}")

                url = f"https://code.djangoproject.com/ticket/{issue_number}"
                while True:
                    resp = requests.get(url)
                    logger.info(response.status_code )
                    if max_tries>5:
                        break
                    if response.status_code == 200:
                        # Read the CSV data into a list of dictionaries
                        csv_reader = csv.DictReader(StringIO(response.text))
                        csv_reader = list(csv_reader)
                        # logger.info(csv_reader)
                        csv_data = csv_reader[0]
                        break
                    elif response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            logger.info(f"Too many requests. Retrying after {retry_after} seconds.")
                            time.sleep(int(retry_after))
                        else:
                            logger.info("Too many requests. Retrying after default 120 seconds.")
                            time.sleep(120)
                        max_tries += 1
                    else:
                        logger.info(f"Failed to retrieve data:{response.status_code}")
                        continue
                        
                
                soup = BeautifulSoup(resp.text, "html.parser")

                # Get problem statement (title + body)
                # issue_desc = soup.find("div", {"id": "ticket"})
                # title = issue_desc.find("h1", class_="searchable").get_text()
                title = csv_data['summary']
                body = csv_data['description']
                title = re.sub(r"\s+", " ", title).strip()
                # body = issue_desc.find("div", class_="description").get_text()
                body = re.sub(r"\n+", "\n", body)
                body = re.sub(r"    ", "\t", body)
                body = re.sub(r"[ ]{2,}", " ", body).strip()
                text += f"{title}\n{body}\n"

                commit_url =  f'https://api.github.com/repos/{repo.owner}/{repo.name}/pulls/{pull["number"]}/commits'
                commits = repo.github_api(url=commit_url, token=repo.token)
                if commits == None:
                    continue
                else:
                    commits =  commits.json()
                if len(commits) == 0:
                    # If there are no comments, return no hints
                    continue
                
                # Get time of first commit in PR
                commit_time = commits[0]['commit']['author']['date']  # str
                commit_time = time.mktime(time.strptime(commit_time, "%Y-%m-%dT%H:%M:%SZ"))


                # # Get time of first commit in PR
                # commits = repo.get_all_loop(
                #     repo.api.pulls.list_commits, pull_number=pull["number"], quiet=True
                # )
                # commits = list(commits)
                # if len(commits) == 0:
                #     continue
                # commit_time = commits[0].commit.author.date
                # commit_time = time.mktime(time.strptime(commit_time, "%Y-%m-%dT%H:%M:%SZ"))

                # Get all comments before first commit
                comments_html = soup.find("div", {"id": "changelog"})
                div_blocks = comments_html.find_all("div", class_="change")
                comments = []
                # Loop through each div block
                for div_block in div_blocks:
                    # Find the comment text and timestamp
                    comment_resp = div_block.find("div", class_="comment")
                    timestamp_resp = div_block.find("a", class_="timeline")
                    if comment_resp is None or timestamp_resp is None:
                        continue

                    comment_text = re.sub(r"\s+", " ", comment_resp.text).strip()
                    timestamp = timestamp_resp["title"]
                    if timestamp.startswith("See timeline at "):
                        timestamp = timestamp[len("See timeline at ") :]
                    # timestamp = time.mktime(time.strptime(timestamp, "%m/%d/%y %H:%M:%S"))
                    timestamp = convert_to_timestamp(timestamp)
                    # Append the comment and timestamp as a tuple to the comments list
                    if timestamp < commit_time:
                        all_hints_text.append((comment_text, timestamp))
            except Exception as e:
                logger.error(f"Error processing issue {issue_number}: {e}")
                continue
        return text, all_hints_text
    except Exception as e:
        logger.error(f"An error occurred in the main block: {e}")
        return "", []





def convert_to_timestamp(timestamp_str):
    formats = ["%m/%d/%y %H:%M:%S", "%b %d, %Y, %I:%M:%S %p", "%b %d, %Y, %H:%M:%S"]
    for fmt in formats:
        try:
            # Attempt to parse the timestamp string with the current format
            parsed_time = time.strptime(timestamp_str, fmt)
     
            # Convert parsed struct_time to timestamp
            return time.mktime(parsed_time)
        except ValueError as e:
            # Print the failed format and error message
            # logger.info(f"Failed to parse '{timestamp_str}' with format '{fmt}': {e}")
            continue
    # If none of the formats match, return None
    print(f"Error: Time data '{timestamp_str}' does not match any known formats.")
    return None
