import json
import os
from datetime import datetime
from typing import Optional

import requests
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SLACK_BOT_TOKEN = os.environ["INPUT_SLACK_BOT_TOKEN"]
SLACK_CHANNEL = os.environ["INPUT_SLACK_CHANNEL"]
GITHUB_TOKEN = os.environ["INPUT_GITHUB_TOKEN"]
REPO_OWNER = os.environ["GITHUB_REPOSITORY"].split("/")[0]
REPO_NAME = os.environ["GITHUB_REPOSITORY"].split("/")[1]
RUN_ID = os.environ["GITHUB_RUN_ID"]
SEND_SUCCESS_MESSAGE = os.environ.get("INPUT_SEND_SUCCESS_MESSAGE", "false").lower() == "true"

# New optional behavior flags
THREAD_BY_PR = os.environ.get("INPUT_THREAD_BY_PR", "true").lower() == "true"
NOTIFY_PR_AUTHOR = os.environ.get("INPUT_NOTIFY_PR_AUTHOR", "true").lower() == "true"

# Optional JSON mapping: {"githublogin":"U12345678"}
GITHUB_TO_SLACK_MAP = os.environ.get("INPUT_GITHUB_TO_SLACK_MAP", "{}")

client = WebClient(token=SLACK_BOT_TOKEN)


