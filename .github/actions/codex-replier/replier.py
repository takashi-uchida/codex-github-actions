import json
import os
import sys
from pathlib import Path
import subprocess
from typing import Optional
import urllib.request
import urllib.parse


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


def boolish(v: str, default: bool = True) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ('1','true','yes','on')


def safe_trim(text: str, limit: int) -> str:
    if text is None:
        return ''
    s = str(text)
    return s if len(s) <= limit else s[: max(0, limit - 1)] + '…'


def fetch_thread_comments(owner: str, repo: str, number: int, gh_token: str, exclude_comment_id: Optional[int], limit: int = 5) -> list:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments?per_page=100"
    req = urllib.request.Request(
        url,
        headers={
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {gh_token}',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'codex-replier-action/1.0',
        },
        method='GET',
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            items = json.loads(body)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    filtered = []
    for it in items:
        if exclude_comment_id and it.get('id') == exclude_comment_id:
            continue
        filtered.append(it)
    filtered.sort(key=lambda x: x.get('created_at') or '')
    return filtered[-limit:]


def build_prompt(
    *,
    event: dict,
    owner: str,
    repo_name: str,
    number: int,
    user_request: str,
    model: str,
    include_metadata: bool,
    include_thread: bool,
    max_context_chars: int,
    max_thread_comments: int,
    system_prompt: str,
    gh_token: str,
) -> tuple[str, Optional[str], str]:
    # Returns (responses_input, system_for_chat, chat_user_content)
    blocks = []
    sys_msg = system_prompt.strip() if system_prompt else ''

    if include_metadata:
        repo_full = f"{owner}/{repo_name}" if owner and repo_name else (event.get('repository', {}).get('full_name') or '')
        issue = event.get('issue') or {}
        title = issue.get('title') or ''
        html_url = issue.get('html_url') or ''
        is_pr = bool(issue.get('pull_request'))
        kind = 'PR' if is_pr else 'Issue'
        meta = [
            f"Repo: {repo_full}",
            f"{kind}: #{number} {title}".strip(),
        ]
        if html_url:
            meta.append(f"URL: {html_url}")
        meta.append(f"Model: {model}")
        blocks.append("[Context]\n" + "\n".join(meta))

    if include_thread and gh_token and owner and repo_name and number:
        exclude_id = (event.get('comment') or {}).get('id')
        comments = fetch_thread_comments(owner, repo_name, number, gh_token, exclude_id, max_thread_comments)
        if comments:
            lines = []
            for c in comments:
                author = ((c.get('user') or {}).get('login') or 'unknown')
                body = c.get('body') or ''
                body = body.replace("\r\n", "\n").strip()
                body = safe_trim(body, 1200)
                lines.append(f"- {author}: {body}")
            thread_text = "\n".join(lines)
            blocks.append("[Recent Thread]\n" + thread_text)

    blocks.append("[User Request]\n" + user_request)

    user_block = blocks[-1]
    context_blocks = blocks[:-1]
    context_text = "\n\n".join(context_blocks) if context_blocks else ''
    if len(context_text) > max_context_chars:
        context_text = safe_trim(context_text, max_context_chars)
    full_no_sys = (context_text + ("\n\n" if context_text else '') + user_block).strip()

    if sys_msg:
        responses_input = f"[System]\n{sys_msg}\n\n" + full_no_sys
    else:
        responses_input = full_no_sys
    return responses_input, (sys_msg or None), full_no_sys


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


def call_openai(prompt: str, model: str, openai_key: str, system_for_chat: Optional[str] = None, chat_user_content: Optional[str] = None) -> str:
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
        "messages": ([{"role": "system", "content": system_for_chat}] if system_for_chat else []) + [
            {"role": "user", "content": (chat_user_content if chat_user_content else prompt)}
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
    comment_obj = (event.get('comment') or {})
    comment = comment_obj.get('body') or ''
    commenter = (comment_obj.get('user') or {}).get('login') or ''
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
    mention = boolish(e('INPUT_MENTION_AUTHOR') or 'true', True)
    system_prompt = e('INPUT_SYSTEM_PROMPT') or ''
    include_metadata = boolish(e('INPUT_INCLUDE_METADATA') or 'true', True)
    include_thread = boolish(e('INPUT_INCLUDE_THREAD_CONTEXT') or 'true', True)
    try:
        max_context_chars = int(e('INPUT_MAX_CONTEXT_CHARS') or '8000')
    except Exception:
        max_context_chars = 8000
    try:
        max_thread_comments = int(e('INPUT_MAX_THREAD_COMMENTS') or '5')
    except Exception:
        max_thread_comments = 5

    if action != 'created':
        print(f"::notice title=Codex Replier::Event action '{action}' not 'created'; skipping")
        sys.exit(0)

    if not comment.strip().startswith(prefix):
        print(f"::notice title=Codex Replier::Comment does not start with prefix '{prefix}'; skipping")
        sys.exit(0)

    raw_request = comment.strip()[len(prefix):].lstrip()
    if not raw_request:
        print("::warning title=Codex Replier::Empty prompt after prefix; nothing to do")
        sys.exit(0)

    openai_key = e('OPENAI_API_KEY')
    if not openai_key:
        print("::error title=Codex Replier::Missing OPENAI_API_KEY secret")
        sys.exit(1)

    # Build rich prompt
    responses_input, sys_for_chat, chat_user_content = build_prompt(
        event=event,
        owner=owner,
        repo_name=repo_name,
        number=number,
        user_request=raw_request,
        model=model,
        include_metadata=include_metadata,
        include_thread=include_thread,
        max_context_chars=max_context_chars,
        max_thread_comments=max_thread_comments,
        system_prompt=system_prompt,
        gh_token=e('GITHUB_TOKEN') or '',
    )

    # Try CLI first, then API (unless dry-run)
    if (os.environ.get('CODEX_DRY_RUN', '').lower() in ('1','true','yes','on')):
        reply_text = call_openai(prompt=responses_input, model=model, openai_key=openai_key, system_for_chat=sys_for_chat, chat_user_content=chat_user_content)
    else:
        reply_text = try_cli(prompt=responses_input, model=model)
        if not reply_text:
            reply_text = call_openai(prompt=responses_input, model=model, openai_key=openai_key, system_for_chat=sys_for_chat, chat_user_content=chat_user_content)

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
