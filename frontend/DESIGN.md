# DESIGN.md — Value Dashboard Frontend

> This document **codifies the existing** Vue 3 + Naive UI dashboard design. It is the
> Design System Gate for upcoming UI work (Quality Warning Banner/Tag, Data Freshness
> Card, disabled states). It does **not** propose a redesign — it records what is
> already on disk so that new primitives stay visually continuous with the shipped UI.

---

## 0. Research Log

No external design research was performed for this document. Every token, spacing
value, and component choice below was extracted from the current frontend source:

- `frontend/src/style.css` — CSS custom properties and dark-mode overrides.
- `frontend/src/App.vue` — shell layout (56px header, 24px content padding).
- `frontend/src/views/DataStatusPage.vue` — `NCard` / `NStatistic` / `NGrid` /
  `NTag` / `NDescriptions` / `NDataTable` usage and 16px section rhythm.
- `frontend/src/views/StockDetailPage.vue` — detail-page card layout and
  `klinecharts` integration.
- `frontend/src/views/ScreeningPage.vue` — rule-tree form, `NDataTable` columns,
  `NGrid` result layout.
- `frontend/src/views/WatchlistPage.vue` — watchlist card/table patterns.

---

## 1. Atmosphere & Identity

**Read as:** a desktop-first, trust-first financial research workspace: quiet,
restrained, and dense enough for data review. Not a consumer marketing surface.

- **User:** Chinese-speaking value-investment researcher / operator.
- **Job of the UI:** surface numbers, coverage, and data-quality signals clearly
  enough that a researcher can decide whether to trust a figure in seconds.
- **Tone:** quiet, workmanlike, information-dense. No decorative gradients, no
  brand-forward hero, no illustration system.
- **Language:** Simplified Chinese for all visible labels, messages, and copy.
  Metrics are Chinese first with a useful abbreviation retained in parentheses,
  e.g. `市盈率（PE-TTM）` and `净资产收益率（ROE）`. Stable English field
  names remain an API, rule-JSON, and machine-export concern, not UI copy.
- **Density over delight:** the page is allowed to feel "spreadsheet-adjacent".
  Whitespace is functional (separating sections), not expressive.

---

## 2. Color

Tokens live in `frontend/src/style.css`. Dark mode is driven by
`@media (prefers-color-scheme: dark)` — there is **no in-app theme switcher**;
Naive UI's `NConfigProvider` is mounted without an explicit `theme`, so it
follows the OS preference by default.

### 2.1 Light tokens

| Token | Value | Role |
|---|---|---|
| `--text` | `#6b6375` | Default body text |
| `--text-h` | `#08060d` | Headings, emphasis, high-priority text |
| `--bg` | `#fff` | Page background |
| `--border` | `#e5e4e7` | Dividers, card borders, table rules |
| `--code-bg` | `#f4f3ec` | Inline code / monospaced background |
| `--accent` | `#aa3bff` | Accent (rare — counter hover, focus ring) |
| `--accent-bg` | `rgba(170, 59, 255, 0.1)` | Accent tint |
| `--accent-border` | `rgba(170, 59, 255, 0.5)` | Accent border on hover |
| `--social-bg` | `rgba(244, 243, 236, 0.5)` | Secondary surface tint |
| `--shadow` | `rgba(0,0,0,0.1) 0 10px 15px -3px, rgba(0,0,0,0.05) 0 4px 6px -2px` | Elevation |

### 2.2 Dark tokens

| Token | Value |
|---|---|
| `--text` | `#9ca3af` |
| `--text-h` | `#f3f4f6` |
| `--bg` | `#16171d` |
| `--border` | `#2e303a` |
| `--code-bg` | `#1f2028` |
| `--accent` | `#c084fc` |
| `--accent-bg` | `rgba(192, 132, 252, 0.15)` |
| `--accent-border` | `rgba(192, 132, 252, 0.5)` |
| `--social-bg` | `rgba(47, 48, 58, 0.5)` |
| `--shadow` | `rgba(0,0,0,0.4) 0 10px 15px -3px, rgba(0,0,0,0.25) 0 4px 6px -2px` |

### 2.3 Semantic colors (Naive UI)

Status color is delegated to Naive UI's `NTag` / `NAlert` type scale. The app
uses these types directly — no custom semantic palette is defined:

