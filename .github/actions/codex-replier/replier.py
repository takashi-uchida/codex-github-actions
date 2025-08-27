import json
import os
import sys
from pathlib import Path
import subprocess
from typing import Optional
import urllib.request


def e(name: str, default=None):
    v = os.environ.get(name, default)
    if v is None:
        print(f"::debug::Missing env {name}")
    return v


def load_event():
    event_path = e('GITHUB_EVENT_PATH')
    if not event_path or not Path(event_path).exists():
        print("::notice title=Codex Replier::No event payload found; nothing to do")
        sys.exit(0)
    with open(event_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def extract_reply_text(resp_json: dict) -> str:
    if not isinstance(resp_json, dict):
        return "(No text response received from the model.)"
    # Preferred field for Responses API
    reply_text = resp_json.get('output_text')
    if reply_text:
        return reply_text
    # Some SDKs wrap under 'response'
    wrapped = resp_json.get('response') or {}
    if isinstance(wrapped, dict):
        if wrapped.get('output_text'):
            return wrapped['output_text']
        # message-like output under response.output
        wout = wrapped.get('output') or []
        if isinstance(wout, list) and wout:
            content = (wout[0] or {}).get('content') or []
            if isinstance(content, list):
                for c in content:
                    if c.get('type') == 'output_text' and c.get('text'):
                        return c['text']
    # Fallback to chat shape
    choices = resp_json.get('choices') or []
    if choices:
        msg = (choices[0] or {}).get('message') or {}
        content = (msg.get('content') or '').strip()
        if content:
            return content
    # Fallback to output blocks
    output = resp_json.get('output') or []
    if output:
        content = (output[0] or {}).get('content') or []
        if isinstance(content, list):
            for c in content:
                if c.get('type') == 'output_text' and c.get('text'):
                    return c['text']
    return "(No text response received from the model.)"


def call_openai(prompt: str, model: str, openai_key: str) -> str:
    if (os.environ.get('CODEX_DRY_RUN', '').lower() in ('1','true','yes','on')):
        return f"(dry-run) prompt: {prompt} | model: {model}"
    payload = {"model": model, "input": prompt}
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        'https://api.openai.com/v1/responses',
        data=data,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {openai_key}',
            'User-Agent': 'codex-replier-action/1.0',
        },
        method='POST',
    )
    print("::group::Calling OpenAI")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            resp_json = json.loads(body)
    except urllib.error.HTTPError as http_err:
        # Surface HTTP error body for easier debugging
        try:
            err_body = http_err.read().decode('utf-8', errors='replace')
        except Exception:
            err_body = str(http_err)
        print("::endgroup::")
        print(f"::error title=Codex Replier::OpenAI /v1/responses failed {http_err.code}: {err_body[:800]}")
        sys.exit(1)
    except Exception as exc:
        print("::endgroup::")
        print(f"::error title=Codex Replier::OpenAI call failed: {exc}")
        sys.exit(1)
    print("::endgroup::")
    # If API returned an error, surface it
    if isinstance(resp_json, dict) and resp_json.get('error'):
        err = resp_json['error']
        msg = err.get('message') or str(err)
        return f"(OpenAI error) {msg}"
    text = extract_reply_text(resp_json)
    if text and text.strip() and text.strip() != "(No text response received from the model.)":
        return text
    # Decide chat fallback model. Some models (e.g., o*-family) are Responses-only.
    disable_chat_fb = (os.environ.get('CODEX_DISABLE_CHAT_FALLBACK','').lower() in ('1','true','yes','on'))
    fb_model = os.environ.get('CODEX_CHAT_FALLBACK_MODEL', 'gpt-4o-mini')
    # If requested model looks like o*-family, prefer switching to fallback chat model
    chat_model = fb_model if any(model.startswith(prefix) for prefix in ("o1","o2","o3","o4")) else model
    if disable_chat_fb:
        return "(No text response received from the model.)"
    # Fallback: try Chat Completions for broader compatibility
    chat_payload = {
        "model": chat_model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
    }
    chat_req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=json.dumps(chat_payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {openai_key}',
            'User-Agent': 'codex-replier-action/1.0',
        },
        method='POST',
    )
    print("::group::Calling OpenAI (chat.fallback)")
    try:
        with urllib.request.urlopen(chat_req, timeout=120) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            chat_json = json.loads(body)
    except urllib.error.HTTPError as http_err:
        try:
            err_body = http_err.read().decode('utf-8', errors='replace')
        except Exception:
            err_body = str(http_err)
        print("::endgroup::")
        print(f"::notice title=Codex Replier::Chat fallback failed {http_err.code}: {err_body[:800]}")
        return f"(OpenAI error) chat fallback {http_err.code}: {err_body[:200]}"
    except Exception as exc:
        print("::endgroup::")
        print(f"::notice title=Codex Replier::Chat fallback failed: {exc}")
        return "(No text response received from the model.)"
    print("::endgroup::")
    if isinstance(chat_json, dict) and chat_json.get('error'):
        msg = chat_json['error'].get('message') or str(chat_json['error'])
        return f"(OpenAI error) {msg}"
    try:
        return (chat_json.get('choices') or [{}])[0].get('message', {}).get('content', '').strip() or "(No text response received from the model.)"
    except Exception:
        return "(No text response received from the model.)"


def shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def try_cli(prompt: str, model: str) -> Optional[str]:
    if (os.environ.get('CODEX_CLI_DISABLE', '').lower() in ('1','true','yes','on')):
        print("::notice title=Codex Replier::CLI disabled by CODEX_CLI_DISABLE")
        return None
    # Allow explicit override via CODEX_CLI_TEMPLATE
    override = os.environ.get('CODEX_CLI_TEMPLATE')
    templates = []
    if override:
        templates.append(override)
    else:
        # Use conservative patterns. Current @openai/codex does not accept a --model flag.
        templates = [
            # Pipe prompt on stdin; rely on CLI default model.
            "printf %s {prompt} | npx -y @openai/codex@latest --",
            # Positional prompt as fallback.
            "npx -y @openai/codex@latest -- {prompt}",
        ]

    env = os.environ.copy()
    for tmpl in templates:
        cmd = tmpl.replace('{model}', shquote(model)).replace('{prompt}', shquote(prompt))
        print(f"::group::Trying CLI: {tmpl}")
        try:
            completed = subprocess.run(
                ["bash", "-lc", cmd],
                capture_output=True,
                text=True,
                env=env,
                timeout=180,
            )
        except Exception as exc:
            print("::endgroup::")
            print(f"::notice title=Codex Replier::CLI attempt failed: {exc}")
            continue

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            print("::endgroup::")
            print(f"::notice title=Codex Replier::CLI exited with {completed.returncode}: {stderr[:400]}")
            continue
        print("::endgroup::")
        if not stdout:
            print("::notice title=Codex Replier::CLI produced no output; trying next pattern")
            continue
        return stdout
    print("::notice title=Codex Replier::All CLI patterns failed; falling back to API")
    return None


def post_comment(owner: str, repo_name: str, number: int, body_md: str, gh_token: str):
    if (os.environ.get('CODEX_DRY_RUN', '').lower() in ('1','true','yes','on')):
        print(f"::notice title=Codex Replier(DRY-RUN)::Would post to {owner}/{repo_name}#${number}:\n{body_md}")
        return
    post_url = f"https://api.github.com/repos/{owner}/{repo_name}/issues/{number}/comments"
    post_payload = json.dumps({"body": body_md}).encode('utf-8')
    post_req = urllib.request.Request(
        post_url,
        data=post_payload,
        headers={
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {gh_token}',
            'X-GitHub-Api-Version': '2022-11-28',
            'Content-Type': 'application/json',
            'User-Agent': 'codex-replier-action/1.0',
        },
        method='POST',
    )
    print("::group::Posting reply to GitHub")
    try:
        with urllib.request.urlopen(post_req, timeout=60) as resp:
            _ = resp.read()
    except urllib.error.HTTPError as http_err:
        try:
            err_body = http_err.read().decode('utf-8', errors='replace')
        except Exception:
            err_body = str(http_err)
        print("::endgroup::")
        print(f"::error title=Codex Replier::Failed to post comment ({http_err.code}): {err_body[:800]}\nIf this is 403, ensure the workflow/job grants: contents: read, issues: write, pull-requests: write. Also avoid fork-origin events with restricted tokens when testing.")
        sys.exit(1)
    except Exception as exc:
        print("::endgroup::")
        print(f"::error title=Codex Replier::Failed to post comment: {exc}\nIf this is HTTP 403, ensure the caller workflow has permissions: issues: write and pull-requests: write.")
        sys.exit(1)
    print("::endgroup::")
    print("::notice title=Codex Replier::Reply posted successfully")


def main():
    event = load_event()

    action = event.get('action')
    comment = (event.get('comment') or {}).get('body') or ''
    commenter = ((event.get('comment') or {}).get('user') or {}).get('login') or ''
    issue = event.get('issue') or {}

    number = (issue.get('number')
              or (event.get('issue') or {}).get('number')
              or (event.get('pull_request') or {}).get('number'))

    repo = ((event.get('repository') or {}).get('full_name') or '')
    if '/' in repo:
        owner, repo_name = repo.split('/', 1)
    else:
        owner = (event.get('repository') or {}).get('owner', {}).get('login', '')
        repo_name = (event.get('repository') or {}).get('name', '')

    prefix = (e('INPUT_TRIGGER_PREFIX') or '/codex').strip()
    model = (e('INPUT_MODEL') or 'o4-mini').strip()
    mention = (e('INPUT_MENTION_AUTHOR') or 'true').strip().lower() in ('1', 'true', 'yes', 'on')

    if action != 'created':
        print(f"::notice title=Codex Replier::Event action '{action}' not 'created'; skipping")
        sys.exit(0)

    if not comment.strip().startswith(prefix):
        print(f"::notice title=Codex Replier::Comment does not start with prefix '{prefix}'; skipping")
        sys.exit(0)

    prompt = comment.strip()[len(prefix):].lstrip()
    if not prompt:
        print("::warning title=Codex Replier::Empty prompt after prefix; nothing to do")
        sys.exit(0)

    openai_key = e('OPENAI_API_KEY')
    if not openai_key:
        print("::error title=Codex Replier::Missing OPENAI_API_KEY secret")
        sys.exit(1)

    # Try CLI first, then API (unless dry-run)
    if (os.environ.get('CODEX_DRY_RUN', '').lower() in ('1','true','yes','on')):
        reply_text = call_openai(prompt=prompt, model=model, openai_key=openai_key)
    else:
        reply_text = try_cli(prompt=prompt, model=model)
        if not reply_text:
            reply_text = call_openai(prompt=prompt, model=model, openai_key=openai_key)

    mention_prefix = f"@{commenter} " if mention and commenter else ""
    body_md = f"{mention_prefix}{reply_text}"

    gh_token = e('GITHUB_TOKEN')
    if not gh_token:
        print("::error title=Codex Replier::Missing GITHUB_TOKEN")
        sys.exit(1)

    if not (owner and repo_name and number):
        print("::error title=Codex Replier::Cannot resolve repository/issue context to post comment")
        sys.exit(1)

    post_comment(owner=owner, repo_name=repo_name, number=number, body_md=body_md, gh_token=gh_token)


if __name__ == '__main__':
    main()
