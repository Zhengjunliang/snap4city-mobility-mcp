# Relazione

Relazione dell'elaborato (Tipo A, *Sistemi Distribuiti*, UNIFI).

| File | Contenuto |
|---|---|
| `relazione.tex` | sorgente LaTeX (da consegnare insieme al PDF) |
| `relazione.pdf` | versione compilata — **da generare e depositare qui** |

## Prima di compilare

Nel frontespizio di `relazione.tex` due campi sono da completare:

```latex
\author{Junliang Zheng \\ \small Matricola: \textit{[inserire matricola]}}
...
Docente: \textit{[inserire docente]} \\[0.6em]
```

## Figure

Le figure **non sono duplicate** in questa cartella: `relazione.tex` le prende dalle
cartelle del progetto tramite

```latex
\graphicspath{{figures/}{../docs/diagrams/}{../screenshots/}}
```

Le sei immagini effettivamente incluse sono:

| Immagine | Provenienza |
|---|---|
| `component.png`, `graph-flow.png`, `sequence.png`, `deployment.png` | `docs/diagrams/` |
| `01-tre-modi.png`, `04-servizi-lungo-percorso.png` | `screenshots/` |

## Compilazione su Overleaf (consigliata)

1. Comprimere **l'intera cartella del progetto** e caricarla su Overleaf
   (*New Project → Upload Project*). Caricando solo `relazione/` le figure non
   verrebbero trovate, perché stanno nelle cartelle sorelle.
2. Impostare `relazione/relazione.tex` come documento principale
   (*Menu → Main document*).
3. Compilare con **pdfLaTeX**. La bibliografia è inclusa nel sorgente
   (`thebibliography`), quindi **non serve BibTeX**: due passate di pdfLaTeX bastano
   per risolvere indice e riferimenti alle figure.
4. Scaricare il PDF e salvarlo qui come `relazione.pdf`.

### Variante: progetto Overleaf autonomo

Se si preferisce caricare solo questa cartella, copiare prima le sei immagini elencate
sopra in `relazione/figures/`: `\graphicspath` prova quel percorso per primo, quindi il
sorgente compila senza modifiche.

## Compilazione locale

Con una distribuzione TeX installata (TeX Live, MiKTeX), dalla cartella `relazione/`:

```bash
pdflatex relazione.tex
pdflatex relazione.tex   # seconda passata: indice e riferimenti
```

La seconda passata è necessaria perché l'indice e i riferimenti alle figure sono
risolti solo dopo che il primo passaggio ha scritto il file ausiliario.