| Type | Used for today | Must NOT be the sole signal for |
|---|---|---|
| `success` | **Unused today — do not introduce without a documented use case** | — |
| `info` | Informational notes (`记录中`) | Critical state |
| `warning` | Attention needed (`需关注`) | Critical state |
| `error` | Failures / action required (`待处理`) | — |
| `default` | Neutral labels | — |

`type="success"` has no current consumer in the dashboard. Adding a green
"all-clear" signal would introduce a new semantic that the researcher has
not been trained to read in this UI; if a use case emerges (e.g. a
validated-data badge), it must be documented here with the specific
trigger conditions before any view starts rendering it.

New primitives (Quality Warning Banner/Tag, Data Freshness Card) must reuse
these Naive UI types — do not introduce new semantic colors beyond the five
listed above.

---

## 3. Typography

### 3.1 Font stacks

Defined in `style.css`:

```
--sans:    system-ui, 'Segoe UI', Roboto, sans-serif
--heading: system-ui, 'Segoe UI', Roboto, sans-serif
--mono:    ui-monospace, Consolas, monospace
```

No web fonts are loaded. Chinese text renders through the CJK fallback of
`system-ui` / `Segoe UI` (Microsoft YaHei on Windows, PingFang SC on macOS).
This is intentional — the dashboard must feel native to the OS, not branded.

### 3.2 Type scale (as shipped)

| Element | Size | Weight | Line-height | Notes |
|---|---|---|---|---|
| `h1` | 56px (36px ≤1024px) | 500 | — | Reserved; rarely used in-page |
| `h2` | 24px (20px ≤1024px) | 500 | 118% | Section titles (`数据状态`, etc.) |
| Body | 18px (16px ≤1024px) | 400 | 145% | Default `:root` font |
| `code` | 15px | 400 | 135% | Monospace |
| `.counter` | 16px | 400 | — | Accent counter (legacy template) |
| Header brand | 18px | 600 | — | Inline style in `App.vue` |
| Naive UI `NStatistic` value | component default | — | — | Used for KPI numbers |
| Naive UI `NStatistic` label | component default | — | — | Used for KPI labels |
| Naive UI `NDataTable` (size=`small`) | component default | — | — | All tables use `size="small"` |

### 3.3 Rules

- Do not introduce a new display font.
- Numbers in tables and statistics use the body stack; do not switch to a
  tabular-nums font without re-auditing column alignment.
- All visible labels are Chinese; the product name `Value Dashboard` is the
  only Latin string in the header.

---

## 4. Spacing & Layout

### 4.1 Shell

`App.vue` defines a fixed desktop workspace shell:

| Region | Size | Padding | Border |
|---|---|---|---|
| Sidebar (`NLayoutSider`, `bordered`) | width `226px` | `27px 17px 18px` | right, via `bordered` |
| Content (`NLayoutContent`) | fills remaining height | `37px 49px 58px` | none |

Sidebar contains the `value` brand, the four V1 modules (筛选、自选列表、个股详情、
数据状态), and a small data-readiness link. The shell uses `NLayout
position="absolute"` filling the viewport. The individual stock detail module
opens to its own stock search state; search is not a global control.

### 4.2 Spacing scale

The app uses a **4-based** scale expressed as inline `style` values. Observed
values in production views:

| Token value | Where used |
|---|---|
| `4px` | Inner icon/button micro-adjustments |
| `8px` | Small internal padding, `NGrid` gutters in tight spots |
| `12px` | (rare) |
| `16px` | **Primary section rhythm** — `margin-bottom` between stacked `NCard`s, `NGrid` `x-gap`/`y-gap`, `NDescriptions` spacing |
| `24px` | Shell content padding, major section gaps |
| `32px` | (rare, legacy template only) |
| `40px` | (rare) |
| `48px` | Brand-label `margin-right` in header |

New primitives must use values from this scale. Do not invent intermediate
values (e.g. `14px`, `20px`) without a documented reason.

### 4.3 Grid

- `NGrid` with explicit `:cols` (4 for KPI tiles, 3 for status tiles, etc.).
- `:x-gap` / `:y-gap` default to `16` — match this for any new grid.
- No CSS Grid or Flex layouts are defined outside Naive UI components; new
  layouts should continue to compose via `NGrid` / `NSpace` / `NLayout`.

### 4.4 Responsive

- One breakpoint at `1024px` in `style.css` (font-size and a few layout
  adjustments for the legacy template hero).
