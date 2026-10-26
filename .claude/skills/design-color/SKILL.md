---
name: design-color
description: Choose and build a color system for a design system or UI — palette construction, harmony schemes, tinted neutrals, dark/light as sibling palettes, and contrast discipline. Use whenever picking ANY colors for visual work: a new design system, a brand palette, a theme, chart series colors, or when a palette "looks off". Trigger on "pick colors", "make a palette", "add dark mode", "what color should this be", or whenever you are about to write a hex code into a file.
---

# design-color

Color is the fastest signal of whether two systems are the same product. Baton
generates palettes from this skill's rules through the executable catalog in
`src/jev_agent/core/design_skills.py` — the catalog is the skill made runnable,
so keep the two in agreement: a rule that lives only in prose will drift from
what the generator actually emits.

Sources: adapted from Atelier's `color` skill (MIT, jangles-byte/atelier —
attributed) and from the ui-design `color-palettes` material, rewritten in
Baton's own words (that source carries no license, so no wording is copied).

## The one rule

**One color is the loudest in any view.** If two colors compete for attention,
demote one. A palette with no hierarchy is decoration, not a system.

## Workflow

1. **Start from the direction, not the swatch.** Read the brief (industry,
   audience, one emotional adjective) and name the palette's register first —
   warm or cool, muted or saturated, light or dark-led. The register comes
   from the product, never from a favorite hex.
2. **Anchor, then build out.** Pick one anchor hue from the steer (the vibe
   keywords and business context), then choose a harmony scheme and derive the
   rest from it:
   - **Analogous** — neighbors on the wheel (anchor ±30°); calm, cohesive,
     the safest default.
   - **Complementary** — the anchor plus its opposite (+180°); high energy,
     use the complement sparingly as an accent.
   - **Split-complementary** — anchor plus the two hues flanking its opposite
     (+150° / +210°); the energy of complementary with less clash.
   - **Triadic** — three hues 120° apart; vivid and balanced only when two of
     the three stay quiet.
   - **Monochromatic** — one hue, lightness and saturation do all the work;
     the most restrained, and the hardest to make boring — it needs real
     lightness range.
   - **Hue-shift** — rotate the anchor 45–135° away from the obvious family
     hue. Same brief, different answer: this is the move that keeps two
     restaurant systems from sharing a palette.
3. **Tint the neutrals, never pure grey.** Every neutral carries a trace of
   the anchor hue. Pure grey reads dead next to a chromatic brand color; a
   tinted neutral makes the whole system feel mixed from one paint set.
4. **Build dark and light as siblings, not inversions.** A dark palette is a
   second palette designed on the same anchor — its own background, surface,
   and text steps — not the light palette with its lightness values flipped.
   Elevation in dark UIs comes from lighter surfaces, not from shadows.
5. **Verify contrast before shipping.** Body text ≥ 4.5:1, large text and UI
   chrome ≥ 3:1 (WCAG AA — see the `design-accessibility` skill). Check with
   numbers, never by eye. If the brand hue cannot carry white text at 3:1,
   the text color changes — the hue does not get to overrule legibility.

## Non-negotiables

- Body text meets WCAG AA (4.5:1) minimum; UI chrome ≥ 3:1. Check, don't eyeball.
- Neutrals carry a trace of the anchor hue. Pure grey reads dead.
- One color is the loudest in any view. If two compete, demote one.
- A palette is not finished until it has been rendered in the real artifact
  and looked at — a list of hex values is not a design.
- Never ship the same palette for two different systems. When the steer
  points at the same hue twice, rotate it: the seed exists so the second
  system is a different answer to the same brief.
