import hashlib
import logging
import os
import re
import asyncio
import aiohttp
import time
from typing import Optional, Tuple
from datetime import datetime

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

from pygments.lexers import get_lexer_for_filename
from pygments.util import ClassNotFound

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


class Repo:
    def __init__(
        self,
        owner: str,
        name: str,
        token: Optional[str] = None,
        language: Optional[str] = 'python',
        session: Optional[aiohttp.ClientSession] = None,
        file_lock: Optional[asyncio.Lock] = None
    ):
        """
        Init to retrieve target repository and create async HTTP session

        Args:
            owner (str): owner of target repository
            name (str): name of target repository
            token (str): github token
            language (str): programming language
            session (aiohttp.ClientSession): async HTTP session
            file_lock (asyncio.Lock): lock for file operations
        """
        self.owner = owner
        self.name = name
        self.token = token
        self.language = language
        self.repo_name = f"{owner}/{name}"
        self.session = session
        self.file_lock = file_lock or asyncio.Lock()

        # Initialize proxy rotator
        self.proxy_rotator = ProxyRotator(self.repo_name)

    async def fetch_with_retries(
        self,
        url: str,
        max_retries: int = 5,
        timeout: int = 30
    ) -> str:
        """
        Async HTTP request with retry logic and proxy rotation.

        Args:
            url (str): URL to fetch
            max_retries (int): maximum number of retries
            timeout (int): request timeout in seconds

        Returns:
            str: response text, empty string on failure
        """
        headers = {}
        if self.token:
            headers['Authorization'] = f'token {self.token}'

        retries = 0
        while retries < max_retries:
            # Get proxy config
            proxy = None
            if self.proxy_rotator.enabled:
                self.proxy_rotator.increment_request_count()
                proxies = self.proxy_rotator.get_proxies()
                if proxies:
                    proxy = proxies.get('http')

            try:
                async with self.session.get(
                    url,
                    headers=headers,
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as response:
                    if response.status == 200:
                        return await response.text()
                    elif response.status == 403:
                        # Rate limit handling
                        remaining = response.headers.get('X-RateLimit-Remaining', '0')
                        if int(remaining) == 0:
                            if self.proxy_rotator.enabled:
                                logger.info(f'[{self.repo_name}] Rate limited, rotating proxy IP...')
                                self.proxy_rotator.rotation_count += 1
                                self.proxy_rotator.request_count = 0
                                self.proxy_rotator.current_session_id = None
                                await asyncio.sleep(1)
                            else:
                                reset_time = int(response.headers.get('X-RateLimit-Reset', time.time() + 60))
                                sleep_time = max(reset_time - int(time.time()) + 1, 0)
                                logger.warning(f'Rate limit exceeded. Sleeping for {sleep_time} seconds.')
                                await asyncio.sleep(sleep_time)
                        retries += 1
                    elif response.status in [502, 503]:
                        wait_time = min(3 * (retries + 1), 15)
                        logger.warning(
                            f'[{self.repo_name}] {response.status} error, '
                            f'retrying in {wait_time}s (attempt {retries+1}/{max_retries})'
                        )
                        if self.proxy_rotator.enabled:
                            self.proxy_rotator.rotation_count += 1
                            self.proxy_rotator.request_count = 0
                            self.proxy_rotator.current_session_id = None
                        await asyncio.sleep(wait_time)
                        retries += 1
                    elif response.status == 429:
                        retry_after = int(response.headers.get('Retry-After', 60))
                        if self.proxy_rotator.enabled:
                            logger.info(f'[{self.repo_name}] 429 rate limited, rotating proxy...')
                            self.proxy_rotator.rotation_count += 1
                            self.proxy_rotator.request_count = 0
                            self.proxy_rotator.current_session_id = None
                            await asyncio.sleep(2)
                        else:
                            logger.info(f'429 Too Many Requests. Sleeping for {retry_after}s')
                            await asyncio.sleep(retry_after)
                        retries += 1
                    elif response.status == 404:
                        logger.info(f"[{self.repo_name}] Resource not found: {url}")
                        return ""
                    else:
                        logger.warning(f'Error: {response.status}')
                        await asyncio.sleep(min(3 * retries, 15))
                        retries += 1

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                wait_time = min(3 * (retries + 1), 15)
                logger.warning(
                    f'[{self.repo_name}] Network error: {e}, '
                    f'retrying in {wait_time}s (attempt {retries+1}/{max_retries})'
                )
                if self.proxy_rotator.enabled:
                    self.proxy_rotator.rotation_count += 1
                    self.proxy_rotator.request_count = 0
                    self.proxy_rotator.current_session_id = None
                await asyncio.sleep(wait_time)
                retries += 1

        logger.error(f"[{self.repo_name}] Failed to fetch {url} after {max_retries} retries")
        return ""

    async def fetch_json(self, url: str) -> dict | list | None:
        """Fetch and parse JSON from a URL."""
        text = await self.fetch_with_retries(url)
        if not text:
            return None
        try:
            import json
            return json.loads(text)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse JSON from {url}")
            return None


async def extract_patches(pull: dict, repo: Repo) -> Tuple[str, str, bool]:
    """
    Get patch and test patch from PR (async version)

    Args:
        pull (dict): PR dictionary object from GitHub
        repo (Repo): Repo object
    Return:
        patch_change_str (str): gold patch
        patch_test_str (str): test patch
        request_success (bool): whether the request was successful
    """
    patch = await repo.fetch_with_retries(pull["diff_url"])
    if patch == '':
        return "", "", False

    if patch.endswith("\n"):
        patch = patch[:-1]

    # Create change patch and test patch
    patch_change, patch_test = [], []
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
                is_js = (language == 'javascript' or language == 'typescript')
                if ('webpack' in repo.name or 'jest' in repo.name) and line.strip().endswith(".json"):
                    is_js = True
                if flag != "test" and not is_js:
                    flag = None
            elif repo.language == 'java':
                file_name = line.split("/")[-1]
                if file_name.endswith(".java"):
                    file_name = file_name.replace(".java", "")
                    if (file_name.startswith("Test") or file_name.startswith("Tests") or
                            file_name.endswith("Test") or file_name.endswith("Tests")):
                        flag = "test"

                language = get_language_with_pygments(line.strip())
                is_java = (language == 'java')
                if ('netty' in repo.name) and (line.strip().endswith(".c") or line.strip().endswith("pom.xml")):
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


async def extract_problem_statement_and_hints_with_official_github_api(
    pull: dict, repo: Repo
) -> Tuple[str, str]:
    """
    Extract problem statement from issues associated with a pull request (async version)

    Args:
        pull (dict): PR dictionary object from GitHub
        repo (Repo): Repo object
    Return:
        text (str): problem statement
        hints (str): hints
    """
    text = ""
    all_hint_texts = []

    for issue_number in pull["resolved_issues"]:
        url = f'https://api.github.com/repos/{repo.owner}/{repo.name}/issues/{issue_number}'
        issue = await repo.fetch_json(url)

        if issue is None:
            continue

        title = issue.get('title', "")
        body = issue.get('body', "")
        text += f"{title}\n{body}\n"

        # Get PR commits to determine first commit time
        commits_url = f'https://api.github.com/repos/{repo.owner}/{repo.name}/pulls/{pull["number"]}/commits'
        commits = await repo.fetch_json(commits_url)

        if not commits or len(commits) == 0:
            continue

        commit_time_str = commits[0]['commit']['author']['date']
        commit_time = time.mktime(time.strptime(commit_time_str, "%Y-%m-%dT%H:%M:%SZ"))

        # Get comments
        comments_url = f'https://api.github.com/repos/{repo.owner}/{repo.name}/issues/{issue_number}/comments'
        all_comments = await repo.fetch_json(comments_url)

        if all_comments:
            for comment in all_comments:
                comment_time = time.mktime(
                    time.strptime(comment['updated_at'], "%Y-%m-%dT%H:%M:%SZ")
                )
                if comment_time < commit_time:
                    all_hint_texts.append(comment.get('body', ''))
                else:
                    break

    hints = "\n---\n".join(all_hint_texts) if all_hint_texts else ""
    return text, hints


async def extract_problem_statement_and_hints(pull: dict, repo: Repo) -> Tuple[str, str]:
    """
    Extract problem statement from issues (simplified async version, calls the API version)

    Args:
        pull (dict): PR dictionary object from GitHub
        repo (Repo): Repo object
    Return:
        text (str): problem statement
        hints (str): hints
    """
    # For now, just use the official GitHub API version
    return await extract_problem_statement_and_hints_with_official_github_api(pull, repo)