- Naive UI components handle their own internal responsiveness.
- The dashboard has **no explicit mobile layout** today; grids collapse only
  through Naive UI's default behavior. This is accepted debt (§8).

---

## 5. Components

### 5.1 Primitive inventory (existing)

All primitives below are Naive UI components imported and used as-is. No
custom wrapper layer exists.

| Primitive | Naive UI component | Where used | Notes |
|---|---|---|---|
| Page shell | `NLayout` + `NLayoutHeader` + `NLayoutContent` | `App.vue` | 56px bordered header, 24px content padding |
| Top navigation | `NMenu` (mode=`horizontal`) | `App.vue` | Three items: 筛选 / 自选列表 / 数据状态 |
| Theme + providers | `NConfigProvider`, `NMessageProvider`, `NDialogProvider` | `App.vue` | Wrap the whole app; no explicit theme |
| Section card | `NCard` (default or `size="small"`) | All views | Primary container for every content block |
| KPI tile | `NCard` > `NStatistic` (label + value, optional `#suffix`) | `DataStatusPage` | 4-col grid of coverage counts; 3-col grid of status counts |
| Status tag | `NTag` (`type="warning\|info\|error"`, `size="small"`) | `DataStatusPage` | Suffix of a statistic when count > 0 |
| Key/value block | `NDescriptions` + `NDescriptionsItem` (`column=3|4`, `size="small"`) | `DataStatusPage` | Backfill, financial range, backup summary |
| Data table | `NDataTable` (`size="small"`, `striped`, paginated) | `DataStatusPage`, `ScreeningPage`, `WatchlistPage` | Default page size 10 |
| Form / rule tree | `NSelect`, `NInputNumber`, `NButton`, `NSpace`, `NCard` | `ScreeningPage` | Nested AND/OR rule tree, max depth 3, max 20 conditions |
| Loading | `NSpin` | `DataStatusPage`, `StockDetailPage` | Wraps the main content area during fetch |
| Empty state | `NEmpty` | All current views (`DataStatusPage`, `StockDetailPage`, `ScreeningPage`, `WatchlistPage`) | Shown when a dataset is empty or no action has been taken yet (e.g. `stock_count === 0`, no K-line data, no screening results, empty watchlist) |
| Primary action | `NButton` (default and `:loading`) | All views | `刷新`, `运行筛选`, `保存`, `导出CSV`, `加入自选` |
| Toast feedback | `useMessage()` (`success` / `warning` / `error`) | `ScreeningPage`, `WatchlistPage` | Async outcomes surface here; `DataStatusPage` and `StockDetailPage` do not currently use toast feedback |
| Chart | `klinecharts` (`init` / `dispose`) | `StockDetailPage` | Candlestick + MA indicator; raw or qfq |

### 5.2 New primitives (contract for upcoming work)

These two primitives do **not yet exist in code**. The spec below is the
contract that UI implementation must satisfy so they read as native to the
existing system.

#### 5.2.1 Quality Warning Banner / Tag

A compact signal that a figure, dataset, or pipeline step has a quality concern
the researcher should weigh before trusting the number.

**Variants**

| Variant | Naive UI binding | Visual | When to use |
|---|---|---|---|
| Inline tag | `NTag type="warning" size="small"` | Matches existing `需关注` / `记录中` / `待处理` tags | Inside a statistic suffix, table cell, or next to a label |
| Section banner | `NAlert type="warning"` (or `type="error"` for blockers) | Naive UI default alert styling, full width of its `NCard` | Top of a card or page when a whole section is compromised |

**States**

| State | Tag | Banner |
|---|---|---|
| All clear | Not rendered | Not rendered |
| Advisory | `type="warning"`, short label (e.g. `数据滞后`, `样本不足`) | `type="warning"`, title + one-line reason |
| Blocked / untrusted | `type="error"`, short label (e.g. `数据失效`) | `type="error"`, title + reason + suggested action |
| Disabled control | The control that would consume the suspect data is `:disabled="true"` | Banner remains visible above the disabled control |

**Accessibility**

- Text label is mandatory — color is **never** the sole signal (see §8).
- Banners use `NAlert`'s built-in `role="alert"` semantics.
- Inline tags next to a figure must also be reachable by screen readers as
  part of the figure's accessible name or via `aria-describedby` pointing at
  the tag.
