---
name: design-motion
description: Give an interface motion with intent — duration weight scale, easing, stagger, entrances and exits, and reduced-motion as part of done. Use for ANY animation work: adding or tuning a transition, hover or press state, loading state, page transition, or toast; or when motion "feels dead", "floaty", "janky", or "cheap". Trigger on "animate", "animation", "transition", "make it feel alive", "microinteraction", "springy", "smooth", or any easing, duration, or timing decision.
---

# design-motion

Motion is communication. Every animation answers one of three questions —
*what just happened?* (feedback), *where did it go?* (orientation), or *is
this alive?* (character). Animation that answers none of them is decoration
on a delay; delete it rather than tune it. Baton generates motion profiles
from this skill's rules through the executable catalog in
`src/jev_agent/core/design_skills.py`.

Sources: adapted from Atelier's `motion` skill (MIT, jangles-byte/atelier —
attributed).

## The one rule

**Nothing moves without a reason.** Name the question it answers or cut it.
Scarcity is what makes the good animation land: one excellent motion beats
six mediocre ones, and a still page with one great transition beats a page
that breathes everywhere.

## Workflow

1. **Name the question and the temperament.** Feedback, orientation, or
   character — and whether the motion is snappy or viscous, calm or
   nervous. One line is enough. If the product has a design direction, the
   temperament comes from there.
2. **Start from a profile, not from scratch.** The catalog's motion profiles
   carry tuned durations, easings, and stagger for a temperament. Adapt the
   values to the temperament rather than inventing timings.
3. **Use the weight scale.** Micro 100–150ms, small 150–250ms, medium
   250–400ms, large 400–600ms. Exits run ~20% faster than entrances.
   Nothing over 700ms without a reason an observer could state.
4. **Ease like physics, not like math.** No linear easing on anything an eye
   follows: enter fast-and-decelerate, exit accelerate-and-gone. Linear is
   honest only for machine progress (determinate bars).
5. **Watch it, then diagnose.** Motion cannot be judged from source code or
   from a still. Run it, capture it, look at it. If it underwhelms, diagnose
   before touching values — dead, floaty, cheap, and janky have different
   causes and different fixes.
6. **Ship the reduced-motion path with the motion, not after it.**
   `prefers-reduced-motion` is part of done: keep opacity and color
   feedback, drop travel, scale, parallax, shake, and ambient. A reduced
   path that merely disables everything is not done — it is a different
   kind of broken.

## Non-negotiables

- Durations from the weight scale; exits ~20% faster than entrances; nothing over 700ms without a reason.
- No linear easing on anything an eye follows.
- Animate compositor-friendly properties (transform, opacity). Layout
  properties — `top`, `height`, `width`, `margin` — get FLIPped, not animated.
- Every entrance has an exit, and every animation survives being interrupted mid-flight.
- Delta-time everywhere; never assume 60fps.
- `prefers-reduced-motion` is part of done, not a follow-up.
- Temperament is a per-system choice: the seed picks the motion profile, so
  two systems from the same brief do not move the same way.
