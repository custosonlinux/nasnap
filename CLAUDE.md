# NaSnap — Assistant Instructions

## Language: English only

All UI text, log messages, error/exception strings, code comments, and
docstrings in this repository MUST be English — no exceptions, no matter how
minor the string.

The **only** allowed exception is the in-app Help content
(`plugins/netapp_storage/ui.html`, the `_helpData` object and the
`showTabHelp`/`setHelpLang` toggle) — that one is intentionally bilingual
(`en`/`de`) and may stay that way.

Do not add a language switcher or translated strings for the main UI
(buttons, labels, table headers, toasts, confirm dialogs, wizard steps,
etc.) — `plugins/netapp_storage/ui.html` used to have a full `de` block in
`_I18N` plus `fr`/`es`/`pt`/`ko`/`it` blocks; the `de` block was removed
(2026-09-09) because it was unreachable dead code (no UI to switch language,
default was always `en`) and it violated this rule. The other unused
language blocks (`fr`/`es`/`pt`/`ko`/`it`) were left in place as they were
out of scope for that cleanup — treat them as dead weight, not as a pattern
to extend.

When writing or editing any file in this repo, if you catch yourself typing
a German (or other non-English) word into anything user-facing or into a log
message, stop and use English instead.