- Focus order: banner comes before the data it qualifies.

#### 5.2.2 Data Freshness Card

A small card that surfaces **when** the data on screen was last produced, so a
researcher can judge staleness without hunting for a timestamp.

**Structure**

```
NCard size="small"
  NSpace align="center" justify="space-between"
    left:  label "数据时间" + value (formatted date / datetime)
    right: optional NTag (size="small") when freshness is outside SLA
```

**Freshness → tag mapping**

| Condition | Tag rendered |
|---|---|
| Within SLA | none |
| Outside SLA but usable | `NTag type="warning" size="small"` (e.g. `滞后 3 天`) |
| Stale beyond trust horizon | `NTag type="error" size="small"` (e.g. `数据过期`) and downstream controls disabled |

**States**

| State | Value display | Tag | Downstream controls |
|---|---|---|---|
| Fresh | Actual timestamp, e.g. `2025-01-20 09:30` | — | Enabled |
| Stale-warning | Actual timestamp | `warning` | Enabled |
| Stale-error | Actual timestamp | `error` | `:disabled="true"` |
| Unknown / never produced | `—` (em-dash, matching existing `fmt`/`fmtPct` convention) | `info` with `未知` | Case-by-case; default disabled |

**Accessibility**

- The timestamp is real text, not an icon — always readable without color.
- When downstream controls are disabled, the freshness card stays in tab
  order and the `NTag` carries the reason; disabled controls must have an
  `aria-describedby` pointing at the freshness card so screen readers explain
  *why* they are disabled.
- The card is a `NCard`, so it inherits Naive UI's theming and dark-mode
  behavior automatically.

### 5.3 Component rules

- Do not wrap Naive UI primitives in custom styled-components for visual
  tweaks — pass props or use the `themeOverrides` slot on `NConfigProvider`.
- Every new container is an `NCard` (default or `size="small"`). Do not invent
  new container shapes.
- Every new status indicator is an `NTag` or `NAlert` using the standard
  `type` scale. Do not hand-roll colored divs.
- Tables stay `size="small"` and `striped`. Pagination is view-specific, not universal:

  | View | `pageSize` |
  |---|---|
  | `DataStatusPage` (retry/missing lists) | 10 |
  | `StockDetailPage` (audit tables) | 10 |
  | `StockDetailPage` (other tables) | 20 |
  | `ScreeningPage` | 50 |
  | `WatchlistPage` | 50 |

  New tables should pick a page size from this set based on row density, not
  invent a new value.

---

## 6. Motion & Interaction

The dashboard today has **almost no motion**. This is intentional — researchers
do not want elements animating while they scan numbers.

### 6.1 Existing motion

**Active dashboard motion** (used by current views):

| Element | Effect | Duration | Easing |
|---|---|---|---|
| `NSpin` | spinner while loading | component default | — |
| `NButton :loading` | spinner inside button | component default | — |

**Unused legacy-template motion** (defined in `style.css` for the original
Vite scaffold; no current dashboard view references these selectors, but the
CSS still ships in the bundle — see §8.3 accepted debt):

| Element | Effect | Duration | Easing |
|---|---|---|---|
| `.counter:hover` | `border-color` transition | 0.3s | default |
| `.counter:focus-visible` | outline | instant | — |
| `#next-steps a:hover` | `box-shadow` transition | 0.3s | default |

Do not treat the legacy transitions as active design language. They are
dead code awaiting cleanup.

### 6.2 Rules

- Do not add entrance animations, staggered reveals, scroll-triggered
  effects, or parallax. They would fight the operational tone.
- New primitives (Warning Banner, Freshness Card) must **not** animate in.
  They appear/disappear with the data they qualify.
- Hover/focus states are allowed only where they communicate interactivity
  (buttons, links, table rows). Do not add hover effects to static cards or
  statistics.
- Loading is always `NSpin` or `NButton :loading` — never skeleton screens
  unless a future audit shows a specific page benefits from them.

---

## 7. Depth & Surface

### 7.1 Surface hierarchy

The dashboard uses a **flat** surface model:

1. **Page background:** `--bg` (`#fff` / `#16171d`).
2. **Cards:** Naive UI's default `NCard` surface — white in light, darkened
   in dark mode, separated from the page by a 1px `--border` rule, not by a
   shadow.
