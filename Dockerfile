FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENTRYPOINT ["sh", "-c", "export SLACK_BOT_TOKEN=\"$INPUT_SLACK_BOT_TOKEN\" && export SLACK_CHANNEL=\"$INPUT_SLACK_CHANNEL\" && export GITHUB_TOKEN=\"$INPUT_GITHUB_TOKEN\" && export SEND_SUCCESS_MESSAGE=\"$INPUT_SEND_SUCCESS_MESSAGE\" && export THREAD_BY_PR=\"$INPUT_THREAD_BY_PR\" && export NOTIFY_PR_AUTHOR=\"$INPUT_NOTIFY_PR_AUTHOR\" && export GITHUB_TO_SLACK_MAP=\"$INPUT_GITHUB_TO_SLACK_MAP\" && export REPO_OWNER=\"${GITHUB_REPOSITORY%/*}\" && export REPO_NAME=\"${GITHUB_REPOSITORY#*/}\" && export RUN_ID=\"$GITHUB_RUN_ID\" && python /app/main.py"]