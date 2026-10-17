# Changjiang Yuketang adapter data contract

| Metadata | Value |
| --- | --- |
| site_id | cjyuketang |
| Initial adapter_version | 0.1.0 |
| Initial schema_version | 1.0 |

The crawler reads cjyuketang.url and cjyuketang.cookie from AutoLearn's per-user config.json (`%LOCALAPPDATA%\AutoLearn\config.json` on Windows). Cookies are optional at crawl time and remain runtime request data. To capture them in the persistent browser profile, run:

    python -m core.auth.cookie_manager cjyuketang

## Interaction

Normal submission is started from the AutoLearn task page. The shared runner requires its WebUI confirmation callback before it will open a normal interaction session. A direct non-WebUI run cannot submit. For a non-submitting interaction check in a visible browser, use:

    python -m core.interaction.runner <run_id> --fill-only

If `execution_result.json` already exists, add `--overwrite` only when intentionally rerunning this package.

The task page passes the task-specific answer-page URL and a backend credential provider to the adapter; Cookie values stay in the backend and browser context. Run packages are stored under the per-user `runs/` directory. The adapter requires Scrapling's visible browser components in the same Python environment as the runner. Before changing the page, it checks that the live question count/order, saved selectors, answer controls, and control types match the package. Structural mismatches stop before filling; different current answers are reconciled to the validated `answers.json` targets.

The answer page hydrates asynchronously. The adapter waits for its saved root to contain the expected number of `.exercise-item` nodes before checking captured selectors. It validates page structure, control ownership, and editability before changing anything. The captured live values are state snapshots, not answer sources: controls are reconciled to `answers.json`, replacing any different current value while leaving values already equal to the target untouched. In `--fill-only` mode it uses a visible browser, fills controls, verifies their echoes, never looks up or invokes the site's submit control, and keeps the window open until the user closes it. Successful checks are recorded as `filled_unsubmitted`. Possible autosave activity is reported in the result reason.

After full preflight, filling, and echo verification, the backend persists `awaiting_submit_confirmation` and sends the WebUI a structured request containing `task_id`, `run_id`, a one-time `confirmation_id`, and its expiry. The visible Scrapling page and worker call remain paused while the WebUI task page displays the confirmation. Only a matching, unexpired one-time decision can resume it. On confirmation, the adapter re-reads the current page and verifies the unique, visible, enabled submit control belongs to the current Rain Classroom page before clicking. Cancel, expiry, duplicate or mismatched decisions, and application loss fail closed without submission. Website outcomes in `execution_result.json` come from page evidence; the UI confirmation token is not written there. Possible autosave activity on cancellation is recorded as `unconfirmed`; inspect the page before retrying. Existing result files are not overwritten unless the runner is deliberately called with `--overwrite`.

## Question package

questions.json contains normalized question content, options, and answer slots. It recognizes single-choice, multiple-choice, true/false, fill-in, and short-answer labels. True/false questions use the single_choice answer shape and retain 判断题 in source_type. A generic 选择题 label is classified as single- or multiple-choice only when its radio or checkbox controls make the distinction clear; otherwise crawling stops safely.

The current tmp.html example set contains single-choice, true/false, and fill-in questions; it does not contain a multi-select example. The multi-select branch is therefore based on the source label and checkbox control contract and still needs confirmation against a real multi-select page.

Question and option content stores readable text, sanitized html, and run-relative asset_paths. Image references are downloaded to the run package's assets folder and rewritten to local paths. A failed, non-image, or inaccessible image prevents package JSON files from being written. Scripts, active controls, hidden values, and remote image references are not copied into question content.

The page's currently selected choices and current text-field values are recorded only in interaction.json; they are not model answers. The file stores unique CSS selectors for the question and its controls. The captured page has no HTML form, so the interaction metadata records the page content container rather than pretending there is a form.

## Answers

answers.json uses the same question IDs and option or slot IDs as questions.json. A single-choice answer references one option_id. A multiple-choice answer contains a non-empty values array of option_id values. A fill-in answer maps every slot_id to plain answer text. See schemas/ for the exact JSON contracts.

## Run the crawler

From the project root:

    python -m core.adapters.cjyuketang.crawler

Optional --run-id and --output-dir values control the package name and destination. The default run ID uses Hong Kong time and avoids same-second collisions. The crawler uses Scrapling's synchronous FetcherSession because the supplied HTML contains the question content without JavaScript execution. Run scrapling[fetchers] and the crawler with the same Python interpreter.
