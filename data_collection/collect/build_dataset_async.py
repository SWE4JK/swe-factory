#!/usr/bin/env python3

import argparse
import asyncio
import aiohttp
import copy
import json
import logging
import os
from typing import Optional
from datetime import datetime
from utils_async import Repo, extract_patches, extract_problem_statement_and_hints, extract_problem_statement_and_hints_with_official_github_api

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def create_instance(repo: Repo, pull: dict, output_path: str, mode: str = 'swebench') -> dict:
    """
    Create a single task instance from a pull request, where task instance is:

    {
        repo (str): owner/repo this task instance is from,
        pull_number (int): number of PR this task instance is from,
        base_commit (str): SHA of the base commit PR is based on,
        patch (str): reference solution as .patch (apply to base commit),
        test_patch (str): test suite as .patch (apply to base commit),
    }
    """
    patch, test_patch, request_success = await extract_patches(pull, repo)

    instance_id = (repo.repo_name + "-" + str(pull["number"])).replace("/", "__")
    successful_path = os.path.join(os.path.dirname(output_path), "successful_requests.txt")
    if request_success:
        async with repo.file_lock:
            with open(successful_path, "a") as f:
                f.write(instance_id + "\n")

    if mode == 'swebench':
        problem_statement, hints = await extract_problem_statement_and_hints(pull, repo)
    else:
        problem_statement, hints = await extract_problem_statement_and_hints_with_official_github_api(pull, repo)

    return {
        "repo": repo.repo_name,
        "pull_number": pull["number"],
        "instance_id": instance_id,
        "issue_numbers": pull["resolved_issues"],
        "base_commit": pull["base"]["sha"],
        "patch": patch,
        "test_patch": test_patch,
        "problem_statement": problem_statement,
        "hints_text": hints,
        "created_at": pull["created_at"],
    }


def is_valid_pull(pull: dict) -> bool:
    """
    Check whether PR has an associated issue and is merged

    Args:
        pull (dict): pull request object
    Returns:
        bool: whether PR is valid
    """
    if pull["merged_at"] is None:
        return False
    if "resolved_issues" not in pull or len(pull["resolved_issues"]) < 1:
        return False
    return True


def is_valid_instance(instance: dict) -> bool:
    """
    Check whether task instance has all required fields for task instance creation

    Args:
        instance (dict): task instance object
    Returns:
        bool: whether task instance is valid
    """
    if instance["patch"] is None or instance["patch"] == "":
        logger.info(f"Instance {instance['pull_number']} no patch")
        return False
    if instance["problem_statement"] is None or instance["problem_statement"] == "":
        logger.info(f"Instance {instance['pull_number']} no problem statement")
        return False
    return True


def has_test_patch(instance: dict) -> bool:
    """
    Check whether task instance has a test suite

    Args:
        instance (dict): task instance object
    Returns:
        bool: whether task instance has a test suite
    """
    if instance["test_patch"] is None or instance["test_patch"].strip() == "":
        logger.info(f"Instance {instance['pull_number']} no test patch")
        return False
    return True


