# 0004: License AGPL-3.0-or-later, Explo credited
Date: 2026-10-21 · Status: Accepted

## Context
Releasomat is a web app like Tagwerk (AGPL-3.0-or-later) and uses mutagen (GPL-2.0-or-later). It is
modelled on Explo (MIT, © LumePart and contributors).

## Decision
- License Releasomat under the **GNU AGPL-3.0-or-later**, same as Tagwerk.
- Releasomat is written from scratch. Explo is a reference for *approaches*; its code is not copied.
  If a piece is ever adapted from Explo, its MIT notice goes into `NOTICE` and the file names the source.

## Consequences
- Modified versions offered to others over a network must publish their source (AGPL section 13); the
  page footer links to the repository.
- MIT code is compatible with the AGPL, so adapting a part of Explo would be allowed with credit.
