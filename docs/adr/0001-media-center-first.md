# 1. Media center first, standard library only

Date: 2026-11-19. Decides [#21](https://github.com/Edracoon/mac-to-tv/issues/21).

## Context

Two directions pulled apart: a universal network remote (more brand drivers, more keys) or a media center for TVs
without apps (catalog, series, player, profiles). The maintainer owns one TV, so a driver for another brand cannot
be tested here. All the recent work went to the media center: catalog, series, home view, playing in the browser.

A related question: how long "Python standard library only, no build step" can hold while `server.py` and
`index.html` grow.

## Decision

1. **The media center comes first.** Issues about the catalog, the series, the player and the sources are
   prioritised over issues about the remote.
2. **The remote stays, and grows by contribution.** `tv.py` keeps every TV-specific part in one place. Drivers for
   other brands come from people who own the TV (see `CONTRIBUTING.md`); the maintainer does not write a driver for
   a TV that cannot be tested here.
3. **The rule holds: no `pip` dependency, no build step.** When a file becomes too large, it is split into modules
   of the same kind (plain Python files, plain scripts loaded by the page). External tools stay separate programs
   (ffmpeg, yt-dlp, TorrServer).

## Consequences

- A feature that needs a `pip` package or a bundler is refused, or done with a separate program.
- Remote drivers for more brands ([#18](https://github.com/Edracoon/mac-to-tv/issues/18)) stay open as "help wanted".
- Splitting `server.py` ([#13](https://github.com/Edracoon/mac-to-tv/issues/13)) is how the code is kept readable
  as it grows.
- Reopen this decision if a second TV brand becomes testable here, or if a needed feature cannot be built without
  a dependency.
