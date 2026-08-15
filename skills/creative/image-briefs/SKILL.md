---
name: image-briefs
description: Turn a chat image request into one correct generate call.
version: 1.0.0
author: [Mark Howell]
license: MIT
platforms: [macos, linux, windows]
metadata:
  hermes:
    tags:
      - image-generation
      - prompting
      - chat
      - creative
      - briefs
    category: creative
---

# Image Briefs Skill

Turns loose conversational phrasing — *"make me a banner for the launch
post"* — into one correct `image_generate` call, then reports honestly what
came back. It covers choosing the aspect ratio and size tier, writing the
prompt, and handling failures.

It does not choose the backend or the model: those are user-configured and not
selectable by the agent. It also does not edit existing images.

## When to Use

Use when someone asks for an image in conversation and you are about to call
`image_generate`.

Skip it when they have already given exact parameters, or when the task is
image *analysis* rather than generation — that is `vision_analyze`.

## Prerequisites

An `image_gen` backend configured by the user. No credentials are handled
here; if none is configured, `image_generate` reports that itself.

The high-resolution guidance in `## Procedure` applies only when the active
backend advertises a hi-res pass. The `image_generate` description says
whether it does — read it rather than assuming.

## How to Run

Call the native `image_generate` tool. There are no scripts in this skill.

```
image_generate(prompt="…", aspect_ratio="landscape", upscale=false)
```

## Quick Reference

Where it will be used decides the shape:

| They said | `aspect_ratio` |
|---|---|
| banner, header, hero, cover, wide, 16:9, video thumbnail | `landscape` |
| avatar, icon, logo mark, profile, tile, album art | `square` |
| poster, story, phone wallpaper, flyer, book cover | `portrait` |

Default to `landscape` when nothing implies a shape.

How final it is decides the tier:

| They said | `upscale` |
|---|---|
| 2K, 4K, high-res, full resolution, print, poster, wallpaper, "the final one" | `true` |
| draft, rough, quick look, thumbnail, "just show me", still exploring | omit |

## Procedure

1. **Settle the brief.** If something under-specified would change the
   picture, ask one consolidated question first. Ask about the subject, where
   it will be used, and hard constraints such as brand colours or required
   text. Do not ask about taste details you can reasonably choose — decide
   those and say what you chose. If the request is already clear, generate.

2. **Pick shape and tier** from `## Quick Reference`. On a self-hosted
   backend the two tiers are often different models with very different
   speeds, so the fast tier is the right default while an idea is still
   moving.

3. **Write the prompt describing the picture, not the request.** The tool
   receives your prompt, not the conversation. Cover subject, composition,
   lighting, style, palette — concretely. Carry over constraints already
   given (a palette, a recurring character, a house style) without making
   anyone repeat them. State exclusions explicitly: "no people", "no text".

4. **Generate once.** Do not fire variations unless options were requested;
   if they were, say how many you are making first.

5. **Report what exists.** Read the returned `size` and, where present,
   `upscaled`. Deliver the file using the file-delivery convention for the
   current platform, and say in one line what you made and how it departs
   from the brief.

## Pitfalls

- **Reporting the requested size instead of the returned one.** Some backends
  snap dimensions to the nearest shape their model supports, so the image can
  differ from the request. Always read the returned `size`.
- **Retrying a refusal.** A busy GPU, a maintenance window, or a declined
  prompt is surfaced as-is and stopped on. A retry queues behind the first
  attempt and turns a clear error into a long silence. Only repair-and-retry
  an obviously fixable parameter, and say what you changed.
- **Asking for high resolution the backend does not have.** If no hi-res pass
  is available, say so rather than returning a small image as though it were
  what was asked for.
- **Long text in the prompt.** Diffusion models garble long strings. Quote
  short text; for copy that must be exact, generate without it and set the
  type afterwards.
- **Speculative batching.** Grouping related images while the model is warm is
  worthwhile, but only for work actually requested.

## Verification

The call succeeded when the response reports success, the returned `size`
matches the shape you intended, and — if you asked for it — `upscaled` is
true. If `upscaled` came back false on a high-resolution request, the hi-res
tier did not serve it: say so instead of presenting the result as final.
