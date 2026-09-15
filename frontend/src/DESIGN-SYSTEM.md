# Design system

The shell (`Shell.jsx`) plus the primitive layer (`ui.jsx`), the token sheet (`theme.css` + `theme.js`) and `screens/` are the app at `/`. The old interface lives one level down, unbroken, at `/classic/*` (`frontend/src/classic/`), sharing one backend. `/v2` and `/v2/*` redirect to the same path without the prefix, query and hash intact.

**Layout.** `frontend/src/` holds `App.jsx` (routes), `Shell.jsx` (light sidebar: five primary destinations, a quieter "More" group, scrape health, user + appearance), `ui.jsx`, `theme.css`, `theme.js`, `hooks.js`, `time.js`, `Toast.jsx`, `ConfirmDialog.jsx`, the three overlays (`LoginModal`, `WelcomeModal`, `NewUiModal`) and this file; one screen per file under `screens/`; Vitest specs under `__tests__/`; the v1 interface under `classic/`; the git-ignored lab pages under `design-base/`.

**Historical prefix.** The CSS root class is still `.jn-v2` and the hover hooks are still `v2-bd`, `v2-row`, `v2-card`, … — the redesign's staging names. They are load-bearing selectors in `theme.css`, matched by `ui.jsx` and by the style tooling, and renaming them is a paint risk for no gain, so they stay. Read `v2-` as "the design system", not "a second UI".

## Primitive layer (`ui.jsx`)

`ui.jsx` is the one file that draws a control; screens compose these exports instead of styling inline.

