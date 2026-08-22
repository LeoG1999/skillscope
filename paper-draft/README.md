# SkillScope CHI Overleaf Package

This folder contains the completed portion of the SkillScope paper in the official ACM CHI 2027 anonymous review format.

## Compile in Overleaf

1. Upload `skillscope-chi-overleaf.zip` as a new Overleaf project, or upload the contents of this folder.
2. Set `main.tex` as the main document.
3. Use pdfLaTeX. Overleaf already provides the `acmart` class and `ACM-Reference-Format` bibliography style.

The review document uses the class declaration required by the [CHI 2027 publication-format instructions](https://chi2027.acm.org/chi-publication-formats/):

```tex
\documentclass[manuscript,review,anonymous]{acmart}
```

This produces the required single-column, line-numbered, anonymous review copy. The project intentionally omits rights, DOI, ISBN, and conference metadata during review; ACM supplies those values after conditional acceptance. For the publication-ready LaTeX source, follow the instructions sent by ACM and switch to `\documentclass[sigconf]{acmart}`.

The template source is ACM's current **Conference Proceedings Primary Article Template**. Do not change its margins, fonts, line spacing, or other layout definitions.

## Draft integrity

Participant counts, quotations, technical-evaluation values, and controlled-study results are synthetic expected placeholders. The visible notice in `main.tex` must remain until every value and quotation has been replaced by collected and verified evidence. Bibliographic metadata should also be audited before submission.

## Project structure

- `main.tex`: document class, title, abstract, metadata, and section includes.
- `sections/01-introduction.tex`: Introduction.
- `sections/02-related-work.tex`: Related Work.
- `sections/03-formative-study.tex`: Formative Study and Design Goals.
- `sections/04-system.tex`: implemented SkillScope workflow and architecture.
- `sections/05-user-study.tex`: frozen controlled-study protocol; results remain pending.
- `sections/appendix-system.tex`: implementation, evidence, scenario, and prompt details.
- `sections/appendix-formative-study.tex`: Formative-study protocol and reproducibility appendices.
- `references.bib`: current references used by the completed sections.

## Editing from a server with Overleaf Git

Overleaf Cloud Git integration is a Premium feature. After creating a blank Overleaf project and obtaining its Git URL from **Integrations > Git**, the project can be used as a remote from this server. Overleaf's Git bridge uses a single remote branch named `master` and token-based authentication.

Typical setup after copying this folder into its own local Git repository:

```bash
git remote add overleaf https://git.overleaf.com/PROJECT_ID
git pull overleaf master --allow-unrelated-histories --rebase=false
git push overleaf HEAD:master
```

Pull before editing and pushing if collaborators also edit in the browser. Avoid editing the same lines simultaneously because conflicts must be resolved locally. Overleaf's direct GitHub synchronization is another Premium option and requires manual push/pull from the Overleaf interface.
