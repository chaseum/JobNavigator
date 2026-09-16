# Jake's Resume — vendored source

`macros.tex` in this folder is Jake Gutierrez's "Jake's Resume" LaTeX template,
copied verbatim from the upstream repository. It was not reconstructed, rewritten
or regenerated; the only edit is that `\documentclass[letterpaper,11pt]{article}`
was moved into `resume.tex.j2` so the preamble can be `\input` from the rendered
document, and `resume.tex.j2` additionally loads `fontenc` (T1) and `lmodern` — see
the comment there: under upstream's OT1 encoding an underscore in a URL does not
survive PDF text extraction, which defeats the `\pdfgentounicode=1` the template
itself sets. Latin Modern is the same Computer Modern design, so the layout is
unchanged.

| | |
|---|---|
| Original author | Jake Gutierrez |
| Based on | [sb2nov/resume](https://github.com/sb2nov/resume) (Sourabh Bajaj) |
| Original repository | https://github.com/jakegut/resume |
| Source file | `resume.tex` |
| Overleaf template | https://www.overleaf.com/latex/templates/jakes-resume/syzfjbzwjncs |
| License | MIT (see `LICENSE` in this folder) |
| Revision used | `78de3c917058f79387b0913cf884ccc2e9a66b8c` (2020-08-29) |
| Retrieved | 2026-09-15 |

## What this folder contains

- `macros.tex` — Jake's preamble and macros, verbatim (`\resumeItem`,
  `\resumeSubheading`, `\resumeSubSubheading`, `\resumeProjectHeading`,
  `\resumeSubHeadingListStart/End`, `\resumeItemListStart/End`, the `\titleformat`
  section rule, the margin adjustments, `\pdfgentounicode=1`).
- `resume.tex.j2` — the document body only, parameterised with Jinja against the
  structured résumé JSON produced by `backend/copilot/resume_pipeline.py`. It uses
  Jake's macros for headings, education/experience/research subheadings, projects,
  bullet lists and technical skills. No LLM ever writes LaTeX.
- `LICENSE` — the upstream MIT license, verbatim.

## Updating

Re-download `resume.tex` from the revision you want, replace everything in
`macros.tex` below the local header comment with lines 10–102 of that file (the
preamble minus `\documentclass`), and update the revision row above.

## LaTeX packages required

`latexsym`, `fullpage`, `titlesec`, `marvosym`, `color`, `verbatim`, `enumitem`,
`hyperref`, `fancyhdr`, `babel`, `tabularx`, `glyphtounicode`, plus `fontenc` and
`lmodern` (added locally). On Debian these come
from `texlive-latex-base`, `texlive-latex-recommended`, `texlive-latex-extra`,
`texlive-fonts-recommended` and `texlive-fonts-extra` (marvosym); see
`Dockerfile.backend`.