async def process_single_pr(
    pull: dict,
    repo: Repo,
    output_path: str,
    mode: str,
    cutoff_date: datetime,
    seen_prs: set,
    successful_instances: set,
    all_output_file,
    output_file,
    file_lock: asyncio.Lock,
    stats: dict,
    semaphore: asyncio.Semaphore
) -> None:
    """
    Process a single PR asynchronously

    Note: This function should be called with explicit parameters to avoid
    closure issues in async loops.
    """
    # DEBUG: Print immediately upon entry
    pr_num = pull.get('number', 'UNKNOWN')
    resolved = pull.get('resolved_issues', [])
    if stats.get('_debug_count', 0) < 5 or (resolved and len(resolved) > 0 and stats.get('_debug_count', 0) < 270):
        logger.info(f"[ENTRY] process_single_pr PR #{pr_num}: resolved_issues={resolved}, id={id(pull)}")
        stats['_debug_count'] = stats.get('_debug_count', 0) + 1

    async with semaphore:  # Limit concurrent tasks
        stats['total_processed'] += 1

        instance_id = (
            pull["base"]["repo"]["full_name"] + "-" + str(pull["number"])
        ).replace("/", "__")

        if instance_id in seen_prs or instance_id in successful_instances:
            logger.debug(f"Skipping {instance_id}: already processed")
            return

        # DEBUG: Check pull data right before is_valid_pull
        if stats['total_processed'] <= 10 or (pull.get('resolved_issues') and len(pull['resolved_issues']) > 0 and stats['total_processed'] <= 270):
            logger.info(f"[PRE-CHECK] PR #{pull.get('number')}: resolved_issues={pull.get('resolved_issues')}, merged_at={pull.get('merged_at') is not None}")

        if not is_valid_pull(pull):
            # Log all invalid PRs for debugging
            logger.debug(
                f"PR #{pull.get('number')} invalid: "
                f"merged_at={'YES' if pull.get('merged_at') else 'NO'}, "
                f"resolved_issues_len={len(pull.get('resolved_issues', []))}"
            )
            return

        stats['valid_pulls'] += 1
        logger.info(f"✅ Valid PR #{pull['number']} with issues {pull['resolved_issues']}")

        try:
            # Create task instance
            instance = await create_instance(repo, pull, output_path, mode)

            if datetime.strptime(instance["created_at"], "%Y-%m-%dT%H:%M:%SZ") >= cutoff_date:
                logger.info(f"Instance {instance_id} created_at {instance['created_at']} exceeds cutoff_date {cutoff_date}")
                return

            if is_valid_instance(instance):
                # Write to .all output file (thread-safe)
                async with file_lock:
                    print(json.dumps(instance), end="\n", flush=True, file=all_output_file)
                    stats['completed'] += 1

                    if has_test_patch(instance):
                        # If has test suite, write to output file
                        print(json.dumps(instance), end="\n", flush=True, file=output_file)
                        stats['with_tests'] += 1

                    # Log progress every 10 instances for better visibility
                    if stats['completed'] % 10 == 0:
                        logger.info(
                            f"[{repo.repo_name}] {stats['completed']} valid, {stats['with_tests']} with tests."
                        )
        except Exception as e:
            logger.error(f"Error processing PR {instance_id}: {e}", exc_info=True)


