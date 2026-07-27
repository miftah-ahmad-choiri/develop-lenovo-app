# Plan: Apply IBM Plex Sans Google Font to All HTML Pages

## Overview

All 12 page templates in this project extend a single base template (`app/templates/base.html`).
The goal is to load **IBM Plex Sans** from Google Fonts and apply it as the primary body font.
Because of the single-base architecture, **only `base.html` needs to be changed** — all pages inherit the font automatically.

---

## Sub-Tasks

### Sub-Task 1 — Add Google Fonts link and update body font-family in `base.html`

- **Status:** `[x] done`

**Intent**
Load IBM Plex Sans (weights 300–700) from Google Fonts and replace the current system-font stack on `body` so every page uses the new font without any per-page changes.

**Expected Outcomes**
- A `<link>` preconnect pair and a Google Fonts stylesheet `<link>` for IBM Plex Sans appear in the `<head>` of `base.html`, before the `<style>` block.
- The `body { font-family: ... }` declaration includes `"IBM Plex Sans"` as the first value.
- All 12 pages that extend `base.html` render with IBM Plex Sans automatically.

**Todo List**
1. Open `app/templates/base.html`.
2. Insert the following three `<link>` tags immediately before the opening `<style>` tag (after line 6):
   ```html
   <link rel="preconnect" href="https://fonts.googleapis.com" />
   <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
   <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
   ```
3. Update the `body` `font-family` declaration (currently on line 11) to:
   ```css
   font-family: "IBM Plex Sans", -apple-system, "Segoe UI", system-ui, sans-serif;
   ```

**Relevant Context**
- File: `app/templates/base.html`, lines 3–17 (head / body style)
- Current font declaration: `font-family: -apple-system, "Segoe UI", system-ui, sans-serif;` (line 11)
- No other HTML files need touching — all extend `base.html` via `{% extends "base.html" %}`

---

## Files Affected

| File | Change |
|------|--------|
| `app/templates/base.html` | Add Google Fonts `<link>` tags + update `body` font-family |

## Non-Goals

- No changes to individual page templates.
- No changes to monospace font stacks used for technical data (serial numbers, part numbers, etc.).
- No CSS refactoring or color/layout changes.
