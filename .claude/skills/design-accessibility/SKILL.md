---
name: design-accessibility
description: Keep a design system usable by everyone — WCAG 2.2 AA contrast and target-size rules, focus visibility, reduced motion, semantic structure. Use whenever checking, reviewing, or shipping any visual work: before a palette, type scale, or component set is final, and whenever anyone asks "is this accessible". Trigger on "accessibility", "a11y", "contrast", "WCAG", "screen reader", "keyboard", "focus", or "reduced motion".
---

# design-accessibility

Accessibility is a gate, not a variable. A system may vary its palette, its
type pairing, its spacing, its motion — it may not vary whether people can
read it, reach it, or operate it. Every generated palette passes this skill's
contrast rules before it ships; a palette that fails is re-rolled, never
shipped with a note. Baton enforces the contrast gate in
`src/jev_agent/core/design_skills.py` (`check_palette_contrast`); the rest of
this skill is the checklist a human or an agent runs over the rendered result.

Sources: the WCAG 2.2 rules below are restated in Baton's own words from the
ui-design `wcag-2.2` material (that source carries no license, so no wording
is copied). The contrast discipline also follows Atelier's `color` skill
(MIT, jangles-byte/atelier — attributed).

## Contrast (WCAG 2.2, AA is the floor)

| What | Minimum | Criterion |
|---|---|---|
| Normal text (under 18px, or under 14px bold) | **4.5:1** | 1.4.3 AA |
| Large text (18px+, or 14px+ bold) | **3:1** | 1.4.3 AA |
| UI components and graphical objects (borders, icons, focus indicators, form controls vs. adjacent colors) | **3:1** | 1.4.11 AA |
| Text on a primary button, on the worse of its resting and hover states | **3:1** | 1.4.11 AA |

AAA (7:1 normal, 4.5:1 large) is the aspiration, not the gate. Check with
numbers, never by eye — and check the *worst* state, not the prettiest one:
a button label must read on hover too, and muted text must read on the
surface it actually sits on, not on white in a swatch book.

## Beyond contrast

- **Targets:** 24×24 CSS px minimum for interactive targets (AA, 2.5.8);
  44×44 is the AAA bar and the mobile minimum — touch targets never shrink
  to fit a dense layout.
- **Focus:** focus is always visible, never removed without a replacement,
  and never obscured by sticky chrome (2.2: focus not obscured).
- **Motion:** `prefers-reduced-motion` is part of done (see the
  `design-motion` skill): keep opacity and color feedback, drop travel,
  scale, parallax, shake, and ambient.
- **Semantics:** real headings in order, real buttons for actions, real
  labels for inputs; placeholder text is not a label, and placeholder text
  that conveys information must itself meet 4.5:1.
- **Links in paragraphs:** underlined, or 3:1 against surrounding text plus
  an underline on hover/focus — color alone is never the only signal.
- **Meaning:** color is never the only carrier of meaning. A status, an
  error, or a selection always has a second signal — text, icon, shape.

## Non-negotiables

- WCAG AA contrast is a gate: a generated palette that fails it is re-rolled, not shipped.
- Check the worst state, not the best one.
- Color is never the only signal.
- Accessibility is verified on the rendered artifact, not on the token list.