async def main(
    pr_file: str,
    output: str,
    token: Optional[str] = None,
    mode: Optional[str] = 'swebench',
    language: Optional[str] = 'python',
    cutoff_date: Optional[str] = None,
    max_concurrency: int = 20
):
    """
    Main async thread for creating task instances from pull requests

    Args:
        pr_file (str): path to pull request JSONL file
        output (str): output file name
        token (str): GitHub token
        mode (str): collecting mode
        language (str): programming language
        cutoff_date (str): cutoff date string
        max_concurrency (int): maximum number of concurrent tasks
    """
    logger.info(f'Language: {language}')
    logger.info(f'Mode: {mode}')
    logger.info(f'Max concurrency: {max_concurrency}')

    cutoff_date = datetime.strptime(cutoff_date, "%Y-%m-%dT%H:%M:%SZ")

    if token is None:
        token = os.environ["GITHUB_TOKEN"]

    repos = dict()
    stats = {'completed': 0, 'with_tests': 0, 'valid_pulls': 0, 'total_processed': 0}
    total_instances = 0
    all_output = output + ".all"
    seen_prs = set()
    file_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(max_concurrency)

    successful_path = os.path.join(os.path.dirname(output), "successful_requests.txt")

    if not os.path.exists(successful_path):
        with open(successful_path, "w") as f:
            pass

    successful_instances = set()
    with open(successful_path, "r") as f:
        for line in f:
            successful_instances.add(line.strip())

    # Continue where we left off if output file already exists
    if os.path.exists(all_output):
        with open(all_output) as f:
            for line in f:
                pr = json.loads(line)
                if "instance_id" not in pr:
                    pr["instance_id"] = (
                        pr["repo"] + "-" + str(pr["pull_number"])
                    ).replace("/", "__")
                instance_id = pr["instance_id"]
                seen_prs.add(instance_id)
                if datetime.strptime(pr["created_at"], "%Y-%m-%dT%H:%M:%SZ") >= cutoff_date:
                    logger.info(f"Instance {instance_id} created_at {pr['created_at']} exceeds cutoff_date {cutoff_date}")
                    continue
                if is_valid_instance(pr):
                    stats['completed'] += 1
                    if has_test_patch(pr):
                        stats['with_tests'] += 1

    logger.info(f"{len(seen_prs)} instance_ids previously recorded")

    # Read all PRs from file
    pulls = []
    with_issues_count = 0
    with open(pr_file) as f:
        for line in f:
            pull = json.loads(line)
            pulls.append(pull)
            total_instances += 1
            if pull.get('resolved_issues') and len(pull['resolved_issues']) > 0:
                with_issues_count += 1
                if with_issues_count <= 3:
                    logger.info(f"Loaded PR #{pull['number']} with {len(pull['resolved_issues'])} resolved issues: {pull['resolved_issues']}")

    logger.info(f"Total PRs to process: {total_instances}, with resolved_issues: {with_issues_count}")

    # Open output files
    write_mode_all = "w" if not os.path.exists(all_output) else "a"
    write_mode = "w" if not os.path.exists(output) else "a"

    with open(all_output, write_mode_all) as all_output_file:
        with open(output, write_mode) as output_file:
            # Create async session
            async with aiohttp.ClientSession() as session:
                # Group PRs by repo
                pulls_by_repo = {}
                for pull in pulls:
                    repo_name = pull["base"]["repo"]["full_name"]
                    if repo_name not in pulls_by_repo:
                        pulls_by_repo[repo_name] = []
                    pulls_by_repo[repo_name].append(pull)

                # Debug: Check grouped data
                for repo_name, repo_pulls in pulls_by_repo.items():
                    with_issues = sum(1 for p in repo_pulls if p.get('resolved_issues') and len(p['resolved_issues']) > 0)
                    logger.info(f"Repo {repo_name}: {len(repo_pulls)} PRs, {with_issues} with resolved_issues")

                # Create tasks for all PRs
                tasks = []
                for repo_name, repo_pulls in pulls_by_repo.items():
                    # Create repo object with async session
                    if repo_name not in repos:
                        owner, name = repo_name.split("/")
                        repos[repo_name] = Repo(
                            owner=owner,
                            name=name,
                            token=token,
                            language=language,
                            session=session,
                            file_lock=file_lock
                        )

                    repo = repos[repo_name]

                    # Create tasks for this repo
                    for i, pull in enumerate(repo_pulls):
                        # CRITICAL: Use deepcopy to avoid shared mutable state
                        # Shallow copy (.copy()) is NOT sufficient because nested lists
                        # like 'resolved_issues' would still be shared references
                        pull_copy = copy.deepcopy(pull)

                        # Create coroutine with deep-copied pull data
                        coro = process_single_pr(
                            pull=pull_copy,
                            repo=repo,
                            output_path=output,
                            mode=mode,
                            cutoff_date=cutoff_date,
                            seen_prs=seen_prs,
                            successful_instances=successful_instances,
                            all_output_file=all_output_file,
                            output_file=output_file,
                            file_lock=file_lock,
                            stats=stats,
                            semaphore=semaphore
                        )
                        tasks.append(coro)

                # Execute all tasks concurrently
                logger.info(f"Starting concurrent processing of {len(tasks)} PRs...")
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # Log any exceptions
                error_count = 0
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        error_count += 1
                        if error_count <= 5:  # Log first 5 errors
                            logger.error(f"Task {i} failed with exception: {result}")

                if error_count > 0:
                    logger.warning(f"Total tasks with exceptions: {error_count}/{len(results)}")

    logger.info(
        f"Total instances: {total_instances}, "
        f"processed: {stats['total_processed']}, "
        f"valid pulls: {stats['valid_pulls']}, "
        f"completed: {stats['completed']}, "
        f"with tests: {stats['with_tests']}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("pr_file", type=str, help="Path to pull request JSONL file")
    parser.add_argument("output", type=str, help="Output file name")
    parser.add_argument("--token", type=str, help="GitHub token")
    parser.add_argument("--mode", type=str, default='omnigirl', help="collecting mode")
    parser.add_argument("--cutoff_date", type=str, default="2025-03-31T23:59:59Z", help="Cutoff date for filtering PRs in YYYY-MM-DDTHH:MM:SSZ format")
    parser.add_argument("--language", type=str, help="language")
    parser.add_argument("--max_concurrency", type=int, default=20, help="Maximum number of concurrent tasks")

    args = parser.parse_args()
    print(">>> reached main()")

    # Run async main
    asyncio.run(main(**vars(args)))
