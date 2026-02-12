#!/usr/bin/env python3

"""Given the `<owner/name>` of a GitHub repo, this script writes the raw information for all the repo's PRs to a single `.jsonl` file."""

import argparse
import json
import logging
import os
import random
import string
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from tqdm import tqdm
from fastcore.xtras import obj2dict
from utils import Repo

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Thread-safe file writer
_write_lock = threading.Lock()


def _process_single_pr(repo_name: str, token: str, pull: dict, language: str = "python") -> dict:
    """
    Process a single PR in its own thread with an independent Repo/proxy session.

    Args:
        repo_name: "owner/repo"
        token: GitHub token (can be empty for anonymous + proxy)
        pull: PR dict
        language: repo language

    Returns:
        pull dict with resolved_issues populated
    """
    owner, name = repo_name.split("/")

    # Each thread gets its own Repo instance → own ProxyRotator → own session ID → own IP
    repo = None
    for attempt in range(3):
        try:
            repo = Repo(owner, name, token=token, language=language)
            break
        except Exception as e:
            if attempt < 2:
                import time
                time.sleep(2 * (attempt + 1))
            else:
                logger.error(f"PR#{pull.get('number')}: Failed to create Repo: {e}")
                pull["resolved_issues"] = []
                return pull

    # Force a unique random session ID for this thread
    random_sid = ''.join(random.choices(string.digits, k=8))
    repo.proxy_rotator.current_session_id = random_sid
    repo.proxy_rotator.rotation_count = 0

    try:
        issues = repo.extract_resolved_issues_with_official_github_api(pull)
        pull["resolved_issues"] = issues
    except Exception as e:
        logger.error(f"PR#{pull.get('number')}: {e}")
        pull["resolved_issues"] = []

    return pull


def log_all_pulls(repo: Repo, output: str, mode: str, pr_data_list=None,
                  workers: int = 16, repo_name: str = "", token: str = ""):
    """
    Iterate over all pull requests in a repository and log them to a file.

    Args:
        repo: Repo object (used for fetching PR list and swebench mode)
        output: output file path
        mode: 'swebench' or 'omnigirl'
        pr_data_list: list of already-processed PR numbers to skip
        workers: number of concurrent workers for PR processing
        repo_name: "owner/repo" string for spawning per-thread Repo instances
        token: GitHub token
    """
    output_dir = os.path.dirname(output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    if mode == 'swebench':
        with open(output, "w") as output_file:
            for pull in repo.get_all_pulls():
                setattr(pull, "resolved_issues", repo.extract_resolved_issues(pull))
                print(json.dumps(obj2dict(pull)), end="\n", flush=True, file=output_file)
        return

    # --- omnigirl mode with concurrency ---
    cache_dir = os.path.dirname(output) or "."
    pulls = repo.get_all_pulls_with_official_github_api(max_workers=workers, cache_dir=cache_dir)
    print(f'total prs number: {len(pulls)}')

    # Filter already processed PRs
    skip_set = set(pr_data_list) if pr_data_list else set()
    to_process = [p for p in pulls if p['number'] not in skip_set]
    skipped = len(pulls) - len(to_process)
    if skipped:
        logger.info(f"Skipping {skipped} already-processed PRs, {len(to_process)} remaining")

    if not to_process:
        logger.info("All PRs already processed")
        return

    # Process PRs concurrently
    pbar = tqdm(total=len(to_process), desc="Processing PRs")
    with open(output, 'a') as f:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for pull in to_process:
                future = executor.submit(
                    _process_single_pr, repo_name, token, pull
                )
                futures[future] = pull['number']

            for future in as_completed(futures):
                pr_number = futures[future]
                try:
                    result = future.result()
                    with _write_lock:
                        json.dump(result, f)
                        f.write('\n')
                        f.flush()
                except Exception as e:
                    logger.error(f"PR#{pr_number} failed: {e}")
                pbar.update(1)

    pbar.close()


def main(repo_name: str, output: str, token: Optional[str] = None,
         mode: Optional[str] = 'swebench', workers: int = 16):
    """
    Logic for logging all pull requests in a repository.
    """
    if token is None:
        token = os.environ.get("GITHUB_TOKEN", "") or None
    try:
        owner, repo = repo_name.split("/")
    except Exception:
        print(repo_name)
        return
    logger.info(repo_name)
    try:
        repo_obj = Repo(owner, repo, token=token)
    except RuntimeError as e:
        logger.error(f"Failed to initialize repository {repo_name}: {e}")
        raise SystemExit(1)

    pr_data_list = None
    if os.path.exists(output):
        pr_data_list = []
        with open(output, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    pr_data_list.append(json.loads(line)['number'])
                except (json.JSONDecodeError, KeyError):
                    continue

    log_all_pulls(repo_obj, output, mode, pr_data_list,
                  workers=workers, repo_name=repo_name, token=token)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_name", type=str, help="Name of the repository")
    parser.add_argument("output", type=str, help="Output file name")
    parser.add_argument("--token", type=str, help="GitHub token")
    parser.add_argument("--mode", type=str, default='omnigirl', help="Collecting mode")
    parser.add_argument("--workers", "-w", type=int, default=16,
                        help="Number of concurrent workers (default: 16)")
    args = parser.parse_args()
    main(**vars(args))
