# Sample UI screenshots

These four screenshots are embedded by `ARCHITECTURE_VIEW.html` and the
generated PDF.

| File | What it captures |
|------|------------------|
| 01-main-ui.png | The Argos UI in SUCCEEDED state. Compose panel on the left, two task plan on the right (T1 TOOL_CALL with translation 1242 of 1285, T2 SYNTHESIZE), planner reasoning streaming visible below. |
| 02-plan-mode.png | The Plan Mode clarifying question that fires when the user asks to translate all documents without specifying ACTIVE only or including PROCESSING and ERROR. Shows the two option buttons and the free text fallback. |
| 03-final-answer.png | The Final Answer panel rendered as markdown. Shows the Key Results table (5,140 total documents, 4,980 successful, 160 failed) and the By Container breakdown. |
| 04-event-log.png | The Event Log panel showing the SSE stream (52 events) and the clickable artifact URL pointing at the standalone HTML report. |

To replace or add screenshots, drop the file at the matching path and
reopen `ARCHITECTURE_VIEW.html` in any browser. The embedded `<img>`
tags resolve relative to the document, so the picture updates the next
time the page is reloaded.
