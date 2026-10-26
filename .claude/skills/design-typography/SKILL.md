---
name: design-typography
description: Choose typefaces and build a type scale — display/body pairings, modular scale ratios, line-height and measure rules, tracking. Use whenever setting ANY text styles: choosing fonts, sizing headings, pairing a display face with a body face, or when a design "feels flat" or "looks like a template". Trigger on "pick a font", "choose typography", "pair fonts", "type scale", or whenever you are about to write a font-family into a file.
---

# design-typography

Typography is most of design: get type and spacing right and a screen looks
designed before it has any color or decoration. The failure mode is timidity —
small scale jumps, default tracking, uniform spacing — not bad taste. Baton
generates type pairings from this skill's rules through the executable catalog
in `src/jev_agent/core/design_skills.py`.

Sources: adapted from Atelier's `typography-and-layout` skill (MIT,
jangles-byte/atelier — attributed) and from the ui-design
`font-recommendations` material, rewritten in Baton's own words (that source
carries no license, so no wording is copied).

## The one rule

**Contrast on exactly one axis.** A pairing works when the two faces differ
clearly in one way — serif display with sans body, geometric display with
humanist body, heavy display with light body — and agree everywhere else.
Two faces that differ on every axis fight; two that differ on none are the
same face twice.

## Workflow

1. **Name the voice first.** Editorial, technical, friendly, luxurious —
   one word, from the brief. The voice picks the family; the catalog picks
   the faces.
2. **Pick a pairing from the catalog, never a default.** Two families
   maximum (a third only for code/data). The catalog's pairings are the
   menu; the seed picks which one this system gets. Inter is a fine body
   face — it is never the unexamined default for both faces, and it is never
   the display face of a system that wants to be remembered.
3. **Give it a real scale ratio.** Sizes come from a ratio (1.2, 1.25, or
   1.333), and at least one jump ≥ 3× body size appears somewhere in display
   contexts (heroes, posters). Interfaces cap lower but still need contrast
   between display and body — a 16px body with a 20px headline is not a
   hierarchy, it is a rounding error.
4. **Set the measure and the leading.** Body copy runs 45–75 characters per
   line and never wider; line-height runs inversely to size (display tight
   ~1.05, body generous ~1.6). Tighten display tracking a touch (-0.01 to
   -0.03em); loosen small caps and labels instead.
5. **Render at real size and look.** Type that looks right in code is
   routinely wrong on screen. The specimen page is the test.

## Non-negotiables

- Two font families maximum (a third only for code/data, and it counts if decorative).
- A real scale ratio, and at least one jump ≥ 3× body size somewhere in display contexts.
- Body measure 45–75 characters; line-height inversely proportional to size.
- Tabular numerals (`font-variant-numeric: tabular-nums`, or a mono face)
  for any column of numbers that can change.
- Never let two systems from the same brief share a pairing. The seed
  chooses the pairing; the same seed reproduces it, a different seed does not.
