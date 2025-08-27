# codex-github-actions

GitHub のコメントに `/codex` などのプレフィックスで指示を書くと、OpenAI API（Codex/LLM）に投げて、結果をそのスレッドへ返信（必要に応じて @mention 付き）する再利用可能ワークフロー一式です。

## 仕組み

- 再利用可能ワークフロー: `.github/workflows/codex-resolver.yml`
- コンポジットアクション: `.github/actions/codex-replier`（中で Python のワンライナーを実行）
- 判定: コメントが `action: created` かつ本文が `trigger_prefix` で始まる場合のみ実行
- 呼び出しは OpenAI Responses API（`/v1/responses`）。返信は Markdown。`mention_author` が true の場合は投稿者を @mention

CLI（`@openai/codex`）→ API のフォールバックに対応しています。CLI が動けば CLI 経由、失敗/未導入なら自動で API 直呼びに切り替わります。

## 使い方（呼び出し側）

リポジトリ側に、コメントイベントで発火するワークフローを作成し、`uses: takashi-uchida/codex-github-actions@main` でシンプルに呼び出します（zudsniper/codex-action@main 風）。

```yaml
# .github/workflows/on-comment.yml（呼び出し側リポジトリ）
name: On Comment
on:
  issue_comment:
    types: [created]

jobs:
  resolve:
    permissions:
      contents: read
      issues: write
      pull-requests: write
    runs-on: ubuntu-latest
    steps:
      - name: Run Codex action
        uses: takashi-uchida/codex-github-actions@main
        with:
          trigger_prefix: ${{ vars.CODEX_TRIGGER || '/codex' }}
          model:          ${{ vars.LLM_MODEL     || 'o4-mini' }}
          mention_author: ${{ vars.MENTION_AUTHOR || true }}
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

コメント例:

```
/codex このPRのテストが落ちた原因を要約して
```

## 権限とシークレット

- `permissions` は `issues: write` および `pull-requests: write` を付与済み（返信投稿のため）
- OpenAI の API キーを `OPENAI_API_KEY` として渡してください

## カスタマイズ

- `trigger_prefix`: 既定は `/codex`。変更可能
- `model`: `o4-mini` や `gpt-4.1` など任意
- `mention_author`: `true/false` で投稿者へのメンションの有無

### CLI フォールバックのテンプレート（任意）

- 既定の実行コマンドは以下の順で試行します（`--` は npx のオプション終端）。現行の `@openai/codex` は `--model/-m` を受け付けないため、モデル指定は行いません（CLI 既定モデルを使用）。
  1. `printf %s {prompt} | npx -y @openai/codex@latest --`
  2. `npx -y @openai/codex@latest -- {prompt}`

- CLIを無効化したい場合は `CODEX_CLI_DISABLE=true` を設定してください（API直呼びのみ）。
- カスタムしたい場合は、環境変数 `CODEX_CLI_TEMPLATE` を設定してください（ジョブ全体の `env:` やリポジトリ変数でOK）。
- テンプレート内で `{prompt}`（および互換のため `{model}`）が置換されます。例:

```yaml
env:
  CODEX_CLI_TEMPLATE: "printf %s {prompt} | npx -y @openai/codex@latest -- --no-color"
```

CLI が失敗（非ゼロ終了・出力なし）の場合は自動で API にフォールバックします。モデル指定が必要な場合は、CLI を無効化して API 側で指定してください。

### API フォールバックとモデル

- 既定では `Responses API (/v1/responses)` に投げ、テキスト抽出に失敗した場合のみ Chat Completions にフォールバックします。
- `o*` 系（例: `o4-mini`）は Responses 専用のため、Chat フォールバック時は既定で `gpt-4o-mini` に切り替えます。
- 必要に応じて環境変数で調整できます。
  - `CODEX_CHAT_FALLBACK_MODEL`: Chat フォールバック時のモデル（既定: `gpt-4o-mini`）
  - `CODEX_DISABLE_CHAT_FALLBACK`: `true` で Chat フォールバックを無効化

## 代替: 再利用可能ワークフロー（OpenHands風）

OpenHands風に `workflow_call` を使いたい場合は、次の書式も利用できます。

```yaml
jobs:
  resolve:
    uses: takashi-uchida/codex-github-actions/.github/workflows/codex-resolver.yml@main
    with:
      trigger_prefix: ${{ vars.CODEX_TRIGGER || '/codex' }}
      model:          ${{ vars.LLM_MODEL     || 'o4-mini' }}
      mention_author: ${{ vars.MENTION_AUTHOR || true }}
    secrets:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

## 既知の制約

- 現状は issue_comment（通常コメント）にフォーカスしています。レビューコメント固有のスレッド返信などが必要な場合は拡張が必要です。
- OpenAI からのレスポンス仕様は将来的に変更される可能性があるため、`output_text` が無い場合のフォールバックを複数用意しています。