- **Actions** — `Button` (`variant`: primary/ai/danger/secondary/ghost; `size`: md/sm/xs; `as="button"` for a real `<button>`, `href`/`target`/`rel` for a real `<a>`; `busy` shows a `Spinner`, `disabled` swaps the disabled look), `Pill` (`on`, `size`: md/sm/xs, `hover` override, `line` line-height opt-out), `IconButton` (`size`: 26/36/25), `ToolbarTrigger` (`label`/`value`/`caret`/`open`, `size`: sm/md), `DashedAdd` (`big` variant).
- **Fields** — `Input`, `Textarea`, `SearchInput` (`variant`: boxed/underline), `Select` (trigger + listbox, `options` as `[[value, label]]`). All take `invalid` (`aria-invalid`, repaints border + error ring); `Input` also takes `adornment` (trailing in-box slot, e.g. a secret's show/hide `Link`) and `mono`.
- **Containers** — `Row` (`selected`, `flush`), `TableRow` (flat-table body row, `size`, `align`), `Card` (`interactive`), `Band` (dashed sibling), `ArchiveBand` (the "Archived · N" shelf), `Surface` (recessed block, `radius`: none/field/row/card/menu).
- **Overlays** — `Menu`/`MenuHead`/`MenuItem` (`MenuItem`: `icon`, `hint`, `selected`, `danger`, `divider`, `href`; `Menu`'s `onDismiss` mounts a click-swallowing backdrop), `ModalPanel` (`title`/`titlebar` name a themed caption bar when one exists, `as="form"`+`onSubmit`, `escape`/`escapeCapture`), `Drawer` (same caption contract, pane- not viewport-relative), `ChoiceCard`/`ChoiceRow`/`ChoiceModal` (the "pick one thing, then commit" shell behind Tailor/Re-tailor/Persona-import).
- **Structure** — `HeaderRow` (`variant`: modal/screen/compact, `line`: none/soft/strong, `bg`, plus `variant="titlebar"`), `FooterRow` (`variant`: modal/compact/wide), `TableHead`, `Rule` (bare hairline, `vertical`), `SectionHead` (`caret`: start/end/pin/false, `boxed`/`card`).
- **Status & text** — `Tag` (`tone`: none/neutral/accent/good/warn/bad/**ai**, `busy`), `Dot` (tones incl. `seg-on`/`seg-off`), `Chip`, `GlyphBadge` (`tone`: accent/bad/neutral/ai/outline/none, `on`, `mono`, `busy`), `Notice` (`tone`: warn/bad/quiet, `action`), `Check`/`Radio`/`Switch`/`Segmented` (options carry `dots`/`dotColor`/`tone`; `variant="inset"` is the framed two-cell toggle), `Meter` (0-1 fill), `ScoreRing` (`size` sm/34px or md/44px; `value`/`label`/`busy`; ring by default, or per-theme `pill`/`bar`/`ascii` — see "shape switches" below), `Spinner` (`weight="bold"`; a segmented block-loader instead of an arc when `--loader-style` is `blocks`).
- **Type** — `Label`, `Helper` (`size="xs"`, `onClick`), `Mono` (`code` for a fixed-advance run like a cron string or id; without it, a numeral run on `--numeral-face`), `Heading` (`strong` for the medium/semibold title face), `PageTitle`, `Link`/`NavLink`.
- **Glyphs** — `CopyGlyph`, `FlaskGlyph`, `CheckGlyph`, `CrossGlyph`: hand-drawn SVGs in `currentColor`, replacing Unicode symbols that fell back to missing-glyph boxes or an uncontrolled symbol font on Linux/Chromium.
- **Misc** — `ShowMore` (pager), `RemoveLink`/`RemoveX`/`MoveArrows` (list-row affordances), `ToastCard` (`kind`: progress/success/error/undo — the box only; `Toast.jsx` owns the taxonomy and stack).

Every primitive takes `style` (layout only) and `className` (appended after its own hover class). Interactive ones are keyboard-operable through the internal `kb()` helper (tab stop, role, Enter/Space) with the right `aria-*`.

**`busy`.** `Tag` and `GlyphBadge` take `busy`: the box, the ground and the ink stay exactly as they are and the *content* becomes an 11px `Spinner` in `currentColor` (`aria-busy="true"`). A badge that starts working must not change size or tone — it sits inline in a run of text, and a resize would move everything after it. Under `--loader-style: blocks` (win98) a `GlyphBadge` draws the segmented bar sized to itself (22×10 at size 22) rather than `Spinner`'s own bar, which has a 34px floor that would spill out of the round box.

**Shape switches.** Some primitives read a CSS custom property at runtime via `useThemeVar` to change what they *draw*, not just how they're painted: `ScoreRing` (`--ring-variant`: ring/pill/bar/ascii), `Spinner` (`--loader-style`: arc/blocks), `Check`/`Radio` (`--check-style`: default/win98 sunken-tick), `HeaderRow`/`ModalPanel`/`Drawer` (`--title-bar`, via `useTitleBar()`). Reads are cached per `(theme, appearance, name)`.

## Theming (`theme.css` + `theme.js`)

`theme.css` has two structural layers, each repeated for light and for `@media (prefers-color-scheme: dark)`, both scoped to `.jn-v2`: a **palette** (raw colours, shadows, ATS/search-mode badge hues `--cc-*`/`--sm-*`, the score-distribution ramp, toast tints), and a **semantic layer** — every name a primitive actually reads (`--btn-primary-bg`, `--pill-on-border`, `--input-border-focus`, `--row-hover`, `--menu-shadow`, `--radius-field`/`-row`/`-card`/`-menu`/`-modal`/`-control`, …), grouped under comment headers by role (type, radii, buttons, pills/icon buttons, fields, rows, cards/bands/dashed-adds, menus, modal+drawer, chips/tags/dots, links+text roles, structure+state, plus a few narrower families like ScoreRing bar geometry and Label's caps sizes). Each semantic name points at a palette token, never the reverse.

One block per **theme** then overrides palette and/or semantic names under `.jn-v2[data-theme="…"]` (plus its own dark pair where needed): `default` has no block (it is the base layer), then `clean` (the default for any browser that never picked one: neutral greys, one teal accent, sans display face, 13px body floor), `alt`, `tone1-3`, `editorial` (hidden dev stops), `board`, `cobalt`, `saas`, `win98`. `win98` also carries a long run of theme-named structural rules (rail-as-Explorer-tree, bevelled scrollbar, group-box cards, the caption bar's `_ □ ×` controls, dialog button minimums) — inert everywhere else. Hover/focus rules sit outside the token blocks, keyed off classes the primitives already apply (`v2-bd`, `v2-bdc`, `v2-act`, `v2-row`, `v2-menuitem`, `v2-chip`, `v2-dashadd`, `v2-hover-accent`, …), and carry `!important` — an inline style otherwise always wins the cascade.

**Adding a theme:** copy an existing block, register the id in `theme.js`'s `THEMES` (and `THEME_PICKER`/`THEME_LABEL` to make it user-selectable), add its boot-background rule in `frontend/index.html`'s no-flash `<style>` block. Then run `py tests/tools/stylelint.py` (fails on any literal colour/font/radius/shadow outside `ui.jsx`/`theme.css`, and on light/dark token parity gaps) and prove it with `tests/tools/stylecrawl.py` + `stylediff.py` against an existing theme — only colours, fonts and prose heights should differ.

`theme.js` owns two independent axes, stored in `localStorage` (never the DB): **appearance** (`light|dark|system`, key `jobnavigator_appearance`) and **theme** (the palette id, key `jobnavigator_theme`). `resolved` is the light/dark actually painted (`system` follows `prefers-color-scheme` live); `mode` is what the user picked. A legacy boolean (`jobnavigator_dark_mode`) and a legacy theme key (`jobnavigator_skin`) migrate once on first read. `useTheme()` — `{ mode, resolved, theme, setMode, setTheme, cycle }` — is the one look-and-feel read a component makes; `useThemeVar(name, fallback)` (declared in `ui.jsx`) reads one CSS custom property live off `.jn-v2` — the shape-switch mechanism above. `<html>` gets `data-appearance`/`data-theme`/`.dark` from `index.html`'s inline boot script before React mounts; `theme.js` re-stamps them on change, and each `.jn-v2` root mirrors both via `themeAttrs()`.

## `hooks.js`

- `useEscape(onClose, active, capture)` — Escape closes; `capture` is for the two overlays (sign-in, welcome) mounted outside `.jn-v2` that must win the key over whatever screen is already listening.
- `useSingleOpen(open, onClose)` — one open popover at a time via a `jn:select-open` broadcast.
- `useSnapTop(ref)` — nudges a flex-centred panel onto the pixel grid.
- `useSettled(loaders, key)` — waits for a batch to settle once, no per-fetch render jump.
- `useWarm(screen, live, ready, ok)` — caches a screen's last-good numbers so a revisit paints instantly, then cross-fades to live values; a failed load renders `—`, never stale zeroes.
- `NBSP` — the "still loading" placeholder (vs. `DASH`, "load failed").

## Conventions

- **Never inline a colour, font, radius or shadow in a screen** — only `ui.jsx`/`theme.css` may. `py tests/tools/stylelint.py` enforces it; a line with a genuine exception is marked `// ui: keep — reason`.
- **Line-heights are whole pixels** so 1px borders never blur on fractional row heights; fixed-height flex controls carry `v2-ctl`.
- **Disabled primary buttons are `--line` on `--muted`.** **Green fill (`--change-bg`) marks only text changed by tailoring** — a generic set-back surface is `--recessed`.
- **Feedback goes through `useToasts`**: progress/success auto-dismiss at 4s, undo at 5s, error never auto-dismisses. **Destructive actions use `ConfirmDialog`**, never `window.confirm`.
- **After creating or deleting rows**, dispatch `window.dispatchEvent(new CustomEvent('jn:counts-changed'))` to refresh the rail's badge counts.
- **Long-running actions poll `/api/monitor/active`** by scope key so a spinner survives navigating away and back.

## Jobs, Role Match and job workspace

The product UI shows **one** match score: the Copilot Role Match (evidence coverage of a posting by the verified profile). Legacy `cv_scores` are never drawn outside `/classic`.

- **Jobs** (`screens/Jobs.jsx`, route `/feed`) — Recommended / Saved / Applied tabs, a chip bar (role, location, level, employment type, arrangement, date found, experience, minimum match) and one card per job. `GET /api/jobs` returns `role_match` per row (score, counts, stale, top matched / missing requirement texts, employment type, seniority, years required) from the job's latest analysis; level, employment type, experience and minimum match filter on that analysis, so unanalyzed jobs drop out while they are set. There is no industry data and no posting date, only when a job was found.
- **Job workspace** (`screens/JobDetail.jsx`, `/jobs/:id?tab=`) — header with Role Match and actions; tabs Overview (summary, strongest matches, important gaps, description), Resume (`ResumeReview`: status, accept/reject, unsupported claims listed first, base-vs-tailored diff, PDF/.tex/Overleaf), Application (status, what autofill will upload) and Evidence (score breakdown, requirement matrix, provenance, gap analysis with add-context).
- **Resume** (`screens/Resumes.jsx`, `/resumes`, `/resumes/versions/:id`) — every fact-based résumé version as a table; the newest accepted base is PRIMARY.

## Routes

`/` is the app shell (`Shell.jsx` + `screens/`); `/classic/*` is the untouched v1 interface (`classic/`). `/v2` and `/v2/*` redirect to the same path with the prefix stripped. `/ui` (primitive gallery) and `/toasts` (toast taxonomy lab) live in the git-ignored `frontend/src/design-base/` (`UiGallery.jsx`, `ToastLab.jsx`); `App.jsx` registers both routes only when the folder exists, via `import.meta.glob('./design-base/*.jsx')` behind `React.lazy`/`Suspense` — a missing folder costs the two routes and nothing else.

## Testing

```bash
# backend (inside the container)
docker compose exec -T backend sh -c "cd /app && python -m pytest backend/tests -q -p no:cacheprovider"

# frontend unit tests (Vitest, via a throwaway node:20-alpine container)
bash tests/tools/fe-test.sh              # whole suite
bash tests/tools/fe-test.sh time         # files matching "time"

# end-to-end cases (Playwright, inside the backend container against http://caddy)
bash tests/e2e/run.sh                    # every case
bash tests/e2e/run.sh modals             # cases whose name contains "modals"

# pixel + style gate (pauses the scheduler, shoots + crawls, diffs a base stage)
bash tests/tools/gate.sh <stage> [<base-stage>] [--theme X]
```