def get_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def github_get(url):
    response = requests.get(url, headers=get_headers(), timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def get_workflow_run(run_id):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/actions/runs/{run_id}"
    return github_get(url)


def get_workflow_run_jobs(run_id):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/actions/runs/{run_id}/jobs"
    data = github_get(url)
    return data["jobs"] if data else []


def get_previous_workflow_run(repo_owner, repo_name, run_id, branch, headers):
    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/actions/runs?per_page=10"
    if branch:
        url += f"&branch={branch}"
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    runs = response.json()["workflow_runs"]
    for run in runs:
        if run["id"] != int(run_id):
            return run
    return None


def get_previous_same_run_number_workflow_run_with_failure(workflow_id, current_run_number):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/actions/workflows/{workflow_id}/runs?status=completed"
    response = requests.get(url, headers=get_headers(), timeout=30)
    response.raise_for_status()
    runs = response.json()["workflow_runs"]
    for run in runs:
        if run["run_number"] == current_run_number and run["conclusion"] == "failure":
            return run
    return None


def get_pull_requests_for_commit(commit_sha):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/commits/{commit_sha}/pulls"
    data = github_get(url)
    return data if isinstance(data, list) else []


def pick_best_pr(prs):
    if not prs:
        return None

    merged_prs = [pr for pr in prs if pr.get("merged_at")]
    if merged_prs:
        merged_prs.sort(key=lambda pr: pr["merged_at"], reverse=True)
        return merged_prs[0]

    prs.sort(key=lambda pr: pr.get("updated_at", ""), reverse=True)
    return prs[0]


def convert_duration(seconds):
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {seconds}s"


def parse_github_to_slack_map():
    try:
        parsed = json.loads(GITHUB_TO_SLACK_MAP)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def resolve_author_reference(pr_author_login):
    if not pr_author_login:
        return "`unknown`"

    if not NOTIFY_PR_AUTHOR:
        return f"`{pr_author_login}`"

    mapping = parse_github_to_slack_map()
    slack_user_id = mapping.get(pr_author_login)

    if slack_user_id:
        return f"<@{slack_user_id}>"

    return f"`{pr_author_login}`"


def summarize_failed_jobs(jobs):
    failed = [job["name"] for job in jobs if job.get("conclusion") == "failure"]
    if not failed:
        return "`unknown`"
    if len(failed) <= 5:
        return ", ".join(f"`{name}`" for name in failed)
    return ", ".join(f"`{name}`" for name in failed[:5]) + f" and {len(failed) - 5} more"


def find_thread_ts(thread_key):
    if not THREAD_BY_PR:
        return None

    try:
        cursor = None
        while True:
            response = client.conversations_history(
                channel=SLACK_CHANNEL,
                limit=200,
                cursor=cursor,
            )
            messages = response.get("messages", [])
            for message in messages:
                text = message.get("text", "")
                if thread_key in text:
                    return message.get("ts")

            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

    except SlackApiError as e:
        print(f"Error looking up thread in Slack history: {e}")

    return None


def create_thread_root(thread_key, repo_slug, pr_number, pr_title, pr_url, author_reference):
    text = (
        f"{thread_key}\n"
        f"🧵 Deploy thread created for *PR #{pr_number}*\n"
        f"*Repo:* `{repo_slug}`\n"
        f"*PR:* <{pr_url}|#{pr_number} - {pr_title}>\n"
        f"*Author:* {author_reference}"
    )

    try:
        response = client.chat_postMessage(
            channel=SLACK_CHANNEL,
            text=text,
            unfurl_links=False,
            unfurl_media=False,
        )
        return response["ts"]
    except SlackApiError as e:
        print(f"Error creating Slack thread root: {e}")
        return None


def send_slack_notification(message, thread_ts=None):
    try:
        kwargs = {
            "channel": SLACK_CHANNEL,
            "text": message,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        client.chat_postMessage(**kwargs)
        destination = f"thread {thread_ts}" if thread_ts else f"channel {SLACK_CHANNEL}"
        print(f"Notification sent to Slack {destination}.")
    except SlackApiError as e:
        print(f"Error sending Slack notification: {e}")
        raise


def get_or_create_thread(repo_slug, pr_number, pr_title, pr_url, author_reference):
    if not THREAD_BY_PR or not pr_number:
        return None

    thread_key = f"[pr-thread:{pr_number}]"

    existing_thread_ts = find_thread_ts(thread_key)
    if existing_thread_ts:
        return existing_thread_ts

    return create_thread_root(
        thread_key=thread_key,
        repo_slug=repo_slug,
        pr_number=pr_number,
        pr_title=pr_title,
        pr_url=pr_url,
        author_reference=author_reference,
    )


current_workflow_run = get_workflow_run(RUN_ID)
if not current_workflow_run:
    raise RuntimeError(f"Workflow run {RUN_ID} not found")

current_jobs = get_workflow_run_jobs(RUN_ID)
workflow_name = current_workflow_run["name"]
commit_sha = current_workflow_run["head_sha"]
commit_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/commit/{commit_sha}"
workflow_url = current_workflow_run["html_url"]
repo_slug = f"{REPO_OWNER}/{REPO_NAME}"

branch = current_workflow_run["head_branch"]
previous_workflow_run = get_previous_workflow_run(
    REPO_OWNER,
    REPO_NAME,
    RUN_ID,
    branch,
    get_headers(),
)

workflow_id = current_workflow_run["workflow_id"]
current_run_number = current_workflow_run["run_number"]
previous_same_run_number_workflow_run_with_failure = (
    get_previous_same_run_number_workflow_run_with_failure(
        workflow_id,
        current_run_number,
    )
)

prs = get_pull_requests_for_commit(commit_sha)
pr = pick_best_pr(prs)

pr_number = pr.get("number") if pr else None
pr_title = pr.get("title") if pr else None
pr_url = pr.get("html_url") if pr else None
pr_author_login = pr.get("user", {}).get("login") if pr else None
author_reference = resolve_author_reference(pr_author_login)
thread_ts = get_or_create_thread(repo_slug, pr_number, pr_title, pr_url, author_reference)

created_at = datetime.strptime(current_workflow_run["created_at"], "%Y-%m-%dT%H:%M:%SZ")
updated_at = datetime.strptime(current_workflow_run["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
duration_seconds = int((updated_at - created_at).total_seconds())
duration_str = convert_duration(duration_seconds)

has_failed_jobs = any(job.get("conclusion") == "failure" for job in current_jobs)
failed_jobs_summary = summarize_failed_jobs(current_jobs)

if has_failed_jobs:
    if pr_number and pr_url:
        message = (
            f":x: Workflow *{workflow_name}* has failed jobs.\n"
            f"*Repo:* `{repo_slug}`\n"
            f"*PR:* <{pr_url}|#{pr_number} - {pr_title}>\n"
            f"*Author:* {author_reference}\n"
            f"*Failed jobs:* {failed_jobs_summary}\n"
            f"*Commit:* <{commit_url}|{commit_sha[:7]}>\n"
            f"*Workflow:* <{workflow_url}|Link>"
        )
    else:
        message = (
            f":x: Workflow *{workflow_name}* has failed jobs.\n"
            f"*Repo:* `{repo_slug}`\n"
            f"*Failed jobs:* {failed_jobs_summary}\n"
            f"*Commit:* <{commit_url}|{commit_sha[:7]}>\n"
            f"*Workflow:* <{workflow_url}|Link>"
        )

    send_slack_notification(message, thread_ts=thread_ts)

elif SEND_SUCCESS_MESSAGE and not has_failed_jobs:
    if (
        (previous_workflow_run and previous_workflow_run["conclusion"] == "failure")
        or previous_same_run_number_workflow_run_with_failure
    ):
        if pr_number and pr_url:
            message = (
                f":white_check_mark: Workflow *{workflow_name}* has succeeded after previous failure.\n"
                f"*Repo:* `{repo_slug}`\n"
                f"*PR:* <{pr_url}|#{pr_number} - {pr_title}>\n"
                f"*Author:* {author_reference}\n"
                f"*Commit:* <{commit_url}|{commit_sha[:7]}>\n"
                f"*Workflow:* <{workflow_url}|Link>\n"
                f"*Build Duration:* {duration_str}"
            )
        else:
            message = (
                f":white_check_mark: Workflow *{workflow_name}* has succeeded after previous failure.\n"
                f"*Repo:* `{repo_slug}`\n"
                f"*Commit:* <{commit_url}|{commit_sha[:7]}>\n"
                f"*Workflow:* <{workflow_url}|Link>\n"
                f"*Build Duration:* {duration_str}"
            )

        send_slack_notification(message, thread_ts=thread_ts)