3. **Header:** `NLayoutHeader bordered` — same 1px rule on the bottom.
4. **Tables:** `striped` rows provide the only internal banding; no cell
   shadows, no raised rows.

### 7.2 Shadow

`--shadow` is defined in `style.css` but is only consumed by the legacy
template's `#next-steps a:hover`. **No dashboard component uses shadow.**
New primitives must not introduce drop shadows — they would break the flat
continuity with existing `NCard`s.

### 7.3 Borders

- 1px solid `--border` is the universal divider.
- Naive UI's `bordered` prop on `NCard` / `NLayoutHeader` is the canonical
  way to apply it.
- Do not use 2px rules, double rules, or colored rules except through
  `NTag` / `NAlert`'s own styling.

---

## 8. Accessibility Constraints & Accepted Debt

### 8.1 Accessibility intent (WCAG 2.2 AA)

The dashboard targets **WCAG 2.2 AA** in spirit. Concrete commitments:

- **Color is never the sole signal for status.** Every `NTag` / `NAlert`
  carries a text label (`需关注`, `记录中`, `待处理`, `数据滞后`, etc.). New
  primitives in §5.2 inherit this rule.
- **Interactive elements are keyboard reachable.** Naive UI's `NButton`,
  `NMenu`, `NDataTable`, `NSelect`, `NInputNumber` ship with keyboard
  support; do not wrap them in ways that break tab order.
- **Focus ring is visible.** `NConfigProvider` default focus styles are
  retained. The `.counter:focus-visible` rule in `style.css` is the only
  custom focus treatment.
- **Form controls have labels.** `NDescriptionsItem label`, `NStatistic label`,
  and table column `title` props are the accessible names. Do not remove them.
- **Disabled controls must explain why.** When a control is disabled because
  of a freshness or quality issue, it must carry `aria-describedby` pointing
  at the Warning Banner or Freshness Card that justifies the disabled state.
- **Language is declared correctly.** `index.html` uses `lang="zh-CN"` for the
  Simplified Chinese interface.

### 8.2 Known gaps (to be addressed incrementally, not in this remediation)

- No automated a11y audit has been run against the shipped views.
- No automated downstream-tooling audit has verified browser translation or
  analytics behavior after the `lang="zh-CN"` correction.
- The legacy `style.css` template (hero, `#next-steps`, `#spacer`, `.ticks`,
  etc.) is still present and ships in the bundle even though the dashboard
  views no longer use those selectors.

### 8.3 Accepted debt (out of scope for this remediation)

The following are **truthfully recorded** as out of scope for the current
design-system documentation pass. They are not being fixed now; they are
being named so future work can plan around them.

- **Legacy template CSS remains.** `style.css` contains the original Vite
  scaffold's hero/social/next-steps styles (~200 lines). They are dead code
  for the dashboard but are not removed here.
- **Inline styles are pervasive.** `App.vue`, `DataStatusPage.vue`,
  `StockDetailPage.vue`, `ScreeningPage.vue`, and `WatchlistPage.vue` use
  `style="..."` attributes for spacing, font-size, color, and layout instead
  of CSS classes or a token system. This document records the *de facto*
  values (§4.2) but does not refactor them.
- **Untyped frontend code.** Some view files contain `any`-typed refs,
  `reactive` objects without full interfaces, and untyped `axios` response
  payloads. TypeScript strictness is not enforced at the component boundary.
  This is a separate remediation track.
- **No responsive mobile layout.** The dashboard has one breakpoint at
  1024px in `style.css` for the legacy template; the operational views rely
  on Naive UI's internal responsiveness. A dedicated mobile pass is out of
  scope.
- **No in-app theme switcher.** Dark mode follows the OS via
  `prefers-color-scheme`. Adding a manual toggle would require wiring
  `NConfigProvider`'s `theme` prop and is out of scope.
- **No design-token build step.** Tokens are CSS custom properties in
  `style.css`, not a generated theme file consumed by Naive UI's
  `themeOverrides`. Bridging them is future work.

### 8.4 What this document does NOT claim

- Functional Playwright QA has run against mocked APIs at 375/768/1280px and
  generated 57 screenshots. No vision-capable or human pixel-level review has
  signed off CJK wrapping, clipping, or visual hierarchy, so this document does
  not claim complete visual fidelity.
- No new brand, font, animation, component library, gradient, or redesign is
  introduced. This document is a codification, not a proposal.
- No commit is made as part of this document.
