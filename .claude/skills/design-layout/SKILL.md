---
name: design-layout
description: Compose a screen or page — spacing ladders, density, radius character, shadow language, grids and hierarchy. Use whenever arranging elements, choosing padding/margin/gap values, picking corner radii or shadows, structuring a page, or when a layout "feels cramped", "feels empty", or "looks flat". Trigger on "layout", "spacing", "whitespace", "grid", "corners", "shadows", "hierarchy", or whenever you are about to write a padding, gap, or border-radius into a file.
---

# design-layout

Layout is the argument a design makes about what matters: the most important
thing gets the most space, the sharpest contrast, the clearest position.
Everything else is arranged to stay out of its way. Baton generates spacing,
radii, and shadow choices from this skill's rules through the executable
catalog in `src/jev_agent/core/design_skills.py`.

Sources: adapted from Atelier's `typography-and-layout` composition guidance
(MIT, jangles-byte/atelier — attributed), the anti-slop-design layout
patterns (MIT, Ferousco-dev/anti-slop-design — attributed), and the ui-design
`ui-design-principles` material, rewritten in Baton's own words (that source
carries no license, so no wording is copied).

## The one rule

**Space groups, not elements.** More space between sections than inside them,
more space between a form group and the next group than between a label and
its input — always. Spacing that treats every gap the same flattens the page
into a list.

## Workflow

1. **Start with the feature, not the shell.** The first thing designed is
   the thing the screen is for; navbars, sidebars, and footers come after.
   Design in grayscale first if the hierarchy is unclear — if it does not
   read without color, color will not save it.
2. **Start with too much whitespace, then remove.** White space is removed,
   not added: begin generous and tighten until the page holds together.
   A page that needs 600px of content uses 600px; it does not stretch to
   fill 1400px.
3. **Pick a spacing ladder and a density, and use nothing else.** Sizes come
   from one ladder (4/8-based, each step at least ~25% from its neighbors —
   a linear ladder feels too small at large sizes and too large at small
   ones). Density is a character choice, not a mood: compact for data-dense
   tools, balanced for most products, airy for editorial and marketing.
   Every gap in the system is a rung on that ladder — ad-hoc values are how
   a system quietly stops being one.
4. **Let radius and shadow carry personality.** Small radii read neutral and
   serious, large radii read playful, none at all reads formal. Shadows are
   a language with three words — flat (no shadow, borders do the work),
   soft (one quiet elevation), dramatic (deep, for things that demand
   attention) — and a system speaks one of them consistently.
5. **Break the grid on purpose or not at all.** Grids are a default, not a
   law: a fixed-width sidebar, a broken column, a full-bleed band are all
   fine when they serve the content — but an accidental break just looks
   like a bug. If nothing about the layout is describable from memory, the
   direction step failed.

## Non-negotiables

- One spacing ladder, applied asymmetrically: space groups, not elements.
- Density, radius character, and shadow language are chosen once per system
  and applied everywhere — never per component.
- Hierarchy uses weight and color first, size second. Size alone is not a hierarchy.
- De-emphasize to emphasize: making the secondary action smaller and lighter
  beats making the primary action bigger.
- Never ship two systems with the same density, radii, and shadow language
  from the same brief. The seed varies the layout profile; the same seed
  reproduces it, a different seed does not.
