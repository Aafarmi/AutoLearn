# Chaoxing adapter data contract

| Metadata | Value |
| --- | --- |
| `site_id` | `chaoxing` |
| Initial `adapter_version` | `0.1.0` |
| Initial `schema_version` | `1.0` |

All four package files carry these metadata fields and the same `run_id`. The schemas in `schemas/` define the Chaoxing-specific file shapes.

## Questions

`questions.json` contains the readable question content, stable question and option IDs, the original page `source_type`, and the answer-slot shape. `question_type` is the normalized answer shape: `single_choice`, `fill_blank`, or `short_answer`. Preserve the original Chaoxing label separately in `source_type`; a calculation prompt reported by the page as `简答题` can still be normalized as `fill_blank` with one answer slot.

Each `answer_slot` has a stable `slot_id`, an ordinal, and an `input_kind` (`text_input` or `rich_text_editor`). For example, a two-blank question has two slots such as `blank_1` and `blank_2`. Question and option IDs must remain stable across all files in one run; labels such as A and B may be used as option IDs when the page provides them.

Question and option content stores readable `text`, optional sanitized `html`, and run-relative `asset_paths`. Option content can be image-only: keep its label and sanitized image markup even when `text` is empty. Download every referenced image into the run's `assets/` directory and rewrite image references to local paths. If a referenced image cannot be downloaded, fail the crawl instead of writing an incomplete package. Do not carry scripts, inline event handlers, signed URLs, or remote image dependencies into the package.

Chaoxing image CDN requests use a safe page-origin `Referer` without the page URL's query string. Authentication cookies are sent only over HTTPS to `chaoxing.com` and its subdomains. The generated question package is self-contained: opening its sanitized HTML with the run directory as the asset root renders its question and option images from `assets/`.

## Interaction metadata

`interaction.json` maps each `question_id` to its page selector, choice controls, and answer-slot controls. Choice controls include stable option IDs and CSS selectors; `selected_option_id` is the page's current selection or `null`. Text controls include a selector, control type, and `current_value`; use an empty string when the field is blank. This records the page state observed by the crawler and is separate from both the question content sent to the solver and the target answers returned by it.

The form contract stores selectors such as `#submitForm`, not the form action URL or hidden submission values. The source page includes per-session/request values such as tokens and signatures; keep those in the live local session and never copy them into a run package.

## Answers and results

`answers.json` contains one entry per solved question. A single-choice answer references an `option_id`; a fill-blank answer maps each `slot_id` to plain answer text; a short-answer value is plain text for the editor. The solver output contains no explanation or HTML. Cross-file validation must confirm that every answer question and option/slot ID exists in `questions.json` and that fill-blank slots match the question's slots.

`execution_result.json` records one status per question: `confirmed`, `failed`, `unconfirmed`, `skipped`, or `filled_unsubmitted`. The last status means the fill-only path verified the page echoes without invoking the submit control. `confirmed` requires confirmation evidence from the page; a submit-button click alone is not evidence of success.

## Run a non-submitting interaction check

From the project root, run:

```powershell
python -m core.interaction.runner <run_id> --fill-only
```

Normal submission is started from the AutoLearn task page. The shared runner requires its WebUI confirmation callback before it will open a normal interaction session. A direct non-WebUI run cannot submit. The task page passes the task-specific HTTPS URL and a backend credential provider; Cookie values stay in the backend and browser context. The module uses Scrapling's `DynamicFetcher` in visible-browser mode. Install `scrapling[fetchers]` with the same Python interpreter used to run the command and install its browser with `scrapling install`.

This mode validates and reconciles the page controls to `answers.json`, verifies every target value, never looks up or invokes the site's submit control, and returns `filled_unsubmitted`. It uses a visible browser and keeps the window open until the user closes it. Possible autosave requests are reported separately from submission. Normal submission is started from the AutoLearn task page.

Before filling any answer, the adapter confirms that `#submitForm`, every question selector, every choice selector, and every answer selector still resolve to the expected elements inside the form. UEditor fields map from their saved textarea selectors to editor frames in the same local answer group. If a selector is missing, outside the form, or cannot be mapped one-to-one, the run stops before filling answers. Saved current values are state snapshots, not answer sources: after structural preflight, the adapter fills values from `answers.json`, skips controls already at their target, and verifies all controls before requesting WebUI confirmation.

After full preflight, filling, and echo verification, the backend persists `awaiting_submit_confirmation` and sends the WebUI a structured request containing `task_id`, `run_id`, a one-time `confirmation_id`, and its expiry. The visible Scrapling page and worker call remain paused while the WebUI task page displays the confirmation. Only a matching, unexpired one-time decision can resume it. On confirmation, the adapter re-reads the current page and verifies the unique, visible, enabled submit control belongs to the current Chaoxing `#submitForm` before clicking. Cancel, expiry, duplicate or mismatched decisions, and application loss fail closed without submission. Website outcomes in `execution_result.json` come from page evidence; the UI confirmation token is not written there.

The runner writes `execution_result.json` atomically and logs counts to `logs/interaction.log`. `confirmed` requires a success message, a result page, or a matching post-submit answer echo; a WebUI confirmation alone is not website evidence. `failed` requires an explicit rejection; `unconfirmed` means a save or submit request may have been sent without enough evidence to decide; `skipped` means no answer interaction was completed; `filled_unsubmitted` means fill-only completed without invoking submission. Check the live Chaoxing page before retrying an `unconfirmed` run. If an `execution_result.json` already exists, the runner refuses to rerun unless `--overwrite` is supplied; normal submission is available only through the WebUI confirmation flow.

## Saved run observations

The saved run package contains seven questions: two single-choice questions, two fill-in questions with rich-text editor slots, and three short-answer questions with rich-text editor slots. Chaoxing may initialize UEditor after the crawler receives the server-rendered HTML, so hidden textarea answer sources are recorded as rich-text controls and are matched to the live editor during interaction. At execution time, the adapter checks the current page against the run metadata and stops before filling if the controls do not match.
